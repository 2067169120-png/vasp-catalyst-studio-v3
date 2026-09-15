from __future__ import annotations

import copy
import json

import pytest

from vcstudio.project.next_calculation import (
    DRAFT_SCHEMA,
    RECOMMENDATION_SCHEMA,
    build_recommendations,
    draft_intent,
)
from vcstudio.project.report_contracts import ValidationCheck, ValidationResult


FINGERPRINT = "a" * 64


def _validation(*, fingerprint=FINGERPRINT, human=True, final_allowed=True,
                warning=False):
    checks = [ValidationCheck(
        id="scientific-gate", status="pass", severity="blocking",
        required=True, evidence_refs=("snapshot:scientific",),
    )]
    status = "passed"
    if warning:
        checks.append(ValidationCheck(
            id="coverage-warning", status="warn", severity="warning",
            required=False, message="bounded coverage warning",
        ))
        status = "passed_with_warnings"
    qualification = "human_scientific_reviewed" if human else (
        "adsorption_result_verified")
    return ValidationResult(
        spec_sha256="b" * 64,
        snapshot_sha256="c" * 64,
        validated_at_utc="2026-08-11T01:02:03Z",
        validator={"id": "scientific-gate", "version": "1"},
        status=status,
        effective_kind="final" if final_allowed else "diagnostic",
        final_allowed=final_allowed,
        scientific_qualification=qualification,
        claim_ceiling="electronic_screen",
        report_model_sha256="d" * 64,
        checks=tuple(checks),
        human_review=({
            "reviewer_type": "human", "reviewed_by": "reviewer-42",
            "reviewed_at_utc": "2026-08-11T01:02:03Z",
            "decision": "approved",
            "evidence_refs": [f"analysis-view:{fingerprint}"],
        } if human else {}),
    )


def _view(**patch):
    value = {
        "schema": "vcstudio.analysis-view/v1",
        "analysis_id": "adsorption-energy",
        "data_fingerprint": FINGERPRINT,
        "denominator": {
            "missing_configurations": 1,
            "near_degenerate_groups": 1,
        },
        "rows": [{
            "configuration_id": "job-opaque-a", "near_degenerate": True,
        }],
        "sensitivity": {"points": [{"deadband_eV": 0.1}]},
        "missing": ["one registered configuration lacks a numeric result"],
        "blocking": [], "warnings": [],
    }
    value.update(patch)
    return value


def test_missing_server_validation_is_unavailable_and_never_recommends():
    result = build_recommendations(
        _view(), None, validation_error="current validation sidecar unavailable")

    assert result["schema"] == RECOMMENDATION_SCHEMA
    assert result["available"] is False
    assert result["status"] == "unavailable"
    assert result["recommendations"] == []
    assert result["blocking"] == ["current validation sidecar unavailable"]
    assert result["recommendation_only"] is True
    assert result["requires_user_confirmation"] is True
    assert result["authorizes_submission"] is False
    assert result["contains_executable_commands"] is False
    assert len(result["semantic_sha256"]) == 64


@pytest.mark.parametrize("validation,match", [
    (_validation(human=False), "not human_scientific_reviewed"),
    (_validation(final_allowed=False), "does not allow"),
    (_validation(fingerprint="f" * 64), "do not bind"),
])
def test_governance_gate_blocks_unreviewed_nonfinal_or_unbound_validation(
        validation, match):
    result = build_recommendations(_view(), validation)

    assert result["available"] is False
    assert result["status"] == "blocked"
    assert any(match in reason for reason in result["blocking"])
    assert result["recommendations"] == []


def test_governed_recommendations_derive_only_from_frozen_auditable_signals():
    view = _view()
    original = copy.deepcopy(view)

    first = build_recommendations(view, _validation(warning=True))
    second = build_recommendations(copy.deepcopy(view), _validation(warning=True))

    assert view == original
    assert first == second
    assert first["available"] is True
    assert first["status"] == "available"
    assert [item["id"] for item in first["recommendations"]] == [
        "resolve-missing-evidence",
        "refine-near-degenerate-set",
        "probe-ranking-uncertainty",
    ]
    for item in first["recommendations"]:
        assert item["recommendation_only"] is True
        assert item["requires_user_confirmation"] is True
        assert item["authorizes_submission"] is False
        assert item["evidence_refs"]
        assert item["draft_intent"]["draft_only"] is True
        assert item["draft_intent"]["authorizes_submission"] is False
    encoded = json.dumps(first, ensure_ascii=False).lower()
    for forbidden in ("subprocess", "shell", "qsub", "sbatch", "powershell"):
        assert forbidden not in encoded


