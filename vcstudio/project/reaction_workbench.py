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
from pathlib import Path
from typing import Any, Protocol, TypedDict, runtime_checkable


PROJECTION_SCHEMA = "vcstudio.reaction-domain-projection/v1"
VIEW_SCHEMA = "vcstudio.reaction-workbench-view/v1"
GRAPH_SCHEMA = "vcstudio.reaction-graph/v1"
LEDGER_SCHEMA = "vcstudio.thermochemistry-ledger/v1"
DERIVED_REVISION_SCHEMA = "vcstudio.condition-derived-revision/v1"
REPORT_BINDING_SCHEMA = "vcstudio.reaction-report-binding/v1"
FROZEN_NETWORK_SCHEMA = "vcstudio.frozen-reaction-network/v1"

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


class EvidenceProjection(TypedDict, total=False):
    opaque_id: str
    sha256: str
    ref_type: str
    revision_id: str | None


class ThermochemistryBinding(TypedDict, total=False):
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
    thermochemistry: ThermochemistryBinding


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


def _unwrap(value: Any, *, field: str) -> tuple[dict[str, Any], str | None, str]:
    """Accept a canonical DomainEnvelope or its already-validated payload."""
    record = _mapping(value, field=field)
    if isinstance(record.get("payload"), Mapping):
        payload = _mapping(record["payload"], field=f"{field}.payload")
        digest = _digest(
            record.get("semantic_sha256"), field=f"{field}.semantic_sha256")
        if semantic_sha256(payload) != digest:
            raise ReactionWorkbenchError(f"{field} semantic hash does not match its payload")
        object_type = _safe_text(record.get("object_type"), field=f"{field}.object_type")
        return payload, digest, object_type
    digest = _digest(
        record.get("semantic_sha256"), field=f"{field}.semantic_sha256",
        optional=True,
    )
    return record, digest, ""


def _method_sha256(payload: Mapping[str, Any], *, field: str) -> str | None:
    method = payload.get("method_fingerprint")
    if not isinstance(method, Mapping):
        return None
    return _digest(method.get("sha256"), field=f"{field}.method_fingerprint.sha256")


def _binding_for(bindings: Mapping[str, Any], object_id: str) -> dict[str, Any]:
    value = bindings.get(object_id) or {}
    return _mapping(value, field=f"bindings.{object_id}")


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
) -> tuple[str, dict[str, Any], str | None, str]:
    payload, semantic_hash, object_type = _unwrap(raw, field=field)
    object_id = _opaque(payload.get(identity_field), field=f"{field}.{identity_field}")
    envelope_id = raw.get("object_id") if isinstance(raw, Mapping) else None
    if envelope_id is not None and _opaque(
            envelope_id, field=f"{field}.object_id") != object_id:
        raise ReactionWorkbenchError(f"{field} envelope identity mismatch")
    return object_id, payload, semantic_hash, object_type


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
        item = _mapping(raw, field=f"applicability.{key}")
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
            "evidence_sha256": None, "sensitivity": [],
        }
    data = _mapping(value, field="low_frequency")
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
    sensitivity = []
    for index, raw in enumerate(_sequence(
            data.get("sensitivity") or [], field="low_frequency.sensitivity")):
        item = _mapping(raw, field=f"low_frequency.sensitivity[{index}]")
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
        })
    return {
        "status": "applied" if rule != "none" else "not_applied",
        "original_frequencies_cm1": frequencies,
        "original_frequencies_display": (
            ", ".join(f"{item:g}" for item in frequencies) + " cm-1"
            if frequencies else "unavailable"),
        "rule": rule, "cutoff_cm1": cutoff, "reason": reason,
        "evidence_sha256": treatment_hash,
        "sensitivity": sensitivity,
    }


