"""Fail-closed authoring transactions for revisioned kinetics model specs.

This module deliberately owns no reaction facts and exposes no caller-selected
filesystem locations.  Browser drafts contain modelling decisions only.  A
trusted, server-side source snapshot supplies membership, source identities and
the evidence catalogue used by the compiler.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, runtime_checkable

from vcstudio.project.kinetics_model_spec import (
    AuthoritativeKineticsSourceSnapshot,
    KineticsModelSpec,
    KineticsModelSpecConflict,
    KineticsModelSpecError,
    KineticsModelSpecStore,
    KineticsModelSpecStoreSnapshot,
    classify_sensitive_text,
)


DRAFT_SCHEMA = "vcstudio.kinetics-model-spec-draft/v1"
SOURCE_SNAPSHOT_SCHEMA = "vcstudio.kinetics-authoring-source-snapshot/v1"
SOURCE_CAS_SCHEMA = "vcstudio.kinetics-authoring-source-cas/v1"
SELECTOR_SCHEMA = "vcstudio.kinetics-active-spec-selection/v2"
LEGACY_SELECTOR_SCHEMA = "vcstudio.kinetics-active-spec-selection/v1"
SELECTOR_SNAPSHOT_SCHEMA = "vcstudio.kinetics-active-spec-selector-snapshot/v2"
PREVIEW_SCHEMA = "vcstudio.kinetics-authoring-preview/v1"
CONFIRMATION_SCHEMA = "vcstudio.kinetics-authoring-confirmation/v1"
CONFIRM_RESULT_SCHEMA = "vcstudio.kinetics-authoring-confirm-result/v1"
CONFLICT_SCHEMA = "vcstudio.kinetics-authoring-conflict/v1"
PENDING_SCHEMA = "vcstudio.kinetics-authoring-pending/v1"
RECEIPT_SCHEMA = "vcstudio.kinetics-authoring-commit-receipt/v1"
RECEIPT_LEDGER_SCHEMA = "vcstudio.kinetics-authoring-receipt-ledger/v1"
SELECTOR_ANCHOR_RECORD_SCHEMA = "vcstudio.kinetics-selector-anchor-record/v1"

AUTHORING_DIRECTORY = (".vcstudio", "kinetics")
ACTIVE_SELECTOR_FILENAME = "active-model-spec.json"
AUTHORING_LOCK_FILENAME = ".authoring.lock"
AUTHORING_PENDING_FILENAME = "authoring-pending.json"
AUTHORING_RECEIPT_FILENAME = "authoring-commit-receipt.json"
SELECTOR_ANCHOR_FILENAME = ".active-model-spec-anchor.wal"

MAX_ISSUES = 64
MAX_COLLECTION = 512
MAX_EVIDENCE_BINDINGS = 256
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_SELECTOR_BYTES = 1024 * 1024
MAX_PENDING_BYTES = 10 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_RECEIPTS = 4096
MAX_SELECTOR_ANCHOR_BYTES = 32 * 1024 * 1024
MAX_SELECTOR_ANCHOR_RECORDS = 2 * (MAX_RECEIPTS + 1)
_MAX_LOCK_BYTES = 4096
_ZERO_SHA256 = "0" * 64

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:+-]{0,159}\Z")
_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PROCESS_LOCK = threading.RLock()


class KineticsAuthoringError(ValueError):
    """The authoring boundary, transaction or persisted state is invalid."""


class KineticsAuthoringRecoveryError(KineticsAuthoringError):
    """A pending transaction cannot be reconciled to its base or target."""


class KineticsAuthoringInjectedFailure(RuntimeError):
    """Test seam used to emulate process termination at a durable boundary."""


@dataclass(frozen=True)
class AuthoringIssue:
    """Bounded, path-free problem report suitable for an API response."""

    code: str
    path: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _identifier(self.code, "issue.code"))
        object.__setattr__(self, "path", _safe_text(self.path, "issue.path"))
        object.__setattr__(self, "message", _safe_text(self.message, "issue.message"))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class KineticsAuthoringValidationError(KineticsAuthoringError):
    """One or more browser-draft or source-snapshot fields are invalid."""

    def __init__(self, issues: Sequence[AuthoringIssue]):
        bounded = tuple(issues[:MAX_ISSUES])
        if not bounded:
            bounded = (AuthoringIssue("invalid", "$", "authoring input is invalid"),)
        self.issues = bounded
        super().__init__(bounded[0].message)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KineticsAuthoringError("authoring values must be canonical finite JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict(value: Any, fields: set[str] | frozenset[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _invalid("invalid_type", path, "must be an object")
    actual = set(value)
    if actual != set(fields):
        _invalid("invalid_fields", path, "has unknown or missing fields")
    return value


def _strict_optional(
    value: Any,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str],
    path: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _invalid("invalid_type", path, "must be an object")
    actual = set(value)
    if not set(required) <= actual or not actual <= set(required) | set(optional):
        _invalid("invalid_fields", path, "has unknown or missing fields")
    return value


def _invalid(code: str, path: str, message: str) -> None:
    raise KineticsAuthoringValidationError((AuthoringIssue(code, path, message),))


def _safe_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _CONTROL_RE.search(value)
        or classify_sensitive_text(value) is not None
    ):
        raise KineticsAuthoringError(f"{label} must be bounded path-free text")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _safe_text(value, label, maximum=160)
    if not _ID_RE.fullmatch(value):
        raise KineticsAuthoringError(f"{label} must be a safe opaque identifier")
    return value


def _draft_identifier(value: Any, path: str) -> str:
    try:
        return _identifier(value, path)
    except KineticsAuthoringError:
        _invalid("invalid_identifier", path, "must be a bounded opaque identifier")


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise KineticsAuthoringError(f"{label} must be a lowercase SHA-256")
    return value


def _authority(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _AUTHORITY_RE.fullmatch(value):
        raise KineticsAuthoringError(f"{label} must be a 128-bit authority id")
    return value


def _number(
    value: Any,
    path: str,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0e100,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid("invalid_number", path, "must be a finite number")
    result = float(value)
    if (
        not math.isfinite(result)
        or result < minimum
        or result > maximum
        or (positive and result <= 0.0)
    ):
        _invalid("invalid_number", path, "must be a bounded finite number")
    return result


def _array(value: Any, path: str, *, nonempty: bool = True) -> Sequence[Any]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or (nonempty and not value)
        or len(value) > MAX_COLLECTION
    ):
        _invalid("invalid_array", path, "must be a bounded array")
    return value


def _unique_ids(value: Any, path: str, *, nonempty: bool = True) -> list[str]:
    output = [
        _draft_identifier(item, f"{path}[{index}]")
        for index, item in enumerate(_array(value, path, nonempty=nonempty))
    ]
    if len(output) != len(set(output)):
        _invalid("duplicate_id", path, "must not contain duplicate identifiers")
    return output


def _detached(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _detached(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_detached(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise KineticsAuthoringError("authoring value is not JSON")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item) for item in value)
    return value


_DRAFT_REQUIRED = frozenset(
    {
        "schema",
        "spec_id",
        "mode",
        "rate_law_policy",
        "assumptions",
        "feed_reservoirs",
        "target_product_ids",
        "steps",
        "site_population_totals",
    }
)
_DRAFT_OPTIONAL = frozenset({"saddle_selector"})
_RATE_FIELDS = frozenset(
    {"activity", "reversibility", "detailed_balance", "prefactor", "electrochemical", "reactor"}
)
_ASSUMPTION_FIELDS = frozenset(
    {
        "mean_field",
        "steady_state",
        "site_uniformity",
        "lateral_interactions",
        "mechanism_completeness",
        "evidence_ref_ids",
    }
)
_RESERVOIR_FIELDS = frozenset({"species_id", "activity", "unit", "source_ref_id"})
_STEP_FIELDS = frozenset(
    {"step_id", "prefactors", "bep", "scaling", "uncertainty_eV", "evidence_ref_ids"}
)
_PREFACTORS_FIELDS = frozenset({"forward", "reverse"})
_PREFACTOR_FIELDS = frozenset({"value", "unit", "source_ref_id"})
_EMPIRICAL_FIELDS = frozenset({"used", "source_ref_id", "parameters_ref_id"})
_SITE_TOTAL_FIELDS = frozenset({"site_type", "value", "unit", "basis", "evidence_ref_ids"})
_SADDLE_FIELDS = frozenset({"mode", "by_step", "evidence_ref_ids"})


class KineticsModelSpecDraft:
    """Strict immutable browser DTO; it never contains server-owned identities."""

    schema: ClassVar[str] = DRAFT_SCHEMA
    __slots__ = ("_canonical", "_sha256", "_value")

    def __init__(self, value: Mapping[str, Any]):
        normalized = self._validate(value)
        canonical = _canonical_bytes(normalized)
        object.__setattr__(self, "_canonical", canonical)
        object.__setattr__(self, "_sha256", hashlib.sha256(canonical).hexdigest())
        object.__setattr__(self, "_value", normalized)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "KineticsModelSpecDraft":
        return cls(value)

    @staticmethod
    def _validate(value: Mapping[str, Any]) -> dict[str, Any]:
        record = _strict_optional(value, _DRAFT_REQUIRED, _DRAFT_OPTIONAL, "$draft")
        if record["schema"] != DRAFT_SCHEMA:
            _invalid("unsupported_schema", "$draft.schema", "draft schema is unsupported")
        spec_id = _draft_identifier(record["spec_id"], "$draft.spec_id")
        mode = record["mode"]
        if mode not in {"create", "advance"}:
            _invalid("unsupported_mode", "$draft.mode", "must be create or advance")

        policy = _strict(record["rate_law_policy"], _RATE_FIELDS, "$draft.rate_law_policy")
        normalized_policy = {
            key: _draft_identifier(policy[key], f"$draft.rate_law_policy.{key}")
            for key in sorted(_RATE_FIELDS)
        }

        assumptions = _strict(
            record["assumptions"], _ASSUMPTION_FIELDS, "$draft.assumptions"
        )
        if not isinstance(assumptions["mean_field"], bool) or not isinstance(
            assumptions["steady_state"], bool
        ):
            _invalid(
                "invalid_boolean",
                "$draft.assumptions",
                "mean_field and steady_state must be explicit booleans",
            )
        normalized_assumptions = {
            "mean_field": assumptions["mean_field"],
            "steady_state": assumptions["steady_state"],
            "site_uniformity": _draft_identifier(
                assumptions["site_uniformity"], "$draft.assumptions.site_uniformity"
            ),
            "lateral_interactions": _draft_identifier(
                assumptions["lateral_interactions"],
                "$draft.assumptions.lateral_interactions",
            ),
            "mechanism_completeness": _draft_identifier(
                assumptions["mechanism_completeness"],
                "$draft.assumptions.mechanism_completeness",
            ),
            "evidence_ref_ids": _unique_ids(
                assumptions["evidence_ref_ids"], "$draft.assumptions.evidence_ref_ids"
            ),
        }

        reservoirs = []
        reservoir_ids: set[str] = set()
        for index, raw in enumerate(_array(record["feed_reservoirs"], "$draft.feed_reservoirs")):
            path = f"$draft.feed_reservoirs[{index}]"
            item = _strict(raw, _RESERVOIR_FIELDS, path)
            species_id = _draft_identifier(item["species_id"], f"{path}.species_id")
            if species_id in reservoir_ids:
                _invalid("duplicate_id", f"{path}.species_id", "species_id is duplicated")
            reservoir_ids.add(species_id)
            unit = item["unit"]
            if unit not in {"bar", "mol/L", "dimensionless"}:
                _invalid("unsupported_unit", f"{path}.unit", "reservoir unit is unsupported")
            reservoirs.append(
                {
                    "species_id": species_id,
                    "activity": _number(item["activity"], f"{path}.activity"),
                    "unit": unit,
                    "source_ref_id": _draft_identifier(
                        item["source_ref_id"], f"{path}.source_ref_id"
                    ),
                }
            )

        targets = _unique_ids(record["target_product_ids"], "$draft.target_product_ids")
        steps = []
        step_ids: set[str] = set()
        for index, raw in enumerate(_array(record["steps"], "$draft.steps")):
            path = f"$draft.steps[{index}]"
            item = _strict(raw, _STEP_FIELDS, path)
            step_id = _draft_identifier(item["step_id"], f"{path}.step_id")
            if step_id in step_ids:
                _invalid("duplicate_id", f"{path}.step_id", "step_id is duplicated")
            step_ids.add(step_id)
            prefactors = _strict(item["prefactors"], _PREFACTORS_FIELDS, f"{path}.prefactors")
            normalized_prefactors = {}
            for direction in ("forward", "reverse"):
                prefactor_path = f"{path}.prefactors.{direction}"
                prefactor = _strict(prefactors[direction], _PREFACTOR_FIELDS, prefactor_path)
                unit = prefactor["unit"]
                if unit not in {"s^-1", "bar^-1 s^-1", "mol^-1 L s^-1"}:
                    _invalid(
                        "unsupported_unit",
                        f"{prefactor_path}.unit",
                        "prefactor unit is unsupported",
                    )
                normalized_prefactors[direction] = {
                    "value": _number(
                        prefactor["value"], f"{prefactor_path}.value", positive=True
                    ),
                    "unit": unit,
                    "source_ref_id": _draft_identifier(
                        prefactor["source_ref_id"], f"{prefactor_path}.source_ref_id"
                    ),
                }
            steps.append(
                {
                    "step_id": step_id,
                    "prefactors": normalized_prefactors,
                    "bep": _validate_empirical(item["bep"], f"{path}.bep"),
                    "scaling": _validate_empirical(item["scaling"], f"{path}.scaling"),
                    "uncertainty_eV": _number(
                        item["uncertainty_eV"], f"{path}.uncertainty_eV", maximum=1.0e4
                    ),
                    "evidence_ref_ids": _unique_ids(
                        item["evidence_ref_ids"], f"{path}.evidence_ref_ids"
                    ),
                }
            )

        totals = []
        site_ids: set[str] = set()
        for index, raw in enumerate(
            _array(record["site_population_totals"], "$draft.site_population_totals")
        ):
            path = f"$draft.site_population_totals[{index}]"
            item = _strict(raw, _SITE_TOTAL_FIELDS, path)
            site_type = _draft_identifier(item["site_type"], f"{path}.site_type")
            if site_type in site_ids:
                _invalid("duplicate_id", f"{path}.site_type", "site_type is duplicated")
            site_ids.add(site_type)
            if item["unit"] not in {"sites", "dimensionless"}:
                _invalid("unsupported_unit", f"{path}.unit", "site total unit is unsupported")
            if item["basis"] not in {"surface_unit_cell", "normalized_site_population"}:
                _invalid("unsupported_basis", f"{path}.basis", "site total basis is unsupported")
            totals.append(
                {
                    "site_type": site_type,
                    "value": _number(item["value"], f"{path}.value", positive=True),
                    "unit": item["unit"],
                    "basis": item["basis"],
                    "evidence_ref_ids": _unique_ids(
                        item["evidence_ref_ids"], f"{path}.evidence_ref_ids"
                    ),
                }
            )

        saddle = None
        if "saddle_selector" in record:
            raw_saddle = record["saddle_selector"]
            if raw_saddle is not None:
                item = _strict(raw_saddle, _SADDLE_FIELDS, "$draft.saddle_selector")
                if item["mode"] != "explicit_species_by_step":
                    _invalid(
                        "unsupported_mode",
                        "$draft.saddle_selector.mode",
                        "saddle selector mode is unsupported",
                    )
                by_step = item["by_step"]
                if not isinstance(by_step, Mapping) or set(by_step) != step_ids:
                    _invalid(
                        "coverage_mismatch",
                        "$draft.saddle_selector.by_step",
                        "must select every draft step exactly once",
                    )
                saddle = {
                    "mode": item["mode"],
                    "by_step": {
                        _draft_identifier(key, "$draft.saddle_selector.by_step key"):
                        _draft_identifier(
                            selected, f"$draft.saddle_selector.by_step.{key}"
                        )
                        for key, selected in sorted(by_step.items())
                    },
                    "evidence_ref_ids": _unique_ids(
                        item["evidence_ref_ids"],
                        "$draft.saddle_selector.evidence_ref_ids",
                    ),
                }

        output = {
            "schema": DRAFT_SCHEMA,
            "spec_id": spec_id,
            "mode": mode,
            "rate_law_policy": normalized_policy,
            "assumptions": normalized_assumptions,
            "feed_reservoirs": reservoirs,
            "target_product_ids": targets,
            "steps": steps,
            "site_population_totals": totals,
        }
        if "saddle_selector" in record:
            output["saddle_selector"] = saddle
        return output

    @property
    def semantic_sha256(self) -> str:
        return self._sha256

    @property
    def spec_id(self) -> str:
        return self._value["spec_id"]

    @property
    def mode(self) -> str:
        return self._value["mode"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical.decode("utf-8"))


def _validate_empirical(value: Any, path: str) -> dict[str, Any]:
    item = _strict(value, _EMPIRICAL_FIELDS, path)
    if not isinstance(item["used"], bool):
        _invalid("invalid_boolean", f"{path}.used", "must be an explicit boolean")
    if item["used"]:
        source = _draft_identifier(item["source_ref_id"], f"{path}.source_ref_id")
        parameters = _draft_identifier(
            item["parameters_ref_id"], f"{path}.parameters_ref_id"
        )
    else:
        if item["source_ref_id"] is not None or item["parameters_ref_id"] is not None:
            _invalid(
                "unexpected_value",
                path,
                "unused empirical models must not carry evidence references",
            )
        source = None
        parameters = None
    return {"used": item["used"], "source_ref_id": source, "parameters_ref_id": parameters}


@dataclass(frozen=True)
class AuthoringSourceSnapshot:
    """Server-owned, path-free source, membership and evidence authority."""

    project_id: str
    domain_authority_id: str
    domain_generation: int
    domain_snapshot_sha256: str
    network_id: str
    network_revision: str
    network_semantic_sha256: str
    source_projection_sha256: str
    required_feed_reservoir_ids: Sequence[str]
    allowed_target_product_ids: Sequence[str]
    required_step_ids: Sequence[str]
    required_site_type_ids: Sequence[str]
    saddle_candidates_by_step: Mapping[str, Sequence[str]]
    evidence_catalog: Sequence[Mapping[str, Any]]
    solver_ready: bool
    solver_readiness_reasons: Sequence[str]
    snapshot_sha256: str = ""
    schema: str = SOURCE_SNAPSHOT_SCHEMA
    _catalog: Mapping[str, Mapping[str, Any]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema != SOURCE_SNAPSHOT_SCHEMA:
            raise KineticsAuthoringError("authoring source snapshot schema is unsupported")
        project_id = _identifier(self.project_id, "source.project_id")
        domain_authority = _authority(self.domain_authority_id, "source.domain_authority_id")
        if (
            isinstance(self.domain_generation, bool)
            or not isinstance(self.domain_generation, int)
            or not 0 <= self.domain_generation <= 2**53 - 1
        ):
            raise KineticsAuthoringError("source.domain_generation is invalid")
        network_id = _identifier(self.network_id, "source.network_id")
        network_revision = _identifier(self.network_revision, "source.network_revision")
        domain_sha = _sha(self.domain_snapshot_sha256, "source.domain_snapshot_sha256")
        network_sha = _sha(self.network_semantic_sha256, "source.network_semantic_sha256")
        projection_sha = _sha(
            self.source_projection_sha256, "source.source_projection_sha256"
        )
        feeds = _source_ids(
            self.required_feed_reservoir_ids, "source.required_feed_reservoir_ids"
        )
        targets = _source_ids(
            self.allowed_target_product_ids, "source.allowed_target_product_ids"
        )
        steps = _source_ids(self.required_step_ids, "source.required_step_ids")
        sites = _source_ids(self.required_site_type_ids, "source.required_site_type_ids")
        if not isinstance(self.saddle_candidates_by_step, Mapping):
            raise KineticsAuthoringError("source.saddle_candidates_by_step is invalid")
        unknown_saddle_steps = set(self.saddle_candidates_by_step) - set(steps)
        if unknown_saddle_steps:
            raise KineticsAuthoringError("source saddle candidates name an unknown step")
        saddles = {
            _identifier(key, "source.saddle_candidates_by_step key"): _source_ids(
                values, f"source.saddle_candidates_by_step.{key}", nonempty=False
            )
            for key, values in sorted(self.saddle_candidates_by_step.items())
        }
        if (
            isinstance(self.evidence_catalog, (str, bytes))
            or not isinstance(self.evidence_catalog, Sequence)
            or len(self.evidence_catalog) > MAX_EVIDENCE_BINDINGS
        ):
            raise KineticsAuthoringError("source.evidence_catalog is invalid")
        catalog: dict[str, dict[str, Any]] = {}
        catalog_records = []
        for index, raw in enumerate(self.evidence_catalog):
            item = _strict(
                raw,
                {"reference_id", "kind", "artifact_sha256"},
                f"source.evidence_catalog[{index}]",
            )
            reference = _identifier(item["reference_id"], "evidence reference")
            if reference in catalog:
                raise KineticsAuthoringError("source evidence catalogue contains duplicates")
            kind = _identifier(item["kind"], "evidence kind")
            artifact_sha = item["artifact_sha256"]
            if artifact_sha is not None:
                artifact_sha = _sha(artifact_sha, "evidence artifact_sha256")
            normalized = {
                "reference_id": reference,
                "kind": kind,
                "artifact_sha256": artifact_sha,
            }
            catalog[reference] = normalized
            catalog_records.append(normalized)
        catalog_records.sort(key=lambda item: item["reference_id"])
        if not isinstance(self.solver_ready, bool):
            raise KineticsAuthoringError("source.solver_ready must be boolean")
        readiness_reasons = _source_ids(
            self.solver_readiness_reasons,
            "source.solver_readiness_reasons",
            nonempty=not self.solver_ready,
        )
        if self.solver_ready and readiness_reasons:
            raise KineticsAuthoringError(
                "solver-ready source snapshot must not declare readiness blockers"
            )
        material = {
            "schema": SOURCE_SNAPSHOT_SCHEMA,
            "project_id": project_id,
            "domain_authority_id": domain_authority,
            "domain_generation": self.domain_generation,
            "domain_snapshot_sha256": domain_sha,
            "network_id": network_id,
            "network_revision": network_revision,
            "network_semantic_sha256": network_sha,
            "source_projection_sha256": projection_sha,
            "required_feed_reservoir_ids": feeds,
            "allowed_target_product_ids": targets,
            "required_step_ids": steps,
            "required_site_type_ids": sites,
            "saddle_candidates_by_step": saddles,
            "evidence_catalog": catalog_records,
            "solver_ready": self.solver_ready,
            "solver_readiness_reasons": readiness_reasons,
        }
        digest = _digest(material)
        if self.snapshot_sha256 not in {"", digest}:
            raise KineticsAuthoringError("authoring source snapshot seal is invalid")
        for key, value in material.items():
            if key != "schema":
                object.__setattr__(self, key, _freeze_json(value))
        object.__setattr__(self, "_catalog", _freeze_json(catalog))
        object.__setattr__(self, "snapshot_sha256", digest)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthoringSourceSnapshot":
        fields = {
            "schema",
            "project_id",
            "domain_authority_id",
            "domain_generation",
            "domain_snapshot_sha256",
            "network_id",
            "network_revision",
            "network_semantic_sha256",
            "source_projection_sha256",
            "required_feed_reservoir_ids",
            "allowed_target_product_ids",
            "required_step_ids",
            "required_site_type_ids",
            "saddle_candidates_by_step",
            "evidence_catalog",
            "solver_ready",
            "solver_readiness_reasons",
            "snapshot_sha256",
        }
        _strict(value, fields, "authoring source snapshot")
        return cls(**dict(value))

    def evidence(self, reference: str) -> Mapping[str, Any] | None:
        value = self._catalog.get(reference)
        return None if value is None else _detached(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "domain_authority_id": self.domain_authority_id,
            "domain_generation": self.domain_generation,
            "domain_snapshot_sha256": self.domain_snapshot_sha256,
            "network_id": self.network_id,
            "network_revision": self.network_revision,
            "network_semantic_sha256": self.network_semantic_sha256,
            "source_projection_sha256": self.source_projection_sha256,
            "required_feed_reservoir_ids": list(self.required_feed_reservoir_ids),
            "allowed_target_product_ids": list(self.allowed_target_product_ids),
            "required_step_ids": list(self.required_step_ids),
            "required_site_type_ids": list(self.required_site_type_ids),
            "saddle_candidates_by_step": _detached(self.saddle_candidates_by_step),
            "evidence_catalog": _detached(self.evidence_catalog),
            "solver_ready": self.solver_ready,
            "solver_readiness_reasons": list(self.solver_readiness_reasons),
            "snapshot_sha256": self.snapshot_sha256,
        }

    def cas_dict(self) -> dict[str, Any]:
        value = {
            "schema": SOURCE_CAS_SCHEMA,
            "project_id": self.project_id,
            "domain_authority_id": self.domain_authority_id,
            "domain_generation": self.domain_generation,
            "domain_snapshot_sha256": self.domain_snapshot_sha256,
            "network_id": self.network_id,
            "network_revision": self.network_revision,
            "network_semantic_sha256": self.network_semantic_sha256,
            "source_projection_sha256": self.source_projection_sha256,
            "snapshot_sha256": self.snapshot_sha256,
        }
        return value


def _source_ids(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or (nonempty and not value)
        or len(value) > MAX_COLLECTION
    ):
        raise KineticsAuthoringError(f"{label} must be a bounded identifier array")
    normalized = [_identifier(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(normalized) != len(set(normalized)):
        raise KineticsAuthoringError(f"{label} contains duplicate identifiers")
    return normalized


@runtime_checkable
class AuthoringSourceAuthority(Protocol):
    """Trusted server seam; every call must read the current sealed authority."""

    def authoring_source_snapshot(self) -> AuthoringSourceSnapshot:
        """Return a new, sealed source/membership/evidence snapshot."""


@runtime_checkable
class EvidenceResolver(Protocol):
    """Resolve one server-catalogued opaque evidence reference to exact bytes."""

    def __call__(self, reference_id: str) -> bytes:
        """Return immutable bytes; filesystem paths are never accepted here."""


class KineticsModelSpecDraftCompiler:
    """Compile an explicit draft against one injected authoritative snapshot."""

    def __init__(self, evidence_resolver: EvidenceResolver | Callable[[str], bytes] | None = None):
        self._resolver = evidence_resolver
        self._resolved_bytes = 0

    def _binding(
        self, source: AuthoringSourceSnapshot, reference: str, path: str
    ) -> dict[str, str]:
        entry = source.evidence(reference)
        if entry is None:
            _invalid("unknown_evidence_ref", path, "evidence reference is outside the server catalogue")
        artifact_sha = entry["artifact_sha256"]
        if artifact_sha is None:
            if not callable(self._resolver):
                _invalid(
                    "evidence_resolver_unavailable",
                    path,
                    "evidence bytes are required but no server resolver is available",
                )
            try:
                payload = self._resolver(reference)
            except Exception:  # noqa: BLE001 - opaque resolver boundary
                _invalid("evidence_unavailable", path, "catalogued evidence could not be resolved")
            if not isinstance(payload, (bytes, bytearray, memoryview)):
                _invalid("evidence_unavailable", path, "evidence resolver did not return bytes")
            frozen = bytes(payload)
            self._resolved_bytes += len(frozen)
            if not frozen or self._resolved_bytes > MAX_EVIDENCE_BYTES:
                _invalid("evidence_unavailable", path, "resolved evidence exceeds its size limit")
            artifact_sha = hashlib.sha256(frozen).hexdigest()
        return {
            "kind": entry["kind"],
            "reference": reference,
            "evidence_sha256": artifact_sha,
        }

    def _evidence_list(
        self, source: AuthoringSourceSnapshot, references: Sequence[str], path: str
    ) -> list[dict[str, str]]:
        return [
            self._binding(source, reference, f"{path}[{index}]")
            for index, reference in enumerate(references)
        ]

    def compile(
        self,
        draft: KineticsModelSpecDraft | Mapping[str, Any],
        source: AuthoringSourceSnapshot,
        current_head: KineticsModelSpec | None,
    ) -> KineticsModelSpec:
        draft = (
            draft
            if isinstance(draft, KineticsModelSpecDraft)
            else KineticsModelSpecDraft.from_dict(draft)
        )
        if not isinstance(source, AuthoringSourceSnapshot):
            raise KineticsAuthoringError("a sealed AuthoringSourceSnapshot is required")
        value = draft.to_dict()
        if draft.mode == "create":
            if current_head is not None:
                _invalid("spec_already_exists", "$draft.mode", "create requires an empty spec head")
            parent_revision = None
            expected_current_hash = None
        else:
            if current_head is None:
                _invalid("spec_head_missing", "$draft.mode", "advance requires an existing spec head")
            if current_head.project_id != source.project_id or current_head.spec_id != draft.spec_id:
                _invalid("spec_axis_mismatch", "$draft.spec_id", "current spec head is on another axis")
            parent_revision = current_head.revision
            expected_current_hash = current_head.semantic_sha256

        feeds = {item["species_id"] for item in value["feed_reservoirs"]}
        if feeds != set(source.required_feed_reservoir_ids):
            _invalid(
                "coverage_mismatch",
                "$draft.feed_reservoirs",
                "feed reservoirs must cover the authoritative required ids exactly",
            )
        targets = set(value["target_product_ids"])
        if not targets <= set(source.allowed_target_product_ids):
            _invalid(
                "membership_mismatch",
                "$draft.target_product_ids",
                "target products must belong to the authoritative source",
            )
        step_ids = {item["step_id"] for item in value["steps"]}
        if step_ids != set(source.required_step_ids):
            _invalid(
                "coverage_mismatch",
                "$draft.steps",
                "steps must cover the authoritative required ids exactly",
            )
        site_ids = {item["site_type"] for item in value["site_population_totals"]}
        if site_ids != set(source.required_site_type_ids):
            _invalid(
                "coverage_mismatch",
                "$draft.site_population_totals",
                "site totals must cover the authoritative required ids exactly",
            )
        saddle = value.get("saddle_selector")
        if saddle is not None:
            for step_id, species_id in saddle["by_step"].items():
                if species_id not in set(source.saddle_candidates_by_step.get(step_id, ())):
                    _invalid(
                        "membership_mismatch",
                        f"$draft.saddle_selector.by_step.{step_id}",
                        "selected saddle species is outside the authoritative candidates",
                    )

        assumptions = value["assumptions"]
        compiled_assumptions = {
            key: assumptions[key]
            for key in (
                "mean_field",
                "steady_state",
                "site_uniformity",
                "lateral_interactions",
                "mechanism_completeness",
            )
        }
        compiled_assumptions["evidence"] = self._evidence_list(
            source, assumptions["evidence_ref_ids"], "$draft.assumptions.evidence_ref_ids"
        )
        reservoirs = []
        for index, item in enumerate(value["feed_reservoirs"]):
            binding = self._binding(
                source, item["source_ref_id"], f"$draft.feed_reservoirs[{index}].source_ref_id"
            )
            reservoirs.append(
                {
                    "species_id": item["species_id"],
                    "activity": item["activity"],
                    "unit": item["unit"],
                    "source": binding["reference"],
                    "evidence_sha256": binding["evidence_sha256"],
                }
            )
        compiled_steps = []
        for index, item in enumerate(value["steps"]):
            prefactors = {}
            for direction in ("forward", "reverse"):
                raw = item["prefactors"][direction]
                prefactors[direction] = {
                    "value": raw["value"],
                    "unit": raw["unit"],
                    "source": self._binding(
                        source,
                        raw["source_ref_id"],
                        f"$draft.steps[{index}].prefactors.{direction}.source_ref_id",
                    ),
                }
            compiled_steps.append(
                {
                    "step_id": item["step_id"],
                    "prefactors": prefactors,
                    "bep": self._compile_empirical(
                        source, item["bep"], f"$draft.steps[{index}].bep"
                    ),
                    "scaling": self._compile_empirical(
                        source, item["scaling"], f"$draft.steps[{index}].scaling"
                    ),
                    "uncertainty_eV": item["uncertainty_eV"],
                    "evidence": self._evidence_list(
                        source,
                        item["evidence_ref_ids"],
                        f"$draft.steps[{index}].evidence_ref_ids",
                    ),
                }
            )
        compiled_totals = []
        for index, item in enumerate(value["site_population_totals"]):
            compiled_totals.append(
                {
                    "site_type": item["site_type"],
                    "value": item["value"],
                    "unit": item["unit"],
                    "basis": item["basis"],
                    "evidence": self._evidence_list(
                        source,
                        item["evidence_ref_ids"],
                        f"$draft.site_population_totals[{index}].evidence_ref_ids",
                    ),
                }
            )
        compiled_saddle = None
        if saddle is not None:
            compiled_saddle = {
                "mode": saddle["mode"],
                "by_step": saddle["by_step"],
                "evidence": self._evidence_list(
                    source,
                    saddle["evidence_ref_ids"],
                    "$draft.saddle_selector.evidence_ref_ids",
                ),
            }
        revision_material = {
            "draft_sha256": draft.semantic_sha256,
            "source_snapshot_sha256": source.snapshot_sha256,
            "parent_revision": parent_revision,
            "expected_current_hash": expected_current_hash,
        }
        revision = f"authoring-{_digest(revision_material)[:32]}"
        try:
            return KineticsModelSpec(
                project_id=source.project_id,
                spec_id=draft.spec_id,
                revision=revision,
                parent_revision=parent_revision,
                expected_current_hash=expected_current_hash,
                source_binding={
                    "domain_authority_id": source.domain_authority_id,
                    "domain_generation": source.domain_generation,
                    "network_id": source.network_id,
                    "network_revision": source.network_revision,
                    "source_projection_sha256": source.source_projection_sha256,
                },
                rate_law_policy=value["rate_law_policy"],
                assumptions=compiled_assumptions,
                feed_reservoirs=reservoirs,
                target_products=value["target_product_ids"],
                steps=compiled_steps,
                site_population_totals=compiled_totals,
                saddle_selector=compiled_saddle,
            )
        except KineticsModelSpecError as exc:
            _invalid("compiled_spec_invalid", "$draft", str(exc))

    def _compile_empirical(
        self, source: AuthoringSourceSnapshot, item: Mapping[str, Any], path: str
    ) -> dict[str, Any]:
        if not item["used"]:
            return {"used": False, "source": None, "parameters_sha256": None}
        source_binding = self._binding(source, item["source_ref_id"], f"{path}.source_ref_id")
        parameters = self._binding(
            source, item["parameters_ref_id"], f"{path}.parameters_ref_id"
        )
        return {
            "used": True,
            "source": source_binding,
            "parameters_sha256": parameters["evidence_sha256"],
        }


@dataclass(frozen=True)
class ActiveKineticsModelSpecSelection:
    """Immutable v2 selector record advanced only through strict CAS."""

    authority_id: str
    revision: int
    parent_revision: int | None
    expected_current_hash: str | None
    project_id: str
    spec_id: str
    spec_revision: str
    spec_sha256: str
    intent_id: str
    transaction_id: str
    confirmed: bool
    selection_sha256: str = ""
    schema: str = SELECTOR_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SELECTOR_SCHEMA:
            raise KineticsAuthoringError("active spec selector schema is unsupported")
        object.__setattr__(self, "authority_id", _authority(self.authority_id, "selector.authority_id"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise KineticsAuthoringError("selector.revision is invalid")
        if self.revision == 1:
            if self.parent_revision is not None or self.expected_current_hash is not None:
                raise KineticsAuthoringError("initial selector must not declare a parent")
        else:
            if self.parent_revision != self.revision - 1:
                raise KineticsAuthoringError("selector.parent_revision is invalid")
            object.__setattr__(
                self,
                "expected_current_hash",
                _sha(self.expected_current_hash, "selector.expected_current_hash"),
            )
        for name in ("project_id", "spec_id", "spec_revision", "intent_id", "transaction_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"selector.{name}"))
        object.__setattr__(self, "spec_sha256", _sha(self.spec_sha256, "selector.spec_sha256"))
        if self.confirmed is not True:
            raise KineticsAuthoringError("active spec selector requires explicit confirmation")
        digest = _digest(self._hash_material())
        if self.selection_sha256 not in {"", digest}:
            raise KineticsAuthoringError("active spec selector hash is invalid")
        object.__setattr__(self, "selection_sha256", digest)

    def _hash_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "authority_id": self.authority_id,
            "revision": self.revision,
            "parent_revision": self.parent_revision,
            "expected_current_hash": self.expected_current_hash,
            "project_id": self.project_id,
            "spec_id": self.spec_id,
            "spec_revision": self.spec_revision,
            "spec_sha256": self.spec_sha256,
            "intent_id": self.intent_id,
            "transaction_id": self.transaction_id,
            "confirmed": self.confirmed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_material(), "selection_sha256": self.selection_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActiveKineticsModelSpecSelection":
        _strict(
            value,
            {
                "schema",
                "authority_id",
                "revision",
                "parent_revision",
                "expected_current_hash",
                "project_id",
                "spec_id",
                "spec_revision",
                "spec_sha256",
                "intent_id",
                "transaction_id",
                "confirmed",
                "selection_sha256",
            },
            "active spec selector",
        )
        return cls(**dict(value))


def _validate_legacy_selector(value: Any) -> tuple[str, dict[str, Any]]:
    """Validate v1 for read/migration visibility without granting write authority."""

    fields = {
        "schema",
        "authority_id",
        "revision",
        "parent_revision",
        "expected_current_hash",
        "project_id",
        "spec_id",
        "spec_revision",
        "spec_sha256",
        "confirmed",
        "selection_sha256",
    }
    record = _strict(value, fields, "legacy active spec selector")
    if record["schema"] != LEGACY_SELECTOR_SCHEMA:
        raise KineticsAuthoringError("legacy selector schema is unsupported")
    authority = _authority(record["authority_id"], "legacy selector.authority_id")
    revision = record["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise KineticsAuthoringError("legacy selector.revision is invalid")
    if revision == 1:
        if record["parent_revision"] is not None or record["expected_current_hash"] is not None:
            raise KineticsAuthoringError("legacy initial selector parent is invalid")
    else:
        if record["parent_revision"] != revision - 1:
            raise KineticsAuthoringError("legacy selector parent is invalid")
        _sha(record["expected_current_hash"], "legacy selector.expected_current_hash")
    for name in ("project_id", "spec_id", "spec_revision"):
        _identifier(record[name], f"legacy selector.{name}")
    _sha(record["spec_sha256"], "legacy selector.spec_sha256")
    if record["confirmed"] is not True:
        raise KineticsAuthoringError("legacy selector is not confirmed")
    material = dict(record)
    declared = _sha(material.pop("selection_sha256"), "legacy selector.selection_sha256")
    if _digest(material) != declared:
        raise KineticsAuthoringError("legacy selector hash is invalid")
    return authority, _detached(record)


@dataclass(frozen=True)
class ActiveKineticsSelectorSnapshot:
    """Path-free CAS view; legacy v1 is visible but never writable."""

    authority_id: str | None
    revision: int
    current_hash: str | None
    selection: ActiveKineticsModelSpecSelection | None
    migration_state: str = "none"
    legacy_selection_sha256: str | None = None
    snapshot_sha256: str = ""
    schema: str = SELECTOR_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SELECTOR_SNAPSHOT_SCHEMA:
            raise KineticsAuthoringError("selector snapshot schema is unsupported")
        if self.migration_state not in {"none", "legacy_v1_read_only"}:
            raise KineticsAuthoringError("selector migration state is invalid")
        if self.migration_state == "legacy_v1_read_only":
            authority = _authority(self.authority_id, "selector snapshot.authority_id")
            if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
                raise KineticsAuthoringError("legacy selector snapshot revision is invalid")
            current = _sha(self.current_hash, "selector snapshot.current_hash")
            legacy_hash = _sha(
                self.legacy_selection_sha256, "selector snapshot.legacy_selection_sha256"
            )
            if self.selection is not None or current != legacy_hash:
                raise KineticsAuthoringError("legacy selector snapshot is invalid")
            object.__setattr__(self, "authority_id", authority)
            object.__setattr__(self, "current_hash", current)
        elif self.revision == 0:
            if any(
                value is not None
                for value in (
                    self.authority_id,
                    self.current_hash,
                    self.selection,
                    self.legacy_selection_sha256,
                )
            ):
                raise KineticsAuthoringError("empty selector snapshot is invalid")
        else:
            authority = _authority(self.authority_id, "selector snapshot.authority_id")
            current = _sha(self.current_hash, "selector snapshot.current_hash")
            if (
                not isinstance(self.selection, ActiveKineticsModelSpecSelection)
                or self.selection.authority_id != authority
                or self.selection.revision != self.revision
                or self.selection.selection_sha256 != current
                or self.legacy_selection_sha256 is not None
            ):
                raise KineticsAuthoringError("selector snapshot current record is invalid")
        digest = _digest(self._hash_material())
        if self.snapshot_sha256 not in {"", digest}:
            raise KineticsAuthoringError("selector snapshot seal is invalid")
        object.__setattr__(self, "snapshot_sha256", digest)

    @property
    def writable(self) -> bool:
        return self.migration_state == "none"

    def _hash_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "authority_id": self.authority_id,
            "revision": self.revision,
            "current_hash": self.current_hash,
            "selection": None if self.selection is None else self.selection.to_dict(),
            "migration_state": self.migration_state,
            "legacy_selection_sha256": self.legacy_selection_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_material(), "snapshot_sha256": self.snapshot_sha256}


@dataclass(frozen=True)
class KineticsAuthoringPreview:
    """Read-only preview result; the compiled spec remains server-private."""

    intent_id: str
    draft_sha256: str
    preview_sha256: str
    source_cas: Mapping[str, Any] | None
    store_cas: Mapping[str, Any] | None
    selector_cas: Mapping[str, Any] | None
    target_spec: Mapping[str, Any] | None
    issues: Sequence[AuthoringIssue]
    can_confirm: bool
    solver_ready: bool
    solver_readiness_reasons: Sequence[str] = ()
    authorizes_execution: bool = False
    schema: str = PREVIEW_SCHEMA
    _compiled_spec: KineticsModelSpec | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema != PREVIEW_SCHEMA or self.authorizes_execution is not False:
            raise KineticsAuthoringError("preview authority boundary is invalid")
        object.__setattr__(self, "intent_id", _identifier(self.intent_id, "preview.intent_id"))
        object.__setattr__(self, "draft_sha256", _sha(self.draft_sha256, "preview.draft_sha256"))
        object.__setattr__(self, "preview_sha256", _sha(self.preview_sha256, "preview.preview_sha256"))
        if len(self.issues) > MAX_ISSUES:
            raise KineticsAuthoringError("preview issues exceed their limit")
        if self.can_confirm and (self.issues or self._compiled_spec is None):
            raise KineticsAuthoringError("confirmable preview is incomplete")
        reasons = tuple(
            _identifier(item, "preview.solver_readiness_reasons")
            for item in self.solver_readiness_reasons
        )
        if self.solver_ready and reasons:
            raise KineticsAuthoringError("solver-ready preview must not declare readiness blockers")
        if self._compiled_spec is None and self.solver_ready:
            raise KineticsAuthoringError("invalid preview cannot be solver-ready")
        if self._compiled_spec is not None and not self.solver_ready and not reasons:
            raise KineticsAuthoringError("solver-not-ready preview must declare bounded reasons")
        object.__setattr__(self, "solver_readiness_reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "intent_id": self.intent_id,
            "draft_sha256": self.draft_sha256,
            "preview_sha256": self.preview_sha256,
            "source_cas": _detached(self.source_cas),
            "store_cas": _detached(self.store_cas),
            "selector_cas": _detached(self.selector_cas),
            "target_spec": _detached(self.target_spec),
            "issues": [issue.to_dict() for issue in self.issues],
            "can_confirm": self.can_confirm,
            "solver_ready": self.solver_ready,
            "solver_readiness_reasons": list(self.solver_readiness_reasons),
            "authorizes_execution": self.authorizes_execution,
        }


@dataclass(frozen=True)
class KineticsAuthoringConfirmation:
    intent_id: str
    draft_sha256: str
    preview_sha256: str
    source_snapshot_sha256: str
    store_snapshot_sha256: str
    selector_snapshot_sha256: str
    confirmed: bool
    schema: str = CONFIRMATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONFIRMATION_SCHEMA:
            raise KineticsAuthoringError("confirmation schema is unsupported")
        object.__setattr__(self, "intent_id", _identifier(self.intent_id, "confirmation.intent_id"))
        for name in (
            "draft_sha256",
            "preview_sha256",
            "source_snapshot_sha256",
            "store_snapshot_sha256",
            "selector_snapshot_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), f"confirmation.{name}"))
        if self.confirmed is not True:
            raise KineticsAuthoringError("explicit confirmation is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "intent_id": self.intent_id,
            "draft_sha256": self.draft_sha256,
            "preview_sha256": self.preview_sha256,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "store_snapshot_sha256": self.store_snapshot_sha256,
            "selector_snapshot_sha256": self.selector_snapshot_sha256,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "KineticsAuthoringConfirmation":
        _strict(
            value,
            {
                "schema",
                "intent_id",
                "draft_sha256",
                "preview_sha256",
                "source_snapshot_sha256",
                "store_snapshot_sha256",
                "selector_snapshot_sha256",
                "confirmed",
            },
            "authoring confirmation",
        )
        return cls(**dict(value))


@dataclass(frozen=True)
class AdoptAndStopConflict:
    reason: str
    latest_source_cas: Mapping[str, Any] | None
    latest_store_cas: Mapping[str, Any] | None
    latest_selector_cas: Mapping[str, Any] | None
    retry_automatically: bool = False
    schema: str = CONFLICT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONFLICT_SCHEMA or self.retry_automatically is not False:
            raise KineticsAuthoringError("authoring conflict boundary is invalid")
        object.__setattr__(self, "reason", _identifier(self.reason, "conflict.reason"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "reason": self.reason,
            "latest_source_cas": _detached(self.latest_source_cas),
            "latest_store_cas": _detached(self.latest_store_cas),
            "latest_selector_cas": _detached(self.latest_selector_cas),
            "retry_automatically": self.retry_automatically,
        }


@dataclass(frozen=True)
class KineticsAuthoringConfirmResult:
    action: str
    spec: Mapping[str, Any] | None = None
    selector: Mapping[str, Any] | None = None
    receipt: Mapping[str, Any] | None = None
    conflict: AdoptAndStopConflict | None = None
    issues: Sequence[AuthoringIssue] = ()
    schema: str = CONFIRM_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONFIRM_RESULT_SCHEMA:
            raise KineticsAuthoringError("confirm result schema is unsupported")
        if self.action not in {
            "committed",
            "replayed",
            "conflict",
            "unavailable",
            "needs_repreview",
        }:
            raise KineticsAuthoringError("confirm result action is invalid")
        if self.action == "conflict" and self.conflict is None:
            raise KineticsAuthoringError("conflict result is missing its conflict DTO")
        if len(self.issues) > MAX_ISSUES:
            raise KineticsAuthoringError("confirm result issues exceed their limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "action": self.action,
            "spec": _detached(self.spec),
            "selector": _detached(self.selector),
            "receipt": _detached(self.receipt),
            "conflict": None if self.conflict is None else self.conflict.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _is_linklike(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    details = os.lstat(path)
    attributes = int(getattr(details, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse)


def _entity_id(details: os.stat_result) -> tuple[int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(getattr(details, "st_file_attributes", 0)),
    )


def _file_id(details: os.stat_result) -> tuple[int, ...]:
    return (
        *_entity_id(details),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        int(stat.S_IFMT(details.st_mode)),
    )


class _AuthoringBoundary:
    """Pin the project and fixed authoring directories for one process object."""

    def __init__(self, project_root: str | os.PathLike[str]):
        if not Path(project_root).is_absolute():
            raise KineticsAuthoringError("project_root must be an existing absolute directory")
        root = Path(os.path.abspath(os.fspath(project_root)))
        self.root = root
        self._pins: list[tuple[Path, tuple[int, int, int]]] = []
        self._pin_directory(root, "project root")
        current = root
        for component in AUTHORING_DIRECTORY:
            candidate = current / component
            try:
                os.mkdir(candidate)
            except FileExistsError:
                pass
            except OSError as exc:
                raise KineticsAuthoringError("authoring directory is unavailable") from exc
            self._pin_directory(candidate, "authoring directory")
            current = candidate
        self.directory = current
        self.selector_path = current / ACTIVE_SELECTOR_FILENAME
        self.selector_anchor_path = current / SELECTOR_ANCHOR_FILENAME
        self.lock_path = current / AUTHORING_LOCK_FILENAME
        self.pending_path = current / AUTHORING_PENDING_FILENAME
        self.receipt_path = current / AUTHORING_RECEIPT_FILENAME
        self.verify()

    def _pin_directory(self, path: Path, label: str) -> None:
        try:
            details = os.lstat(path)
        except OSError as exc:
            raise KineticsAuthoringError(f"{label} is unavailable") from exc
        if (
            _is_linklike(path)
            or not stat.S_ISDIR(details.st_mode)
            or os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.realpath(path))
        ):
            raise KineticsAuthoringError(f"{label} must not be a link or reparse point")
        self._pins.append((path, _entity_id(details)))

    def verify(self) -> None:
        for path, identity in self._pins:
            try:
                details = os.lstat(path)
            except OSError as exc:
                raise KineticsAuthoringError("authoring directory boundary changed") from exc
            if _is_linklike(path) or not stat.S_ISDIR(details.st_mode) or _entity_id(details) != identity:
                raise KineticsAuthoringError("authoring directory boundary changed")


def _read_regular(path: Path, *, maximum: int, label: str) -> bytes | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KineticsAuthoringError(f"{label} is unreadable") from exc
    if _is_linklike(path) or not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
        raise KineticsAuthoringError(f"{label} is unsafe or outside its size limit")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KineticsAuthoringError(f"{label} is unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _entity_id(opened) != _entity_id(before):
            raise KineticsAuthoringError(f"{label} entity changed")
        chunks = []
        remaining = int(opened.st_size)
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise KineticsAuthoringError(f"{label} changed while read") from exc
        if (
            len(payload) != opened.st_size
            or _file_id(opened) != _file_id(after)
            or _entity_id(current) != _entity_id(after)
            or int(current.st_size) != int(after.st_size)
            or int(current.st_mtime_ns) != int(after.st_mtime_ns)
        ):
            raise KineticsAuthoringError(f"{label} changed while read")
        return payload
    finally:
        os.close(descriptor)


def _read_json(path: Path, *, maximum: int, label: str) -> Any | None:
    payload = _read_regular(path, maximum=maximum, label=label)
    if payload is None:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise KineticsAuthoringError(f"{label} is invalid JSON") from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _atomic_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    maximum: int,
    label: str,
    boundary: _AuthoringBoundary,
) -> None:
    boundary.verify()
    if _is_linklike(path):
        raise KineticsAuthoringError(f"{label} must not be a link or reparse point")
    payload = _canonical_bytes(value) + b"\n"
    if len(payload) > maximum:
        raise KineticsAuthoringError(f"{label} exceeds its size limit")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        boundary.verify()
        os.replace(temporary, path)
        boundary.verify()
        _fsync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _unlink_regular(path: Path, *, label: str, boundary: _AuthoringBoundary) -> None:
    boundary.verify()
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise KineticsAuthoringError(f"{label} is unavailable") from exc
    if _is_linklike(path) or not stat.S_ISREG(details.st_mode):
        raise KineticsAuthoringError(f"{label} is unsafe")
    try:
        os.unlink(path)
    except OSError as exc:
        raise KineticsAuthoringError(f"{label} could not be removed") from exc
    _fsync_directory(path.parent)
    boundary.verify()


@contextlib.contextmanager
def _authoring_lock(boundary: _AuthoringBoundary) -> Iterator[None]:
    with _PROCESS_LOCK:
        boundary.verify()
        path = boundary.lock_path
        if _is_linklike(path):
            raise KineticsAuthoringError("authoring lock is unsafe")
        flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise KineticsAuthoringError("authoring lock is unavailable") from exc
        with os.fdopen(descriptor, "a+b") as handle:
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_LOCK_BYTES:
                raise KineticsAuthoringError("authoring lock is unsafe")
            current = os.lstat(path)
            if _entity_id(details) != _entity_id(current):
                raise KineticsAuthoringError("authoring lock entity changed")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    boundary.verify()
                    yield
                    boundary.verify()
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    boundary.verify()
                    yield
                    boundary.verify()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_SELECTOR_ANCHOR_DESCRIPTOR_FIELDS = frozenset(
    {"authority_id", "revision", "selection_sha256", "selection", "chain_sha256"}
)
_SELECTOR_ANCHOR_PREPARE_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "sequence",
        "previous_record_sha256",
        "transaction_id",
        "base",
        "target",
        "record_sha256",
    }
)
_SELECTOR_ANCHOR_COMMIT_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "sequence",
        "previous_record_sha256",
        "transaction_id",
        "prepare_record_sha256",
        "target",
        "record_sha256",
    }
)


def _selector_anchor_record_sha256(value: Mapping[str, Any]) -> str:
    return _digest({
        key: item for key, item in value.items() if key != "record_sha256"
    })


def _seal_selector_anchor_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(value)
    record["record_sha256"] = _selector_anchor_record_sha256(record)
    return record


def _selector_anchor_chain_sha256(
    selection: ActiveKineticsModelSpecSelection, previous_chain_sha256: str
) -> str:
    return _digest({
        "previous_chain_sha256": _sha(
            previous_chain_sha256, "selector anchor previous chain"
        ),
        "authority_id": selection.authority_id,
        "revision": selection.revision,
        "selection_sha256": selection.selection_sha256,
    })


def _selector_anchor_descriptor(
    selection: ActiveKineticsModelSpecSelection, previous_chain_sha256: str
) -> dict[str, Any]:
    return {
        "authority_id": selection.authority_id,
        "revision": selection.revision,
        "selection_sha256": selection.selection_sha256,
        "selection": selection.to_dict(),
        "chain_sha256": _selector_anchor_chain_sha256(
            selection, previous_chain_sha256
        ),
    }


def _validated_selector_anchor_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(
        _SELECTOR_ANCHOR_DESCRIPTOR_FIELDS
    ):
        raise KineticsAuthoringError("selector anchor descriptor is invalid")
    selection = ActiveKineticsModelSpecSelection.from_dict(value.get("selection"))
    authority_id = value.get("authority_id")
    revision = value.get("revision")
    selection_sha256 = value.get("selection_sha256")
    if (
        authority_id != selection.authority_id
        or revision != selection.revision
        or selection_sha256 != selection.selection_sha256
    ):
        raise KineticsAuthoringError("selector anchor descriptor is inconsistent")
    return {
        "authority_id": selection.authority_id,
        "revision": selection.revision,
        "selection_sha256": selection.selection_sha256,
        "selection": selection.to_dict(),
        "chain_sha256": _sha(
            value.get("chain_sha256"), "selector anchor chain_sha256"
        ),
    }


@dataclass(frozen=True)
class _SelectorAnchorState:
    records: tuple[Mapping[str, Any], ...]
    committed: Mapping[str, Any] | None
    pending: Mapping[str, Any] | None
    last_record_sha256: str


def _validated_selector_anchor_log(payload: bytes) -> _SelectorAnchorState:
    if payload and not payload.endswith(b"\n"):
        raise KineticsAuthoringError("selector anchor WAL has an incomplete tail")
    raw_lines = payload.splitlines()
    if len(raw_lines) > MAX_SELECTOR_ANCHOR_RECORDS:
        raise KineticsAuthoringError("selector anchor WAL exceeds its record limit")
    records: list[Mapping[str, Any]] = []
    committed: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    previous_record_sha256 = _ZERO_SHA256
    for sequence, raw_line in enumerate(raw_lines, start=1):
        try:
            raw = json.loads(raw_line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise KineticsAuthoringError("selector anchor WAL is invalid") from exc
        kind = raw.get("kind") if isinstance(raw, Mapping) else None
        fields = (
            _SELECTOR_ANCHOR_PREPARE_FIELDS
            if kind == "prepare"
            else _SELECTOR_ANCHOR_COMMIT_FIELDS
            if kind == "commit"
            else frozenset()
        )
        if not fields or not isinstance(raw, Mapping) or set(raw) != set(fields):
            raise KineticsAuthoringError("selector anchor WAL record is invalid")
        if (
            raw.get("schema") != SELECTOR_ANCHOR_RECORD_SCHEMA
            or raw.get("sequence") != sequence
            or raw.get("previous_record_sha256") != previous_record_sha256
        ):
            raise KineticsAuthoringError("selector anchor WAL chain is invalid")
        transaction_id = _identifier(
            raw.get("transaction_id"), "selector anchor transaction_id"
        )
        declared_record_sha256 = _sha(
            raw.get("record_sha256"), "selector anchor record_sha256"
        )
        if _selector_anchor_record_sha256(raw) != declared_record_sha256:
            raise KineticsAuthoringError("selector anchor WAL seal is invalid")
        target = _validated_selector_anchor_descriptor(raw.get("target"))
        target_selection = ActiveKineticsModelSpecSelection.from_dict(
            target["selection"]
        )
        if transaction_id != target_selection.transaction_id:
            raise KineticsAuthoringError("selector anchor transaction is inconsistent")
        if kind == "prepare":
            if pending is not None:
                raise KineticsAuthoringError(
                    "selector anchor WAL contains nested transactions"
                )
            raw_base = raw.get("base")
            if committed is None:
                if raw_base is not None or target_selection.revision != 1:
                    raise KineticsAuthoringError(
                        "selector anchor initialization is invalid"
                    )
                previous_chain_sha256 = _ZERO_SHA256
            else:
                base = _validated_selector_anchor_descriptor(raw_base)
                base_selection = ActiveKineticsModelSpecSelection.from_dict(
                    base["selection"]
                )
                if (
                    base != committed
                    or target_selection.authority_id != base_selection.authority_id
                    or target_selection.revision != base_selection.revision + 1
                    or target_selection.parent_revision != base_selection.revision
                    or target_selection.expected_current_hash
                    != base_selection.selection_sha256
                ):
                    raise KineticsAuthoringError(
                        "selector anchor transition is invalid"
                    )
                previous_chain_sha256 = committed["chain_sha256"]
            if target["chain_sha256"] != _selector_anchor_chain_sha256(
                target_selection, previous_chain_sha256
            ):
                raise KineticsAuthoringError(
                    "selector anchor transition seal is invalid"
                )
            pending = {
                "record": dict(raw),
                "transaction_id": transaction_id,
                "base": None if committed is None else dict(committed),
                "target": target,
            }
        else:
            if (
                pending is None
                or raw.get("prepare_record_sha256")
                != pending["record"]["record_sha256"]
                or transaction_id != pending["transaction_id"]
                or target != pending["target"]
            ):
                raise KineticsAuthoringError("selector anchor commit is invalid")
            committed = target
            pending = None
        normalized = dict(raw)
        normalized["target"] = target
        if normalized.get("base") is not None:
            normalized["base"] = _validated_selector_anchor_descriptor(
                normalized["base"]
            )
        records.append(normalized)
        previous_record_sha256 = declared_record_sha256
    return _SelectorAnchorState(
        records=tuple(records),
        committed=committed,
        pending=pending,
        last_record_sha256=previous_record_sha256,
    )


def _truncate_selector_anchor_tail(
    path: Path, length: int, boundary: _AuthoringBoundary
) -> None:
    boundary.verify()
    before = os.lstat(path)
    if (
        _is_linklike(path)
        or not stat.S_ISREG(before.st_mode)
        or not 0 <= length <= before.st_size
    ):
        raise KineticsAuthoringError("selector anchor WAL is unsafe")
    flags = os.O_RDWR | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KineticsAuthoringError("selector anchor WAL is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            _entity_id(opened) != _entity_id(before)
            or not stat.S_ISREG(opened.st_mode)
            or int(opened.st_size) != int(before.st_size)
        ):
            raise KineticsAuthoringError("selector anchor WAL changed")
        os.ftruncate(descriptor, length)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise KineticsAuthoringError(
            "selector anchor WAL could not be recovered"
        ) from exc
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    if _entity_id(after) != _entity_id(current) or int(current.st_size) != length:
        raise KineticsAuthoringError("selector anchor WAL changed")
    boundary.verify()


def _read_selector_anchor_state(
    path: Path, boundary: _AuthoringBoundary
) -> _SelectorAnchorState:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return _SelectorAnchorState((), None, None, _ZERO_SHA256)
    except OSError as exc:
        raise KineticsAuthoringError("selector anchor WAL is unavailable") from exc
    if _is_linklike(path) or not stat.S_ISREG(metadata.st_mode):
        raise KineticsAuthoringError("selector anchor WAL is unsafe")
    if metadata.st_size == 0:
        return _SelectorAnchorState((), None, None, _ZERO_SHA256)
    payload = _read_regular(
        path, maximum=MAX_SELECTOR_ANCHOR_BYTES, label="selector anchor WAL"
    )
    assert payload is not None
    if not payload.endswith(b"\n"):
        complete_length = payload.rfind(b"\n") + 1
        complete = payload[:complete_length]
        state = _validated_selector_anchor_log(complete)
        _truncate_selector_anchor_tail(path, complete_length, boundary)
        return state
    return _validated_selector_anchor_log(payload)


def _append_selector_anchor_record(
    path: Path, record: Mapping[str, Any], boundary: _AuthoringBoundary
) -> None:
    payload = _canonical_bytes(record) + b"\n"
    boundary.verify()
    if _is_linklike(path):
        raise KineticsAuthoringError(
            "selector anchor WAL must not be a link or reparse point"
        )
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise KineticsAuthoringError("selector anchor WAL is unavailable") from exc
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise KineticsAuthoringError("selector anchor WAL is unsafe")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise KineticsAuthoringError("selector anchor WAL is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before is not None and _entity_id(opened) != _entity_id(before))
            or opened.st_size + len(payload) > MAX_SELECTOR_ANCHOR_BYTES
        ):
            raise KineticsAuthoringError("selector anchor WAL is unsafe")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise KineticsAuthoringError(
                    "selector anchor WAL append made no progress"
                )
            offset += written
        os.fsync(descriptor)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise KineticsAuthoringError("selector anchor WAL append failed") from exc
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    if _entity_id(after) != _entity_id(current) or int(current.st_size) != int(
        after.st_size
    ):
        raise KineticsAuthoringError("selector anchor WAL changed")
    if before is None:
        _fsync_directory(path.parent)
    boundary.verify()


def _selector_anchor_prepare_record(
    state: _SelectorAnchorState, target: ActiveKineticsModelSpecSelection
) -> dict[str, Any]:
    if len(state.records) >= MAX_SELECTOR_ANCHOR_RECORDS:
        raise KineticsAuthoringError("selector anchor WAL exceeds its record limit")
    previous_chain = (
        _ZERO_SHA256
        if state.committed is None
        else str(state.committed["chain_sha256"])
    )
    return _seal_selector_anchor_record({
        "schema": SELECTOR_ANCHOR_RECORD_SCHEMA,
        "kind": "prepare",
        "sequence": len(state.records) + 1,
        "previous_record_sha256": state.last_record_sha256,
        "transaction_id": target.transaction_id,
        "base": None if state.committed is None else dict(state.committed),
        "target": _selector_anchor_descriptor(target, previous_chain),
    })


def _selector_anchor_commit_record(
    state: _SelectorAnchorState, prepare: Mapping[str, Any]
) -> dict[str, Any]:
    if len(state.records) >= MAX_SELECTOR_ANCHOR_RECORDS:
        raise KineticsAuthoringError("selector anchor WAL exceeds its record limit")
    return _seal_selector_anchor_record({
        "schema": SELECTOR_ANCHOR_RECORD_SCHEMA,
        "kind": "commit",
        "sequence": len(state.records) + 1,
        "previous_record_sha256": state.last_record_sha256,
        "transaction_id": prepare["transaction_id"],
        "prepare_record_sha256": prepare["record_sha256"],
        "target": dict(prepare["target"]),
    })


class ActiveKineticsModelSpecSelectorStore:
    """Fixed-file v2 selector store with strict CAS and read-first recovery."""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        model_store_factory: Callable[[str | os.PathLike[str]], Any] = KineticsModelSpecStore,
        _boundary: _AuthoringBoundary | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ):
        self._boundary = _boundary or _AuthoringBoundary(project_root)
        self._model_store_factory = model_store_factory
        self._fault_injector = fault_injector
        self.path = self._boundary.selector_path
        self.anchor_path = self._boundary.selector_anchor_path
        self._anchor_identity: tuple[int, int, int] | None = None

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _verify_anchor_entity(self) -> None:
        if self._anchor_identity is None:
            return
        try:
            metadata = os.lstat(self.anchor_path)
        except OSError as exc:
            raise KineticsAuthoringError("selector anchor WAL entity changed") from exc
        if (
            _is_linklike(self.anchor_path)
            or not stat.S_ISREG(metadata.st_mode)
            or _entity_id(metadata) != self._anchor_identity
        ):
            raise KineticsAuthoringError("selector anchor WAL entity changed")

    def _pin_anchor_entity(self) -> None:
        if not os.path.lexists(self.anchor_path):
            return
        metadata = os.lstat(self.anchor_path)
        if _is_linklike(self.anchor_path) or not stat.S_ISREG(metadata.st_mode):
            raise KineticsAuthoringError("selector anchor WAL is unsafe")
        identity = _entity_id(metadata)
        if self._anchor_identity is None:
            self._anchor_identity = identity
        elif self._anchor_identity != identity:
            raise KineticsAuthoringError("selector anchor WAL entity changed")

    def _anchor_state_unlocked(self) -> _SelectorAnchorState:
        self._verify_anchor_entity()
        state = _read_selector_anchor_state(self.anchor_path, self._boundary)
        self._pin_anchor_entity()
        self._verify_anchor_entity()
        return state

    def _append_anchor_unlocked(self, record: Mapping[str, Any]) -> None:
        self._verify_anchor_entity()
        _append_selector_anchor_record(
            self.anchor_path, record, self._boundary
        )
        self._pin_anchor_entity()
        self._verify_anchor_entity()

    def _raw_snapshot_unlocked(self) -> ActiveKineticsSelectorSnapshot:
        raw = _read_json(self.path, maximum=MAX_SELECTOR_BYTES, label="active spec selector")
        if raw is None:
            return ActiveKineticsSelectorSnapshot(
                authority_id=None, revision=0, current_hash=None, selection=None
            )
        if not isinstance(raw, Mapping):
            raise KineticsAuthoringError("active spec selector is invalid")
        if raw.get("schema") == SELECTOR_SCHEMA:
            selection = ActiveKineticsModelSpecSelection.from_dict(raw)
            return ActiveKineticsSelectorSnapshot(
                authority_id=selection.authority_id,
                revision=selection.revision,
                current_hash=selection.selection_sha256,
                selection=selection,
            )
        if raw.get("schema") == LEGACY_SELECTOR_SCHEMA:
            authority, legacy = _validate_legacy_selector(raw)
            return ActiveKineticsSelectorSnapshot(
                authority_id=authority,
                revision=legacy["revision"],
                current_hash=legacy["selection_sha256"],
                selection=None,
                migration_state="legacy_v1_read_only",
                legacy_selection_sha256=legacy["selection_sha256"],
            )
        raise KineticsAuthoringError("active spec selector schema is unsupported")

    @staticmethod
    def _matches_anchor_descriptor(
        snapshot: ActiveKineticsSelectorSnapshot,
        descriptor: Mapping[str, Any] | None,
    ) -> bool:
        if descriptor is None:
            return snapshot.revision == 0 and snapshot.selection is None
        if snapshot.selection is None:
            return False
        selection = ActiveKineticsModelSpecSelection.from_dict(
            descriptor["selection"]
        )
        return (
            snapshot.current_hash == descriptor["selection_sha256"]
            and snapshot.selection.to_dict() == selection.to_dict()
        )

    def _recover_anchor_unlocked(
        self,
        state: _SelectorAnchorState,
        current: ActiveKineticsSelectorSnapshot,
    ) -> tuple[_SelectorAnchorState, ActiveKineticsSelectorSnapshot]:
        pending = state.pending
        if pending is None:
            return state, current
        if not current.writable:
            raise KineticsAuthoringRecoveryError(
                "pending selector anchor conflicts with a legacy selector"
            )
        base = pending["base"]
        target = pending["target"]
        if self._matches_anchor_descriptor(current, base):
            target_selection = ActiveKineticsModelSpecSelection.from_dict(
                target["selection"]
            )
            _atomic_json(
                self.path,
                target_selection.to_dict(),
                maximum=MAX_SELECTOR_BYTES,
                label="active spec selector",
                boundary=self._boundary,
            )
            current = self._raw_snapshot_unlocked()
        if not self._matches_anchor_descriptor(current, target):
            raise KineticsAuthoringRecoveryError(
                "selector is neither the exact prepared base nor target"
            )
        commit = _selector_anchor_commit_record(state, pending["record"])
        self._append_anchor_unlocked(commit)
        committed_state = self._anchor_state_unlocked()
        if (
            committed_state.pending is not None
            or committed_state.committed != target
        ):
            raise KineticsAuthoringRecoveryError(
                "selector anchor forward recovery did not commit exactly"
            )
        return committed_state, current

    def _snapshot_unlocked(self) -> ActiveKineticsSelectorSnapshot:
        current = self._raw_snapshot_unlocked()
        state = self._anchor_state_unlocked()
        if not current.writable:
            if state.records:
                raise KineticsAuthoringError(
                    "legacy selector conflicts with a v2 independent anchor"
                )
            return current
        state, current = self._recover_anchor_unlocked(state, current)
        committed = state.committed
        if committed is None:
            if current.selection is not None:
                raise KineticsAuthoringError(
                    "active v2 selector exists without its independent anchor"
                )
            return current
        if not self._matches_anchor_descriptor(current, committed):
            raise KineticsAuthoringError(
                "active v2 selector does not match its independent anchor"
            )
        return current

    def _advance_unlocked(
        self, target: ActiveKineticsModelSpecSelection, expected_snapshot_sha256: str
    ) -> tuple[str, ActiveKineticsSelectorSnapshot]:
        expected = _sha(expected_snapshot_sha256, "expected selector snapshot")
        current = self._snapshot_unlocked()
        if not current.writable:
            raise KineticsAuthoringError("legacy v1 selector is read-only and cannot authorize a write")
        if current.selection is not None and current.selection.intent_id == target.intent_id:
            if current.selection.selection_sha256 == target.selection_sha256:
                return "replayed", current
            raise KineticsAuthoringError("selector intent_id was reused with different content")
        if current.snapshot_sha256 != expected:
            raise KineticsAuthoringError("selector compare-and-swap is stale")
        if current.revision == 0:
            if target.revision != 1 or target.parent_revision is not None or target.expected_current_hash is not None:
                raise KineticsAuthoringError("initial selector target is invalid")
        elif (
            target.authority_id != current.authority_id
            or target.revision != current.revision + 1
            or target.parent_revision != current.revision
            or target.expected_current_hash != current.current_hash
        ):
            raise KineticsAuthoringError("selector target does not advance the exact current CAS")
        state = self._anchor_state_unlocked()
        if state.pending is not None:
            raise KineticsAuthoringRecoveryError(
                "selector anchor contains an unrecovered transition"
            )
        if (
            (current.revision == 0 and state.committed is not None)
            or (
                current.revision > 0
                and (
                    state.committed is None
                    or not self._matches_anchor_descriptor(current, state.committed)
                )
            )
        ):
            raise KineticsAuthoringError(
                "selector does not match its independent anchor"
            )
        prepare = _selector_anchor_prepare_record(state, target)
        self._append_anchor_unlocked(prepare)
        self._fault("after_selector_prepare")
        _atomic_json(
            self.path,
            target.to_dict(),
            maximum=MAX_SELECTOR_BYTES,
            label="active spec selector",
            boundary=self._boundary,
        )
        self._fault("after_selector_replace")
        prepared_state = self._anchor_state_unlocked()
        if (
            prepared_state.pending is None
            or prepared_state.pending["record"]["record_sha256"]
            != prepare["record_sha256"]
        ):
            raise KineticsAuthoringRecoveryError(
                "selector anchor prepare could not be revalidated"
            )
        raw_committed = self._raw_snapshot_unlocked()
        target_descriptor = prepared_state.pending["target"]
        if not self._matches_anchor_descriptor(raw_committed, target_descriptor):
            raise KineticsAuthoringRecoveryError(
                "selector replacement does not match its prepared anchor"
            )
        commit = _selector_anchor_commit_record(prepared_state, prepare)
        self._append_anchor_unlocked(commit)
        committed = self._snapshot_unlocked()
        if (
            committed.selection is None
            or committed.selection.selection_sha256 != target.selection_sha256
            or committed.selection.to_dict() != target.to_dict()
        ):
            raise KineticsAuthoringError("selector commit could not be revalidated")
        return "created" if current.revision == 0 else "advanced", committed

    def snapshot(self) -> ActiveKineticsSelectorSnapshot:
        with _authoring_lock(self._boundary):
            model_store = self._model_store_factory(self._boundary.root)
            _recover_pending_locked(self._boundary, model_store, self)
            return self._snapshot_unlocked()

    def select(self, *, project_id: str | None = None) -> ActiveKineticsModelSpecSelection | None:
        snapshot = self.snapshot()
        selection = snapshot.selection
        if selection is None:
            return None
        if project_id is not None and selection.project_id != _identifier(project_id, "project_id"):
            return None
        return ActiveKineticsModelSpecSelection.from_dict(selection.to_dict())

    def read_compatible(self) -> Mapping[str, Any] | None:
        """Return validated v1/v2 data for migration display; never write from v1."""

        with _authoring_lock(self._boundary):
            model_store = self._model_store_factory(self._boundary.root)
            _recover_pending_locked(self._boundary, model_store, self)
            snapshot = self._snapshot_unlocked()
            if snapshot.revision == 0:
                return None
            raw = _read_json(self.path, maximum=MAX_SELECTOR_BYTES, label="active spec selector")
            assert isinstance(raw, Mapping)
            if raw.get("schema") == SELECTOR_SCHEMA:
                return ActiveKineticsModelSpecSelection.from_dict(raw).to_dict()
            _validate_legacy_selector(raw)
            return _detached(raw)

    def compare_and_swap(
        self,
        target: ActiveKineticsModelSpecSelection | Mapping[str, Any],
        *,
        expected_snapshot_sha256: str,
    ) -> tuple[str, ActiveKineticsSelectorSnapshot]:
        target = (
            target
            if isinstance(target, ActiveKineticsModelSpecSelection)
            else ActiveKineticsModelSpecSelection.from_dict(target)
        )
        with _authoring_lock(self._boundary):
            model_store = self._model_store_factory(self._boundary.root)
            _recover_pending_locked(self._boundary, model_store, self)
            return self._advance_unlocked(target, expected_snapshot_sha256)


def _head_record(
    project_id: str, spec_id: str, head: KineticsModelSpec | None
) -> dict[str, str] | None:
    if head is None:
        return None
    return {
        "project_id": project_id,
        "spec_id": spec_id,
        "revision": head.revision,
        "spec_sha256": head.semantic_sha256,
    }


def _validate_head_record(value: Any, label: str) -> dict[str, str] | None:
    if value is None:
        return None
    item = _strict(value, {"project_id", "spec_id", "revision", "spec_sha256"}, label)
    return {
        "project_id": _identifier(item["project_id"], f"{label}.project_id"),
        "spec_id": _identifier(item["spec_id"], f"{label}.spec_id"),
        "revision": _identifier(item["revision"], f"{label}.revision"),
        "spec_sha256": _sha(item["spec_sha256"], f"{label}.spec_sha256"),
    }


_PENDING_FIELDS = frozenset(
    {
        "schema",
        "transaction_id",
        "intent_id",
        "draft_sha256",
        "preview_sha256",
        "request_sha256",
        "source_snapshot_sha256",
        "base_store_snapshot_sha256",
        "base_model_head",
        "base_selector_snapshot_sha256",
        "target_spec",
        "target_spec_sha256",
        "target_selector",
        "target_selector_sha256",
        "pending_sha256",
    }
)


def _seal_pending(value: Mapping[str, Any]) -> dict[str, Any]:
    material = dict(value)
    material.pop("pending_sha256", None)
    return {**material, "pending_sha256": _digest(material)}


def _validated_pending(value: Any) -> dict[str, Any]:
    item = _strict(value, _PENDING_FIELDS, "authoring pending journal")
    if item["schema"] != PENDING_SCHEMA:
        raise KineticsAuthoringError("authoring pending schema is unsupported")
    transaction_id = _identifier(item["transaction_id"], "pending.transaction_id")
    intent_id = _identifier(item["intent_id"], "pending.intent_id")
    draft_sha = _sha(item["draft_sha256"], "pending.draft_sha256")
    preview_sha = _sha(item["preview_sha256"], "pending.preview_sha256")
    request_sha = _sha(item["request_sha256"], "pending.request_sha256")
    source_sha = _sha(item["source_snapshot_sha256"], "pending.source_snapshot_sha256")
    base_store_sha = _sha(
        item["base_store_snapshot_sha256"], "pending.base_store_snapshot_sha256"
    )
    base_head = _validate_head_record(item["base_model_head"], "pending.base_model_head")
    base_selector_sha = _sha(
        item["base_selector_snapshot_sha256"], "pending.base_selector_snapshot_sha256"
    )
    if not isinstance(item["target_spec"], Mapping):
        raise KineticsAuthoringError("pending target spec is invalid")
    target_spec = KineticsModelSpec.from_dict(item["target_spec"])
    target_spec_sha = _sha(item["target_spec_sha256"], "pending.target_spec_sha256")
    if target_spec.semantic_sha256 != target_spec_sha:
        raise KineticsAuthoringError("pending target spec hash is invalid")
    target_selector = ActiveKineticsModelSpecSelection.from_dict(item["target_selector"])
    target_selector_sha = _sha(
        item["target_selector_sha256"], "pending.target_selector_sha256"
    )
    if target_selector.selection_sha256 != target_selector_sha:
        raise KineticsAuthoringError("pending target selector hash is invalid")
    if (
        target_selector.project_id != target_spec.project_id
        or target_selector.spec_id != target_spec.spec_id
        or target_selector.spec_revision != target_spec.revision
        or target_selector.spec_sha256 != target_spec.semantic_sha256
        or target_selector.intent_id != intent_id
        or target_selector.transaction_id != transaction_id
    ):
        raise KineticsAuthoringError("pending target axes do not match")
    material = {
        "schema": PENDING_SCHEMA,
        "transaction_id": transaction_id,
        "intent_id": intent_id,
        "draft_sha256": draft_sha,
        "preview_sha256": preview_sha,
        "request_sha256": request_sha,
        "source_snapshot_sha256": source_sha,
        "base_store_snapshot_sha256": base_store_sha,
        "base_model_head": base_head,
        "base_selector_snapshot_sha256": base_selector_sha,
        "target_spec": target_spec.to_dict(),
        "target_spec_sha256": target_spec_sha,
        "target_selector": target_selector.to_dict(),
        "target_selector_sha256": target_selector_sha,
    }
    pending_sha = _sha(item["pending_sha256"], "pending.pending_sha256")
    if _digest(material) != pending_sha:
        raise KineticsAuthoringError("authoring pending journal seal is invalid")
    return {**material, "pending_sha256": pending_sha}


def _read_pending(boundary: _AuthoringBoundary) -> dict[str, Any] | None:
    raw = _read_json(
        boundary.pending_path,
        maximum=MAX_PENDING_BYTES,
        label="authoring pending journal",
    )
    return None if raw is None else _validated_pending(raw)


_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "outcome",
        "transaction_id",
        "intent_id",
        "draft_sha256",
        "preview_sha256",
        "request_sha256",
        "source_snapshot_sha256",
        "base_store_snapshot_sha256",
        "base_selector_snapshot_sha256",
        "project_id",
        "spec_id",
        "spec_revision",
        "spec_sha256",
        "selector_sha256",
        "receipt_sha256",
    }
)


def _seal_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    material = dict(value)
    material.pop("receipt_sha256", None)
    return {**material, "receipt_sha256": _digest(material)}


def _validated_receipt(value: Any) -> dict[str, Any]:
    item = _strict(value, _RECEIPT_FIELDS, "authoring commit receipt")
    if item["schema"] != RECEIPT_SCHEMA:
        raise KineticsAuthoringError("authoring receipt schema is unsupported")
    normalized = {
        "schema": RECEIPT_SCHEMA,
        "outcome": item["outcome"],
        "transaction_id": _identifier(item["transaction_id"], "receipt.transaction_id"),
        "intent_id": _identifier(item["intent_id"], "receipt.intent_id"),
        "draft_sha256": _sha(item["draft_sha256"], "receipt.draft_sha256"),
        "preview_sha256": _sha(item["preview_sha256"], "receipt.preview_sha256"),
        "request_sha256": _sha(item["request_sha256"], "receipt.request_sha256"),
        "source_snapshot_sha256": _sha(
            item["source_snapshot_sha256"], "receipt.source_snapshot_sha256"
        ),
        "base_store_snapshot_sha256": _sha(
            item["base_store_snapshot_sha256"], "receipt.base_store_snapshot_sha256"
        ),
        "base_selector_snapshot_sha256": _sha(
            item["base_selector_snapshot_sha256"], "receipt.base_selector_snapshot_sha256"
        ),
        "project_id": _identifier(item["project_id"], "receipt.project_id"),
        "spec_id": _identifier(item["spec_id"], "receipt.spec_id"),
        "spec_revision": _identifier(item["spec_revision"], "receipt.spec_revision"),
        "spec_sha256": _sha(item["spec_sha256"], "receipt.spec_sha256"),
        "selector_sha256": _sha(item["selector_sha256"], "receipt.selector_sha256"),
    }
    if normalized["outcome"] not in {"committed", "abandoned_requires_repreview"}:
        raise KineticsAuthoringError("authoring receipt outcome is invalid")
    receipt_sha = _sha(item["receipt_sha256"], "receipt.receipt_sha256")
    if _digest(normalized) != receipt_sha:
        raise KineticsAuthoringError("authoring commit receipt seal is invalid")
    return {**normalized, "receipt_sha256": receipt_sha}


def _empty_receipt_ledger() -> dict[str, Any]:
    material = {"schema": RECEIPT_LEDGER_SCHEMA, "records": []}
    return {**material, "ledger_sha256": _digest(material)}


def _validated_receipt_ledger(value: Any) -> dict[str, Any]:
    item = _strict(value, {"schema", "records", "ledger_sha256"}, "receipt ledger")
    if item["schema"] != RECEIPT_LEDGER_SCHEMA:
        raise KineticsAuthoringError("receipt ledger schema is unsupported")
    records_raw = item["records"]
    if (
        isinstance(records_raw, (str, bytes))
        or not isinstance(records_raw, Sequence)
        or len(records_raw) > MAX_RECEIPTS
    ):
        raise KineticsAuthoringError("receipt ledger records are invalid")
    records = [_validated_receipt(record) for record in records_raw]
    identities = [(record["intent_id"], record["transaction_id"]) for record in records]
    if len(identities) != len(set(identities)) or len({item[0] for item in identities}) != len(records):
        raise KineticsAuthoringError("receipt ledger contains duplicate intents")
    material = {"schema": RECEIPT_LEDGER_SCHEMA, "records": records}
    declared = _sha(item["ledger_sha256"], "receipt ledger seal")
    if _digest(material) != declared:
        raise KineticsAuthoringError("receipt ledger seal is invalid")
    return {**material, "ledger_sha256": declared}


def _read_receipts(boundary: _AuthoringBoundary) -> dict[str, Any]:
    raw = _read_json(
        boundary.receipt_path,
        maximum=MAX_RECEIPT_BYTES,
        label="authoring receipt ledger",
    )
    return _empty_receipt_ledger() if raw is None else _validated_receipt_ledger(raw)


def _receipt_for_intent(
    boundary: _AuthoringBoundary, intent_id: str
) -> dict[str, Any] | None:
    ledger = _read_receipts(boundary)
    for record in ledger["records"]:
        if record["intent_id"] == intent_id:
            return record
    return None


def _append_receipt(boundary: _AuthoringBoundary, receipt: Mapping[str, Any]) -> dict[str, Any]:
    receipt = _validated_receipt(receipt)
    ledger = _read_receipts(boundary)
    for existing in ledger["records"]:
        if existing["intent_id"] == receipt["intent_id"]:
            if existing != receipt:
                raise KineticsAuthoringRecoveryError(
                    "an authoring intent already has a different durable receipt"
                )
            return existing
    if len(ledger["records"]) >= MAX_RECEIPTS:
        raise KineticsAuthoringRecoveryError("authoring receipt ledger is full")
    material = {
        "schema": RECEIPT_LEDGER_SCHEMA,
        "records": [*ledger["records"], receipt],
    }
    sealed = {**material, "ledger_sha256": _digest(material)}
    _atomic_json(
        boundary.receipt_path,
        sealed,
        maximum=MAX_RECEIPT_BYTES,
        label="authoring receipt ledger",
        boundary=boundary,
    )
    return receipt


def _receipt_from_pending(
    pending: Mapping[str, Any], *, outcome: str = "committed"
) -> dict[str, Any]:
    spec = KineticsModelSpec.from_dict(pending["target_spec"])
    return _seal_receipt(
        {
            "schema": RECEIPT_SCHEMA,
            "outcome": outcome,
            "transaction_id": pending["transaction_id"],
            "intent_id": pending["intent_id"],
            "draft_sha256": pending["draft_sha256"],
            "preview_sha256": pending["preview_sha256"],
            "request_sha256": pending["request_sha256"],
            "source_snapshot_sha256": pending["source_snapshot_sha256"],
            "base_store_snapshot_sha256": pending["base_store_snapshot_sha256"],
            "base_selector_snapshot_sha256": pending["base_selector_snapshot_sha256"],
            "project_id": spec.project_id,
            "spec_id": spec.spec_id,
            "spec_revision": spec.revision,
            "spec_sha256": spec.semantic_sha256,
            "selector_sha256": pending["target_selector_sha256"],
        }
    )


def _model_side(
    model_store: Any, pending: Mapping[str, Any]
) -> tuple[str, KineticsModelSpec | None]:
    target = KineticsModelSpec.from_dict(pending["target_spec"])
    current = model_store.head(target.project_id, target.spec_id)
    current_record = _head_record(target.project_id, target.spec_id, current)
    target_record = _head_record(target.project_id, target.spec_id, target)
    if current_record == pending["base_model_head"]:
        try:
            snapshot = model_store.snapshot()
        except Exception as exc:  # noqa: BLE001 - injected store boundary
            raise KineticsAuthoringRecoveryError("model store is unavailable during recovery") from exc
        if snapshot.snapshot_sha256 != pending["base_store_snapshot_sha256"]:
            return "neither", current
        return "base", current
    if current_record == target_record:
        return "target", current
    return "neither", current


def _selector_side(
    selector_store: ActiveKineticsModelSpecSelectorStore, pending: Mapping[str, Any]
) -> tuple[str, ActiveKineticsSelectorSnapshot]:
    current = selector_store._snapshot_unlocked()
    if current.snapshot_sha256 == pending["base_selector_snapshot_sha256"]:
        return "base", current
    if current.current_hash == pending["target_selector_sha256"]:
        target = ActiveKineticsModelSpecSelection.from_dict(pending["target_selector"])
        if current.selection is not None and current.selection.to_dict() == target.to_dict():
            return "target", current
    return "neither", current


def _recover_pending_locked(
    boundary: _AuthoringBoundary,
    model_store: Any,
    selector_store: ActiveKineticsModelSpecSelectorStore,
) -> str:
    """Recover one durable transaction; caller must hold the authoring lock."""

    pending = _read_pending(boundary)
    if pending is None:
        return "none"
    model_side, _head = _model_side(model_store, pending)
    selector_side, selector_snapshot = _selector_side(selector_store, pending)
    if model_side == "base" and selector_side == "base":
        _append_receipt(
            boundary,
            _receipt_from_pending(pending, outcome="abandoned_requires_repreview"),
        )
        _unlink_regular(
            boundary.pending_path,
            label="authoring pending journal",
            boundary=boundary,
        )
        return "discarded_requires_repreview"
    if model_side == "target" and selector_side == "base":
        target = ActiveKineticsModelSpecSelection.from_dict(pending["target_selector"])
        selector_store._advance_unlocked(target, selector_snapshot.snapshot_sha256)
        selector_side, _selector = _selector_side(selector_store, pending)
        if selector_side != "target":
            raise KineticsAuthoringRecoveryError("selector forward recovery did not commit exactly")
    if model_side == "target" and selector_side == "target":
        _append_receipt(boundary, _receipt_from_pending(pending))
        _unlink_regular(
            boundary.pending_path,
            label="authoring pending journal",
            boundary=boundary,
        )
        return "completed"
    raise KineticsAuthoringRecoveryError(
        "pending authoring transaction is neither at its exact base nor exact target"
    )


class _LiveModelStoreSourceAuthority:
    """Adapt each model-store callback to a fresh authoring authority read."""

    def __init__(
        self,
        coordinator: "KineticsAuthoringCoordinator",
        expected: AuthoringSourceSnapshot,
    ):
        self._coordinator = coordinator
        self._expected = expected

    def authoritative_kinetics_source_snapshot(self) -> AuthoritativeKineticsSourceSnapshot:
        current = self._coordinator._source_snapshot()
        if current.snapshot_sha256 != self._expected.snapshot_sha256:
            raise KineticsAuthoringError("authoritative authoring source changed")
        return AuthoritativeKineticsSourceSnapshot(
            project_id=current.project_id,
            domain_authority_id=current.domain_authority_id,
            domain_generation=current.domain_generation,
            network_revision=current.network_revision,
            source_projection_sha256=current.source_projection_sha256,
            network_id=current.network_id,
        )


class KineticsAuthoringCoordinator:
    """Preview, confirm and recover one fixed-file authoring transaction."""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        source_authority: AuthoringSourceAuthority | Any | None,
        evidence_resolver: EvidenceResolver | Callable[[str], bytes] | None = None,
        model_store: Any | None = None,
        selector_store: ActiveKineticsModelSpecSelectorStore | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ):
        self._boundary = _AuthoringBoundary(project_root)
        self._source_authority = source_authority
        self._evidence_resolver = evidence_resolver
        self._model_store = model_store or KineticsModelSpecStore(self._boundary.root)
        self._fault_injector = fault_injector
        self._selector_store = selector_store or ActiveKineticsModelSpecSelectorStore(
            self._boundary.root,
            _boundary=self._boundary,
            fault_injector=self._fault,
        )

    @property
    def selector_store(self) -> ActiveKineticsModelSpecSelectorStore:
        return self._selector_store

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _source_snapshot(self) -> AuthoringSourceSnapshot:
        loader = getattr(self._source_authority, "authoring_source_snapshot", None)
        if not callable(loader):
            raise KineticsAuthoringError("authoring source authority adapter is unavailable")
        try:
            snapshot = loader()
        except KineticsAuthoringError:
            raise
        except Exception as exc:  # noqa: BLE001 - trusted adapter still fails closed
            raise KineticsAuthoringError("authoring source authority is unavailable") from exc
        if not isinstance(snapshot, AuthoringSourceSnapshot):
            raise KineticsAuthoringError(
                "authoring source authority must return AuthoringSourceSnapshot"
            )
        return AuthoringSourceSnapshot.from_dict(snapshot.to_dict())

    def _store_snapshot(self) -> KineticsModelSpecStoreSnapshot:
        path = getattr(self._model_store, "path", None)
        if isinstance(path, Path) and not path.exists():
            raise KineticsAuthoringError(
                "model-spec store authority is unavailable; initialize it outside preview"
            )
        try:
            snapshot = self._model_store.snapshot()
        except Exception as exc:  # noqa: BLE001 - injected store boundary
            raise KineticsAuthoringError("model-spec store snapshot is unavailable") from exc
        if not isinstance(snapshot, KineticsModelSpecStoreSnapshot):
            raise KineticsAuthoringError("model-spec store returned an unsealed snapshot")
        return KineticsModelSpecStoreSnapshot(**snapshot.to_dict())

    def _read_model_axis(
        self, project_id: str, spec_id: str
    ) -> tuple[KineticsModelSpecStoreSnapshot, KineticsModelSpec | None]:
        before = self._store_snapshot()
        try:
            head = self._model_store.head(project_id, spec_id)
        except Exception as exc:  # noqa: BLE001 - injected store boundary
            raise KineticsAuthoringError("model-spec head is unavailable") from exc
        after = self._store_snapshot()
        if before.snapshot_sha256 != after.snapshot_sha256:
            raise KineticsAuthoringError("model-spec store changed during preview")
        if head is not None:
            head = KineticsModelSpec.from_dict(head.to_dict())
        return after, head

    @staticmethod
    def _raw_draft_hash(draft: Any) -> str:
        try:
            return _digest(draft)
        except KineticsAuthoringError:
            return _ZERO_SHA256

    def _preview_locked(
        self,
        draft_value: KineticsModelSpecDraft | Mapping[str, Any],
        intent_id: str,
    ) -> KineticsAuthoringPreview:
        issues: list[AuthoringIssue] = []
        try:
            intent_id = _identifier(intent_id, "preview.intent_id")
        except KineticsAuthoringError:
            intent_id = "invalid-intent"
            issues.append(
                AuthoringIssue(
                    "invalid_intent_id", "$intent_id", "intent_id must be a bounded opaque id"
                )
            )
        draft: KineticsModelSpecDraft | None = None
        try:
            draft = (
                draft_value
                if isinstance(draft_value, KineticsModelSpecDraft)
                else KineticsModelSpecDraft.from_dict(draft_value)
            )
            draft_sha = draft.semantic_sha256
        except KineticsAuthoringValidationError as exc:
            issues.extend(exc.issues)
            draft_sha = self._raw_draft_hash(draft_value)
        except Exception:
            issues.append(AuthoringIssue("invalid_draft", "$draft", "draft is not valid JSON"))
            draft_sha = _ZERO_SHA256

        source: AuthoringSourceSnapshot | None = None
        source_cas = None
        try:
            source = self._source_snapshot()
            source_cas = source.cas_dict()
        except KineticsAuthoringError as exc:
            issues.append(AuthoringIssue("source_unavailable", "$source", str(exc)))

        store_snapshot: KineticsModelSpecStoreSnapshot | None = None
        store_cas = None
        head = None
        if draft is not None and source is not None:
            try:
                store_snapshot, head = self._read_model_axis(source.project_id, draft.spec_id)
                store_cas = store_snapshot.to_dict()
            except KineticsAuthoringError as exc:
                issues.append(AuthoringIssue("model_store_unavailable", "$store", str(exc)))

        selector_snapshot: ActiveKineticsSelectorSnapshot | None = None
        selector_cas = None
        try:
            selector_snapshot = self._selector_store._snapshot_unlocked()
            selector_cas = selector_snapshot.to_dict()
            if not selector_snapshot.writable:
                issues.append(
                    AuthoringIssue(
                        "legacy_selector_read_only",
                        "$selector",
                        "v1 selector is readable for migration but cannot authorize a v2 write",
                    )
                )
        except KineticsAuthoringError as exc:
            issues.append(AuthoringIssue("selector_unavailable", "$selector", str(exc)))

        compiled = None
        target_spec = None
        if draft is not None and source is not None and store_snapshot is not None:
            try:
                compiled = KineticsModelSpecDraftCompiler(self._evidence_resolver).compile(
                    draft, source, head
                )
                target_spec = {
                    "project_id": compiled.project_id,
                    "spec_id": compiled.spec_id,
                    "revision": compiled.revision,
                    "spec_sha256": compiled.semantic_sha256,
                }
            except KineticsAuthoringValidationError as exc:
                issues.extend(exc.issues)
            except KineticsAuthoringError as exc:
                issues.append(AuthoringIssue("compiler_unavailable", "$draft", str(exc)))
        issues = issues[:MAX_ISSUES]
        material = {
            "schema": PREVIEW_SCHEMA,
            "intent_id": intent_id,
            "draft_sha256": draft_sha,
            "source_snapshot_sha256": (
                None if source is None else source.snapshot_sha256
            ),
            "store_snapshot_sha256": (
                None if store_snapshot is None else store_snapshot.snapshot_sha256
            ),
            "selector_snapshot_sha256": (
                None if selector_snapshot is None else selector_snapshot.snapshot_sha256
            ),
            "target_spec": target_spec,
            "issue_codes": [issue.code for issue in issues],
        }
        preview_sha = _digest(material)
        can_confirm = bool(
            compiled is not None
            and not issues
            and source is not None
            and store_snapshot is not None
            and selector_snapshot is not None
            and selector_snapshot.writable
        )
        solver_ready = bool(compiled is not None and source is not None and source.solver_ready)
        readiness_reasons = (
            tuple(source.solver_readiness_reasons)
            if compiled is not None and source is not None and not source.solver_ready
            else ()
        )
        return KineticsAuthoringPreview(
            intent_id=intent_id,
            draft_sha256=draft_sha,
            preview_sha256=preview_sha,
            source_cas=source_cas,
            store_cas=store_cas,
            selector_cas=selector_cas,
            target_spec=target_spec,
            issues=tuple(issues),
            can_confirm=can_confirm,
            solver_ready=solver_ready,
            solver_readiness_reasons=readiness_reasons,
            _compiled_spec=compiled,
        )

    def preview(
        self,
        draft: KineticsModelSpecDraft | Mapping[str, Any],
        *,
        intent_id: str,
    ) -> KineticsAuthoringPreview:
        """Compile and seal a preview without writing store, selector or journal."""

        with _authoring_lock(self._boundary):
            _recover_pending_locked(self._boundary, self._model_store, self._selector_store)
            return self._preview_locked(draft, intent_id)

    @staticmethod
    def confirmation_from_preview(
        preview: KineticsAuthoringPreview,
    ) -> KineticsAuthoringConfirmation:
        if not preview.can_confirm:
            raise KineticsAuthoringError("only a confirmable preview can be confirmed")
        assert preview.source_cas is not None
        assert preview.store_cas is not None
        assert preview.selector_cas is not None
        return KineticsAuthoringConfirmation(
            intent_id=preview.intent_id,
            draft_sha256=preview.draft_sha256,
            preview_sha256=preview.preview_sha256,
            source_snapshot_sha256=preview.source_cas["snapshot_sha256"],
            store_snapshot_sha256=preview.store_cas["snapshot_sha256"],
            selector_snapshot_sha256=preview.selector_cas["snapshot_sha256"],
            confirmed=True,
        )

    def _latest_conflict(self, reason: str) -> AdoptAndStopConflict:
        source_cas = None
        store_cas = None
        selector_cas = None
        try:
            source_cas = self._source_snapshot().cas_dict()
        except Exception:  # noqa: BLE001 - conflict details are best effort
            pass
        try:
            store_cas = self._store_snapshot().to_dict()
        except Exception:  # noqa: BLE001 - conflict details are best effort
            pass
        try:
            selector_cas = self._selector_store._snapshot_unlocked().to_dict()
        except Exception:  # noqa: BLE001 - conflict details are best effort
            pass
        return AdoptAndStopConflict(
            reason=reason,
            latest_source_cas=source_cas,
            latest_store_cas=store_cas,
            latest_selector_cas=selector_cas,
        )

    def _replay_result(
        self, receipt: Mapping[str, Any], request_sha256: str
    ) -> KineticsAuthoringConfirmResult:
        if receipt["request_sha256"] != request_sha256:
            return KineticsAuthoringConfirmResult(
                action="conflict", conflict=self._latest_conflict("intent_reused")
            )
        if receipt["outcome"] == "abandoned_requires_repreview":
            return KineticsAuthoringConfirmResult(
                action="needs_repreview",
                conflict=self._latest_conflict("pending_discarded"),
            )
        head = self._model_store.head(receipt["project_id"], receipt["spec_id"])
        selector = self._selector_store._snapshot_unlocked()
        if (
            head is None
            or head.revision != receipt["spec_revision"]
            or head.semantic_sha256 != receipt["spec_sha256"]
            or selector.selection is None
            or selector.current_hash != receipt["selector_sha256"]
        ):
            raise KineticsAuthoringRecoveryError(
                "durable receipt no longer matches the exact spec and selector heads"
            )
        return KineticsAuthoringConfirmResult(
            action="replayed",
            spec=head.to_dict(),
            selector=selector.selection.to_dict(),
            receipt=_detached(receipt),
        )

    def confirm(
        self,
        draft: KineticsModelSpecDraft | Mapping[str, Any],
        confirmation: KineticsAuthoringConfirmation | Mapping[str, Any],
    ) -> KineticsAuthoringConfirmResult:
        """Commit spec then selector, returning success only after exact re-read."""

        try:
            confirmation = (
                confirmation
                if isinstance(confirmation, KineticsAuthoringConfirmation)
                else KineticsAuthoringConfirmation.from_dict(confirmation)
            )
            parsed_draft = (
                draft
                if isinstance(draft, KineticsModelSpecDraft)
                else KineticsModelSpecDraft.from_dict(draft)
            )
        except KineticsAuthoringValidationError as exc:
            return KineticsAuthoringConfirmResult(action="unavailable", issues=exc.issues)
        except KineticsAuthoringError as exc:
            return KineticsAuthoringConfirmResult(
                action="unavailable",
                issues=(AuthoringIssue("invalid_confirmation", "$confirmation", str(exc)),),
            )
        request_sha = _digest(confirmation.to_dict())
        with _authoring_lock(self._boundary):
            recovery = _recover_pending_locked(
                self._boundary, self._model_store, self._selector_store
            )
            receipt = _receipt_for_intent(self._boundary, confirmation.intent_id)
            if receipt is not None:
                return self._replay_result(receipt, request_sha)
            if recovery == "discarded_requires_repreview":
                return KineticsAuthoringConfirmResult(
                    action="needs_repreview",
                    conflict=self._latest_conflict("pending_discarded"),
                )

            preview = self._preview_locked(parsed_draft, confirmation.intent_id)
            if not preview.can_confirm:
                current_seals = (
                    None if preview.source_cas is None else preview.source_cas["snapshot_sha256"],
                    None if preview.store_cas is None else preview.store_cas["snapshot_sha256"],
                    None
                    if preview.selector_cas is None
                    else preview.selector_cas["snapshot_sha256"],
                )
                expected_seals = (
                    confirmation.source_snapshot_sha256,
                    confirmation.store_snapshot_sha256,
                    confirmation.selector_snapshot_sha256,
                )
                if current_seals != expected_seals:
                    return KineticsAuthoringConfirmResult(
                        action="conflict", conflict=self._latest_conflict("stale_preview")
                    )
                return KineticsAuthoringConfirmResult(
                    action="unavailable", issues=tuple(preview.issues)
                )
            assert preview.source_cas is not None
            assert preview.store_cas is not None
            assert preview.selector_cas is not None
            echoed = {
                "draft_sha256": (preview.draft_sha256, confirmation.draft_sha256),
                "preview_sha256": (preview.preview_sha256, confirmation.preview_sha256),
                "source_snapshot_sha256": (
                    preview.source_cas["snapshot_sha256"],
                    confirmation.source_snapshot_sha256,
                ),
                "store_snapshot_sha256": (
                    preview.store_cas["snapshot_sha256"],
                    confirmation.store_snapshot_sha256,
                ),
                "selector_snapshot_sha256": (
                    preview.selector_cas["snapshot_sha256"],
                    confirmation.selector_snapshot_sha256,
                ),
            }
            stale = [name for name, values in echoed.items() if values[0] != values[1]]
            if stale:
                return KineticsAuthoringConfirmResult(
                    action="conflict", conflict=self._latest_conflict("stale_preview")
                )
            spec = preview._compiled_spec
            assert spec is not None
            source = self._source_snapshot()
            if source.snapshot_sha256 != confirmation.source_snapshot_sha256:
                return KineticsAuthoringConfirmResult(
                    action="conflict", conflict=self._latest_conflict("source_changed")
                )
            current_store, current_head = self._read_model_axis(source.project_id, spec.spec_id)
            selector_base = self._selector_store._snapshot_unlocked()
            if (
                current_store.snapshot_sha256 != confirmation.store_snapshot_sha256
                or selector_base.snapshot_sha256 != confirmation.selector_snapshot_sha256
            ):
                return KineticsAuthoringConfirmResult(
                    action="conflict", conflict=self._latest_conflict("stale_cas")
                )
            expected_base_head = None
            if spec.parent_revision is not None:
                expected_base_head = {
                    "project_id": source.project_id,
                    "spec_id": spec.spec_id,
                    "revision": spec.parent_revision,
                    "spec_sha256": spec.expected_current_hash,
                }
            if _head_record(source.project_id, spec.spec_id, current_head) != expected_base_head:
                return KineticsAuthoringConfirmResult(
                    action="conflict", conflict=self._latest_conflict("stale_spec_head")
                )
            transaction_id = f"txn-{_digest([request_sha, spec.semantic_sha256])[:48]}"
            if selector_base.revision == 0:
                selector_authority = secrets.token_hex(16)
                selector_revision = 1
                selector_parent = None
                selector_expected = None
            else:
                assert selector_base.authority_id is not None
                selector_authority = selector_base.authority_id
                selector_revision = selector_base.revision + 1
                selector_parent = selector_base.revision
                selector_expected = selector_base.current_hash
            target_selector = ActiveKineticsModelSpecSelection(
                authority_id=selector_authority,
                revision=selector_revision,
                parent_revision=selector_parent,
                expected_current_hash=selector_expected,
                project_id=spec.project_id,
                spec_id=spec.spec_id,
                spec_revision=spec.revision,
                spec_sha256=spec.semantic_sha256,
                intent_id=confirmation.intent_id,
                transaction_id=transaction_id,
                confirmed=True,
            )
            pending = _seal_pending(
                {
                    "schema": PENDING_SCHEMA,
                    "transaction_id": transaction_id,
                    "intent_id": confirmation.intent_id,
                    "draft_sha256": confirmation.draft_sha256,
                    "preview_sha256": confirmation.preview_sha256,
                    "request_sha256": request_sha,
                    "source_snapshot_sha256": confirmation.source_snapshot_sha256,
                    "base_store_snapshot_sha256": confirmation.store_snapshot_sha256,
                    "base_model_head": _head_record(spec.project_id, spec.spec_id, current_head),
                    "base_selector_snapshot_sha256": confirmation.selector_snapshot_sha256,
                    "target_spec": spec.to_dict(),
                    "target_spec_sha256": spec.semantic_sha256,
                    "target_selector": target_selector.to_dict(),
                    "target_selector_sha256": target_selector.selection_sha256,
                }
            )
            pending = _validated_pending(pending)
            _atomic_json(
                self._boundary.pending_path,
                pending,
                maximum=MAX_PENDING_BYTES,
                label="authoring pending journal",
                boundary=self._boundary,
            )
            self._fault("after_pending")

            try:
                self._model_store.put(
                    spec,
                    confirmed=True,
                    intent=parsed_draft.mode,
                    expected_domain_authority_id=source.domain_authority_id,
                    expected_source_projection_sha256=source.source_projection_sha256,
                    expected_domain_generation=source.domain_generation,
                    expected_store_snapshot_sha256=current_store.snapshot_sha256,
                    source_authority=_LiveModelStoreSourceAuthority(self, source),
                    expected_network_id=source.network_id,
                )
            except KineticsModelSpecConflict as exc:
                _recover_pending_locked(self._boundary, self._model_store, self._selector_store)
                return KineticsAuthoringConfirmResult(
                    action="conflict", conflict=self._latest_conflict(exc.reason)
                )
            except (KineticsModelSpecError, KineticsAuthoringError):
                _recover_pending_locked(self._boundary, self._model_store, self._selector_store)
                return KineticsAuthoringConfirmResult(
                    action="conflict", conflict=self._latest_conflict("source_or_store_changed")
                )
            self._fault("after_spec")
            committed_spec = self._model_store.head(spec.project_id, spec.spec_id)
            if (
                committed_spec is None
                or committed_spec.revision != spec.revision
                or committed_spec.semantic_sha256 != spec.semantic_sha256
            ):
                raise KineticsAuthoringRecoveryError(
                    "model store commit does not match the exact pending target"
                )
            self._selector_store._advance_unlocked(
                target_selector, selector_base.snapshot_sha256
            )
            self._fault("after_selector")
            committed_selector = self._selector_store._snapshot_unlocked()
            committed_spec = self._model_store.head(spec.project_id, spec.spec_id)
            if (
                committed_spec is None
                or committed_spec.revision != spec.revision
                or committed_spec.semantic_sha256 != spec.semantic_sha256
                or committed_selector.selection is None
                or committed_selector.current_hash != target_selector.selection_sha256
                or committed_selector.selection.to_dict() != target_selector.to_dict()
            ):
                raise KineticsAuthoringRecoveryError(
                    "authoring commit could not be revalidated exactly"
                )
            receipt = _append_receipt(self._boundary, _receipt_from_pending(pending))
            self._fault("after_receipt")
            _unlink_regular(
                self._boundary.pending_path,
                label="authoring pending journal",
                boundary=self._boundary,
            )
            return KineticsAuthoringConfirmResult(
                action="committed",
                spec=committed_spec.to_dict(),
                selector=committed_selector.selection.to_dict(),
                receipt=receipt,
            )

    def recover(self) -> str:
        """Run crash recovery under the fixed cross-process authoring lock."""

        with _authoring_lock(self._boundary):
            return _recover_pending_locked(
                self._boundary, self._model_store, self._selector_store
            )

    def selector_snapshot(self) -> ActiveKineticsSelectorSnapshot:
        """Read selector only after crash recovery under the same authoring lock."""

        with _authoring_lock(self._boundary):
            _recover_pending_locked(self._boundary, self._model_store, self._selector_store)
            return self._selector_store._snapshot_unlocked()


# Compact names for API adapters while preserving explicit implementation names.
KineticsAuthoringService = KineticsAuthoringCoordinator
ActiveKineticsSpecSelectorStore = ActiveKineticsModelSpecSelectorStore
ActiveKineticsSpecSelection = ActiveKineticsModelSpecSelection


__all__ = [
    "ACTIVE_SELECTOR_FILENAME",
    "AUTHORING_DIRECTORY",
    "AUTHORING_LOCK_FILENAME",
    "AUTHORING_PENDING_FILENAME",
    "AUTHORING_RECEIPT_FILENAME",
    "SELECTOR_ANCHOR_FILENAME",
    "SELECTOR_ANCHOR_RECORD_SCHEMA",
    "ActiveKineticsModelSpecSelection",
    "ActiveKineticsModelSpecSelectorStore",
    "ActiveKineticsSelectorSnapshot",
    "ActiveKineticsSpecSelection",
    "ActiveKineticsSpecSelectorStore",
    "AdoptAndStopConflict",
    "AuthoringIssue",
    "AuthoringSourceAuthority",
    "AuthoringSourceSnapshot",
    "CONFIRMATION_SCHEMA",
    "CONFIRM_RESULT_SCHEMA",
    "CONFLICT_SCHEMA",
    "DRAFT_SCHEMA",
    "EvidenceResolver",
    "KineticsAuthoringConfirmation",
    "KineticsAuthoringConfirmResult",
    "KineticsAuthoringCoordinator",
    "KineticsAuthoringError",
    "KineticsAuthoringInjectedFailure",
    "KineticsAuthoringPreview",
    "KineticsAuthoringRecoveryError",
    "KineticsAuthoringService",
    "KineticsAuthoringValidationError",
    "KineticsModelSpecDraft",
    "KineticsModelSpecDraftCompiler",
    "LEGACY_SELECTOR_SCHEMA",
    "PENDING_SCHEMA",
    "PREVIEW_SCHEMA",
    "RECEIPT_LEDGER_SCHEMA",
    "RECEIPT_SCHEMA",
    "SELECTOR_SCHEMA",
    "SELECTOR_SNAPSHOT_SCHEMA",
    "SOURCE_CAS_SCHEMA",
    "SOURCE_SNAPSHOT_SCHEMA",
]