def test_no_uncertainty_signal_returns_available_empty_recommendations():
    view = _view(
        denominator={"missing_configurations": 0, "near_degenerate_groups": 0},
        rows=[], sensitivity={"points": []}, missing=[], blocking=[], warnings=[])
    result = build_recommendations(view, _validation())

    assert result["available"] is True
    assert result["recommendations"] == []
    assert result["warnings"] == [
        "No auditable uncertainty signal requires a follow-up calculation."]


def test_stable_sensitivity_points_do_not_create_ranking_suggestion():
    stable = {
        "points": [
            {"deadband_eV": 0.05, "lowest_energy_sets": [{
                "species": "Li2S", "within_deadband_project_ids": ["project-a"],
            }]},
            {"deadband_eV": 0.20, "lowest_energy_sets": [{
                "species": "Li2S", "within_deadband_project_ids": ["project-a"],
            }]},
        ],
        "membership": [{
            "species": "Li2S", "project_id": "project-a",
            "membership_count": 2, "tested_deadbands": 2,
            "membership_fraction": 1.0,
        }],
    }
    view = _view(
        denominator={"missing_configurations": 0, "near_degenerate_groups": 0},
        rows=[], sensitivity=stable, missing=[], blocking=[], warnings=[])

    result = build_recommendations(view, _validation())

    assert result["recommendations"] == []
    assert result["warnings"] == [
        "No auditable uncertainty signal requires a follow-up calculation."]


def test_changed_sensitivity_membership_creates_auditable_ranking_suggestion():
    changed = {
        "points": [
            {"deadband_eV": 0.05, "lowest_energy_sets": [{
                "species": "Li2S", "within_deadband_project_ids": ["project-a"],
            }]},
            {"deadband_eV": 0.20, "lowest_energy_sets": [{
                "species": "Li2S",
                "within_deadband_project_ids": ["project-a", "project-b"],
            }]},
        ],
        "membership": [
            {"species": "Li2S", "project_id": "project-a",
             "membership_fraction": 1.0},
            {"species": "Li2S", "project_id": "project-b",
             "membership_fraction": 0.5},
        ],
    }
    view = _view(
        denominator={"missing_configurations": 0, "near_degenerate_groups": 0},
        rows=[], sensitivity=changed, missing=[], blocking=[], warnings=[])

    result = build_recommendations(view, _validation())

    assert [item["id"] for item in result["recommendations"]] == [
        "probe-ranking-uncertainty"]
    assert result["recommendations"][0]["evidence_refs"] == [
        f"analysis-view:{FINGERPRINT}",
        f"analysis-view:{FINGERPRINT}#sensitivity",
    ]


def test_confirmation_creates_only_a_hashed_non_executable_draft_intent():
    governed = build_recommendations(_view(), _validation())

    with pytest.raises(ValueError, match="confirmation"):
        draft_intent(governed, "resolve-missing-evidence", confirmed=False)
    draft = draft_intent(
        governed, "resolve-missing-evidence", confirmed=True)

    assert draft["schema"] == DRAFT_SCHEMA
    assert draft["status"] == "draft"
    assert draft["draft_only"] is True
    assert draft["authorizes_submission"] is False
    assert draft["contains_executable_commands"] is False
    assert len(draft["semantic_sha256"]) == 64
    encoded = json.dumps(draft, ensure_ascii=False).lower()
    assert "qsub" not in encoded and "sbatch" not in encoded
    assert not ({"command", "commands", "submit", "submission_bridge"} & set(draft))
    assert not ({"command", "commands", "submit", "submission_bridge"} &
                set(draft["intent"]))

    tampered = copy.deepcopy(governed)
    tampered["recommendations"][0]["reason"] = "forged"
    with pytest.raises(ValueError, match="hash mismatch"):
        draft_intent(tampered, "resolve-missing-evidence", confirmed=True)
