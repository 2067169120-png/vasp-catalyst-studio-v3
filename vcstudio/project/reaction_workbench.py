"""Evidence-bound reaction map, thermochemistry ledger and condition explorer.

This module is deliberately downstream of the canonical catalysis domain
model.  It consumes a narrow, path-free ``Mapping`` projection (or a provider
implementing :class:`ReactionDomainSource`) and never persists a second copy of
surfaces, adsorbate states, elementary steps, conditions, or evidence.

The public schemas are stable inputs for the Web Analyze workbench, report
binding, and later microkinetics consumers.  Missing evidence remains missing;
rendering and derived-revision creation never raise scientific qualification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol, TypedDict, runtime_checkable


PROJECTION_SCHEMA = "vcstudio.reaction-domain-projection/v1"
VIEW_SCHEMA = "vcstudio.reaction-workbench-view/v1"
GRAPH_SCHEMA = "vcstudio.reaction-graph/v1"
LEDGER_SCHEMA = "vcstudio.thermochemistry-ledger/v1"
DERIVED_REVISION_SCHEMA = "vcstudio.condition-derived-revision/v1"
REPORT_BINDING_SCHEMA = "vcstudio.reaction-report-binding/v1"
FROZEN_NETWORK_SCHEMA = "vcstudio.frozen-reaction-network/v1"
DOMAIN_ENVELOPE_SCHEMA = "vcstudio.catalysis-domain-envelope/v1"
THERMOCHEMISTRY_BINDING_SCHEMA = "vcstudio.thermochemistry-binding/v1"
THERMOCHEMISTRY_TERM_SCHEMA = "vcstudio.thermochemistry-term/v1"
STANDARD_STATE_SCHEMA = "vcstudio.standard-state/v1"
LOW_FREQUENCY_SCHEMA = "vcstudio.low-frequency-treatment/v1"
FREQUENCY_EVIDENCE_SCHEMA = "vcstudio.frequency-evidence/v1"
EDGE_EVIDENCE_SCHEMA = "vcstudio.edge-evidence/v1"
NEB_EVIDENCE_SCHEMA = "vcstudio.neb-evidence/v1"
ENERGY_OBSERVATION_SCHEMA = "vcstudio.energy-observation/v1"

_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]{0,159}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PATH_RE = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/]|(?:^|[\s\"'])(?:\\\\|//|/[^/\s]|~[\\/])|\bfile:)")
_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{8,}"
    r"|\bBearer\s+\S+|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+)")

SCIENTIFIC_STATUSES = (
    "unknown", "unavailable", "blocked", "candidate", "machine_pass",
    "human_review", "verified", "release",
)
THERMOCHEMISTRY_MODELS = frozenset({
    "electronic_energy", "ideal_gas", "rigid_rotor", "harmonic",
    "hindered_translator", "hindered_rotor", "quasi_harmonic", "explicit",
})
LOW_FREQUENCY_RULES = frozenset({
    "none", "floor_to_cutoff", "quasi_harmonic", "hindered_translator",
    "hindered_rotor", "excluded",
})
MODEL_ROLES = frozenset({
    "electronic", "translation", "rotation", "vibration", "low_frequency",
    "standard_state", "solvation", "coverage",
})
CONDITION_RANGES = {
    "temperature_k": (1.0, 5000.0),
    "pressure_pa": (1e-12, 1e12),
    "ph": (-5.0, 20.0),
    "electrode_potential_v": (-20.0, 20.0),
    "coverage": (0.0, 1.0),
}
_TERM_DEFINITIONS = (
    ("electronic_energy_e0_eV", "E0"),
    ("zpe_eV", "ZPE"),
    ("delta_h_thermal_eV", "ΔH"),
    ("minus_t_delta_s_eV", "-TΔS"),
    ("standard_state_correction_eV", "standard-state correction"),
)
_TERM_MODELS = {
    "electronic_energy_e0_eV": frozenset({"electronic_energy"}),
    "zpe_eV": frozenset({"harmonic", "quasi_harmonic", "explicit"}),
    "delta_h_thermal_eV": frozenset({
        "ideal_gas", "harmonic", "hindered_translator", "hindered_rotor", "explicit",
    }),
    "minus_t_delta_s_eV": frozenset({
        "ideal_gas", "harmonic", "hindered_translator", "hindered_rotor",
        "quasi_harmonic", "explicit",
    }),
    "standard_state_correction_eV": frozenset({"ideal_gas", "explicit"}),
}
_STANDARD_STATES = {
    "1-bar": (100000.0, "Pa"),
    "1-atm": (101325.0, "Pa"),
    "1-molar": (1.0, "mol/L"),
    "surface-site": (1.0, "site_fraction"),
}
_ORIGIN_STATUS_CEILINGS = {
    "observed": "release",
    "imported": "machine_pass",
    "derived": "machine_pass",
    "inferred": "candidate",
    "unknown": "unknown",
}
_PAYLOAD_SCHEMAS = {
    "CatalystSurface": "vcstudio.catalyst-surface/v1",
    "AdsorbateState": "vcstudio.adsorbate-state/v1",
    "TransitionState": "vcstudio.transition-state/v1",
    "ElementaryStep": "vcstudio.elementary-step/v1",
    "ConditionSet": "vcstudio.condition-set/v1",
    "ReactionNetwork": "vcstudio.reaction-network/v1",
}
_RAW_PAYLOAD_FIELDS = {
    "CatalystSurface": frozenset({
        "schema", "model_version", "surface_id", "composition", "miller_indices",
        "termination_id", "geometric_site_ids", "provenance", "evidence_refs",
        "method_fingerprint",
    }),
    "AdsorbateState": frozenset({
        "schema", "model_version", "state_id", "surface_id", "adsorbate_id",
        "chemical_formula", "elemental_composition", "geometric_site_id",
        "site_occupancy", "charge", "multiplicity", "provenance", "evidence_refs",
        "method_fingerprint",
    }),
    "TransitionState": frozenset({
        "schema", "model_version", "transition_state_id", "surface_id",
        "chemical_formula", "elemental_composition", "geometric_site_id",
        "site_occupancy", "charge", "multiplicity", "provenance", "evidence_refs",
        "method_fingerprint",
    }),
    "ElementaryStep": frozenset({
        "schema", "model_version", "step_id", "reactants", "products",
        "transition_state_id", "condition_set_id", "reversible", "provenance",
        "evidence_refs", "method_fingerprint",
    }),
    "ConditionSet": frozenset({
        "schema", "model_version", "condition_set_id", "temperature_k", "pressure_pa",
        "ph", "electrode_potential_v", "coverage", "provenance", "evidence_refs",
        "method_fingerprint",
    }),
    "ReactionNetwork": frozenset({
        "schema", "model_version", "network_id", "surface_ids", "state_ids",
        "step_ids", "condition_set_ids", "provenance", "evidence_refs",
        "method_fingerprint",
    }),
}


class EvidenceProjection(TypedDict, total=False):
    opaque_id: str
    sha256: str
    ref_type: str
    revision_id: str | None
    origin: str


class RationalDTO(TypedDict):
    numerator: int
    denominator: int


class ReactionParticipant(TypedDict):
    state_id: str
    coefficient: RationalDTO


class ThermochemistryBinding(TypedDict, total=False):
    schema: str
    origin: str
    electronic_energy_e0_eV: Mapping[str, Any]
    zpe_eV: Mapping[str, Any]
    delta_h_thermal_eV: Mapping[str, Any]
    minus_t_delta_s_eV: Mapping[str, Any]
    standard_state_correction_eV: Mapping[str, Any]
    temperature_k: float
    pressure_pa: float
    models: Mapping[str, str]
    standard_state: Mapping[str, Any]
    reference_state_sha256: str
    condition_set_id: str
    condition_set_sha256: str
    ph: float
    electrode_potential_v: float
    coverage: float
    solvent_model_sha256: str | None
    coverage_model_sha256: str | None
    low_frequency: Mapping[str, Any]
    frequency_evidence: Mapping[str, Any]
    condition_response: Mapping[str, Any]


class ObjectBinding(TypedDict, total=False):
    label: str
    structure_sha256: str
    evidence_sha256: str
    scientific_status: str
    origin: str
    thermochemistry: ThermochemistryBinding
    edge_evidence: Mapping[str, Any]


class ReactionDomainProjection(TypedDict, total=False):
    schema: str
    project_id: str
    network: Mapping[str, Any]
    surfaces: Sequence[Mapping[str, Any]]
    states: Sequence[Mapping[str, Any]]
    transition_states: Sequence[Mapping[str, Any]]
    steps: Sequence[Mapping[str, Any]]
    conditions: Sequence[Mapping[str, Any]]
    bindings: Mapping[str, ObjectBinding]
    applicability: Mapping[str, Any]


@runtime_checkable
class ReactionDomainSource(Protocol):
    """Narrow adapter owned by the canonical catalysis-model integration."""

    def load_reaction_projection(
        self, *, project_id: str, project: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        """Return one immutable, path-free projection or ``None``."""


class ReactionWorkbenchError(ValueError):
    """A canonical projection or derived-condition request is malformed."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReactionWorkbenchError("reaction projection is not canonical JSON") from exc


def semantic_sha256(value: Any) -> str:
    """Return a deterministic digest for one path-free public value."""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise ReactionWorkbenchError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReactionWorkbenchError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ReactionWorkbenchError(f"{field} must be a finite number")
    return number


def _optional_finite(value: Any, *, field: str) -> float | None:
    return None if value is None else _finite(value, field=field)


