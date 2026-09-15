"""Governed, recommendation-only next-calculation suggestions.

Suggestions never authorize or submit work.  They are derived only from an
already frozen Analysis Workbench view and an exact, final-allowed
``ValidationResult`` carrying an auditable human approval bound to that view's
data fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

from vcstudio.project.report_contracts import ValidationResult


RECOMMENDATION_SCHEMA = "vcstudio.next-calculation-recommendations/v1"
DRAFT_SCHEMA = "vcstudio.next-calculation-draft/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _base(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RECOMMENDATION_SCHEMA,
        "analysis_id": str(view.get("analysis_id") or ""),
        "data_fingerprint": str(view.get("data_fingerprint") or "").lower(),
        "recommendation_only": True,
        "requires_user_confirmation": True,
        "authorizes_submission": False,
        "contains_executable_commands": False,
        "available": False,
        "status": "blocked",
        "validation_sha256": None,
        "recommendations": [],
        "blocking": [],
        "warnings": [],
    }


def _finish(payload: dict[str, Any]) -> dict[str, Any]:
    payload["semantic_sha256"] = _canonical_hash(payload)
    return payload


def _binding_refs(fingerprint: str) -> set[str]:
    return {
        fingerprint,
        f"analysis-view:{fingerprint}",
        f"data_fingerprint:{fingerprint}",
    }


def _positive_denominator(view: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    denominator = view.get("denominator") or {}
    if not isinstance(denominator, Mapping):
        return []
    result = []
    for key in keys:
        value = denominator.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            result.append(key)
    return result


def _has_sensitivity_uncertainty(value: Any) -> bool:
    """Return true only for a ranking set that is genuinely unstable/ambiguous.

    Merely requesting deadband points is not an uncertainty signal.  The
    frozen result must show changing within-deadband membership, a tied set,
    or a non-binary membership fraction.
    """
    if not isinstance(value, Mapping):
        return False
    memberships = value.get("membership")
    if isinstance(memberships, list):
        by_species: dict[str, set[str]] = {}
        for row in memberships:
            if not isinstance(row, Mapping):
                continue
            fraction = row.get("membership_fraction")
            if (isinstance(fraction, (int, float))
                    and not isinstance(fraction, bool)
                    and math.isfinite(float(fraction))
                    and 0.0 < float(fraction) < 1.0):
                return True
            species = str(row.get("species") or "")
            project_id = str(row.get("project_id") or "")
            if species and project_id and fraction == 1:
                by_species.setdefault(species, set()).add(project_id)
        if any(len(project_ids) > 1 for project_ids in by_species.values()):
            return True

    points = value.get("points")
    if not isinstance(points, list):
        return False
    observed: dict[str, list[tuple[str, ...]]] = {}
    for point in points:
        sets = point.get("lowest_energy_sets") if isinstance(point, Mapping) else None
        if not isinstance(sets, list):
            continue
        for row in sets:
            if not isinstance(row, Mapping):
                continue
            species = str(row.get("species") or "")
            raw_ids = row.get("within_deadband_project_ids")
            if not species or not isinstance(raw_ids, list):
                continue
            project_ids = tuple(sorted({str(item) for item in raw_ids if str(item)}))
            if len(project_ids) > 1:
                return True
            observed.setdefault(species, []).append(project_ids)
    return any(len(set(sets)) > 1 for sets in observed.values() if len(sets) > 1)


def _recommendation(
        identifier: str, kind: str, title: str, reason: str,
        evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": kind,
        "title": title,
        "reason": reason,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "recommendation_only": True,
        "requires_user_confirmation": True,
        "authorizes_submission": False,
        "draft_intent": {
            "kind": kind,
            "status": "proposal",
            "draft_only": True,
            "authorizes_submission": False,
        },
    }


def build_recommendations(
        view: Mapping[str, Any], validation: ValidationResult | None,
        *, validation_error: str | None = None) -> dict[str, Any]:
    """Return auditable suggestions or one fail-closed governance result."""
    if not isinstance(view, Mapping):
        raise TypeError("view must be a server-frozen mapping")
    payload = _base(view)
    fingerprint = payload["data_fingerprint"]
    if not _SHA256_RE.fullmatch(fingerprint):
        payload["status"] = "unavailable"
        payload["blocking"] = ["Current analysis data_fingerprint is unavailable or invalid."]
        return _finish(payload)
    if not isinstance(validation, ValidationResult):
        payload["status"] = "unavailable"
        payload["blocking"] = [
            str(validation_error or
                "No server-constructed ValidationResult is available for this analysis view.")]
        return _finish(payload)
    payload["validation_sha256"] = validation.semantic_sha256
    if validation.scientific_qualification != "human_scientific_reviewed":
        payload["blocking"].append(
            "ValidationResult is not human_scientific_reviewed.")
    if validation.final_allowed is not True:
        payload["blocking"].append("ValidationResult does not allow a final scientific result.")
    review_refs = {str(value) for value in validation.human_review.get("evidence_refs") or []}
    if not review_refs.intersection(_binding_refs(fingerprint)):
        payload["blocking"].append(
            "Human review evidence_refs do not bind the current analysis data_fingerprint.")
    if payload["blocking"]:
        return _finish(payload)

    base_ref = f"analysis-view:{fingerprint}"
    missing_keys = _positive_denominator(view, (
        "missing_configurations", "missing_numeric_cells", "missing_steps",
        "blocked_results", "blocked_projects",
    ))
    missing_messages = [str(value) for value in (
        list(view.get("missing") or []) + list(view.get("blocking") or [])) if value]
    if missing_keys or missing_messages:
        refs = [base_ref, *[f"{base_ref}#denominator.{key}" for key in missing_keys]]
        payload["recommendations"].append(_recommendation(
            "resolve-missing-evidence", "resolve_missing_prerequisite",
            "Resolve missing scientific evidence",
            "The frozen view contains missing or blocked denominator entries; complete only the cited evidence before rerunning the analysis.",
            refs,
        ))

    near_keys = _positive_denominator(view, ("near_degenerate_groups",))
    near_rows = [
        str(row.get("configuration_id") or row.get("source", {}).get("source_id") or "")
        for row in view.get("rows") or []
        if isinstance(row, Mapping) and row.get("near_degenerate") is True
    ]
    if near_keys or near_rows:
        payload["recommendations"].append(_recommendation(
            "refine-near-degenerate-set", "refine_near_degenerate_set",
            "Refine the near-degenerate set",
            "The frozen view identifies near-degenerate candidates; a user may draft a method-consistent refinement study before changing the ranking.",
            [base_ref, f"{base_ref}#near-degenerate"],
        ))

    sensitivity = view.get("sensitivity") or {}
    sensitivity_uncertainty = _has_sensitivity_uncertainty(sensitivity)
    view_warnings = [str(value) for value in view.get("warnings") or [] if value]
    validation_warnings = [
        check.id for check in validation.checks
        if check.status not in {"pass", "not_applicable"} and not check.blocks_final
    ]
    if sensitivity_uncertainty or view_warnings or validation_warnings:
        refs = [base_ref]
        if sensitivity_uncertainty:
            refs.append(f"{base_ref}#sensitivity")
        refs.extend(f"validation:{validation.semantic_sha256}#check.{item}"
                    for item in validation_warnings)
        payload["recommendations"].append(_recommendation(
            "probe-ranking-uncertainty", "probe_ranking_uncertainty",
            "Probe ranking uncertainty",
            "Sensitivity or warning evidence remains in the governed result; draft a bounded follow-up calculation that targets only those cited uncertainties.",
            refs,
        ))

    payload["available"] = True
    payload["status"] = "available"
    payload["warnings"] = [
        "No auditable uncertainty signal requires a follow-up calculation."
    ] if not payload["recommendations"] else []
    return _finish(payload)


def draft_intent(
        recommendations: Mapping[str, Any], recommendation_id: str,
        *, confirmed: bool) -> dict[str, Any]:
    """Create a non-executable draft after explicit confirmation."""
    if not isinstance(recommendations, Mapping):
        raise TypeError("recommendations must be a server-built mapping")
    if recommendations.get("schema") != RECOMMENDATION_SCHEMA:
        raise ValueError("unsupported recommendation schema")
    expected = str(recommendations.get("semantic_sha256") or "")
    frozen = dict(recommendations)
    frozen.pop("semantic_sha256", None)
    if not _SHA256_RE.fullmatch(expected) or _canonical_hash(frozen) != expected:
        raise ValueError("recommendation semantic hash mismatch")
    if recommendations.get("available") is not True:
        raise ValueError("governed recommendations are unavailable")
    if confirmed is not True:
        raise ValueError("explicit user confirmation is required for a draft intent")
    identifier = str(recommendation_id or "").strip()
    selected = next((item for item in recommendations.get("recommendations") or []
                     if str(item.get("id") or "") == identifier), None)
    if selected is None:
        raise ValueError("unknown recommendation_id")
    payload = {
        "schema": DRAFT_SCHEMA,
        "ok": True,
        "status": "draft",
        "recommendation_id": identifier,
        "recommendation_semantic_sha256": expected,
        "analysis_id": recommendations.get("analysis_id"),
        "data_fingerprint": recommendations.get("data_fingerprint"),
        "validation_sha256": recommendations.get("validation_sha256"),
        "intent": dict(selected.get("draft_intent") or {}),
        "evidence_refs": list(selected.get("evidence_refs") or []),
        "draft_only": True,
        "recommendation_only": True,
        "requires_user_confirmation": True,
        "authorizes_submission": False,
        "contains_executable_commands": False,
    }
    payload["semantic_sha256"] = _canonical_hash(payload)
    return payload


__all__ = [
    "DRAFT_SCHEMA", "RECOMMENDATION_SCHEMA", "build_recommendations",
    "draft_intent",
]