def _frequency_qualification(value: Any, *, is_ts: bool) -> dict[str, Any]:
    if value in (None, {}):
        return {
            "status": "unavailable", "context": "ts" if is_ts else "minimum",
            "imaginary_frequencies_cm1": [], "noise_threshold_cm1": None,
            "frequency_evidence_sha256": None, "mode_evidence_sha256": None,
            "mode_alignment_status": "unavailable",
            "kinetic_qualification": "unavailable",
            "reason": "frequency and mode evidence are unavailable",
        }
    data = _mapping(value, field="frequency_evidence")
    context = str(data.get("context") or ("ts" if is_ts else "minimum")).strip()
    if context not in {"ts", "minimum"}:
        raise ReactionWorkbenchError("frequency_evidence.context is unsupported")
    original_frequencies = [
        _finite(item, field="frequency_evidence.imaginary_frequencies_cm1[]")
        for item in _sequence(
            data.get("imaginary_frequencies_cm1") or [],
            field="frequency_evidence.imaginary_frequencies_cm1",
        )
    ]
    threshold = _finite(
        data.get("noise_threshold_cm1"), field="frequency_evidence.noise_threshold_cm1")
    if threshold <= 0:
        raise ReactionWorkbenchError("frequency_evidence.noise_threshold_cm1 must be positive")
    frequency_hash = _digest(
        data.get("sha256"), field="frequency_evidence.sha256", optional=True)
    mode_hash = _digest(
        data.get("mode_evidence_sha256"),
        field="frequency_evidence.mode_evidence_sha256", optional=True)
    alignment = str(data.get("mode_alignment_status") or "unavailable").strip()
    if alignment not in {"confirmed", "rejected", "unavailable"}:
        raise ReactionWorkbenchError("frequency_evidence.mode_alignment_status is unsupported")
    magnitudes = [abs(item) for item in original_frequencies]
    large = [item for item in magnitudes if item >= threshold]
    qualified = bool(
        is_ts and context == "ts" and len(large) == 1 and frequency_hash
        and mode_hash and alignment == "confirmed")
    minimum_supported = bool(
        not is_ts and context == "minimum" and not large and frequency_hash)
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
        "sign_convention": "source_preserved",
        "noise_threshold_cm1": threshold,
        "frequency_evidence_sha256": frequency_hash,
        "mode_evidence_sha256": mode_hash,
        "mode_alignment_status": alignment,
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
            "evidence_sha256": None, "model": "",
        }
    item = dict(value)
    try:
        number = _finite(item.get("value"), field=f"thermochemistry.{key}.value")
        evidence_hash = _digest(
            item.get("evidence_sha256"),
            field=f"thermochemistry.{key}.evidence_sha256")
        model = _safe_text(item.get("model"), field=f"thermochemistry.{key}.model")
        if not model or model not in THERMOCHEMISTRY_MODELS:
            raise ReactionWorkbenchError(f"thermochemistry.{key}.model is unsupported")
    except ReactionWorkbenchError:
        return {
            "key": key, "label": label, "status": "unavailable",
            "value_eV": None, "display": "unavailable",
            "evidence_sha256": None, "model": "",
        }
    return {
        "key": key, "label": label, "status": "available",
        "value_eV": number, "display": _display(number, precision),
        "evidence_sha256": evidence_hash, "model": model,
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
    thermo = dict(raw) if isinstance(raw, Mapping) else {}
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
            if key not in MODEL_ROLES or model not in THERMOCHEMISTRY_MODELS:
                invalid_models.append(str(key))
            else:
                models[str(key)] = model
    else:
        invalid_models.append("models")
    standard_raw = thermo.get("standard_state")
    standard = dict(standard_raw) if isinstance(standard_raw, Mapping) else {}
    standard_hash = _digest(
        standard.get("evidence_sha256"), field="standard_state.evidence_sha256",
        optional=True,
    )
    standard_state = {
        "kind": _safe_text(standard.get("kind"), field="standard_state.kind"),
        "value": _optional_finite(standard.get("value"), field="standard_state.value"),
        "unit": _safe_text(standard.get("unit"), field="standard_state.unit"),
        "evidence_sha256": standard_hash,
    }
    standard_state["display"] = " ".join(
        str(item) for item in (
            standard_state["kind"], standard_state["value"], standard_state["unit"])
        if item not in (None, "")) or "unavailable"
    temperature = _optional_finite(
        thermo.get("temperature_k"), field="thermochemistry.temperature_k")
    pressure = _optional_finite(
        thermo.get("pressure_pa"), field="thermochemistry.pressure_pa")
    low_frequency = _normalise_low_frequency(thermo.get("low_frequency"))
    frequency = _frequency_qualification(
        thermo.get("frequency_evidence"), is_ts=is_ts)
    missing = [item["key"] for item in terms if item["status"] != "available"]
    if temperature is None:
        missing.append("temperature_k")
    if (not standard_state["kind"] or standard_hash is None
            or standard_state["value"] is None or not standard_state["unit"]):
        missing.append("standard_state")
    if "ideal_gas" in models.values() and pressure is None:
        missing.append("pressure_pa")
    required_model_roles = {"electronic", "vibration", "standard_state"}
    if invalid_models or not required_model_roles <= set(models):
        missing.append("models")
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
        "standard_state_sha256": standard_hash,
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
        "ph": _optional_finite(thermo.get("ph"), field="thermochemistry.ph"),
        "electrode_potential_v": _optional_finite(
            thermo.get("electrode_potential_v"),
            field="thermochemistry.electrode_potential_v"),
        "coverage": _optional_finite(
            thermo.get("coverage"), field="thermochemistry.coverage"),
        "models_sha256": semantic_sha256(models) if models else None,
        "component_models_sha256": semantic_sha256({
            "terms": [{"key": item["key"], "model": item["model"]}
                      for item in terms],
            "roles": models,
        }) if models else None,
        "low_frequency_sha256": semantic_sha256(low_frequency),
    }
    return {
        "entity_id": node["node_id"], "entity_type": node["entity_type"],
        "label": node["label"], "scientific_status": node["scientific_status"],
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
    object_id: str, payload: Mapping[str, Any], semantic_hash: str | None,
    binding: Mapping[str, Any], *, entity_type: str, precision: int,
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
    return {
        "node_id": object_id, "object_id": object_id,
        "entity_type": entity_type, "label": label,
        "surface_id": payload.get("surface_id"),
        "structure_sha256": structure_hash,
        "method_sha256": method_hash,
        "evidence_sha256": evidence_hash,
        "object_semantic_sha256": semantic_hash,
        "scientific_status": _status(binding.get("scientific_status")),
        "artifact_status": "available" if not missing else "missing",
        "missing": missing, "precision": precision,
    }


def _compatibility_signature(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
    compatibility = row.get("compatibility") or {}
    if not isinstance(compatibility, Mapping):
        return None
    required = (
        "method_sha256", "reference_state_sha256", "standard_state_sha256",
        "condition_set_id", "condition_set_sha256", "temperature_k",
        "models_sha256", "low_frequency_sha256",
        "component_models_sha256",
    )
    if any(not compatibility.get(key) for key in required):
        return None
    return tuple(compatibility.get(key) for key in (
        "method_sha256", "reference_state_sha256", "standard_state_sha256",
        "solvent_model_sha256", "coverage_model_sha256",
        "condition_set_id", "condition_set_sha256", "temperature_k", "pressure_pa",
        "ph", "electrode_potential_v", "coverage", "models_sha256",
        "component_models_sha256", "low_frequency_sha256",
    ))


def _edge_thermochemistry(
    edge: Mapping[str, Any], ledger_by_id: Mapping[str, Mapping[str, Any]],
    *, condition_record: tuple[Mapping[str, Any], str | None] | None,
    precision: int,
) -> dict[str, Any]:
    reactants = [ledger_by_id.get(item) for item in edge["reactant_node_ids"]]
    products = [ledger_by_id.get(item) for item in edge["product_node_ids"]]
    ts = ledger_by_id.get(edge.get("transition_state_id"))
    participants = [*reactants, *products]
    thermodynamic_missing = []
    kinetic_missing = []
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
    expected_condition_id = edge.get("condition_set_id")
    if condition_record is None:
        thermodynamic_missing.append("canonical_condition_set")
    else:
        condition, condition_hash = condition_record
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
            sum(float(item["final_delta_g_eV"]) for item in products if item)
            - sum(float(item["final_delta_g_eV"]) for item in reactants if item)
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
        if ts_signature is None or ts_signature != participant_signature:
            kinetic_missing.append("transition_state_compatibility")
        if ts_qualification != "frequency_mode_supported":
            kinetic_missing.append("transition_state_frequency_mode_evidence")
        if not thermodynamic_missing and not kinetic_missing:
            reactant_g = sum(
                float(item["final_delta_g_eV"]) for item in reactants if item)
            product_g = sum(
                float(item["final_delta_g_eV"]) for item in products if item)
            barrier = float(ts["final_delta_g_eV"]) - reactant_g
            reverse_barrier = float(ts["final_delta_g_eV"]) - product_g
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
        "activation_delta_g_eV": barrier,
        "activation_delta_g_display": _display(barrier, precision),
        "reverse_activation_delta_g_eV": reverse_barrier,
        "reverse_activation_delta_g_display": _display(reverse_barrier, precision),
        "ts_qualification": ts_qualification,
        "thermodynamic_missing": list(dict.fromkeys(thermodynamic_missing)),
        "kinetic_missing": list(dict.fromkeys(kinetic_missing)),
        "missing": list(dict.fromkeys([*thermodynamic_missing, *kinetic_missing])),
    }


def _normalise_projection(
    projection: Mapping[str, Any], *, project_id: str,
) -> dict[str, Any]:
    data = _mapping(projection, field="reaction projection")
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
        reactants = [derived_by_id.get(item) for item in edge.get("reactant_node_ids") or []]
        products = [derived_by_id.get(item) for item in edge.get("product_node_ids") or []]
        ts = derived_by_id.get(edge.get("transition_state_id"))
        missing = []
        base_thermo = edge.get("thermochemistry") or {}
        if base_thermo.get("thermodynamic_status") != "available":
            missing.append("base edge thermodynamic compatibility is unavailable")
        if any(item is None or item.get("status") != "available"
               for item in [*reactants, *products]):
            missing.append("condition-derived participant thermochemistry")
        reaction_delta = None
        if not missing:
            reaction_delta = (
                sum(float(item["derived_delta_g_eV"]) for item in products if item)
                - sum(float(item["derived_delta_g_eV"]) for item in reactants if item))
        barrier = reverse_barrier = None
        if (ts is None or ts.get("status") != "available"
                or base_thermo.get("kinetic_status") != "available"):
            missing.append("condition-derived activation thermochemistry")
        elif reaction_delta is not None:
            reactant_g = sum(
                float(item["derived_delta_g_eV"]) for item in reactants if item)
            product_g = sum(
                float(item["derived_delta_g_eV"]) for item in products if item)
            barrier = float(ts["derived_delta_g_eV"]) - reactant_g
            reverse_barrier = float(ts["derived_delta_g_eV"]) - product_g
        derived_edges.append({
            "edge_id": edge.get("edge_id"),
            "stoichiometry": copy.deepcopy(edge.get("stoichiometry") or {}),
            "condition_set_id": edge.get("condition_set_id"),
            "condition_set_sha256": edge.get("condition_set_sha256"),
            "reaction_delta_g_eV": reaction_delta,
            "reaction_delta_g_display": _display(reaction_delta, precision),
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
        edge_rows.append([
            edge.get("edge_id"), ", ".join(edge.get("reactant_node_ids") or []),
            ", ".join(edge.get("product_node_ids") or []),
            edge.get("transition_state_id") or "missing",
            (edge.get("thermochemistry") or {}).get("reaction_delta_g_display"),
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
                "columns": ["Step", "Reactants", "Products", "TS", "ΔG / eV", "Status"],
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
            "reactant_state_ids": list(edge.get("reactant_node_ids") or []),
            "product_state_ids": list(edge.get("product_node_ids") or []),
            "transition_state_id": edge.get("transition_state_id"),
            "stoichiometry": copy.deepcopy(edge.get("stoichiometry") or {}),
            "condition_set_id": edge.get("condition_set_id"),
            "condition_set_sha256": edge.get("condition_set_sha256"),
            "method_sha256": edge.get("method_sha256"),
            "evidence_sha256": edge.get("evidence_sha256"),
            "reaction_delta_g_eV": derived.get("reaction_delta_g_eV"),
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
        "scientific_status": graph.get("scientific_status", "unknown"),
        "readiness": "ready" if ready else "blocked",
        "microkinetics_ready": ready,
        "authorizes_execution": False,
        "limitations": [
            "Only unit stoichiometric coefficients present in the canonical step projection are frozen.",
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
    projection_hash = semantic_sha256(data)
    bindings = data["bindings"]

    network, network_hash, network_type = _unwrap(data["network"], field="network")
    if network_type and network_type != "ReactionNetwork":
        raise ReactionWorkbenchError("network envelope object_type must be ReactionNetwork")
    network_id = _opaque(network.get("network_id"), field="network.network_id")
    network_surface_ids = [
        _opaque(item, field="network.surface_ids[]")
        for item in _sequence(network.get("surface_ids") or [], field="network.surface_ids")]
    network_state_ids = [
        _opaque(item, field="network.state_ids[]")
        for item in _sequence(network.get("state_ids") or [], field="network.state_ids")]
    network_step_ids = [
        _opaque(item, field="network.step_ids[]")
        for item in _sequence(network.get("step_ids") or [], field="network.step_ids")]
    network_condition_ids = [
        _opaque(item, field="network.condition_set_ids[]")
        for item in _sequence(
            network.get("condition_set_ids") or [], field="network.condition_set_ids")]

    surfaces, states, transition_states, steps, condition_sets = {}, {}, {}, {}, {}
    for index, raw in enumerate(data["surfaces"]):
        identity, payload, digest, _kind = _projection_record(
            raw, identity_field="surface_id", field=f"surfaces[{index}]")
        if _kind and _kind != "CatalystSurface":
            raise ReactionWorkbenchError("surface envelope object_type must be CatalystSurface")
        if identity in surfaces:
            raise ReactionWorkbenchError(f"duplicate surface_id: {identity}")
        surfaces[identity] = (payload, digest)
    for index, raw in enumerate(data["states"]):
        identity, payload, digest, _kind = _projection_record(
            raw, identity_field="state_id", field=f"states[{index}]")
        if _kind and _kind != "AdsorbateState":
            raise ReactionWorkbenchError("state envelope object_type must be AdsorbateState")
        if identity in states:
            raise ReactionWorkbenchError(f"duplicate state_id: {identity}")
        states[identity] = (payload, digest)
    for index, raw in enumerate(data["transition_states"]):
        payload, digest, kind = _unwrap(raw, field=f"transition_states[{index}]")
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
        transition_states[identity] = (payload, digest)
    for index, raw in enumerate(data["steps"]):
        identity, payload, digest, _kind = _projection_record(
            raw, identity_field="step_id", field=f"steps[{index}]")
        if _kind and _kind != "ElementaryStep":
            raise ReactionWorkbenchError("step envelope object_type must be ElementaryStep")
        if identity in steps:
            raise ReactionWorkbenchError(f"duplicate step_id: {identity}")
        steps[identity] = (payload, digest)
    for index, raw in enumerate(data["conditions"]):
        identity, payload, digest, kind = _projection_record(
            raw, identity_field="condition_set_id", field=f"conditions[{index}]")
        if kind and kind != "ConditionSet":
            raise ReactionWorkbenchError("condition envelope object_type must be ConditionSet")
        if identity in condition_sets:
            raise ReactionWorkbenchError(f"duplicate condition_set_id: {identity}")
        condition_sets[identity] = (payload, digest)

    referenced_ts_ids = {
        str(payload.get("transition_state_id"))
        for step_id, (payload, _digest_value) in steps.items()
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
        payload, digest = record
        node = _node(
            surface_id, payload, digest, _binding_for(bindings, surface_id),
            entity_type="surface", precision=precision)
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
        payload, digest = record
        node = _node(
            state_id, payload, digest, _binding_for(bindings, state_id),
            entity_type=entity_type, precision=precision)
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
        payload, digest = record
        node = _node(
            ts_id, payload, digest, _binding_for(bindings, ts_id),
            entity_type="transition_state", precision=precision)
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
        payload, digest = record
        reactants = [
            _opaque(item, field=f"steps.{step_id}.reactant_state_ids[]")
            for item in _sequence(
                payload.get("reactant_state_ids") or [],
                field=f"steps.{step_id}.reactant_state_ids")]
        products = [
            _opaque(item, field=f"steps.{step_id}.product_state_ids[]")
            for item in _sequence(
                payload.get("product_state_ids") or [],
                field=f"steps.{step_id}.product_state_ids")]
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
            item for item in [*reactants, *products, *([ts_id] if ts_id else [])]
            if item not in node_by_id]
        if method_hash is None:
            missing.append("method_sha256")
        if evidence_hash is None:
            missing.append("evidence_sha256")
        condition_record = condition_sets.get(condition_set_id) if condition_set_id else None
        if condition_record is None:
            missing.append("condition_set_binding")
        if condition_set_id not in network_condition_ids:
            missing.append("network_condition_set_membership")
        stoichiometry = {
            **{item: -1.0 for item in reactants},
            **{item: 1.0 for item in products},
        }
        edge = {
            "edge_id": step_id, "object_id": step_id,
            "reactant_node_ids": reactants, "product_node_ids": products,
            "transition_state_id": ts_id,
            "condition_set_id": condition_set_id,
            "condition_set_sha256": (
                condition_record[1] if condition_record is not None else None),
            "stoichiometry": stoichiometry,
            "structure_sha256": (
                node_by_id.get(ts_id, {}).get("structure_sha256") if ts_id else None),
            "method_sha256": method_hash, "evidence_sha256": evidence_hash,
            "object_semantic_sha256": digest,
            "scientific_status": _status(binding.get("scientific_status")),
            "artifact_status": "available" if not missing else "missing",
            "missing": list(dict.fromkeys(missing)),
            "display": {
                "reactants": " + ".join(
                    node_by_id.get(item, {"label": item})["label"] for item in reactants),
                "products": " + ".join(
                    node_by_id.get(item, {"label": item})["label"] for item in products),
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

    graph_body = {
        "schema": GRAPH_SCHEMA, "network_id": network_id,
        "network_semantic_sha256": network_hash,
        "source_projection_sha256": projection_hash,
        "nodes": nodes, "edges": edges,
        "condition_set_ids": network_condition_ids,
        "missing_nodes": missing_nodes, "missing_edges": missing_edges,
        "scientific_status": _combined_status([
            _status(_binding_for(bindings, network_id).get("scientific_status")),
            *(node["scientific_status"] for node in nodes),
            *(edge["scientific_status"] for edge in edges),
            *(_status(_binding_for(bindings, condition_id).get("scientific_status"))
              for condition_id in network_condition_ids),
        ]),
        "artifact_status": (
            "available" if nodes and edges and not missing_nodes and not missing_edges
            and all(edge["artifact_status"] == "available" for edge in edges)
            else "incomplete"),
        "thermodynamic_ready": bool(
            edges and all((edge.get("thermochemistry") or {}).get(
                "thermodynamic_status") == "available" for edge in edges)),
        "kinetic_ready": bool(
            edges and all((edge.get("thermochemistry") or {}).get(
                "kinetic_status") == "available" for edge in edges)),
        "microkinetics_ready": bool(
            edges and not missing_nodes and not missing_edges
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
    "DERIVED_REVISION_SCHEMA", "FROZEN_NETWORK_SCHEMA", "GRAPH_SCHEMA", "LEDGER_SCHEMA",
    "PROJECTION_SCHEMA", "REPORT_BINDING_SCHEMA", "VIEW_SCHEMA",
    "ObjectBinding", "ReactionDomainProjection", "ReactionDomainSource",
    "ReactionWorkbenchError", "ThermochemistryBinding",
    "build_reaction_workbench_view", "normalize_conditions",
    "render_reaction_map_png", "semantic_sha256", "unavailable_reaction_workbench_view",
]