def _opaque(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _OPAQUE_ID_RE.fullmatch(text) or text in {".", ".."}:
        raise ReactionWorkbenchError(f"{field} must be an opaque identifier")
    return text


def _digest(value: Any, *, field: str, optional: bool = False) -> str | None:
    if optional and value in (None, ""):
        return None
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ReactionWorkbenchError(f"{field} must be a SHA-256 digest")
    return text


def _safe_text(value: Any, *, field: str, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) > limit or _PATH_RE.search(text) or _SECRET_RE.search(text):
        raise ReactionWorkbenchError(f"{field} contains a path, secret, or oversized text")
    return text


def _sequence(value: Any, *, field: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ReactionWorkbenchError(f"{field} must be an array")
    return list(value)


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReactionWorkbenchError(f"{field} must be an object")
    return copy.deepcopy(dict(value))


def _strict_mapping(
    value: Any, *, field: str, allowed: set[str] | frozenset[str],
    required: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    data = _mapping(value, field=field)
    unknown = set(data) - set(allowed)
    if unknown:
        raise ReactionWorkbenchError(
            f"{field} contains unknown fields: {', '.join(sorted(unknown))}")
    missing = {key for key in required if data.get(key) is None}
    if missing:
        raise ReactionWorkbenchError(
            f"{field} is missing required fields: {', '.join(sorted(missing))}")
    return data


def _origin(value: Any, *, field: str, optional: bool = False) -> str:
    if optional and value in (None, ""):
        return "unknown"
    text = str(value or "").strip().lower()
    if text not in _ORIGIN_STATUS_CEILINGS:
        raise ReactionWorkbenchError(f"{field} has an unsupported provenance/origin")
    return text


def _origin_ceiling(value: Any, *, field: str, optional: bool = False) -> str:
    return _ORIGIN_STATUS_CEILINGS[_origin(value, field=field, optional=optional)]


def _rational(value: Any, *, field: str, positive: bool = True) -> Fraction:
    item = _strict_mapping(
        value, field=field, allowed=frozenset({"numerator", "denominator"}),
        required=frozenset({"numerator", "denominator"}))
    numerator = item["numerator"]
    denominator = item["denominator"]
    if (isinstance(numerator, bool) or not isinstance(numerator, int)
            or isinstance(denominator, bool) or not isinstance(denominator, int)):
        raise ReactionWorkbenchError(
            f"{field} numerator and denominator must be integers")
    if denominator <= 0 or (positive and numerator <= 0) or (not positive and numerator == 0):
        qualifier = "positive" if positive else "non-zero"
        raise ReactionWorkbenchError(f"{field} must be a {qualifier} rational")
    return Fraction(numerator, denominator)


def _rational_dto(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _coefficient_display(value: Mapping[str, Any]) -> str:
    coefficient = _fraction_from_dto(value, field="coefficient")
    return (str(coefficient.numerator) if coefficient.denominator == 1
            else f"{coefficient.numerator}/{coefficient.denominator}")


def _payload_type_from_schema(schema: Any, *, field: str) -> str:
    text = _safe_text(schema, field=f"{field}.schema")
    matches = [kind for kind, expected in _PAYLOAD_SCHEMAS.items() if text == expected]
    if len(matches) != 1:
        raise ReactionWorkbenchError(f"{field}.schema is not a supported domain payload")
    return matches[0]


def _validate_payload_schema(
    payload: Mapping[str, Any], *, object_type: str, field: str, strict: bool,
) -> None:
    expected = _PAYLOAD_SCHEMAS.get(object_type)
    if expected is None or payload.get("schema") != expected:
        raise ReactionWorkbenchError(
            f"{field}.schema must match canonical {object_type}")
    if strict:
        unknown = set(payload) - set(_RAW_PAYLOAD_FIELDS[object_type])
        if unknown:
            raise ReactionWorkbenchError(
                f"{field} raw payload contains unknown fields: "
                + ", ".join(sorted(unknown)))


def _unwrap(
    value: Any, *, field: str,
) -> tuple[dict[str, Any], str, str, bool]:
    """Validate an envelope, or recompute a compatibility payload digest.

    Raw payloads remain renderable for migration, but callers receive
    ``canonical=False`` and must not authorize formal frozen/report outputs.
    A raw ``semantic_sha256`` claim is deliberately discarded.
    """
    record = _mapping(value, field=field)
    if isinstance(record.get("payload"), Mapping):
        record = _strict_mapping(
            record, field=field,
            allowed=frozenset({
                "schema", "object_type", "object_id", "object_version", "payload",
                "semantic_sha256", "job_source_of_truth", "authorizes_execution",
            }),
            required=frozenset({
                "schema", "object_type", "object_id", "object_version", "payload",
                "semantic_sha256", "job_source_of_truth", "authorizes_execution",
            }))
        if record.get("schema") != DOMAIN_ENVELOPE_SCHEMA:
            raise ReactionWorkbenchError(f"{field} envelope schema is unsupported")
        if record.get("job_source_of_truth") != "job.yaml":
            raise ReactionWorkbenchError(f"{field} envelope must bind job.yaml authority")
        if record.get("authorizes_execution") is not False:
            raise ReactionWorkbenchError(f"{field} envelope must not authorize execution")
        _opaque(record.get("object_id"), field=f"{field}.object_id")
        _safe_text(record.get("object_version"), field=f"{field}.object_version")
        payload = _mapping(record["payload"], field=f"{field}.payload")
        digest = _digest(
            record.get("semantic_sha256"), field=f"{field}.semantic_sha256")
        if semantic_sha256(payload) != digest:
            raise ReactionWorkbenchError(f"{field} semantic hash does not match its payload")
        object_type = _safe_text(record.get("object_type"), field=f"{field}.object_type")
        _validate_payload_schema(
            payload, object_type=object_type, field=f"{field}.payload", strict=False)
        return payload, str(digest), object_type, True
    record.pop("semantic_sha256", None)
    object_type = _payload_type_from_schema(record.get("schema"), field=field)
    _validate_payload_schema(record, object_type=object_type, field=field, strict=True)
    return record, semantic_sha256(record), object_type, False


def _method_sha256(payload: Mapping[str, Any], *, field: str) -> str | None:
    method = payload.get("method_fingerprint")
    if not isinstance(method, Mapping):
        return None
    return _digest(method.get("sha256"), field=f"{field}.method_fingerprint.sha256")


def _binding_for(bindings: Mapping[str, Any], object_id: str) -> dict[str, Any]:
    value = bindings.get(object_id) or {}
    return _strict_mapping(
        value, field=f"bindings.{object_id}",
        allowed=frozenset({
            "label", "structure_sha256", "evidence_sha256", "scientific_status",
            "origin", "thermochemistry", "edge_evidence",
        }))


def _status(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    return text if text in SCIENTIFIC_STATUSES else "unknown"


def _combined_status(values: Sequence[str]) -> str:
    """Return the weakest declared status; never infer a higher status."""
    statuses = [_status(item) for item in values]
    if not statuses:
        return "unknown"
    ranks = {name: index for index, name in enumerate(SCIENTIFIC_STATUSES)}
    return min(statuses, key=lambda item: ranks[item])


def _source_status(
    payload: Mapping[str, Any], binding: Mapping[str, Any], *, field: str,
) -> str:
    statuses = [
        _status(binding.get("scientific_status")),
        _origin_ceiling(
            payload.get("provenance"), field=f"{field}.provenance", optional=True),
        _origin_ceiling(
            binding.get("origin"), field=f"bindings.{field}.origin", optional=True),
    ]
    refs = payload.get("evidence_refs") or []
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
        statuses.extend(
            _origin_ceiling(
                item.get("origin") if isinstance(item, Mapping) else None,
                field=f"{field}.evidence_refs[{index}].origin", optional=True)
            for index, item in enumerate(refs))
    else:
        statuses.append("unknown")
    return _combined_status(statuses)


def _source_origins_observed(
    payload: Mapping[str, Any], binding: Mapping[str, Any], *, field: str,
) -> bool:
    refs = payload.get("evidence_refs")
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence) or not refs:
        return False
    return bool(
        _origin(payload.get("provenance"), field=f"{field}.provenance", optional=True)
        == "observed"
        and _origin(
            binding.get("origin"), field=f"bindings.{field}.origin", optional=True)
        == "observed"
        and all(
            isinstance(item, Mapping)
            and _origin(
                item.get("origin"), field=f"{field}.evidence_refs[{index}].origin",
                optional=True) == "observed"
            for index, item in enumerate(refs)))


def _bounded_condition(value: Any, *, key: str, optional: bool = False) -> float | None:
    if optional and value is None:
        return None
    number = _finite(value, field=key)
    lower, upper = CONDITION_RANGES[key.rsplit(".", 1)[-1]]
    if not lower <= number <= upper:
        raise ReactionWorkbenchError(
            f"{key} must be between {lower:g} and {upper:g}")
    return number


def _display(value: float | None, precision: int) -> str:
    return "unavailable" if value is None else f"{value:.{precision}f}"


def _hash_evidence_refs(value: Any) -> str | None:
    """Hash evidence only when every reference carries a content digest."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        return None
    refs = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            return None
        digest = _digest(
            raw.get("sha256"), field=f"evidence_refs[{index}].sha256", optional=True)
        if digest is None:
            return None
        refs.append({
            "opaque_id": _opaque(raw.get("opaque_id"), field="evidence_refs.opaque_id"),
            "sha256": digest,
            "revision_id": raw.get("revision_id"),
        })
    return semantic_sha256(refs)


def _projection_record(
    raw: Mapping[str, Any], *, identity_field: str, field: str,
) -> tuple[str, dict[str, Any], str, str, bool]:
    payload, semantic_hash, object_type, canonical = _unwrap(raw, field=field)
    object_id = _opaque(payload.get(identity_field), field=f"{field}.{identity_field}")
    envelope_id = raw.get("object_id") if isinstance(raw, Mapping) else None
    if envelope_id is not None and _opaque(
            envelope_id, field=f"{field}.object_id") != object_id:
        raise ReactionWorkbenchError(f"{field} envelope identity mismatch")
    return object_id, payload, semantic_hash, object_type, canonical


def normalize_conditions(value: Mapping[str, Any] | None) -> dict[str, float]:
    """Normalize the five supported explorer parameters without defaults."""
    if value is None:
        return {}
    data = _mapping(value, field="conditions")
    unknown = set(data) - set(CONDITION_RANGES)
    if unknown:
        raise ReactionWorkbenchError(
            "conditions contains unsupported parameters: " + ", ".join(sorted(unknown)))
    result = {}
    for key, raw in data.items():
        if raw in (None, ""):
            continue
        number = _finite(raw, field=f"conditions.{key}")
        lower, upper = CONDITION_RANGES[key]
        if not lower <= number <= upper:
            raise ReactionWorkbenchError(
                f"conditions.{key} must be between {lower:g} and {upper:g}")
        result[key] = number
    return dict(sorted(result.items()))


def _normalise_applicability(value: Any) -> dict[str, dict[str, Any]]:
    if value in (None, {}):
        return {}
    data = _mapping(value, field="applicability")
    result = {}
    for key, raw in data.items():
        if key not in CONDITION_RANGES:
            raise ReactionWorkbenchError(f"applicability.{key} is unsupported")
        item = _strict_mapping(
            raw, field=f"applicability.{key}",
            allowed=frozenset({"minimum", "maximum", "evidence_sha256"}),
            required=frozenset({"evidence_sha256"}))
        lower = _optional_finite(item.get("minimum"), field=f"applicability.{key}.minimum")
        upper = _optional_finite(item.get("maximum"), field=f"applicability.{key}.maximum")
        if lower is not None and upper is not None and lower > upper:
            raise ReactionWorkbenchError(f"applicability.{key} minimum exceeds maximum")
        evidence_hash = _digest(
            item.get("evidence_sha256"),
            field=f"applicability.{key}.evidence_sha256")
        result[key] = {
            "minimum": lower, "maximum": upper,
            "evidence_sha256": evidence_hash,
        }
    return result


def _normalise_low_frequency(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {
            "status": "not_applied", "original_frequencies_cm1": [],
            "original_frequencies_display": "unavailable",
            "rule": "none", "cutoff_cm1": None, "reason": "",
            "evidence_sha256": None, "origin": "unknown",
            "scientific_status": "unknown", "sensitivity": [],
        }
    data = _strict_mapping(
        value, field="low_frequency",
        allowed=frozenset({
            "schema", "original_frequencies_cm1", "rule", "cutoff_cm1", "reason",
            "evidence_sha256", "origin", "sensitivity",
        }), required=frozenset({"schema", "rule", "origin"}))
    if data.get("schema") != LOW_FREQUENCY_SCHEMA:
        raise ReactionWorkbenchError("low_frequency.schema is unsupported")
    raw_frequencies = _sequence(
        data.get("original_frequencies_cm1") or [],
        field="low_frequency.original_frequencies_cm1",
    )
    frequencies = [
        _finite(item, field="low_frequency.original_frequencies_cm1[]")
        for item in raw_frequencies
    ]
    rule = str(data.get("rule") or "none").strip()
    if rule not in LOW_FREQUENCY_RULES:
        raise ReactionWorkbenchError("low_frequency.rule is unsupported")
    cutoff = _optional_finite(data.get("cutoff_cm1"), field="low_frequency.cutoff_cm1")
    reason = _safe_text(data.get("reason"), field="low_frequency.reason", limit=1000)
    if rule != "none" and (not frequencies or not reason):
        raise ReactionWorkbenchError(
            "a low-frequency treatment requires original frequencies and a reason")
    if rule in {"floor_to_cutoff", "quasi_harmonic"} and (
            cutoff is None or cutoff <= 0):
        raise ReactionWorkbenchError(
            "this low-frequency rule requires a positive cutoff_cm1")
    treatment_hash = _digest(
        data.get("evidence_sha256"), field="low_frequency.evidence_sha256",
        optional=rule == "none")
    origin = _origin(data.get("origin"), field="low_frequency.origin")
    sensitivity = []
    for index, raw in enumerate(_sequence(
            data.get("sensitivity") or [], field="low_frequency.sensitivity")):
        item = _strict_mapping(
            raw, field=f"low_frequency.sensitivity[{index}]",
            allowed=frozenset({
                "parameter", "value", "unit", "delta_g_eV", "evidence_sha256",
                "origin",
            }), required=frozenset({
                "parameter", "value", "unit", "delta_g_eV", "evidence_sha256",
                "origin",
            }))
        sensitivity.append({
            "parameter": _safe_text(
                item.get("parameter"), field="low_frequency.sensitivity.parameter"),
            "value": _finite(item.get("value"), field="low_frequency.sensitivity.value"),
            "unit": _safe_text(
                item.get("unit"), field="low_frequency.sensitivity.unit"),
            "delta_g_eV": _finite(
                item.get("delta_g_eV"), field="low_frequency.sensitivity.delta_g_eV"),
            "evidence_sha256": _digest(
                item.get("evidence_sha256"),
                field="low_frequency.sensitivity.evidence_sha256"),
            "origin": _origin(
                item.get("origin"), field="low_frequency.sensitivity.origin"),
        })
    return {
        "status": "applied" if rule != "none" else "not_applied",
        "original_frequencies_cm1": frequencies,
        "original_frequencies_display": (
            ", ".join(f"{item:g}" for item in frequencies) + " cm-1"
            if frequencies else "unavailable"),
        "rule": rule, "cutoff_cm1": cutoff, "reason": reason,
        "evidence_sha256": treatment_hash,
        "origin": origin,
        "scientific_status": _combined_status([
            _ORIGIN_STATUS_CEILINGS[origin],
            *(_ORIGIN_STATUS_CEILINGS[item["origin"]] for item in sensitivity),
        ]),
        "sensitivity": sensitivity,
    }


def _frequency_qualification(
    value: Any, *, is_ts: bool, node: Mapping[str, Any],
) -> dict[str, Any]:
    if value in (None, {}):
        return {
            "status": "unavailable", "context": "ts" if is_ts else "minimum",
            "original_imaginary_frequencies_cm1": [],
            "imaginary_magnitudes_cm1": [], "imaginary_frequencies_cm1": [],
            "sign_convention": "unavailable", "noise_threshold_cm1": None,
            "frequency_evidence_sha256": None, "mode_evidence_sha256": None,
            "method_sha256": None, "structure_sha256": None, "origin": "unknown",
            "scientific_status": "unknown", "binding_status": "unavailable",
            "mode_alignment_status": "unavailable",
            "kinetic_qualification": "unavailable", "minimum_qualification": "unavailable",
            "reason": "frequency and mode evidence are unavailable",
            "display": "unavailable",
        }
    data = _strict_mapping(
        value, field="frequency_evidence",
        allowed=frozenset({
            "schema", "context", "sign_convention", "imaginary_frequencies_cm1",
            "imaginary_frequency_magnitudes_cm1", "noise_threshold_cm1",
            "evidence_sha256", "mode_evidence_sha256", "mode_alignment_status",
            "method_sha256", "structure_sha256", "origin",
        }), required=frozenset({
            "schema", "context", "sign_convention", "noise_threshold_cm1",
            "evidence_sha256", "method_sha256", "structure_sha256", "origin",
        }))
    if data.get("schema") != FREQUENCY_EVIDENCE_SCHEMA:
        raise ReactionWorkbenchError("frequency_evidence.schema is unsupported")
    context = str(data.get("context") or ("ts" if is_ts else "minimum")).strip()
    if context not in {"ts", "minimum"}:
        raise ReactionWorkbenchError("frequency_evidence.context is unsupported")
    sign_convention = str(data.get("sign_convention") or "").strip()
    if sign_convention == "signed_negative":
        if data.get("imaginary_frequency_magnitudes_cm1") not in (None, []):
            raise ReactionWorkbenchError(
                "signed frequency evidence cannot also provide positive magnitudes")
        original_frequencies = [
            _finite(item, field="frequency_evidence.imaginary_frequencies_cm1[]")
            for item in _sequence(
                data.get("imaginary_frequencies_cm1") or [],
                field="frequency_evidence.imaginary_frequencies_cm1")]
        if any(item >= 0 for item in original_frequencies):
            raise ReactionWorkbenchError(
                "signed imaginary frequencies must be strictly negative")
        magnitudes = [abs(item) for item in original_frequencies]
    elif sign_convention == "positive_magnitude":
        if data.get("imaginary_frequencies_cm1") not in (None, []):
            raise ReactionWorkbenchError(
                "positive magnitude evidence must use its independent magnitude field")
        magnitudes = [
            _finite(item, field="frequency_evidence.imaginary_frequency_magnitudes_cm1[]")
            for item in _sequence(
                data.get("imaginary_frequency_magnitudes_cm1") or [],
                field="frequency_evidence.imaginary_frequency_magnitudes_cm1")]
        if any(item <= 0 for item in magnitudes):
            raise ReactionWorkbenchError(
                "imaginary frequency magnitudes must be strictly positive")
        original_frequencies = []
    else:
        raise ReactionWorkbenchError("frequency_evidence.sign_convention is unsupported")
    threshold = _finite(
        data.get("noise_threshold_cm1"), field="frequency_evidence.noise_threshold_cm1")
    if threshold <= 0:
        raise ReactionWorkbenchError("frequency_evidence.noise_threshold_cm1 must be positive")
    frequency_hash = _digest(
        data.get("evidence_sha256"), field="frequency_evidence.evidence_sha256",
        optional=True)
    mode_hash = _digest(
        data.get("mode_evidence_sha256"),
        field="frequency_evidence.mode_evidence_sha256", optional=True)
    alignment = str(data.get("mode_alignment_status") or "unavailable").strip()
    if alignment not in {"confirmed", "rejected", "unavailable"}:
        raise ReactionWorkbenchError("frequency_evidence.mode_alignment_status is unsupported")
    method_hash = _digest(
        data.get("method_sha256"), field="frequency_evidence.method_sha256",
        optional=True)
    structure_hash = _digest(
        data.get("structure_sha256"), field="frequency_evidence.structure_sha256",
        optional=True)
    origin = _origin(data.get("origin"), field="frequency_evidence.origin")
    binding_ok = bool(
        frequency_hash and method_hash == node.get("method_sha256")
        and structure_hash == node.get("structure_sha256"))
    large = [item for item in magnitudes if item >= threshold]
    qualified = bool(
        is_ts and context == "ts" and len(large) == 1 and frequency_hash
        and mode_hash and alignment == "confirmed" and binding_ok
        and origin == "observed")
    minimum_supported = bool(
        not is_ts and context == "minimum" and not large and frequency_hash
        and binding_ok)
    if qualified:
        reason = "one significant imaginary mode is frequency- and mode-evidence bound"
    elif is_ts:
        reason = (
            "TS qualification requires exactly one significant imaginary frequency, "
            "a frequency hash, and confirmed mode evidence")
    elif minimum_supported:
        reason = "minimum has no significant imaginary frequency in bound evidence"
    else:
        reason = "minimum frequency evidence is missing or has a significant imaginary mode"
    return {
        "status": "qualified" if qualified or minimum_supported else "unavailable",
        "context": context,
        "original_imaginary_frequencies_cm1": original_frequencies,
        "imaginary_magnitudes_cm1": magnitudes,
        # v1 compatibility alias remains signed so original evidence is not lost.
        "imaginary_frequencies_cm1": original_frequencies,
        "sign_convention": sign_convention,
        "noise_threshold_cm1": threshold,
        "frequency_evidence_sha256": frequency_hash,
        "mode_evidence_sha256": mode_hash,
        "mode_alignment_status": alignment,
        "method_sha256": method_hash, "structure_sha256": structure_hash,
        "origin": origin,
        "scientific_status": _ORIGIN_STATUS_CEILINGS[origin],
        "binding_status": "available" if binding_ok else "unavailable",
        "kinetic_qualification": (
            "frequency_mode_supported" if qualified else "unavailable"),
        "minimum_qualification": (
            "minimum_frequency_supported" if minimum_supported else "unavailable"),
        "reason": reason,
        "display": (
            "frequency_mode_supported" if qualified else "unavailable"),
    }


def _term(value: Any, *, key: str, label: str, precision: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "key": key, "label": label, "status": "unavailable",
            "value_eV": None, "display": "unavailable",
            "unit": "eV", "evidence_sha256": None, "model": "",
            "origin": "unknown", "scientific_status": "unknown",
        }
    item = _strict_mapping(
        value, field=f"thermochemistry.{key}",
        allowed=frozenset({
            "schema", "value", "unit", "model", "evidence_sha256", "origin",
        }), required=frozenset({"schema", "value", "unit", "model", "origin"}))
    if item.get("schema") != THERMOCHEMISTRY_TERM_SCHEMA:
        raise ReactionWorkbenchError(f"thermochemistry.{key}.schema is unsupported")
    if item.get("unit") != "eV":
        raise ReactionWorkbenchError(f"thermochemistry.{key}.unit must be eV")
    number = _finite(item.get("value"), field=f"thermochemistry.{key}.value")
    if key == "zpe_eV" and number < 0:
        raise ReactionWorkbenchError("thermochemistry.zpe_eV must be non-negative")
    model = _safe_text(item.get("model"), field=f"thermochemistry.{key}.model")
    if model not in _TERM_MODELS[key]:
        raise ReactionWorkbenchError(
            f"thermochemistry.{key}.model is incompatible with the term role")
    evidence_hash = _digest(
        item.get("evidence_sha256"),
        field=f"thermochemistry.{key}.evidence_sha256", optional=True)
    origin = _origin(item.get("origin"), field=f"thermochemistry.{key}.origin")
    if evidence_hash is None:
        return {
            "key": key, "label": label, "status": "unavailable",
            "value_eV": None, "display": "unavailable",
            "unit": "eV", "evidence_sha256": None, "model": model,
            "origin": origin,
            "scientific_status": _ORIGIN_STATUS_CEILINGS[origin],
        }
    return {
        "key": key, "label": label, "status": "available",
        "value_eV": number, "display": _display(number, precision),
        "unit": "eV", "evidence_sha256": evidence_hash, "model": model,
        "origin": origin, "scientific_status": _ORIGIN_STATUS_CEILINGS[origin],
    }


def _normalise_standard_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "kind": "", "value": None, "unit": "", "evidence_sha256": None,
            "origin": "unknown", "scientific_status": "unknown",
            "definition_sha256": None, "display": "unavailable",
        }
    data = _strict_mapping(
        value, field="standard_state",
        allowed=frozenset({
            "schema", "kind", "value", "unit", "evidence_sha256", "origin",
        }), required=frozenset({"schema", "kind", "value", "unit", "origin"}))
    if data.get("schema") != STANDARD_STATE_SCHEMA:
        raise ReactionWorkbenchError("standard_state.schema is unsupported")
    kind = str(data.get("kind") or "").strip()
    if kind not in _STANDARD_STATES:
        raise ReactionWorkbenchError("standard_state.kind is unsupported")
    value_number = _finite(data.get("value"), field="standard_state.value")
    unit = str(data.get("unit") or "").strip()
    expected_value, expected_unit = _STANDARD_STATES[kind]
    if value_number <= 0 or unit != expected_unit or not math.isclose(
            value_number, expected_value, rel_tol=0.0, abs_tol=1e-12):
        raise ReactionWorkbenchError(
            "standard_state value/unit do not match the canonical definition")
    evidence_hash = _digest(
        data.get("evidence_sha256"), field="standard_state.evidence_sha256",
        optional=True)
    origin = _origin(data.get("origin"), field="standard_state.origin")
    definition = {"kind": kind, "value": value_number, "unit": unit}
    return {
        **definition, "evidence_sha256": evidence_hash, "origin": origin,
        "scientific_status": _ORIGIN_STATUS_CEILINGS[origin],
        "definition_sha256": semantic_sha256(definition),
        "display": f"{kind} {value_number:g} {unit}",
    }


def _normalise_condition_response(value: Any) -> dict[str, Any]:
    """Project only typed response-model fields into derived/report DTOs."""
    source = dict(value) if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    raw_base = source.get("base_conditions")
    if isinstance(raw_base, Mapping):
        base_conditions = {}
        for parameter in CONDITION_RANGES:
            number = _optional_finite(
                raw_base.get(parameter),
                field=f"condition_response.base_conditions.{parameter}")
            if number is not None:
                base_conditions[parameter] = number
        if base_conditions:
            result["base_conditions"] = base_conditions
    raw_joint = source.get("joint_model")
    if isinstance(raw_joint, Mapping):
        result["joint_model"] = {
            "model": (
                "additive" if str(raw_joint.get("model") or "").strip()
                == "additive" else ""),
            "evidence_sha256": _digest(
                raw_joint.get("evidence_sha256"),
                field="condition_response.joint_model.evidence_sha256",
                optional=True),
        }
    for parameter in CONDITION_RANGES:
        raw_item = source.get(parameter)
        if not isinstance(raw_item, Mapping):
            continue
        model = str(raw_item.get("model") or "").strip()
        allowed_models = (
            {"local_linear", "ideal_gas_log"}
            if parameter == "pressure_pa" else {"local_linear"})
        item: dict[str, Any] = {
            "model": model if model in allowed_models else "",
            "evidence_sha256": _digest(
                raw_item.get("evidence_sha256"),
                field=f"condition_response.{parameter}.evidence_sha256",
                optional=True),
        }
        numeric_field = (
            "coefficient_eV" if model == "ideal_gas_log"
            else "slope_eV_per_unit")
        number = _optional_finite(
            raw_item.get(numeric_field),
            field=f"condition_response.{parameter}.{numeric_field}")
        if number is not None:
            item[numeric_field] = number
        result[parameter] = item
    return result


def _ledger_row(
    node: Mapping[str, Any], binding: Mapping[str, Any], *, is_ts: bool,
    precision: int,
) -> dict[str, Any]:
    raw = binding.get("thermochemistry")
    if isinstance(raw, Mapping):
        thermo = _strict_mapping(
            raw, field=f"bindings.{node['node_id']}.thermochemistry",
            allowed=frozenset({
                "schema", "origin", *[key for key, _label in _TERM_DEFINITIONS],
                "temperature_k", "pressure_pa", "models", "standard_state",
                "reference_state_sha256", "condition_set_id", "condition_set_sha256",
                "ph", "electrode_potential_v", "coverage", "solvent_model_sha256",
                "coverage_model_sha256", "low_frequency", "frequency_evidence",
                "condition_response",
            }), required=frozenset({"schema", "origin"}))
        if thermo.get("schema") != THERMOCHEMISTRY_BINDING_SCHEMA:
            raise ReactionWorkbenchError("thermochemistry.schema is unsupported")
        thermo_origin = _origin(
            thermo.get("origin"), field=f"bindings.{node['node_id']}.thermochemistry.origin")
    else:
        thermo = {}
        thermo_origin = "unknown"
    terms = [
        _term(thermo.get(key), key=key, label=label, precision=precision)
        for key, label in _TERM_DEFINITIONS
    ]
    models_raw = thermo.get("models") or {}
    models = {}
    invalid_models = []
    if isinstance(models_raw, Mapping):
        for key, raw_model in sorted(models_raw.items()):
            model = str(raw_model or "").strip()
            if key not in MODEL_ROLES:
                raise ReactionWorkbenchError(f"thermochemistry.models.{key} is unknown")
            if model not in THERMOCHEMISTRY_MODELS:
                raise ReactionWorkbenchError(
                    f"thermochemistry.models.{key} is unsupported")
            models[str(key)] = model
    else:
        invalid_models.append("models")
    standard_state = _normalise_standard_state(thermo.get("standard_state"))
    standard_hash = standard_state.get("evidence_sha256")
    temperature = _bounded_condition(
        thermo.get("temperature_k"), key="thermochemistry.temperature_k", optional=True)
    pressure = _bounded_condition(
        thermo.get("pressure_pa"), key="thermochemistry.pressure_pa", optional=True)
    ph = _bounded_condition(
        thermo.get("ph"), key="thermochemistry.ph", optional=True)
    potential = _bounded_condition(
        thermo.get("electrode_potential_v"),
        key="thermochemistry.electrode_potential_v", optional=True)
    coverage = _bounded_condition(
        thermo.get("coverage"), key="thermochemistry.coverage", optional=True)
    low_frequency = _normalise_low_frequency(thermo.get("low_frequency"))
    frequency = _frequency_qualification(
        thermo.get("frequency_evidence"), is_ts=is_ts, node=node)
    missing = [item["key"] for item in terms if item["status"] != "available"]
    if node.get("artifact_status") != "available":
        missing.append("canonical_domain_object_binding")
    if temperature is None:
        missing.append("temperature_k")
    if (not standard_state["kind"] or standard_hash is None
            or standard_state["definition_sha256"] is None):
        missing.append("standard_state")
    if pressure is None:
        missing.append("pressure_pa")
    required_model_roles = {"electronic", "vibration", "standard_state"}
    if invalid_models or not required_model_roles <= set(models):
        missing.append("models")
    if models.get("electronic") != terms[0].get("model"):
        missing.append("electronic_term_model_binding")
    if models.get("standard_state") != terms[4].get("model"):
        missing.append("standard_state_term_model_binding")
    if models.get("vibration") not in {terms[1].get("model"), "explicit"}:
        missing.append("vibration_term_model_binding")
    low_frequency_model = models.get("low_frequency")
    low_frequency_rule = low_frequency.get("rule")
    applied_low_frequency_models = {
        "quasi_harmonic", "hindered_translator", "hindered_rotor", "explicit",
    }
    if low_frequency_model in applied_low_frequency_models:
        compatible_rule = (
            low_frequency_rule in {"quasi_harmonic", "floor_to_cutoff"}
            if low_frequency_model == "quasi_harmonic" else
            low_frequency_rule == low_frequency_model
            if low_frequency_model in {"hindered_translator", "hindered_rotor"}
            else low_frequency_rule != "none")
        if (low_frequency.get("status") != "applied"
                or not low_frequency.get("evidence_sha256")
                or not compatible_rule):
            missing.append("low_frequency_model_evidence")
    elif low_frequency.get("status") == "applied":
        missing.append("low_frequency_model_binding")
    if is_ts and frequency["kinetic_qualification"] == "unavailable":
        missing.append("ts_frequency_mode_evidence")
    if (not is_ts and frequency.get("imaginary_magnitudes_cm1")
            and any(value >= float(frequency["noise_threshold_cm1"])
                    for value in frequency["imaginary_magnitudes_cm1"])):
        missing.append("minimum_has_significant_imaginary_mode")
    available_terms = [
        item["value_eV"] for item in terms if item["status"] == "available"]
    final = sum(available_terms) if len(available_terms) == len(terms) else None
    available = final is not None and not [
        item for item in missing if item != "ts_frequency_mode_evidence"]
    compatibility = {
        "method_sha256": node.get("method_sha256"),
        "reference_state_sha256": _digest(
            thermo.get("reference_state_sha256"),
            field="thermochemistry.reference_state_sha256", optional=True),
        "standard_state_definition_sha256": standard_state.get("definition_sha256"),
        "standard_state_evidence_sha256": standard_hash,
        "solvent_model_sha256": _digest(
            thermo.get("solvent_model_sha256"),
            field="thermochemistry.solvent_model_sha256", optional=True),
        "coverage_model_sha256": _digest(
            thermo.get("coverage_model_sha256"),
            field="thermochemistry.coverage_model_sha256", optional=True),
        "condition_set_id": (
            _opaque(thermo.get("condition_set_id"), field="thermochemistry.condition_set_id")
            if thermo.get("condition_set_id") else None),
        "condition_set_sha256": _digest(
            thermo.get("condition_set_sha256"),
            field="thermochemistry.condition_set_sha256", optional=True),
        "temperature_k": temperature,
        "pressure_pa": pressure,
        "ph": ph, "electrode_potential_v": potential, "coverage": coverage,
        "models_sha256": semantic_sha256(models) if models else None,
        "component_models_sha256": semantic_sha256({
            "terms": [{"key": item["key"], "model": item["model"]}
                      for item in terms],
            "roles": models,
        }) if models else None,
        "low_frequency_sha256": semantic_sha256(low_frequency),
    }
    row_scientific_status = _combined_status([
        node["scientific_status"], _ORIGIN_STATUS_CEILINGS[thermo_origin],
        standard_state["scientific_status"], low_frequency["scientific_status"],
        frequency["scientific_status"],
        *(item["scientific_status"] for item in terms),
    ])
    observed_origin_chain = bool(
        node.get("observed_origin_chain") is True
        and thermo_origin == "observed"
        and standard_state.get("origin") == "observed"
        and low_frequency.get("origin") == "observed"
        and all(item.get("origin") == "observed"
                for item in low_frequency.get("sensitivity") or [])
        and frequency.get("origin") == "observed"
        and all(item.get("origin") == "observed" for item in terms))
    return {
        "entity_id": node["node_id"], "entity_type": node["entity_type"],
        "label": node["label"], "scientific_status": row_scientific_status,
        "origin": thermo_origin,
        "observed_origin_chain": observed_origin_chain,
        "artifact_status": "available" if available else "unavailable",
        "terms": terms, "electronic_energy_e0_eV": terms[0]["value_eV"],
        "zpe_eV": terms[1]["value_eV"],
        "delta_h_thermal_eV": terms[2]["value_eV"],
        "minus_t_delta_s_eV": terms[3]["value_eV"],
        "standard_state_correction_eV": terms[4]["value_eV"],
        "final_delta_g_eV": final if available else None,
        "final_delta_g_display": _display(final if available else None, precision),
        "temperature_k": temperature, "pressure_pa": pressure,
        "temperature_display": (
            "unavailable" if temperature is None else f"{temperature:.2f} K"),
        "pressure_display": (
            "unavailable" if pressure is None else f"{pressure:.6g} Pa"),
        "standard_state": standard_state, "models": models,
        "models_display": ", ".join(
            f"{key}={value}" for key, value in models.items()) or "unavailable",
        "low_frequency": low_frequency, "frequency_qualification": frequency,
        "compatibility": compatibility,
        "condition_response": _normalise_condition_response(
            thermo.get("condition_response")),
        "missing": list(dict.fromkeys(missing)),
    }


def _node(
    object_id: str, payload: Mapping[str, Any], semantic_hash: str,
    binding: Mapping[str, Any], *, entity_type: str, precision: int,
    canonical_envelope: bool,
) -> dict[str, Any]:
    structure_hash = _digest(
        binding.get("structure_sha256"), field=f"bindings.{object_id}.structure_sha256",
        optional=True,
    )
    evidence_hash = _digest(
        binding.get("evidence_sha256"), field=f"bindings.{object_id}.evidence_sha256",
        optional=True,
    ) or _hash_evidence_refs(payload.get("evidence_refs"))
    method_hash = _method_sha256(payload, field=object_id)
    label = _safe_text(
        binding.get("label") or payload.get("chemical_formula")
        or payload.get("composition") or object_id,
        field=f"bindings.{object_id}.label",
    )
    missing = []
    if structure_hash is None:
        missing.append("structure_sha256")
    if method_hash is None:
        missing.append("method_sha256")
    if evidence_hash is None:
        missing.append("evidence_sha256")
    if not canonical_envelope:
        missing.append("canonical_domain_envelope")
    return {
        "node_id": object_id, "object_id": object_id,
        "entity_type": entity_type, "label": label,
        "surface_id": payload.get("surface_id"),
        "structure_sha256": structure_hash,
        "method_sha256": method_hash,
        "evidence_sha256": evidence_hash,
        "object_semantic_sha256": semantic_hash,
        "canonical_envelope": canonical_envelope,
        "provenance": _origin(
            payload.get("provenance"), field=f"{object_id}.provenance", optional=True),
        "origin": _origin(
            binding.get("origin"), field=f"bindings.{object_id}.origin", optional=True),
        "scientific_status": _source_status(
            payload, binding, field=object_id),
        "observed_origin_chain": _source_origins_observed(
            payload, binding, field=object_id),
        "artifact_status": "available" if not missing else "missing",
        "missing": missing, "precision": precision,
    }


def _normalise_participants(
    value: Any, *, field: str,
) -> list[dict[str, Any]]:
    raw_items = _sequence(value, field=field)
    if not raw_items:
        raise ReactionWorkbenchError(f"{field} must contain at least one participant")
    result = []
    seen = set()
    for index, raw in enumerate(raw_items):
        item = _strict_mapping(
            raw, field=f"{field}[{index}]",
            allowed=frozenset({"state_id", "coefficient"}),
            required=frozenset({"state_id", "coefficient"}))
        state_id = _opaque(item.get("state_id"), field=f"{field}[{index}].state_id")
        if state_id in seen:
            raise ReactionWorkbenchError(f"{field} contains duplicate state_id {state_id}")
        seen.add(state_id)
        coefficient = _rational(
            item.get("coefficient"), field=f"{field}[{index}].coefficient")
        result.append({"state_id": state_id, "coefficient": _rational_dto(coefficient)})
    return result


def _fraction_from_dto(value: Mapping[str, Any], *, field: str) -> Fraction:
    return _rational(value, field=field, positive=False)


def _normalise_state_chemistry(
    payload: Mapping[str, Any], *, surface_sites: Mapping[str, set[str]], field: str,
) -> dict[str, Any]:
    missing = []
    composition: dict[str, int] = {}
    raw_composition = payload.get("elemental_composition")
    if isinstance(raw_composition, Mapping):
        from vcstudio.project.structure_identity import ELEMENTS

        for raw_element, raw_count in raw_composition.items():
            element = str(raw_element or "").strip()
            if (element not in ELEMENTS or isinstance(raw_count, bool)
                    or not isinstance(raw_count, int) or raw_count <= 0):
                raise ReactionWorkbenchError(
                    f"{field}.elemental_composition must contain positive integer element counts")
            composition[element] = raw_count
    else:
        formula = str(payload.get("chemical_formula") or "").strip()
        if formula in {"", "*"}:
            composition = {}
        else:
            from vcstudio.project.structure_identity import formula_composition

            parsed = formula_composition(formula)
            if parsed is None:
                missing.append("elemental_composition")
            else:
                composition = parsed
    raw_charge = payload.get("charge")
    charge = None
    if isinstance(raw_charge, bool) or not isinstance(raw_charge, int):
        missing.append("integer_total_charge")
    else:
        charge = raw_charge
    surface_id = str(payload.get("surface_id") or "").strip()
    if not surface_id or surface_id not in surface_sites:
        missing.append("surface_membership")
    occupancies = []
    seen_sites = set()
    raw_occupancy = payload.get("site_occupancy")
    if not isinstance(raw_occupancy, Sequence) or isinstance(raw_occupancy, (str, bytes)):
        missing.append("explicit_site_occupancy")
    else:
        for index, raw in enumerate(raw_occupancy):
            item = _strict_mapping(
                raw, field=f"{field}.site_occupancy[{index}]",
                allowed=frozenset({"site_id", "count"}),
                required=frozenset({"site_id", "count"}))
            site_id = _opaque(
                item.get("site_id"), field=f"{field}.site_occupancy[{index}].site_id")
            if site_id in seen_sites:
                raise ReactionWorkbenchError(
                    f"{field}.site_occupancy contains duplicate site_id {site_id}")
            seen_sites.add(site_id)
            count = _rational(
                item.get("count"), field=f"{field}.site_occupancy[{index}].count")
            if surface_id not in surface_sites or site_id not in surface_sites[surface_id]:
                missing.append(f"site_membership:{site_id}")
            occupancies.append({"site_id": site_id, "count": _rational_dto(count)})
        if not occupancies:
            missing.append("explicit_site_occupancy")
    return {
        "status": "available" if not missing else "unavailable",
        "elemental_composition": dict(sorted(composition.items())),
        "charge": charge, "surface_id": surface_id or None,
        "site_occupancy": occupancies,
        "missing": list(dict.fromkeys(missing)),
    }


def _normalise_condition_set(
    payload: Mapping[str, Any], *, field: str,
) -> dict[str, Any]:
    """Validate the condition values that enter compatibility arithmetic."""
    normalized = copy.deepcopy(dict(payload))
    normalized["temperature_k"] = _bounded_condition(
        payload.get("temperature_k"), key=f"{field}.temperature_k")
    normalized["pressure_pa"] = _bounded_condition(
        payload.get("pressure_pa"), key=f"{field}.pressure_pa")
    for key in ("ph", "electrode_potential_v", "coverage"):
        normalized[key] = _bounded_condition(
            payload.get(key), key=f"{field}.{key}", optional=True)
    return normalized


def _unique_opaque_ids(value: Any, *, field: str) -> list[str]:
    result = [
        _opaque(item, field=f"{field}[]")
        for item in _sequence(value, field=field)]
    if len(result) != len(set(result)):
        raise ReactionWorkbenchError(f"{field} contains duplicate identifiers")
    return result


def _fraction_map_dto(value: Mapping[str, Fraction]) -> dict[str, dict[str, int]]:
    return {
        key: _rational_dto(number) for key, number in sorted(value.items())
        if number != 0
    }


def _reaction_conservation(
    *, reactants: Sequence[Mapping[str, Any]], products: Sequence[Mapping[str, Any]],
    transition_state_id: str | None, node_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    missing = []

    def accumulate(items, side):
        elements: dict[str, Fraction] = {}
        charge = Fraction(0)
        surface_occupancy: dict[str, Fraction] = {}
        for index, participant in enumerate(items):
            state_id = str(participant.get("state_id") or "")
            coefficient = _fraction_from_dto(
                participant.get("coefficient") or {},
                field=f"{side}[{index}].coefficient")
            node = node_by_id.get(state_id)
            chemistry = (node or {}).get("chemistry") or {}
            if node is None or chemistry.get("status") != "available":
                missing.append(f"{side}_chemistry:{state_id}")
                missing.extend(
                    f"{side}_chemistry:{state_id}:{reason}"
                    for reason in chemistry.get("missing") or [])
                continue
            for element, count in (chemistry.get("elemental_composition") or {}).items():
                elements[element] = elements.get(element, Fraction(0)) + coefficient * int(count)
            charge += coefficient * int(chemistry["charge"])
            surface_id = str(chemistry.get("surface_id") or "")
            occupied = sum(
                (_fraction_from_dto(
                    item.get("count") or {}, field=f"{side}.{state_id}.site_occupancy")
                 for item in chemistry.get("site_occupancy") or []),
                Fraction(0))
            surface_occupancy[surface_id] = (
                surface_occupancy.get(surface_id, Fraction(0)) + coefficient * occupied)
        return elements, charge, surface_occupancy

    left_elements, left_charge, left_surface = accumulate(reactants, "reactants")
    right_elements, right_charge, right_surface = accumulate(products, "products")
    failed = []
    if not missing and left_elements != right_elements:
        failed.append("elemental_conservation")
    if not missing and left_charge != right_charge:
        failed.append("charge_conservation")
    if not missing and left_surface != right_surface:
        failed.append("surface_site_occupancy_conservation")
    ts_checks = []
    if transition_state_id:
        ts = node_by_id.get(transition_state_id)
        chemistry = (ts or {}).get("chemistry") or {}
        if ts is None or chemistry.get("status") != "available":
            missing.append("transition_state_chemistry")
        elif not missing:
            ts_elements = {
                key: Fraction(int(value))
                for key, value in (chemistry.get("elemental_composition") or {}).items()}
            ts_charge = Fraction(int(chemistry["charge"]))
            ts_surface_id = str(chemistry.get("surface_id") or "")
            ts_occupied = sum(
                (_fraction_from_dto(
                    item.get("count") or {},
                    field="transition_state.site_occupancy")
                 for item in chemistry.get("site_occupancy") or []),
                Fraction(0))
            ts_surface = {ts_surface_id: ts_occupied}
            if ts_elements != left_elements or ts_elements != right_elements:
                ts_checks.append("transition_state_elemental_composition")
            if ts_charge != left_charge or ts_charge != right_charge:
                ts_checks.append("transition_state_charge")
            if ts_surface != left_surface or ts_surface != right_surface:
                ts_checks.append("transition_state_surface_site_occupancy")
    failed.extend(ts_checks)
    status = "unavailable" if missing else "failed" if failed else "available"
    return {
        "status": status,
        "elemental": {
            "reactants": _fraction_map_dto(left_elements),
            "products": _fraction_map_dto(right_elements),
            "status": "available" if not missing and left_elements == right_elements
            else "unavailable" if missing else "failed",
        },
        "charge": {
            "reactants": _rational_dto(left_charge),
            "products": _rational_dto(right_charge),
            "status": "available" if not missing and left_charge == right_charge
            else "unavailable" if missing else "failed",
        },
        "surface_site_occupancy": {
            "reactants": _fraction_map_dto(left_surface),
            "products": _fraction_map_dto(right_surface),
            "status": "available" if not missing and left_surface == right_surface
            else "unavailable" if missing else "failed",
        },
        "transition_state_status": (
            "available" if transition_state_id and not missing and not ts_checks
            else "unavailable" if missing or not transition_state_id else "failed"),
        "missing": list(dict.fromkeys([*missing, *failed])),
    }


def _structure_hash_map(
    value: Any, *, expected_ids: Sequence[str], field: str,
) -> dict[str, str]:
    data = _mapping(value, field=field)
    if set(data) != set(expected_ids):
        raise ReactionWorkbenchError(
            f"{field} keys must exactly match the elementary-step participants")
    return {
        state_id: str(_digest(data[state_id], field=f"{field}.{state_id}"))
        for state_id in sorted(data)
    }


def _energy_observation(value: Any, *, field: str, required: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        if required:
            return {
                "status": "unavailable", "value_eV": None, "display": "unavailable",
                "model": "", "evidence_sha256": None, "origin": "unknown",
                "scientific_status": "unknown",
            }
        return {}
    item = _strict_mapping(
        value, field=field,
        allowed=frozenset({
            "schema", "value", "unit", "model", "evidence_sha256", "origin",
        }), required=frozenset({
            "schema", "value", "unit", "model", "evidence_sha256", "origin",
        }))
    if item.get("schema") != ENERGY_OBSERVATION_SCHEMA:
        raise ReactionWorkbenchError(f"{field}.schema is unsupported")
    if item.get("unit") != "eV":
        raise ReactionWorkbenchError(f"{field}.unit must be eV")
    model = str(item.get("model") or "").strip()
    if model not in {"ci_neb", "neb"}:
        raise ReactionWorkbenchError(f"{field}.model must identify a NEB observation")
    number = _finite(item.get("value"), field=f"{field}.value")
    if number < 0:
        raise ReactionWorkbenchError(f"{field}.value must be non-negative")
    evidence_hash = _digest(
        item.get("evidence_sha256"), field=f"{field}.evidence_sha256")
    origin = _origin(item.get("origin"), field=f"{field}.origin")
    return {
        "status": "available", "value_eV": number, "display": _display(number, 6),
        "model": model, "evidence_sha256": evidence_hash, "origin": origin,
        "scientific_status": _ORIGIN_STATUS_CEILINGS[origin],
    }


def _normalise_edge_evidence(
    value: Any, *, edge_method_sha256: str | None, binding_evidence_sha256: str | None,
    reactant_ids: Sequence[str], product_ids: Sequence[str],
    transition_state_id: str | None, node_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "status": "unavailable", "method_sha256": None,
            "reference_state_sha256": None, "evidence_sha256": None,
            "origin": "unknown", "scientific_status": "unknown", "neb": None,
            "observed_origin_chain": False,
            "edge_compatibility_status": "unavailable",
            "neb_compatibility_status": "unavailable",
            "edge_missing": ["edge_evidence"],
            "neb_missing": ["neb_evidence"],
            "missing": ["edge_evidence", "neb_evidence"],
            "compatibility_sha256": None,
        }
    data = _strict_mapping(
        value, field="edge_evidence",
        allowed=frozenset({
            "schema", "method_sha256", "reference_state_sha256", "evidence_sha256",
            "origin", "reactant_structure_sha256", "product_structure_sha256",
            "transition_state_structure_sha256", "neb",
        }), required=frozenset({
            "schema", "method_sha256", "reference_state_sha256", "evidence_sha256",
            "origin", "reactant_structure_sha256", "product_structure_sha256",
            "transition_state_structure_sha256", "neb",
        }))
    if data.get("schema") != EDGE_EVIDENCE_SCHEMA:
        raise ReactionWorkbenchError("edge_evidence.schema is unsupported")
    method_hash = _digest(data.get("method_sha256"), field="edge_evidence.method_sha256")
    reference_hash = _digest(
        data.get("reference_state_sha256"), field="edge_evidence.reference_state_sha256")
    evidence_hash = _digest(
        data.get("evidence_sha256"), field="edge_evidence.evidence_sha256")
    origin = _origin(data.get("origin"), field="edge_evidence.origin")
    reactant_structures = _structure_hash_map(
        data.get("reactant_structure_sha256"), expected_ids=reactant_ids,
        field="edge_evidence.reactant_structure_sha256")
    product_structures = _structure_hash_map(
        data.get("product_structure_sha256"), expected_ids=product_ids,
        field="edge_evidence.product_structure_sha256")
    ts_structure = _digest(
        data.get("transition_state_structure_sha256"),
        field="edge_evidence.transition_state_structure_sha256")
    neb_raw = _strict_mapping(
        data.get("neb"), field="edge_evidence.neb",
        allowed=frozenset({
            "schema", "method_sha256", "reference_state_sha256", "evidence_sha256",
            "origin", "reactant_structure_sha256", "product_structure_sha256",
            "transition_state_structure_sha256", "observed_forward_delta_e_barrier",
            "observed_reverse_delta_e_barrier",
        }), required=frozenset({
            "schema", "method_sha256", "reference_state_sha256", "evidence_sha256",
            "origin", "reactant_structure_sha256", "product_structure_sha256",
            "transition_state_structure_sha256", "observed_forward_delta_e_barrier",
        }))
    if neb_raw.get("schema") != NEB_EVIDENCE_SCHEMA:
        raise ReactionWorkbenchError("edge_evidence.neb.schema is unsupported")
    neb_method = _digest(
        neb_raw.get("method_sha256"), field="edge_evidence.neb.method_sha256")
    neb_reference = _digest(
        neb_raw.get("reference_state_sha256"),
        field="edge_evidence.neb.reference_state_sha256")
    neb_evidence = _digest(
        neb_raw.get("evidence_sha256"), field="edge_evidence.neb.evidence_sha256")
    neb_origin = _origin(neb_raw.get("origin"), field="edge_evidence.neb.origin")
    neb_reactant_structures = _structure_hash_map(
        neb_raw.get("reactant_structure_sha256"), expected_ids=reactant_ids,
        field="edge_evidence.neb.reactant_structure_sha256")
    neb_product_structures = _structure_hash_map(
        neb_raw.get("product_structure_sha256"), expected_ids=product_ids,
        field="edge_evidence.neb.product_structure_sha256")
    neb_ts_structure = _digest(
        neb_raw.get("transition_state_structure_sha256"),
        field="edge_evidence.neb.transition_state_structure_sha256")
    observed_forward = _energy_observation(
        neb_raw.get("observed_forward_delta_e_barrier"),
        field="edge_evidence.neb.observed_forward_delta_e_barrier", required=True)
    observed_reverse = _energy_observation(
        neb_raw.get("observed_reverse_delta_e_barrier"),
        field="edge_evidence.neb.observed_reverse_delta_e_barrier", required=False)
    edge_missing = []
    neb_missing = []
    if method_hash != edge_method_sha256:
        edge_missing.append("edge_method_binding")
    if evidence_hash != binding_evidence_sha256:
        edge_missing.append("edge_evidence_hash_binding")
    for state_id, digest in {**reactant_structures, **product_structures}.items():
        if digest != (node_by_id.get(state_id) or {}).get("structure_sha256"):
            edge_missing.append(f"endpoint_structure_binding:{state_id}")
        if edge_method_sha256 != (node_by_id.get(state_id) or {}).get("method_sha256"):
            edge_missing.append(f"endpoint_method_binding:{state_id}")
    ts_node = node_by_id.get(transition_state_id) if transition_state_id else None
    if ts_node is None or ts_structure != ts_node.get("structure_sha256"):
        neb_missing.append("transition_state_structure_binding")
    if ts_node is None or edge_method_sha256 != ts_node.get("method_sha256"):
        neb_missing.append("transition_state_method_binding")
    if neb_method != method_hash:
        neb_missing.append("neb_method_binding")
    if neb_reference != reference_hash:
        neb_missing.append("neb_reference_state_binding")
    if neb_reactant_structures != reactant_structures:
        neb_missing.append("neb_reactant_structure_binding")
    if neb_product_structures != product_structures:
        neb_missing.append("neb_product_structure_binding")
    if neb_ts_structure != ts_structure:
        neb_missing.append("neb_transition_state_structure_binding")
    if observed_forward.get("status") != "available":
        neb_missing.append("observed_forward_delta_e_barrier")
    elif observed_forward.get("evidence_sha256") != neb_evidence:
        neb_missing.append("observed_forward_evidence_hash_binding")
    if observed_reverse and observed_reverse.get("evidence_sha256") != neb_evidence:
        neb_missing.append("observed_reverse_evidence_hash_binding")
    if origin != "observed" or neb_origin != "observed" \
            or observed_forward.get("origin") != "observed":
        neb_missing.append("observed_kinetic_evidence_origin")
    scientific_status = _combined_status([
        _ORIGIN_STATUS_CEILINGS[origin], _ORIGIN_STATUS_CEILINGS[neb_origin],
        observed_forward.get("scientific_status", "unknown"),
        observed_reverse.get("scientific_status", "release") if observed_reverse else "release",
    ])
    neb = {
        "method_sha256": neb_method, "reference_state_sha256": neb_reference,
        "evidence_sha256": neb_evidence, "origin": neb_origin,
        "reactant_structure_sha256": neb_reactant_structures,
        "product_structure_sha256": neb_product_structures,
        "transition_state_structure_sha256": neb_ts_structure,
        "observed_forward_delta_e_barrier": observed_forward,
        "observed_reverse_delta_e_barrier": observed_reverse or None,
    }
    normalized = {
        "status": "available" if not edge_missing and not neb_missing else "unavailable",
        "edge_compatibility_status": "available" if not edge_missing else "unavailable",
        "neb_compatibility_status": "available" if not neb_missing else "unavailable",
        "method_sha256": method_hash, "reference_state_sha256": reference_hash,
        "evidence_sha256": evidence_hash, "origin": origin,
        "scientific_status": scientific_status,
        "observed_origin_chain": bool(
            origin == "observed" and neb_origin == "observed"
            and observed_forward.get("origin") == "observed"
            and (not observed_reverse
                 or observed_reverse.get("origin") == "observed")),
        "reactant_structure_sha256": reactant_structures,
        "product_structure_sha256": product_structures,
        "transition_state_structure_sha256": ts_structure,
        "neb": neb,
        "edge_missing": list(dict.fromkeys(edge_missing)),
        "neb_missing": list(dict.fromkeys(neb_missing)),
        "missing": list(dict.fromkeys([*edge_missing, *neb_missing])),
    }
    normalized["compatibility_sha256"] = semantic_sha256({
        key: normalized[key] for key in (
            "method_sha256", "reference_state_sha256", "evidence_sha256",
            "reactant_structure_sha256", "product_structure_sha256",
            "transition_state_structure_sha256", "neb",
        )})
    return normalized


def _compatibility_signature(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
    compatibility = row.get("compatibility") or {}
    if not isinstance(compatibility, Mapping):
        return None
    required = (
        "method_sha256", "reference_state_sha256",
        "standard_state_definition_sha256", "standard_state_evidence_sha256",
        "condition_set_id", "condition_set_sha256", "temperature_k",
        "models_sha256", "low_frequency_sha256",
        "component_models_sha256",
    )
    if any(not compatibility.get(key) for key in required):
        return None
    return tuple(compatibility.get(key) for key in (
        "method_sha256", "reference_state_sha256",
        "standard_state_definition_sha256", "standard_state_evidence_sha256",
        "solvent_model_sha256", "coverage_model_sha256",
        "condition_set_id", "condition_set_sha256", "temperature_k", "pressure_pa",
        "ph", "electrode_potential_v", "coverage", "models_sha256",
        "component_models_sha256", "low_frequency_sha256",
    ))


def _edge_thermochemistry(
    edge: Mapping[str, Any], ledger_by_id: Mapping[str, Mapping[str, Any]],
    *, condition_record: tuple[Mapping[str, Any], str | None, bool] | None,
    precision: int,
) -> dict[str, Any]:
    reactants = [
        (ledger_by_id.get(str(item.get("state_id"))), _fraction_from_dto(
            item.get("coefficient") or {}, field="edge.reactants.coefficient"))
        for item in edge.get("reactants") or []]
    products = [
        (ledger_by_id.get(str(item.get("state_id"))), _fraction_from_dto(
            item.get("coefficient") or {}, field="edge.products.coefficient"))
        for item in edge.get("products") or []]
    ts = ledger_by_id.get(edge.get("transition_state_id"))
    participants = [*(item[0] for item in reactants), *(item[0] for item in products)]
    thermodynamic_missing = []
    kinetic_missing = []
    conservation = edge.get("conservation") or {}
    if conservation.get("status") != "available":
        thermodynamic_missing.append("reaction_conservation")
        kinetic_missing.append("reaction_conservation")
    if any(item is None for item in participants):
        thermodynamic_missing.append("thermochemistry_participant")
    available_rows = [item for item in participants if item is not None]
    if any(item.get("artifact_status") != "available" for item in available_rows):
        thermodynamic_missing.append("complete_thermochemistry_ledger")
    signatures = [_compatibility_signature(item) for item in available_rows]
    if any(item is None for item in signatures):
        thermodynamic_missing.append("compatibility_binding")
    elif len(set(signatures)) > 1:
        thermodynamic_missing.append(
            "incompatible_method_reference_standard_state_condition_solvent_or_coverage")
    edge_evidence = edge.get("edge_evidence") or {}
    if edge_evidence.get("edge_compatibility_status") != "available":
        thermodynamic_missing.extend(
            edge_evidence.get("edge_missing") or ["edge_evidence_compatibility"])
    edge_reference = edge_evidence.get("reference_state_sha256")
    for row in available_rows:
        if (row.get("compatibility") or {}).get(
                "reference_state_sha256") != edge_reference:
            thermodynamic_missing.append(
                f"edge_reference_state_binding:{row.get('entity_id')}")
    if edge.get("observed_origin_chain") is not True:
        kinetic_missing.append("observed_edge_provenance")
    if edge.get("condition_observed_origin_chain") is not True:
        kinetic_missing.append("observed_condition_provenance")
    if any(row.get("observed_origin_chain") is not True for row in available_rows):
        kinetic_missing.append("observed_participant_provenance")
    expected_condition_id = edge.get("condition_set_id")
    if condition_record is None:
        thermodynamic_missing.append("canonical_condition_set")
    else:
        condition, condition_hash, condition_canonical = condition_record
        if not condition_canonical:
            thermodynamic_missing.append("canonical_condition_set_envelope")
        expected_values = {
            "condition_set_id": expected_condition_id,
            "condition_set_sha256": condition_hash,
            "temperature_k": condition.get("temperature_k"),
            "pressure_pa": condition.get("pressure_pa"),
            "ph": condition.get("ph"),
            "electrode_potential_v": condition.get("electrode_potential_v"),
            "coverage": condition.get("coverage"),
        }
        for row in available_rows:
            compatibility = row.get("compatibility") or {}
            for key, expected in expected_values.items():
                if compatibility.get(key) != expected:
                    thermodynamic_missing.append(
                        f"condition_mismatch:{row.get('entity_id')}:{key}")
    reaction_delta = None
    if not thermodynamic_missing and len(available_rows) == len(participants):
        reaction_delta = (
            sum(float(item["final_delta_g_eV"]) * float(coefficient)
                for item, coefficient in products if item)
            - sum(float(item["final_delta_g_eV"]) * float(coefficient)
                  for item, coefficient in reactants if item)
        )
    barrier = None
    reverse_barrier = None
    ts_qualification = "unavailable" if ts is None else str(
        (ts.get("frequency_qualification") or {}).get(
            "kinetic_qualification") or "unavailable")
    if ts is None:
        kinetic_missing.extend([
            "transition_state_binding", "transition_state_frequency_mode_evidence"])
    else:
        ts_signature = _compatibility_signature(ts)
        participant_signature = signatures[0] if signatures and signatures[0] else None
        if ts.get("artifact_status") != "available":
            kinetic_missing.append("transition_state_thermochemistry")
        if ts.get("observed_origin_chain") is not True:
            kinetic_missing.append("observed_transition_state_provenance")
        if ts_signature is None or ts_signature != participant_signature:
            kinetic_missing.append("transition_state_compatibility")
        if (ts.get("compatibility") or {}).get(
                "reference_state_sha256") != edge_reference:
            kinetic_missing.append("transition_state_reference_state_binding")
        if ts_qualification != "frequency_mode_supported":
            kinetic_missing.append("transition_state_frequency_mode_evidence")
        if edge_evidence.get("neb_compatibility_status") != "available":
            kinetic_missing.extend(
                edge_evidence.get("neb_missing") or ["neb_evidence_compatibility"])
        if not thermodynamic_missing and not kinetic_missing:
            reactant_g = sum(
                float(item["final_delta_g_eV"]) * float(coefficient)
                for item, coefficient in reactants if item)
            product_g = sum(
                float(item["final_delta_g_eV"]) * float(coefficient)
                for item, coefficient in products if item)
            barrier = float(ts["final_delta_g_eV"]) - reactant_g
            reverse_barrier = float(ts["final_delta_g_eV"]) - product_g
    neb = edge_evidence.get("neb") or {}
    observed_forward = (neb.get("observed_forward_delta_e_barrier") or {}).get(
        "value_eV")
    observed_reverse = (neb.get("observed_reverse_delta_e_barrier") or {}).get(
        "value_eV")
    thermodynamic_status = "available" if reaction_delta is not None else "unavailable"
    kinetic_status = "available" if barrier is not None else "unavailable"
    return {
        "status": (
            "available" if thermodynamic_status == kinetic_status == "available"
            else "incomplete"),
        "thermodynamic_status": thermodynamic_status,
        "kinetic_status": kinetic_status,
        "reaction_delta_g_eV": reaction_delta,
        "reaction_delta_g_display": _display(reaction_delta, precision),
        "observed_activation_delta_e_eV": observed_forward,
        "observed_activation_delta_e_display": _display(observed_forward, precision),
        "observed_reverse_activation_delta_e_eV": observed_reverse,
        "observed_reverse_activation_delta_e_display": _display(
            observed_reverse, precision),
        "thermal_activation_delta_g_eV": barrier,
        "thermal_activation_delta_g_display": _display(barrier, precision),
        "activation_delta_g_eV": barrier,
        "activation_delta_g_display": _display(barrier, precision),
        "reverse_activation_delta_g_eV": reverse_barrier,
        "reverse_activation_delta_g_display": _display(reverse_barrier, precision),
        "barrier_sources": {
            "delta_e": "observed_neb" if observed_forward is not None else "unavailable",
            "delta_g": "thermochemistry_ledger" if barrier is not None else "unavailable",
        },
        "ts_qualification": ts_qualification,
        "thermodynamic_missing": list(dict.fromkeys(thermodynamic_missing)),
        "kinetic_missing": list(dict.fromkeys(kinetic_missing)),
        "missing": list(dict.fromkeys([*thermodynamic_missing, *kinetic_missing])),
    }


def _normalise_projection(
    projection: Mapping[str, Any], *, project_id: str,
) -> dict[str, Any]:
    data = _mapping(projection, field="reaction projection")
    unknown = set(data) - {
        "schema", "project_id", "network", "surfaces", "states",
        "transition_states", "steps", "conditions", "bindings", "applicability",
    }
    if unknown:
        raise ReactionWorkbenchError(
            "reaction projection contains unknown fields: " + ", ".join(sorted(unknown)))
    schema = str(data.get("schema") or PROJECTION_SCHEMA)
    if schema != PROJECTION_SCHEMA:
        raise ReactionWorkbenchError("reaction projection schema is unsupported")
    supplied_project = data.get("project_id")
    if supplied_project is None:
        raise ReactionWorkbenchError("reaction projection requires an explicit project_id")
    if _opaque(supplied_project, field="projection.project_id") != project_id:
        raise ReactionWorkbenchError("reaction projection project binding mismatch")
    data["schema"] = schema
    data["project_id"] = project_id
    for key in ("surfaces", "states", "steps", "conditions"):
        data[key] = _sequence(data.get(key) or [], field=key)
    data["transition_states"] = _sequence(
        data.get("transition_states") or [], field="transition_states")
    data["bindings"] = _mapping(data.get("bindings") or {}, field="bindings")
    data["applicability"] = _normalise_applicability(data.get("applicability"))
    if not isinstance(data.get("network"), Mapping):
        raise ReactionWorkbenchError("reaction projection requires one canonical network")
    return data


def _projection_hash_material(data: Mapping[str, Any]) -> dict[str, Any]:
    """Bind projection hashes to server-verified object digests.

    In particular, a raw migration payload cannot influence the projection
    identity with a caller-supplied ``semantic_sha256`` claim.
    """
    material = {
        "schema": data["schema"], "project_id": data["project_id"],
        "bindings": copy.deepcopy(data["bindings"]),
        "applicability": copy.deepcopy(data["applicability"]),
    }

    def reference(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
        _payload, digest, object_type, canonical = _unwrap(value, field=field)
        return {
            "object_type": object_type, "semantic_sha256": digest,
            "canonical_envelope": canonical,
        }

    material["network"] = reference(data["network"], field="network")
    for key in ("surfaces", "states", "transition_states", "steps", "conditions"):
        material[key] = [
            reference(value, field=f"{key}[{index}]")
            for index, value in enumerate(data[key])]
    return material


def _condition_response_delta(
    response: Mapping[str, Any], *, parameter: str, base: float, target: float,
) -> tuple[float | None, str | None]:
    raw = response.get(parameter)
    if not isinstance(raw, Mapping):
        return None, f"{parameter} response evidence is unavailable"
    item = dict(raw)
    evidence_hash = _digest(
        item.get("evidence_sha256"), field=f"condition_response.{parameter}.evidence_sha256",
        optional=True,
    )
    if evidence_hash is None:
        return None, f"{parameter} response evidence hash is unavailable"
    model = str(item.get("model") or "").strip()
    if model == "local_linear":
        slope = _finite(
            item.get("slope_eV_per_unit"),
            field=f"condition_response.{parameter}.slope_eV_per_unit")
        return slope * (target - base), None
    if model == "ideal_gas_log":
        if parameter != "pressure_pa" or base <= 0 or target <= 0:
            return None, "ideal_gas_log requires positive base and target pressure"
        coefficient = _finite(
            item.get("coefficient_eV"),
            field="condition_response.pressure_pa.coefficient_eV")
        return coefficient * math.log(target / base), None
    return None, f"{parameter} response model is unsupported"


def _derived_revision(
    *, projection_sha256: str, graph: Mapping[str, Any], ledger: Mapping[str, Any],
    conditions: Mapping[str, float], applicability: Mapping[str, Any],
    precision: int,
) -> dict[str, Any]:
    outside = []
    for parameter, target in conditions.items():
        limits = applicability.get(parameter)
        if not isinstance(limits, Mapping) or not limits.get("evidence_sha256"):
            outside.append(
                f"{parameter} has no evidence-bound applicability range")
            continue
        lower, upper = limits.get("minimum"), limits.get("maximum")
        if lower is not None and target < lower:
            outside.append(f"{parameter} is below the evidence-bound applicability range")
        if upper is not None and target > upper:
            outside.append(f"{parameter} is above the evidence-bound applicability range")
    derived_rows = []
    for row in ledger.get("rows") or []:
        base_g = row.get("final_delta_g_eV")
        response = row.get("condition_response") or {}
        base_conditions = (
            dict(response.get("base_conditions"))
            if isinstance(response, Mapping)
            and isinstance(response.get("base_conditions"), Mapping) else {})
        base_conditions.setdefault("temperature_k", row.get("temperature_k"))
        base_conditions.setdefault("pressure_pa", row.get("pressure_pa"))
        row_missing = list(outside)
        delta = 0.0
        response_hashes = []
        if base_g is None:
            row_missing.append("base thermochemistry is unavailable")
        changed = []
        for parameter, target in conditions.items():
            base = _optional_finite(
                base_conditions.get(parameter), field=f"base_conditions.{parameter}")
            if base is None:
                row_missing.append(f"{parameter} base condition is unavailable")
                continue
            if abs(target - base) <= 1e-15:
                continue
            changed.append((parameter, base, target))
        if len(changed) > 1:
            joint = response.get("joint_model") if isinstance(response, Mapping) else None
            if (not isinstance(joint, Mapping) or joint.get("model") != "additive"
                    or _digest(
                        joint.get("evidence_sha256") if isinstance(joint, Mapping) else None,
                        field="condition_response.joint_model.evidence_sha256",
                        optional=True) is None):
                row_missing.append(
                    "multi-parameter derivation requires evidence-bound additive joint_model")
        for parameter, base, target in changed:
            item = response.get(parameter) if isinstance(response, Mapping) else None
            row_delta, reason = _condition_response_delta(
                response if isinstance(response, Mapping) else {},
                parameter=parameter, base=base, target=target)
            if reason:
                row_missing.append(reason)
            else:
                delta += float(row_delta)
                response_hashes.append(str((item or {}).get("evidence_sha256")))
        available = base_g is not None and not row_missing
        final = float(base_g) + delta if available else None
        derived_rows.append({
            "entity_id": row.get("entity_id"), "label": row.get("label"),
            "scientific_status": row.get("scientific_status", "unknown"),
            "base_delta_g_eV": base_g,
            "base_delta_g_display": _display(base_g, precision),
            "condition_delta_g_eV": delta if available else None,
            "condition_delta_g_display": _display(delta if available else None, precision),
            "derived_delta_g_eV": final,
            "derived_delta_g_display": _display(final, precision),
            "status": "available" if available else "unavailable",
            "response_evidence_sha256": sorted(set(response_hashes)),
            "missing": list(dict.fromkeys(row_missing)),
        })
    derived_by_id = {row["entity_id"]: row for row in derived_rows}
    derived_edges = []
    for edge in graph.get("edges") or []:
        reactants = [
            (derived_by_id.get(str(item.get("state_id"))), _fraction_from_dto(
                item.get("coefficient") or {}, field="derived.reactants.coefficient"))
            for item in edge.get("reactants") or []]
        products = [
            (derived_by_id.get(str(item.get("state_id"))), _fraction_from_dto(
                item.get("coefficient") or {}, field="derived.products.coefficient"))
            for item in edge.get("products") or []]
        ts = derived_by_id.get(edge.get("transition_state_id"))
        missing = []
        base_thermo = edge.get("thermochemistry") or {}
        if base_thermo.get("thermodynamic_status") != "available":
            missing.append("base edge thermodynamic compatibility is unavailable")
        if any(item is None or item.get("status") != "available"
               for item, _coefficient in [*reactants, *products]):
            missing.append("condition-derived participant thermochemistry")
        reaction_delta = None
        if not missing:
            reaction_delta = (
                sum(float(item["derived_delta_g_eV"]) * float(coefficient)
                    for item, coefficient in products if item)
                - sum(float(item["derived_delta_g_eV"]) * float(coefficient)
                      for item, coefficient in reactants if item))
        barrier = reverse_barrier = None
        if (ts is None or ts.get("status") != "available"
                or base_thermo.get("kinetic_status") != "available"):
            missing.append("condition-derived activation thermochemistry")
        elif reaction_delta is not None:
            reactant_g = sum(
                float(item["derived_delta_g_eV"]) * float(coefficient)
                for item, coefficient in reactants if item)
            product_g = sum(
                float(item["derived_delta_g_eV"]) * float(coefficient)
                for item, coefficient in products if item)
            barrier = float(ts["derived_delta_g_eV"]) - reactant_g
            reverse_barrier = float(ts["derived_delta_g_eV"]) - product_g
        derived_edges.append({
            "edge_id": edge.get("edge_id"),
            "stoichiometry": copy.deepcopy(edge.get("stoichiometry") or {}),
            "reactants": copy.deepcopy(edge.get("reactants") or []),
            "products": copy.deepcopy(edge.get("products") or []),
            "condition_set_id": edge.get("condition_set_id"),
            "condition_set_sha256": edge.get("condition_set_sha256"),
            "reaction_delta_g_eV": reaction_delta,
            "reaction_delta_g_display": _display(reaction_delta, precision),
            "observed_activation_delta_e_eV": base_thermo.get(
                "observed_activation_delta_e_eV"),
            "observed_activation_delta_e_display": base_thermo.get(
                "observed_activation_delta_e_display", "unavailable"),
            "thermal_activation_delta_g_eV": barrier,
            "thermal_activation_delta_g_display": _display(barrier, precision),
            "activation_delta_g_eV": barrier,
            "activation_delta_g_display": _display(barrier, precision),
            "reverse_activation_delta_g_eV": reverse_barrier,
            "reverse_activation_delta_g_display": _display(reverse_barrier, precision),
            "thermodynamic_status": (
                "available" if reaction_delta is not None else "unavailable"),
            "kinetic_status": (
                "available" if barrier is not None else "unavailable"),
            "missing": list(dict.fromkeys(missing)),
        })
    request_sha = semantic_sha256({"conditions": conditions})
    body = {
        "schema": DERIVED_REVISION_SCHEMA,
        "base_projection_sha256": projection_sha256,
        "condition_request_sha256": request_sha,
        "conditions": dict(conditions),
        "applicability": copy.deepcopy(dict(applicability)),
        "applicability_display": {
            key: (
                f"{value.get('minimum') if value.get('minimum') is not None else '-∞'}"
                f" to {value.get('maximum') if value.get('maximum') is not None else '+∞'}")
            for key, value in applicability.items()
        },
        "applicability_evidence_sha256": {
            key: value.get("evidence_sha256") for key, value in applicability.items()
        },
        "rows": derived_rows,
        "edges": derived_edges,
        "scientific_status": _combined_status([
            str(row.get("scientific_status") or "unknown") for row in derived_rows]),
        "artifact_status": (
            "available" if derived_rows
            and all(row["status"] == "available" for row in derived_rows)
            and derived_edges
            and all(edge["thermodynamic_status"] == "available"
                    and edge["kinetic_status"] == "available"
                    for edge in derived_edges)
            else "unavailable"),
        "outside_applicability": outside,
        "source_evidence_mutated": False,
        "limitations": [
            "This hash-bound revision is derived and does not modify canonical domain objects.",
            "A derived revision does not prove a complete mechanism or raise scientific status.",
            "Parameters without an evidence-bound response model remain unavailable.",
        ],
    }
    revision_hash = semantic_sha256(body)
    return {
        **body, "revision_sha256": revision_hash,
        "revision_id": f"derived-{revision_hash[:20]}",
    }


def _report_binding(
    *, graph: Mapping[str, Any], ledger: Mapping[str, Any],
    revision: Mapping[str, Any], scientific_status: str,
) -> dict[str, Any]:
    edge_rows = []
    for edge in graph.get("edges") or []:
        thermo = edge.get("thermochemistry") or {}
        display = edge.get("display") or {}
        edge_rows.append([
            edge.get("edge_id"), display.get("reactants"), display.get("products"),
            edge.get("transition_state_id") or "missing",
            thermo.get("reaction_delta_g_display"),
            thermo.get("observed_activation_delta_e_display"),
            thermo.get("thermal_activation_delta_g_display"),
            edge.get("artifact_status"),
        ])
    ledger_rows = [[
        row.get("entity_id"), row.get("label"),
        _display(row.get("electronic_energy_e0_eV"), 6),
        _display(row.get("zpe_eV"), 6),
        _display(row.get("delta_h_thermal_eV"), 6),
        _display(row.get("minus_t_delta_s_eV"), 6),
        _display(row.get("standard_state_correction_eV"), 6),
        (row.get("standard_state") or {}).get("display"),
        row.get("temperature_display"), row.get("pressure_display"),
        row.get("models_display"),
        row.get("final_delta_g_display"), row.get("artifact_status"),
    ] for row in ledger.get("rows") or []]
    limitations = [
        "Missing nodes and edges are reported explicitly and are never imputed.",
        "Different method, reference-state, standard-state, solvent, or coverage bindings are not mixed.",
        "TS frequency/mode qualification supports a saddle-point assignment only; it does not prove a complete mechanism.",
        "Successful rendering does not raise scientific qualification.",
        *(revision.get("limitations") or []),
    ]
    body = {
        "schema": REPORT_BINDING_SCHEMA,
        "scientific_status": scientific_status,
        "artifact_status": (
            "available" if graph.get("artifact_status") == "available"
            and ledger.get("artifact_status") == "available"
            and revision.get("artifact_status") == "available" else "incomplete"),
        "tables": [
            {
                "table_id": "reaction-map-edges", "title": "Reaction map edges",
                "columns": [
                    "Step", "Reactants", "Products", "TS", "ΔG_rxn / eV",
                    "Observed ΔE‡ / eV", "Thermal ΔG‡ / eV", "Status",
                ],
                "rows": edge_rows,
            },
            {
                "table_id": "thermochemistry-ledger", "title": "Thermochemistry ledger",
                "columns": [
                    "Entity", "Label", "E0", "ZPE", "ΔH", "-TΔS",
                    "Standard correction", "Standard state", "T", "P",
                    "Models", "ΔG", "Status"],
                "rows": ledger_rows,
            },
        ],
        "figures": [{
            "figure_id": "reaction-map", "kind": "reaction_map",
            "title": "Evidence-bound reaction map", "data_sha256": graph.get("graph_sha256"),
            "artifact_status": graph.get("artifact_status"),
            "data": copy.deepcopy(dict(graph)),
        }],
        "limitations": list(dict.fromkeys(limitations)),
        "condition_revision_sha256": revision.get("revision_sha256"),
    }
    return {**body, "binding_sha256": semantic_sha256(body)}


def _frozen_network(
    *, graph: Mapping[str, Any], revision: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the only supported downstream microkinetics consumer payload."""
    derived_by_id = {
        str(item.get("edge_id")): item for item in revision.get("edges") or []}
    edges = []
    for edge in graph.get("edges") or []:
        derived = derived_by_id.get(str(edge.get("edge_id"))) or {}
        edges.append({
            "edge_id": edge.get("edge_id"),
            "reactants": copy.deepcopy(edge.get("reactants") or []),
            "products": copy.deepcopy(edge.get("products") or []),
            "reactant_state_ids": list(edge.get("reactant_node_ids") or []),
            "product_state_ids": list(edge.get("product_node_ids") or []),
            "transition_state_id": edge.get("transition_state_id"),
            "stoichiometry": copy.deepcopy(edge.get("stoichiometry") or {}),
            "condition_set_id": edge.get("condition_set_id"),
            "condition_set_sha256": edge.get("condition_set_sha256"),
            "method_sha256": edge.get("method_sha256"),
            "evidence_sha256": edge.get("evidence_sha256"),
            "edge_compatibility_sha256": (
                edge.get("edge_evidence") or {}).get("compatibility_sha256"),
            "conservation": copy.deepcopy(edge.get("conservation") or {}),
            "reaction_delta_g_eV": derived.get("reaction_delta_g_eV"),
            "observed_activation_delta_e_eV": derived.get(
                "observed_activation_delta_e_eV"),
            "thermal_activation_delta_g_eV": derived.get(
                "thermal_activation_delta_g_eV"),
            "activation_delta_g_eV": derived.get("activation_delta_g_eV"),
            "reverse_activation_delta_g_eV": derived.get(
                "reverse_activation_delta_g_eV"),
            "thermodynamic_status": derived.get(
                "thermodynamic_status", "unavailable"),
            "kinetic_status": derived.get("kinetic_status", "unavailable"),
            "missing": list(dict.fromkeys([
                *(edge.get("missing") or []), *(derived.get("missing") or []),
            ])),
        })
    ready = bool(
        edges and graph.get("microkinetics_ready") is True
        and revision.get("artifact_status") == "available"
        and all(edge["thermodynamic_status"] == "available"
                and edge["kinetic_status"] == "available"
                and not edge["missing"] for edge in edges))
    body = {
        "schema": FROZEN_NETWORK_SCHEMA,
        "network_id": graph.get("network_id"),
        "source_projection_sha256": graph.get("source_projection_sha256"),
        "reaction_graph_sha256": graph.get("graph_sha256"),
        "condition_revision_id": revision.get("revision_id"),
        "condition_revision_sha256": revision.get("revision_sha256"),
        "conditions": copy.deepcopy(revision.get("conditions") or {}),
        "edges": edges,
        "canonical_envelope_authority": graph.get("canonical_envelope_authority") is True,
        "scientific_status": graph.get("scientific_status", "unknown"),
        "readiness": "ready" if ready else "blocked",
        "microkinetics_ready": ready,
        "authorizes_execution": False,
        "limitations": [
            "Stoichiometric coefficients are exact normalized rationals from canonical ElementaryStep participants.",
            "This payload supplies evidence-bound energetics; it does not choose a rate law or reactor model.",
            "Blocked or missing edges must not be imputed by downstream consumers.",
        ],
    }
    return {**body, "frozen_network_sha256": semantic_sha256(body)}


def render_reaction_map_png(
    graph: Mapping[str, Any], out_path: str | Path,
) -> dict[str, Any]:
    """Render one server-owned topology figure; no scientific value is computed."""
    import matplotlib.pyplot as plt

    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    if not nodes:
        raise ReactionWorkbenchError("reaction map figure requires at least one node")
    ordered = sorted(nodes, key=lambda item: (
        item.get("entity_type") == "transition_state", str(item.get("node_id") or "")))
    positions = {}
    ground = [item for item in ordered if item.get("entity_type") != "transition_state"]
    transition = [item for item in ordered if item.get("entity_type") == "transition_state"]
    for index, node in enumerate(ground):
        positions[str(node["node_id"])] = (float(index), 0.0)
    for index, node in enumerate(transition):
        positions[str(node["node_id"])] = (index + 0.5, 0.8)
    fig, ax = plt.subplots(figsize=(max(5.2, len(ground) * 1.25), 3.4))
    try:
        for edge in edges:
            for reactant in edge.get("reactant_node_ids") or []:
                for product in edge.get("product_node_ids") or []:
                    if reactant not in positions or product not in positions:
                        continue
                    start, end = positions[reactant], positions[product]
                    ax.annotate(
                        "", xy=end, xytext=start,
                        arrowprops={
                            "arrowstyle": "->", "color": "#46616f", "lw": 1.3,
                            "shrinkA": 22, "shrinkB": 22,
                        },
                    )
            ts_id = edge.get("transition_state_id")
            if ts_id in positions:
                for participant in [
                        *(edge.get("reactant_node_ids") or []),
                        *(edge.get("product_node_ids") or [])]:
                    if participant in positions:
                        ax.plot(
                            [positions[participant][0], positions[ts_id][0]],
                            [positions[participant][1], positions[ts_id][1]],
                            color="#a36d2f", lw=0.8, ls="--", zorder=1)
        for node in ordered:
            node_id = str(node["node_id"])
            x, y = positions[node_id]
            missing = node.get("artifact_status") != "available"
            ax.scatter(
                [x], [y], s=540, marker="o",
                facecolor="#f8d7da" if missing else "#d8ebe4",
                edgecolor="#8a2f37" if missing else "#245447", zorder=3)
            ax.text(x, y, str(node.get("label") or node_id), ha="center", va="center",
                    fontsize=8, zorder=4)
        ax.set_title("Evidence-bound reaction map")
        ax.set_axis_off()
        ax.margins(x=0.15, y=0.35)
        destination = Path(out_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(destination, dpi=180, bbox_inches="tight", facecolor="white")
    finally:
        plt.close(fig)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "path": str(destination), "sha256": digest,
        "graph_sha256": graph.get("graph_sha256"),
    }


def build_reaction_workbench_view(
    projection: Mapping[str, Any], *, project_id: str,
    conditions: Mapping[str, Any] | None = None, precision: int = 4,
) -> dict[str, Any]:
    """Build the immutable public view from one canonical domain projection.

    No filesystem lookup or legacy name inference occurs here.  Missing
    topology, structure hashes, evidence hashes, thermochemistry terms and TS
    modes are retained as explicit unavailable fields.
    """
    if isinstance(precision, bool) or not isinstance(precision, int) or not 2 <= precision <= 8:
        raise ReactionWorkbenchError("precision must be an integer between 2 and 8")
    project_id = _opaque(project_id, field="project_id")
    data = _normalise_projection(projection, project_id=project_id)
    projection_hash = semantic_sha256(_projection_hash_material(data))
    bindings = data["bindings"]

    network, network_hash, network_type, network_canonical = _unwrap(
        data["network"], field="network")
    if network_type and network_type != "ReactionNetwork":
        raise ReactionWorkbenchError("network envelope object_type must be ReactionNetwork")
    network_id = _opaque(network.get("network_id"), field="network.network_id")
    network_envelope_id = (
        data["network"].get("object_id")
        if isinstance(data["network"], Mapping) else None)
    if network_envelope_id is not None and _opaque(
            network_envelope_id, field="network.object_id") != network_id:
        raise ReactionWorkbenchError("network envelope identity mismatch")
    network_surface_ids = _unique_opaque_ids(
        network.get("surface_ids") or [], field="network.surface_ids")
    network_state_ids = _unique_opaque_ids(
        network.get("state_ids") or [], field="network.state_ids")
    network_step_ids = _unique_opaque_ids(
        network.get("step_ids") or [], field="network.step_ids")
    network_condition_ids = _unique_opaque_ids(
        network.get("condition_set_ids") or [], field="network.condition_set_ids")

    surfaces, states, transition_states, steps, condition_sets = {}, {}, {}, {}, {}
    for index, raw in enumerate(data["surfaces"]):
        identity, payload, digest, _kind, canonical = _projection_record(
            raw, identity_field="surface_id", field=f"surfaces[{index}]")
        if _kind and _kind != "CatalystSurface":
            raise ReactionWorkbenchError("surface envelope object_type must be CatalystSurface")
        if identity in surfaces:
            raise ReactionWorkbenchError(f"duplicate surface_id: {identity}")
        surfaces[identity] = (payload, digest, canonical)
    for index, raw in enumerate(data["states"]):
        identity, payload, digest, _kind, canonical = _projection_record(
            raw, identity_field="state_id", field=f"states[{index}]")
        if _kind and _kind != "AdsorbateState":
            raise ReactionWorkbenchError("state envelope object_type must be AdsorbateState")
        if identity in states:
            raise ReactionWorkbenchError(f"duplicate state_id: {identity}")
        states[identity] = (payload, digest, canonical)
    for index, raw in enumerate(data["transition_states"]):
        payload, digest, kind, canonical = _unwrap(
            raw, field=f"transition_states[{index}]")
        if kind and kind != "TransitionState":
            raise ReactionWorkbenchError(
                "transition-state envelope object_type must be TransitionState")
        identity = _opaque(
            payload.get("transition_state_id") or payload.get("state_id"),
            field=f"transition_states[{index}].transition_state_id")
        envelope_id = raw.get("object_id") if isinstance(raw, Mapping) else None
        if envelope_id is not None and _opaque(
                envelope_id, field=f"transition_states[{index}].object_id") != identity:
            raise ReactionWorkbenchError("transition-state envelope identity mismatch")
        if identity in transition_states:
            raise ReactionWorkbenchError(f"duplicate transition_state_id: {identity}")
        transition_states[identity] = (payload, digest, canonical)
    for index, raw in enumerate(data["steps"]):
        identity, payload, digest, _kind, canonical = _projection_record(
            raw, identity_field="step_id", field=f"steps[{index}]")
        if _kind and _kind != "ElementaryStep":
            raise ReactionWorkbenchError("step envelope object_type must be ElementaryStep")
        if identity in steps:
            raise ReactionWorkbenchError(f"duplicate step_id: {identity}")
        steps[identity] = (payload, digest, canonical)
    for index, raw in enumerate(data["conditions"]):
        identity, payload, digest, kind, canonical = _projection_record(
            raw, identity_field="condition_set_id", field=f"conditions[{index}]")
        if kind and kind != "ConditionSet":
            raise ReactionWorkbenchError("condition envelope object_type must be ConditionSet")
        if identity in condition_sets:
            raise ReactionWorkbenchError(f"duplicate condition_set_id: {identity}")
        condition_sets[identity] = (
            _normalise_condition_set(payload, field=f"conditions[{index}]"),
            digest, canonical)

    surface_sites = {}
    for surface_id, (payload, _digest_value, _canonical) in surfaces.items():
        surface_sites[surface_id] = set(_unique_opaque_ids(
            payload.get("geometric_site_ids") or [],
            field=f"surfaces.{surface_id}.geometric_site_ids"))

    canonical_envelope_authority = bool(
        network_canonical
        and all(record[2] for records in (
            surfaces, states, transition_states, steps, condition_sets)
                for record in records.values()))
    network_observed_origin_chain = _source_origins_observed(
        network, _binding_for(bindings, network_id), field=network_id)

    referenced_ts_ids = {
        str(payload.get("transition_state_id"))
        for step_id, (payload, _digest_value, _canonical) in steps.items()
        if step_id in network_step_ids and payload.get("transition_state_id")
    }
    nodes = []
    node_by_id = {}
    missing_nodes = []
    for surface_id in network_surface_ids:
        record = surfaces.get(surface_id)
        if record is None:
            missing_nodes.append({
                "node_id": surface_id, "entity_type": "surface",
                "status": "missing", "reason": "network surface is not bound"})
            continue
        payload, digest, canonical = record
        node = _node(
            surface_id, payload, digest, _binding_for(bindings, surface_id),
            entity_type="surface", precision=precision,
            canonical_envelope=canonical)
        nodes.append(node)
        node_by_id[surface_id] = node
    for state_id in network_state_ids:
        if state_id in referenced_ts_ids:
            if state_id in states:
                raise ReactionWorkbenchError(
                    f"{state_id} is bound as both AdsorbateState and TransitionState")
            # Some canonical network revisions include TS identities in the
            # broad state_ids set.  The explicit transition_states projection
            # below remains authoritative for the object type.
            continue
        record = states.get(state_id)
        entity_type = "adsorbate_state"
        if record is None:
            missing_nodes.append({
                "node_id": state_id, "entity_type": entity_type,
                "status": "missing", "reason": "network state is not bound"})
            continue
        payload, digest, canonical = record
        node = _node(
            state_id, payload, digest, _binding_for(bindings, state_id),
            entity_type=entity_type, precision=precision,
            canonical_envelope=canonical)
        node["chemistry"] = _normalise_state_chemistry(
            payload, surface_sites=surface_sites, field=f"states.{state_id}")
        if node["chemistry"]["status"] != "available":
            node["missing"].extend(node["chemistry"]["missing"])
            node["artifact_status"] = "missing"
        surface_id = str(payload.get("surface_id") or "")
        if surface_id not in surfaces:
            node["missing"].append("surface_binding")
            node["artifact_status"] = "missing"
        nodes.append(node)
        node_by_id[state_id] = node
    for ts_id in sorted(referenced_ts_ids):
        record = transition_states.get(ts_id)
        if record is None:
            missing_nodes.append({
                "node_id": ts_id, "entity_type": "transition_state",
                "status": "missing",
                "reason": "elementary-step transition state is not explicitly bound"})
            continue
        payload, digest, canonical = record
        node = _node(
            ts_id, payload, digest, _binding_for(bindings, ts_id),
            entity_type="transition_state", precision=precision,
            canonical_envelope=canonical)
        node["chemistry"] = _normalise_state_chemistry(
            payload, surface_sites=surface_sites,
            field=f"transition_states.{ts_id}")
        if node["chemistry"]["status"] != "available":
            node["missing"].extend(node["chemistry"]["missing"])
            node["artifact_status"] = "missing"
        surface_id = str(payload.get("surface_id") or "")
        if surface_id not in surfaces:
            node["missing"].append("surface_binding")
            node["artifact_status"] = "missing"
        nodes.append(node)
        node_by_id[ts_id] = node

    edges, missing_edges = [], []
    for step_id in network_step_ids:
        record = steps.get(step_id)
        if record is None:
            missing_edges.append({
                "edge_id": step_id, "status": "missing",
                "reason": "network elementary step is not bound"})
            continue
        payload, digest, canonical = record
        reactants = _normalise_participants(
            payload.get("reactants"), field=f"steps.{step_id}.reactants")
        products = _normalise_participants(
            payload.get("products"), field=f"steps.{step_id}.products")
        reactant_ids = [str(item["state_id"]) for item in reactants]
        product_ids = [str(item["state_id"]) for item in products]
        overlap = set(reactant_ids) & set(product_ids)
        if overlap:
            raise ReactionWorkbenchError(
                f"steps.{step_id} contains state identifiers on both sides: "
                + ", ".join(sorted(overlap)))
        ts_id = payload.get("transition_state_id")
        ts_id = _opaque(ts_id, field=f"steps.{step_id}.transition_state_id") if ts_id else None
        condition_set_id = payload.get("condition_set_id")
        condition_set_id = (
            _opaque(condition_set_id, field=f"steps.{step_id}.condition_set_id")
            if condition_set_id else None)
        binding = _binding_for(bindings, step_id)
        method_hash = _method_sha256(payload, field=step_id)
        evidence_hash = _digest(
            binding.get("evidence_sha256"), field=f"bindings.{step_id}.evidence_sha256",
            optional=True) or _hash_evidence_refs(payload.get("evidence_refs"))
        missing = [
            item for item in [*reactant_ids, *product_ids, *([ts_id] if ts_id else [])]
            if item not in node_by_id]
        if not canonical:
            missing.append("canonical_domain_envelope")
        if method_hash is None:
            missing.append("method_sha256")
        if evidence_hash is None:
            missing.append("evidence_sha256")
        condition_record = condition_sets.get(condition_set_id) if condition_set_id else None
        if condition_record is None:
            missing.append("condition_set_binding")
        elif not condition_record[2]:
            missing.append("canonical_condition_set_envelope")
        if condition_set_id not in network_condition_ids:
            missing.append("network_condition_set_membership")
        condition_source_status = (
            _source_status(
                condition_record[0], _binding_for(bindings, condition_set_id),
                field=condition_set_id)
            if condition_record is not None and condition_set_id else "unavailable")
        condition_observed_origin_chain = bool(
            condition_record is not None and condition_set_id
            and _source_origins_observed(
                condition_record[0], _binding_for(bindings, condition_set_id),
                field=condition_set_id))
        stoichiometry = {
            **{
                str(item["state_id"]): _rational_dto(-_fraction_from_dto(
                    item["coefficient"], field=f"steps.{step_id}.reactants.coefficient"))
                for item in reactants
            },
            **{
                str(item["state_id"]): _rational_dto(_fraction_from_dto(
                    item["coefficient"], field=f"steps.{step_id}.products.coefficient"))
                for item in products
            },
        }
        edge_evidence = _normalise_edge_evidence(
            binding.get("edge_evidence"), edge_method_sha256=method_hash,
            binding_evidence_sha256=evidence_hash,
            reactant_ids=reactant_ids, product_ids=product_ids,
            transition_state_id=ts_id, node_by_id=node_by_id)
        conservation = _reaction_conservation(
            reactants=reactants, products=products,
            transition_state_id=ts_id, node_by_id=node_by_id)
        if conservation["status"] != "available":
            missing.extend(conservation["missing"] or ["reaction_conservation"])
        edge = {
            "edge_id": step_id, "object_id": step_id,
            "reactants": reactants, "products": products,
            "reactant_node_ids": reactant_ids, "product_node_ids": product_ids,
            "transition_state_id": ts_id,
            "condition_set_id": condition_set_id,
            "condition_set_sha256": (
                condition_record[1] if condition_record is not None else None),
            "stoichiometry": stoichiometry,
            "structure_sha256": (
                node_by_id.get(ts_id, {}).get("structure_sha256") if ts_id else None),
            "method_sha256": method_hash, "evidence_sha256": evidence_hash,
            "canonical_envelope": canonical,
            "object_semantic_sha256": digest,
            "edge_evidence": edge_evidence,
            "conservation": conservation,
            "observed_origin_chain": bool(
                _source_origins_observed(payload, binding, field=step_id)
                and edge_evidence.get("observed_origin_chain") is True),
            "condition_observed_origin_chain": condition_observed_origin_chain,
            "scientific_status": _combined_status([
                _source_status(payload, binding, field=step_id),
                edge_evidence["scientific_status"],
                condition_source_status,
            ]),
            "artifact_status": "available" if not missing else "missing",
            "missing": list(dict.fromkeys(missing)),
            "display": {
                "reactants": " + ".join(
                    (("" if _coefficient_display(item["coefficient"]) == "1" else
                      _coefficient_display(item["coefficient"]) + " ")
                     + str(node_by_id.get(str(item["state_id"]), {
                         "label": item["state_id"]})["label"]))
                    for item in reactants),
                "products": " + ".join(
                    (("" if _coefficient_display(item["coefficient"]) == "1" else
                      _coefficient_display(item["coefficient"]) + " ")
                     + str(node_by_id.get(str(item["state_id"]), {
                         "label": item["state_id"]})["label"]))
                    for item in products),
                "transition_state": (
                    node_by_id.get(ts_id, {"label": "missing"})["label"]
                    if ts_id else "missing"),
            },
        }
        edges.append(edge)
        if missing:
            missing_edges.append({
                "edge_id": step_id, "status": "missing",
                "reason": "elementary step has missing endpoint or evidence bindings",
                "missing": edge["missing"],
            })

    ledger_rows = []
    for node in nodes:
        if node["entity_type"] == "surface":
            continue
        ledger_rows.append(_ledger_row(
            node, _binding_for(bindings, node["node_id"]),
            is_ts=node["entity_type"] == "transition_state", precision=precision))
    ledger_by_id = {row["entity_id"]: row for row in ledger_rows}
    for edge in edges:
        edge["thermochemistry"] = _edge_thermochemistry(
            edge, ledger_by_id,
            condition_record=(condition_sets.get(edge.get("condition_set_id"))
                              if edge.get("condition_set_id") else None),
            precision=precision)
        if edge["thermochemistry"]["status"] != "available":
            edge["artifact_status"] = "missing"
        edge["missing"] = list(dict.fromkeys([
            *edge["missing"], *edge["thermochemistry"]["missing"]]))
        if edge["missing"] and not any(
                item.get("edge_id") == edge["edge_id"] for item in missing_edges):
            missing_edges.append({
                "edge_id": edge["edge_id"], "status": "missing",
                "reason": "elementary step is not thermodynamic/kinetic ready",
                "missing": list(edge["missing"]),
            })

    projection_observed_origin_chain = bool(
        network_observed_origin_chain
        and all(node.get("observed_origin_chain") is True for node in nodes)
        and all(edge.get("observed_origin_chain") is True
                and edge.get("condition_observed_origin_chain") is True
                for edge in edges)
        and all(
            condition_id in condition_sets
            and _source_origins_observed(
                condition_sets[condition_id][0],
                _binding_for(bindings, condition_id), field=condition_id)
            for condition_id in network_condition_ids))

    graph_body = {
        "schema": GRAPH_SCHEMA, "network_id": network_id,
        "network_semantic_sha256": network_hash,
        "source_projection_sha256": projection_hash,
        "canonical_envelope_authority": canonical_envelope_authority,
        "observed_origin_chain": projection_observed_origin_chain,
        "nodes": nodes, "edges": edges,
        "condition_set_ids": network_condition_ids,
        "missing_nodes": missing_nodes, "missing_edges": missing_edges,
        "scientific_status": _combined_status([
            _source_status(
                network, _binding_for(bindings, network_id), field=network_id),
            *(node["scientific_status"] for node in nodes),
            *(edge["scientific_status"] for edge in edges),
            *(_source_status(
                condition_sets[condition_id][0],
                _binding_for(bindings, condition_id), field=condition_id)
              if condition_id in condition_sets else "unavailable"
              for condition_id in network_condition_ids),
        ]),
        "artifact_status": (
            "available" if nodes and edges and not missing_nodes and not missing_edges
            and canonical_envelope_authority
            and all(edge["artifact_status"] == "available" for edge in edges)
            else "incomplete"),
        "thermodynamic_ready": bool(
            edges and all((edge.get("thermochemistry") or {}).get(
                "thermodynamic_status") == "available" for edge in edges)),
        "kinetic_ready": bool(
            edges and projection_observed_origin_chain
            and all((edge.get("thermochemistry") or {}).get(
                "kinetic_status") == "available" for edge in edges)),
        "microkinetics_ready": bool(
            edges and canonical_envelope_authority and projection_observed_origin_chain
            and not missing_nodes and not missing_edges
            and all((edge.get("thermochemistry") or {}).get("status") == "available"
                    for edge in edges)),
        "mechanism_complete": False,
        "limitations": [
            "Graph completeness is not mechanism completeness.",
            "A frequency-qualified TS does not prove that all elementary steps are present.",
        ],
    }
    graph = {**graph_body, "graph_sha256": semantic_sha256(graph_body)}
    ledger_body = {
        "schema": LEDGER_SCHEMA, "source_projection_sha256": projection_hash,
        "rows": ledger_rows,
        "model_catalog": sorted(THERMOCHEMISTRY_MODELS),
        "mixing_policy": "deny_by_default",
        "scientific_status": _combined_status([
            str(row.get("scientific_status") or "unknown") for row in ledger_rows]),
        "artifact_status": (
            "available" if ledger_rows
            and all(row["artifact_status"] == "available" for row in ledger_rows)
            else "incomplete"),
        "limitations": [
            "No missing thermochemistry term is imputed.",
            "Method, reference state, standard state, solvent, and coverage bindings must match before arithmetic.",
        ],
    }
    ledger = {**ledger_body, "ledger_sha256": semantic_sha256(ledger_body)}
    requested_conditions = normalize_conditions(conditions)
    revision = _derived_revision(
        projection_sha256=projection_hash, graph=graph, ledger=ledger,
        conditions=requested_conditions, applicability=data["applicability"],
        precision=precision)
    frozen_network = _frozen_network(graph=graph, revision=revision)
    scientific_status = _combined_status([
        graph["scientific_status"], ledger["scientific_status"],
        revision["scientific_status"],
    ])
    report_binding = _report_binding(
        graph=graph, ledger=ledger, revision=revision,
        scientific_status=scientific_status)
    limitations = list(dict.fromkeys([
        *graph["limitations"], *ledger["limitations"],
        *revision["limitations"], *report_binding["limitations"],
    ]))
    body = {
        "schema": VIEW_SCHEMA, "analysis_id": "free-energy-path",
        "project_id": project_id, "source_projection_schema": PROJECTION_SCHEMA,
        "source_projection_sha256": projection_hash,
        "scientific_status": scientific_status,
        "artifact_status": (
            "available" if graph["artifact_status"] == "available"
            and ledger["artifact_status"] == "available" else "incomplete"),
        "available": bool(nodes or edges),
        "graph": graph, "ledger": ledger, "condition_revision": revision,
        "frozen_network": frozen_network,
        "report_binding": report_binding,
        "rows": copy.deepcopy(ledger_rows),
        "method_matrix": [{
            "configuration_id": node["node_id"], "name": node["label"],
            "status": node["scientific_status"],
            "method_sha256": node["method_sha256"],
            "issues": list(node["missing"]),
        } for node in nodes],
        "missing": [
            *(item["reason"] for item in missing_nodes),
            *(item["reason"] for item in missing_edges),
        ],
        "blocking": [
            f"{edge['edge_id']}: {item}"
            for edge in edges for item in edge.get("missing") or []
        ],
        "warnings": limitations,
        "limitations": limitations,
        "denominator": {
            "network_nodes": (
                len(set(network_surface_ids))
                + len(set(network_state_ids) | set(referenced_ts_ids))),
            "bound_nodes": len(nodes), "missing_nodes": len(missing_nodes),
            "network_edges": len(network_step_ids), "bound_edges": len(edges),
            "missing_edges": len(missing_edges), "ledger_rows": len(ledger_rows),
            "available_ledger_rows": sum(
                row["artifact_status"] == "available" for row in ledger_rows),
            "visible_rows": len(ledger_rows),
        },
    }
    return {**body, "data_fingerprint": semantic_sha256(body)}


def unavailable_reaction_workbench_view(
    *, project_id: str, reason: str, precision: int = 4,
) -> dict[str, Any]:
    """Return a stable fail-closed view when no canonical projection exists."""
    project_id = _opaque(project_id, field="project_id")
    reason = _safe_text(reason, field="reason", limit=1000)
    body = {
        "schema": VIEW_SCHEMA, "analysis_id": "free-energy-path",
        "project_id": project_id, "source_projection_schema": PROJECTION_SCHEMA,
        "source_projection_sha256": None, "scientific_status": "unavailable",
        "artifact_status": "unavailable", "available": False,
        "graph": None, "ledger": None, "condition_revision": None,
        "frozen_network": None, "report_binding": None,
        "rows": [], "method_matrix": [],
        "missing": [reason], "blocking": [reason], "warnings": [],
        "limitations": [
            "No canonical reaction projection was available; no values were inferred."],
        "denominator": {
            "network_nodes": 0, "bound_nodes": 0, "missing_nodes": 0,
            "network_edges": 0, "bound_edges": 0, "missing_edges": 0,
            "ledger_rows": 0, "available_ledger_rows": 0, "visible_rows": 0,
        },
        "precision": precision,
    }
    return {**body, "data_fingerprint": semantic_sha256(body)}


__all__ = [
    "DERIVED_REVISION_SCHEMA", "DOMAIN_ENVELOPE_SCHEMA", "EDGE_EVIDENCE_SCHEMA",
    "ENERGY_OBSERVATION_SCHEMA", "FREQUENCY_EVIDENCE_SCHEMA",
    "FROZEN_NETWORK_SCHEMA", "GRAPH_SCHEMA", "LEDGER_SCHEMA",
    "LOW_FREQUENCY_SCHEMA", "NEB_EVIDENCE_SCHEMA", "PROJECTION_SCHEMA",
    "REPORT_BINDING_SCHEMA", "STANDARD_STATE_SCHEMA",
    "THERMOCHEMISTRY_BINDING_SCHEMA", "THERMOCHEMISTRY_TERM_SCHEMA", "VIEW_SCHEMA",
    "ObjectBinding", "RationalDTO", "ReactionDomainProjection",
    "ReactionDomainSource", "ReactionParticipant",
    "ReactionWorkbenchError", "ThermochemistryBinding",
    "build_reaction_workbench_view", "normalize_conditions",
    "render_reaction_map_png", "semantic_sha256", "unavailable_reaction_workbench_view",
]
