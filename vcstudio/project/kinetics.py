"""Fail-closed microkinetic projection audit and result normalization.

This module deliberately does not define or persist a reaction-network domain
model.  The Reaction Map/Catalysis Model layers remain authoritative.  Kinetics
consumes either a read-only mapping or a structural ``KineticsInputProvider``
and audits one frozen projection before an external adapter may use it.

No solver is imported here.  Imported results remain diagnostic and can never
open an accepted/final publication gate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


NETWORK_SCHEMA = "vcstudio.kinetics-network/v1"
AUDIT_SCHEMA = "vcstudio.kinetics-audit/v1"
RESULT_SCHEMA = "vcstudio.kinetics-result/v1"
NORMALIZED_RESULT_SCHEMA = "vcstudio.kinetics-normalized-result/v1"

RESULT_UNITS = {
    "temperature": "K",
    "pressure": "bar",
    "potential": "V",
    "tof": "s^-1",
    "coverage": "fraction",
    "selectivity": "fraction",
    "drc": "dimensionless",
    "dsc": "dimensionless",
    "reaction_order": "dimensionless",
    "apparent_activation_energy": "eV",
    "free_energy": "eV",
    "residual": "dimensionless",
}

_TOP_LEVEL_KEYS = frozenset({
    "schema", "input_sha256", "network_id", "revision", "source_projection",
    "assumptions", "standard_state", "operating_range", "methodology",
    "feed_species", "target_products", "species", "elementary_steps", "extensions",
})
_SPECIES_KEYS = frozenset({
    "id", "phase", "composition", "charge", "sites", "formation_energy",
    "frequencies_cm1", "activity",
})
_STEP_KEYS = frozenset({
    "id", "reactants", "transition_state", "products", "reversible", "delta_g",
    "forward_barrier", "reverse_barrier", "prefactors", "bep", "scaling",
    "uncertainty_eV",
})
_ENERGY_KEYS = frozenset({
    "value", "unit", "method_id", "source", "uncertainty_eV",
})
_SOURCE_KEYS = frozenset({"kind", "reference", "evidence_sha256"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_ELEMENT_RE = re.compile(r"^[A-Z][a-z]?$|^e-$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_RE = re.compile(
    r"(?i)(?:github_pat_|gh[opusr]_|sk-|bearer\s+|private key|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[:=])")
_PATH_RE = re.compile(r"(?i)(?:^[A-Z]:[\\/]|^\\\\|^/|^~[\\/]|\.\.[\\/]|file:)")
_BARRIER_TOL_EV = 1.0e-3
_BALANCE_TOL = 1.0e-8


class KineticsContractError(ValueError):
    """A frozen input or imported result violates the public contract."""


@runtime_checkable
class KineticsInputProvider(Protocol):
    """Structural seam implemented by an upstream frozen projection adapter."""

    def kinetics_input(self) -> Mapping[str, Any]:
        """Return one immutable-by-convention kinetics projection mapping."""


def _as_mapping(source: Mapping[str, Any] | KineticsInputProvider) -> dict[str, Any]:
    if isinstance(source, Mapping):
        value = source
    else:
        provider = getattr(source, "kinetics_input", None)
        if not callable(provider):
            raise KineticsContractError(
                "kinetics input must be a mapping or KineticsInputProvider")
        value = provider()
    if not isinstance(value, Mapping):
        raise KineticsContractError("KineticsInputProvider must return a mapping")
    return copy.deepcopy(dict(value))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_input_sha256(
        source: Mapping[str, Any] | KineticsInputProvider) -> str:
    """Hash an exact adapter projection, excluding its self-declared hash."""
    value = _as_mapping(source)
    value.pop("input_sha256", None)
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _issue(issues: list[dict[str, str]], code: str, path: str, message: str,
           *, severity: str = "error") -> None:
    record = {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
    }
    if record not in issues:
        issues.append(record)


def _unknown_fields(value: Any, allowed: frozenset[str], path: str,
                    issues: list[dict[str, str]]) -> bool:
    if not isinstance(value, Mapping):
        _issue(issues, "INVALID_OBJECT", path, f"{path} must be an object")
        return False
    for key in sorted(set(value) - allowed):
        _issue(issues, "UNKNOWN_FIELD", f"{path}.{key}",
               f"unknown field {path}.{key}")
    return True


def _safe_identifier(value: Any, path: str, issues: list[dict[str, str]]) -> str | None:
    if (not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value)
            or _CONTROL_RE.search(value) or _PATH_RE.search(value)
            or _SECRET_RE.search(value)):
        _issue(issues, "UNSAFE_IDENTIFIER", path,
               f"{path} must be a safe opaque identifier")
        return None
    return value


def _safe_text(value: Any, path: str, issues: list[dict[str, str]],
               *, maximum: int = 512) -> str | None:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or _CONTROL_RE.search(value) or _SECRET_RE.search(value)
            or _PATH_RE.search(value)):
        _issue(issues, "UNSAFE_TEXT", path, f"{path} must be bounded safe text")
        return None
    return value.strip()


def _sha(value: Any, path: str, issues: list[dict[str, str]],
         *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        _issue(issues, "INVALID_SHA256", path, f"{path} must be a lowercase SHA-256")
        return None
    return value


def _number(value: Any, path: str, issues: list[dict[str, str]], *, minimum=None,
            maximum=None, code: str = "NONFINITE_NUMBER") -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _issue(issues, code, path, f"{path} must be a finite number")
        return None
    number = float(value)
    if not math.isfinite(number):
        _issue(issues, "NONFINITE_NUMBER", path, f"{path} must be a finite number")
        return None
    if minimum is not None and number < minimum:
        _issue(issues, code, path, f"{path} must be at least {minimum:g}")
        return None
    if maximum is not None and number > maximum:
        _issue(issues, code, path, f"{path} must be at most {maximum:g}")
        return None
    return number


def _audit_source(value: Any, path: str, issues: list[dict[str, str]]) -> None:
    if not _unknown_fields(value, _SOURCE_KEYS, path, issues):
        return
    _safe_identifier(value.get("kind"), f"{path}.kind", issues)
    _safe_text(value.get("reference"), f"{path}.reference", issues)
    _sha(value.get("evidence_sha256"), f"{path}.evidence_sha256", issues)


def _audit_energy(value: Any, path: str, issues: list[dict[str, str]],
                  *, method_id: str | None) -> float | None:
    if not _unknown_fields(value, _ENERGY_KEYS, path, issues):
        return None
    number = _number(value.get("value"), f"{path}.value", issues)
    if value.get("unit") != "eV":
        _issue(issues, "ENERGY_UNIT_MISMATCH", f"{path}.unit",
               f"{path}.unit must be eV")
    record_method = _safe_identifier(
        value.get("method_id"), f"{path}.method_id", issues)
    if method_id and record_method and record_method != method_id:
        _issue(issues, "METHOD_ID_MISMATCH", f"{path}.method_id",
               "energy record method_id does not match methodology.method_id")
    _audit_source(value.get("source"), f"{path}.source", issues)
    uncertainty = _number(
        value.get("uncertainty_eV"), f"{path}.uncertainty_eV", issues,
        minimum=0.0, code="ENERGY_UNCERTAINTY_REQUIRED")
    if uncertainty is None:
        _issue(issues, "ENERGY_UNCERTAINTY_REQUIRED", f"{path}.uncertainty_eV",
               "every energy requires a finite non-negative uncertainty_eV")
    return number


def _audit_range(value: Any, path: str, issues: list[dict[str, str]], *, minimum: float,
                 maximum: float) -> tuple[float, float] | None:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or len(value) != 2):
        _issue(issues, "INVALID_OPERATING_RANGE", path,
               f"{path} must be [minimum, maximum]")
        return None
    lo = _number(value[0], f"{path}[0]", issues, minimum=minimum, maximum=maximum,
                 code="INVALID_OPERATING_RANGE")
    hi = _number(value[1], f"{path}[1]", issues, minimum=minimum, maximum=maximum,
                 code="INVALID_OPERATING_RANGE")
    if lo is None or hi is None or lo > hi:
        _issue(issues, "INVALID_OPERATING_RANGE", path,
               f"{path} minimum must not exceed maximum")
        return None
    return lo, hi


def _state(value: Any, path: str, issues: list[dict[str, str]],
           species: Mapping[str, dict[str, Any]]) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        _issue(issues, "INVALID_STOICHIOMETRY", path,
               f"{path} must be a non-empty species-to-coefficient object")
        return {}
    out = {}
    for raw_id, raw_coef in value.items():
        species_id = _safe_identifier(raw_id, f"{path} key", issues)
        coef = _number(raw_coef, f"{path}.{raw_id}", issues, minimum=0.0,
                       code="INVALID_STOICHIOMETRY")
        if coef is not None and coef <= 0:
            _issue(issues, "INVALID_STOICHIOMETRY", f"{path}.{raw_id}",
                   "stoichiometric coefficients must be positive")
            continue
        if coef is not None and abs(coef - round(coef)) > _BALANCE_TOL:
            _issue(issues, "CATMAP_STOICHIOMETRY_UNSUPPORTED", f"{path}.{raw_id}",
                   "phase-1 CatMAP export requires integer elementary-step coefficients")
        if species_id and species_id not in species:
            _issue(issues, "UNKNOWN_STEP_SPECIES", f"{path}.{raw_id}",
                   f"step references unknown species {raw_id}")
        if species_id and coef is not None and coef > 0:
            out[species_id] = coef
    return out


def _state_property(state: Mapping[str, float], species: Mapping[str, dict[str, Any]],
                    field: str) -> dict[str, float] | float | None:
    if field == "charge":
        total = 0.0
        for species_id, coef in state.items():
            record = species.get(species_id) or {}
            value = record.get("_charge")
            if value is None:
                return None
            total += coef * value
        return total
    totals: dict[str, float] = defaultdict(float)
    for species_id, coef in state.items():
        record = species.get(species_id) or {}
        value = record.get(f"_{field}")
        if not isinstance(value, Mapping):
            return None
        for key, count in value.items():
            totals[key] += coef * count
    return dict(totals)


def _state_energy(state: Mapping[str, float], species: Mapping[str, dict[str, Any]]) -> float | None:
    total = 0.0
    for species_id, coef in state.items():
        energy = (species.get(species_id) or {}).get("_energy")
        if energy is None:
            return None
        total += coef * energy
    return total


def _same_scalar(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and abs(left - right) <= _BALANCE_TOL


def _same_mapping(left: Any, right: Any) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    keys = set(left) | set(right)
    return all(abs(float(left.get(key, 0.0)) - float(right.get(key, 0.0)))
               <= _BALANCE_TOL for key in keys)


def _audit_assumptions(value: Any, issues: list[dict[str, str]]) -> None:
    allowed = frozenset({
        "mean_field", "steady_state", "site_uniformity",
        "lateral_interactions", "mechanism_completeness",
    })
    if not _unknown_fields(value, allowed, "assumptions", issues):
        return
    if value.get("mean_field") is not True:
        _issue(issues, "UNSUPPORTED_KINETIC_MODEL", "assumptions.mean_field",
               "phase 1 requires an explicit mean-field model")
    if value.get("steady_state") is not True:
        _issue(issues, "UNSUPPORTED_KINETIC_MODEL", "assumptions.steady_state",
               "phase 1 requires an explicit steady-state model")
    if value.get("site_uniformity") != "uniform":
        _issue(issues, "SITE_UNIFORMITY_UNSUPPORTED", "assumptions.site_uniformity",
               "phase 1 supports only uniform site populations")
    interactions = value.get("lateral_interactions")
    if interactions not in {"neglected", "parameterized"}:
        _issue(issues, "LATERAL_INTERACTION_BOUNDARY_MISSING",
               "assumptions.lateral_interactions",
               "lateral interactions must be declared neglected or parameterized")
    elif interactions == "parameterized":
        _issue(issues, "LATERAL_INTERACTIONS_UNSUPPORTED",
               "assumptions.lateral_interactions",
               "parameterized lateral interactions are outside the phase-1 adapter")
    else:
        _issue(issues, "LATERAL_INTERACTIONS_NEGLECTED",
               "assumptions.lateral_interactions",
               "coverage-dependent lateral interactions are neglected",
               severity="warning")
    completeness = value.get("mechanism_completeness")
    if completeness not in {"claimed_complete", "partial", "unknown"}:
        _issue(issues, "MECHANISM_COMPLETENESS_UNRESOLVED",
               "assumptions.mechanism_completeness",
               "mechanism completeness must be claimed_complete, partial, or unknown")
    elif completeness != "claimed_complete":
        _issue(issues, "MECHANISM_COMPLETENESS_UNRESOLVED",
               "assumptions.mechanism_completeness",
               "an incomplete or unknown mechanism cannot be exported for solving")


def _audit_standard_state(value: Any, issues: list[dict[str, str]]) -> None:
    allowed = frozenset({"temperature", "pressure", "concentration", "potential"})
    if not _unknown_fields(value, allowed, "standard_state", issues):
        return
    required = {
        "temperature": ("K", 1.0, 5000.0),
        "pressure": ("bar", 0.0, 1.0e6),
        "concentration": ("mol/L", 0.0, 1.0e4),
    }
    for key, (unit, minimum, maximum) in required.items():
        record = value.get(key)
        path = f"standard_state.{key}"
        if not isinstance(record, Mapping) or set(record) != {"value", "unit"}:
            _issue(issues, "STANDARD_STATE_REQUIRED", path,
                   f"{path} requires exactly value and unit")
            continue
        number = _number(record.get("value"), f"{path}.value", issues,
                         minimum=minimum, maximum=maximum,
                         code="STANDARD_STATE_REQUIRED")
        if key in {"pressure", "concentration"} and number is not None and number <= 0:
            _issue(issues, "STANDARD_STATE_REQUIRED", f"{path}.value",
                   f"standard-state {key} must be positive")
        if record.get("unit") != unit:
            _issue(issues, "STANDARD_STATE_REQUIRED", f"{path}.unit",
                   f"{path}.unit must be {unit}")
    potential = value.get("potential")
    if potential is not None:
        if (not isinstance(potential, Mapping)
                or set(potential) != {"value", "unit", "reference"}):
            _issue(issues, "STANDARD_STATE_REQUIRED", "standard_state.potential",
                   "potential requires exactly value, unit and reference")
        else:
            _number(potential.get("value"), "standard_state.potential.value", issues,
                    minimum=-10.0, maximum=10.0, code="STANDARD_STATE_REQUIRED")
            if potential.get("unit") != "V":
                _issue(issues, "STANDARD_STATE_REQUIRED", "standard_state.potential.unit",
                       "standard_state.potential.unit must be V")
            _safe_identifier(potential.get("reference"),
                             "standard_state.potential.reference", issues)


def _audit_methodology(value: Any, issues: list[dict[str, str]]) -> str | None:
    allowed = frozenset({
        "method_id", "energy_basis", "thermochemistry", "solvation",
        "potential_model", "compatibility_status", "identity_sha256",
    })
    if not _unknown_fields(value, allowed, "methodology", issues):
        return None
    method_id = _safe_identifier(value.get("method_id"), "methodology.method_id", issues)
    if value.get("energy_basis") not in {"gibbs_free_energy", "electronic_plus_corrections"}:
        _issue(issues, "ENERGY_BASIS_UNSUPPORTED", "methodology.energy_basis",
               "energy_basis must be gibbs_free_energy or electronic_plus_corrections")
    for key in ("thermochemistry", "solvation", "potential_model"):
        _safe_identifier(value.get(key), f"methodology.{key}", issues)
    if value.get("compatibility_status") != "verified":
        _issue(issues, "METHOD_COMPATIBILITY_UNVERIFIED",
               "methodology.compatibility_status",
               "all energies and corrections require verified method compatibility")
    _sha(value.get("identity_sha256"), "methodology.identity_sha256", issues)
    return method_id


def _audit_species(value: Any, method_id: str | None,
                   issues: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or not value):
        _issue(issues, "SPECIES_REQUIRED", "species",
               "species must be a non-empty array")
        return {}
    records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        path = f"species[{index}]"
        if not _unknown_fields(raw, _SPECIES_KEYS, path, issues):
            continue
        species_id = _safe_identifier(raw.get("id"), f"{path}.id", issues)
        if species_id in records:
            _issue(issues, "DUPLICATE_SPECIES", f"{path}.id",
                   f"duplicate species id {species_id}")
            continue
        phase = raw.get("phase")
        if phase not in {"gas", "liquid", "solution", "adsorbate", "surface",
                         "transition_state"}:
            _issue(issues, "INVALID_SPECIES_PHASE", f"{path}.phase",
                   "unsupported species phase")
        composition = raw.get("composition")
        normalized_comp = {}
        if not isinstance(composition, Mapping):
            _issue(issues, "INVALID_COMPOSITION", f"{path}.composition",
                   "composition must be an element-to-count object")
        else:
            for element, count in composition.items():
                if not isinstance(element, str) or not _ELEMENT_RE.fullmatch(element):
                    _issue(issues, "INVALID_COMPOSITION", f"{path}.composition",
                           f"invalid element key {element!r}")
                    continue
                number = _number(count, f"{path}.composition.{element}", issues,
                                 minimum=0.0, code="INVALID_COMPOSITION")
                if number is not None and number > 0:
                    normalized_comp[element] = number
        charge = _number(raw.get("charge"), f"{path}.charge", issues,
                         minimum=-100.0, maximum=100.0, code="INVALID_CHARGE")
        sites = raw.get("sites")
        normalized_sites = {}
        if not isinstance(sites, Mapping):
            _issue(issues, "INVALID_SITE_COUNT", f"{path}.sites",
                   "sites must be a site-type-to-count object")
        else:
            for site, count in sites.items():
                site_id = _safe_identifier(site, f"{path}.sites key", issues)
                number = _number(count, f"{path}.sites.{site}", issues,
                                 minimum=0.0, code="INVALID_SITE_COUNT")
                if site_id and number is not None and number > 0:
                    normalized_sites[site_id] = number
        if phase in {"adsorbate", "surface", "transition_state"} and not normalized_sites:
            _issue(issues, "SITE_COUNT_REQUIRED", f"{path}.sites",
                   f"{phase} species requires occupied site counts")
        if phase in {"adsorbate", "surface", "transition_state"} and (
                len(normalized_sites) != 1
                or any(abs(count - 1.0) > _BALANCE_TOL
                       for count in normalized_sites.values())):
            _issue(issues, "MULTISITE_SPECIES_UNSUPPORTED", f"{path}.sites",
                   "phase 1 requires each explicit surface species to occupy one site; "
                   "represent additional empty sites explicitly in each reaction state")
        if phase in {"gas", "liquid", "solution"} and normalized_sites:
            _issue(issues, "GAS_SITE_COUNT_FORBIDDEN", f"{path}.sites",
                   f"{phase} species must not consume surface sites")
        activity = raw.get("activity")
        if phase in {"gas", "liquid", "solution"}:
            activity_path = f"{path}.activity"
            if (not isinstance(activity, Mapping)
                    or set(activity) != {"value", "unit", "source"}):
                _issue(issues, "SPECIES_ACTIVITY_REQUIRED", activity_path,
                       f"{phase} species requires value, unit and source activity")
            else:
                expected_unit = "bar" if phase == "gas" else "mol/L"
                _number(activity.get("value"), f"{activity_path}.value", issues,
                        minimum=0.0, code="SPECIES_ACTIVITY_REQUIRED")
                if activity.get("unit") != expected_unit:
                    _issue(issues, "SPECIES_ACTIVITY_REQUIRED", f"{activity_path}.unit",
                           f"{phase} activity unit must be {expected_unit}")
                _audit_source(activity.get("source"), f"{activity_path}.source", issues)
        elif activity is not None:
            _issue(issues, "SPECIES_ACTIVITY_FORBIDDEN", f"{path}.activity",
                   f"{phase} species must not carry a gas/solution activity")
        energy = _audit_energy(raw.get("formation_energy"),
                               f"{path}.formation_energy", issues,
                               method_id=method_id)
        frequencies = raw.get("frequencies_cm1")
        if (isinstance(frequencies, (str, bytes))
                or not isinstance(frequencies, Sequence) or len(frequencies) > 1000):
            _issue(issues, "INVALID_FREQUENCIES", f"{path}.frequencies_cm1",
                   "frequencies_cm1 must be a bounded array")
        else:
            for freq_index, frequency in enumerate(frequencies):
                _number(frequency, f"{path}.frequencies_cm1[{freq_index}]", issues,
                        minimum=-1.0e5, maximum=1.0e5, code="INVALID_FREQUENCIES")
        if species_id:
            records[species_id] = {
                **copy.deepcopy(dict(raw)),
                "_composition": normalized_comp,
                "_charge": charge,
                "_sites": normalized_sites,
                "_energy": energy,
                "_activity": (
                    float(activity["value"])
                    if isinstance(activity, Mapping)
                    and isinstance(activity.get("value"), (int, float))
                    and not isinstance(activity.get("value"), bool)
                    and math.isfinite(float(activity["value"])) else None),
            }
    return records


def _audit_prefactors(value: Any, path: str, issues: list[dict[str, str]]) -> None:
    if not isinstance(value, Mapping):
        _issue(issues, "PREFACTOR_REQUIRED", path,
               "forward and reverse prefactors are required")
        return
    for direction in ("forward", "reverse"):
        record = value.get(direction)
        record_path = f"{path}.{direction}"
        if not isinstance(record, Mapping) or set(record) != {"value", "unit", "source"}:
            _issue(issues, "PREFACTOR_REQUIRED", record_path,
                   f"{record_path} requires value, unit and source")
            continue
        _number(record.get("value"), f"{record_path}.value", issues,
                minimum=0.0, code="PREFACTOR_REQUIRED")
        if (isinstance(record.get("value"), (int, float))
                and not isinstance(record.get("value"), bool)
                and math.isfinite(float(record["value"]))
                and float(record["value"]) <= 0):
            _issue(issues, "PREFACTOR_REQUIRED", f"{record_path}.value",
                   "prefactors must be strictly positive")
        if record.get("unit") not in {
                "s^-1", "bar^-1 s^-1", "mol^-1 L s^-1"}:
            _issue(issues, "PREFACTOR_UNIT_UNSUPPORTED", f"{record_path}.unit",
                   "unsupported prefactor unit")
        _audit_source(record.get("source"), f"{record_path}.source", issues)


def _audit_empirical_model(value: Any, path: str, issues: list[dict[str, str]]) -> None:
    allowed = frozenset({"used", "source", "parameters_sha256"})
    if not _unknown_fields(value, allowed, path, issues):
        return
    if not isinstance(value.get("used"), bool):
        _issue(issues, "EMPIRICAL_SOURCE_REQUIRED", f"{path}.used",
               f"{path}.used must be boolean")
        return
    if value.get("used") is True:
        _audit_source(value.get("source"), f"{path}.source", issues)
        _sha(value.get("parameters_sha256"), f"{path}.parameters_sha256", issues)
    else:
        if value.get("source") is not None or value.get("parameters_sha256") is not None:
            _issue(issues, "EMPIRICAL_SOURCE_CONTRADICTION", path,
                   f"{path} cannot carry source parameters when used=false")


def _audit_steps(value: Any, method_id: str | None,
                 species: Mapping[str, dict[str, Any]],
                 issues: list[dict[str, str]]) -> list[dict[str, Any]]:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or not value):
        _issue(issues, "ELEMENTARY_STEPS_REQUIRED", "elementary_steps",
               "elementary_steps must be a non-empty array")
        return []
    normalized = []
    ids = set()
    signatures = {}
    for index, raw in enumerate(value):
        path = f"elementary_steps[{index}]"
        if not _unknown_fields(raw, _STEP_KEYS, path, issues):
            continue
        step_id = _safe_identifier(raw.get("id"), f"{path}.id", issues)
        if step_id in ids:
            _issue(issues, "DUPLICATE_STEP_ID", f"{path}.id",
                   f"duplicate elementary-step id {step_id}")
        if step_id:
            ids.add(step_id)
        reactants = _state(raw.get("reactants"), f"{path}.reactants", issues, species)
        transition = _state(
            raw.get("transition_state"), f"{path}.transition_state", issues, species)
        products = _state(raw.get("products"), f"{path}.products", issues, species)
        signature = (
            tuple(sorted(reactants.items())), tuple(sorted(products.items())))
        reverse_signature = (signature[1], signature[0])
        if signature in signatures or reverse_signature in signatures:
            _issue(issues, "DUPLICATE_STEP", path,
                   "duplicate or reverse-duplicate elementary step")
        signatures[signature] = step_id
        if raw.get("reversible") is not True:
            _issue(issues, "REVERSE_BARRIER_REQUIRED", f"{path}.reversible",
                   "phase-1 detailed-balance audit requires reversible=true")
        delta_g = _audit_energy(raw.get("delta_g"), f"{path}.delta_g", issues,
                                method_id=method_id)
        barrier_f = _audit_energy(
            raw.get("forward_barrier"), f"{path}.forward_barrier", issues,
            method_id=method_id)
        barrier_r = _audit_energy(
            raw.get("reverse_barrier"), f"{path}.reverse_barrier", issues,
            method_id=method_id)
        _audit_prefactors(raw.get("prefactors"), f"{path}.prefactors", issues)
        _audit_empirical_model(raw.get("bep"), f"{path}.bep", issues)
        _audit_empirical_model(raw.get("scaling"), f"{path}.scaling", issues)
        _number(raw.get("uncertainty_eV"), f"{path}.uncertainty_eV", issues,
                minimum=0.0, code="STEP_UNCERTAINTY_REQUIRED")

        for field, code in (
                ("composition", "ELEMENT_NOT_CONSERVED"),
                ("sites", "SITE_NOT_CONSERVED")):
            left = _state_property(reactants, species, field)
            middle = _state_property(transition, species, field)
            right = _state_property(products, species, field)
            if not _same_mapping(left, middle) or not _same_mapping(left, right):
                _issue(issues, code, path,
                       f"{field} is not conserved across reactants/transition state/products")
        left_charge = _state_property(reactants, species, "charge")
        middle_charge = _state_property(transition, species, "charge")
        right_charge = _state_property(products, species, "charge")
        if (not _same_scalar(left_charge, middle_charge)
                or not _same_scalar(left_charge, right_charge)):
            _issue(issues, "CHARGE_NOT_CONSERVED", path,
                   "charge is not conserved across reactants/transition state/products")

        e_initial = _state_energy(reactants, species)
        e_ts = _state_energy(transition, species)
        e_final = _state_energy(products, species)
        if None not in {e_initial, e_ts, e_final}:
            expected_delta = e_final - e_initial
            expected_forward = e_ts - e_initial
            expected_reverse = e_ts - e_final
            for actual, expected, code, field in (
                    (delta_g, expected_delta, "REACTION_ENERGY_MISMATCH", "delta_g"),
                    (barrier_f, expected_forward, "FORWARD_BARRIER_MISMATCH",
                     "forward_barrier"),
                    (barrier_r, expected_reverse, "REVERSE_BARRIER_MISMATCH",
                     "reverse_barrier")):
                if actual is not None and abs(actual - expected) > _BARRIER_TOL_EV:
                    _issue(issues, code, f"{path}.{field}",
                           f"{field} disagrees with frozen state energies")
        if None not in {barrier_f, barrier_r, delta_g}:
            if abs((barrier_f - barrier_r) - delta_g) > _BARRIER_TOL_EV:
                _issue(issues, "DETAILED_BALANCE_MISMATCH", path,
                       "forward barrier - reverse barrier must equal delta_g")
            if barrier_f < -_BARRIER_TOL_EV or barrier_r < -_BARRIER_TOL_EV:
                _issue(issues, "NEGATIVE_BARRIER", path,
                       "forward and reverse barriers must be non-negative")
        normalized.append({
            **copy.deepcopy(dict(raw)), "_id": step_id,
            "_reactants": reactants, "_transition_state": transition,
            "_products": products,
        })
    return normalized


def _identifier_array(value: Any, path: str, issues: list[dict[str, str]],
                      species: Mapping[str, Any]) -> list[str]:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or not value):
        _issue(issues, "MECHANISM_BOUNDARY_REQUIRED", path,
               f"{path} must be a non-empty identifier array")
        return []
    result = []
    for index, raw in enumerate(value):
        identifier = _safe_identifier(raw, f"{path}[{index}]", issues)
        if identifier and identifier not in species:
            code = "UNKNOWN_TARGET_SPECIES" if path == "target_products" else "UNKNOWN_FEED_SPECIES"
            _issue(issues, code, f"{path}[{index}]",
                   f"{path} references unknown species {identifier}")
        if identifier and identifier in result:
            _issue(issues, "DUPLICATE_IDENTIFIER", f"{path}[{index}]",
                   f"duplicate identifier {identifier}")
        elif identifier:
            result.append(identifier)
    return result


def _audit_connectivity(feeds: Sequence[str], targets: Sequence[str],
                        steps: Sequence[Mapping[str, Any]], species: Mapping[str, Any],
                        issues: list[dict[str, str]]) -> None:
    reachable = set(feeds)
    changed = True
    while changed:
        changed = False
        for step in steps:
            reactants = set(step.get("_reactants") or {})
            if reactants and reactants <= reachable:
                additions = set(step.get("_products") or {}) - reachable
                if additions:
                    reachable.update(additions)
                    changed = True
    for target in targets:
        if target in species and target not in reachable:
            _issue(issues, "MISSING_MECHANISM_PATH", "target_products",
                   f"no elementary-step path connects feed species to {target}")
    used = set(feeds) | set(targets)
    for step in steps:
        used.update(step.get("_reactants") or {})
        used.update(step.get("_transition_state") or {})
        used.update(step.get("_products") or {})
    for species_id in sorted(set(species) - used):
        _issue(issues, "UNUSED_SPECIES", "species",
               f"species {species_id} is not used by the declared mechanism",
               severity="warning")


def audit_network(
        source: Mapping[str, Any] | KineticsInputProvider) -> dict[str, Any]:
    """Audit a frozen reaction/thermochemistry projection without solving it."""
    issues: list[dict[str, str]] = []
    try:
        network = _as_mapping(source)
    except KineticsContractError as exc:
        _issue(issues, "INVALID_INPUT", "$", str(exc))
        network = {}
    _unknown_fields(network, _TOP_LEVEL_KEYS, "$", issues)
    if network.get("schema") != NETWORK_SCHEMA:
        _issue(issues, "UNSUPPORTED_SCHEMA", "schema",
               f"schema must be {NETWORK_SCHEMA}")
    _safe_identifier(network.get("network_id"), "network_id", issues)
    _safe_identifier(network.get("revision"), "revision", issues)

    declared_hash = _sha(network.get("input_sha256"), "input_sha256", issues)
    computed_hash = None
    try:
        computed_hash = compute_input_sha256(network)
    except (TypeError, ValueError):
        _issue(issues, "NONFINITE_NUMBER", "$",
               "input must be canonical JSON without non-finite numbers")
    if declared_hash and computed_hash and declared_hash != computed_hash:
        _issue(issues, "INPUT_HASH_MISMATCH", "input_sha256",
               "declared input_sha256 does not match the exact frozen projection")

    projection = network.get("source_projection")
    projection_keys = frozenset({
        "schema", "version", "projection_sha256", "evidence_refs",
    })
    if _unknown_fields(projection, projection_keys, "source_projection", issues):
        _safe_text(projection.get("schema"), "source_projection.schema", issues)
        version = projection.get("version")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            _issue(issues, "UNSAFE_IDENTIFIER", "source_projection.version",
                   "source_projection.version must be a safe version token")
        _sha(projection.get("projection_sha256"),
             "source_projection.projection_sha256", issues)
        refs = projection.get("evidence_refs")
        if (isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence)
                or not refs or len(refs) > 256):
            _issue(issues, "SOURCE_EVIDENCE_REQUIRED", "source_projection.evidence_refs",
                   "source projection requires bounded evidence references")
        else:
            for index, reference in enumerate(refs):
                _safe_text(reference, f"source_projection.evidence_refs[{index}]", issues)

    _audit_assumptions(network.get("assumptions"), issues)
    _audit_standard_state(network.get("standard_state"), issues)
    operating = network.get("operating_range")
    ranges = {}
    if _unknown_fields(
            operating, frozenset({"temperature_K", "pressure_bar", "potential_V"}),
            "operating_range", issues):
        ranges["temperature_K"] = _audit_range(
            operating.get("temperature_K"), "operating_range.temperature_K", issues,
            minimum=1.0, maximum=5000.0)
        ranges["pressure_bar"] = _audit_range(
            operating.get("pressure_bar"), "operating_range.pressure_bar", issues,
            minimum=0.0, maximum=1.0e6)
        potential = operating.get("potential_V")
        ranges["potential_V"] = (None if potential is None else _audit_range(
            potential, "operating_range.potential_V", issues,
            minimum=-10.0, maximum=10.0))

    method_id = _audit_methodology(network.get("methodology"), issues)
    species = _audit_species(network.get("species"), method_id, issues)
    standard = network.get("standard_state") or {}
    standard_temperature = (standard.get("temperature") or {}).get("value")
    standard_pressure = (standard.get("pressure") or {}).get("value")
    if (isinstance(standard_temperature, (int, float))
            and not isinstance(standard_temperature, bool)
            and math.isfinite(float(standard_temperature))
            and ranges.get("temperature_K") is not None
            and not (ranges["temperature_K"][0] <= float(standard_temperature)
                     <= ranges["temperature_K"][1])):
        _issue(issues, "STANDARD_STATE_OUTSIDE_OPERATING_RANGE",
               "standard_state.temperature",
               "standard-state temperature is outside operating_range.temperature_K")
    if (isinstance(standard_pressure, (int, float))
            and not isinstance(standard_pressure, bool)
            and math.isfinite(float(standard_pressure))
            and ranges.get("pressure_bar") is not None
            and not (ranges["pressure_bar"][0] <= float(standard_pressure)
                     <= ranges["pressure_bar"][1])):
        _issue(issues, "STANDARD_STATE_OUTSIDE_OPERATING_RANGE",
               "standard_state.pressure",
               "standard-state pressure is outside operating_range.pressure_bar")
    methodology = network.get("methodology") or {}
    uses_potential = methodology.get("potential_model") != "none"
    if uses_potential and (
            standard.get("potential") is None or ranges.get("potential_V") is None):
        _issue(issues, "POTENTIAL_RANGE_REQUIRED", "operating_range.potential_V",
               "an electrochemical potential model requires a standard potential and range")
    if not uses_potential and (
            standard.get("potential") is not None or ranges.get("potential_V") is not None):
        _issue(issues, "POTENTIAL_MODEL_MISMATCH", "methodology.potential_model",
               "potential values require an explicit non-none potential model")
    gas_activities = [
        record.get("_activity") for record in species.values()
        if record.get("phase") == "gas" and record.get("_activity") is not None
    ]
    if (gas_activities and isinstance(standard_pressure, (int, float))
            and not isinstance(standard_pressure, bool)
            and math.isfinite(float(standard_pressure))
            and abs(sum(gas_activities) - float(standard_pressure)) > 1.0e-8):
        _issue(issues, "PARTIAL_PRESSURE_SUM_MISMATCH", "species.activity",
               "gas partial pressures must sum to standard_state.pressure")
    steps = _audit_steps(network.get("elementary_steps"), method_id, species, issues)
    feeds = _identifier_array(network.get("feed_species"), "feed_species", issues, species)
    targets = _identifier_array(
        network.get("target_products"), "target_products", issues, species)
    _audit_connectivity(feeds, targets, steps, species, issues)
    if not isinstance(network.get("extensions"), Mapping):
        _issue(issues, "INVALID_EXTENSIONS", "extensions", "extensions must be an object")

    errors = [item for item in issues if item["severity"] == "error"]
    warnings = [item for item in issues if item["severity"] == "warning"]
    machine_pass = not errors
    return {
        "schema": AUDIT_SCHEMA,
        "input_sha256": computed_hash,
        "declared_input_sha256": declared_hash,
        "source_projection_sha256": (
            projection.get("projection_sha256") if isinstance(projection, Mapping) else None),
        "machine_pass": machine_pass,
        "export_ready": machine_pass,
        "scientific_status": "diagnostic" if machine_pass else "unavailable",
        "eligible_final": False,
        "issues": issues,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "denominator": {
            "species": len(network.get("species") or [])
            if isinstance(network.get("species"), Sequence) else 0,
            "elementary_steps": len(network.get("elementary_steps") or [])
            if isinstance(network.get("elementary_steps"), Sequence) else 0,
            "checks": 12,
        },
        "model_boundaries": {
            "mean_field": True,
            "steady_state": True,
            "site_uniformity": "uniform",
            "lateral_interactions": "neglected_or_explicitly_unsupported",
            "mechanism_completeness_is_asserted_not_proven": True,
        },
        "report_limitation": {
            "result_kind": "diagnostic",
            "may_enter_accepted_or_final": False,
            "human_review_not_replaced": True,
        },
        "operating_range": ranges,
    }


def _strict_object(value: Any, allowed: set[str] | frozenset[str], path: str) -> dict:
    if not isinstance(value, Mapping):
        raise KineticsContractError(f"{path} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise KineticsContractError(
            f"{path} contains unknown fields: {', '.join(sorted(unknown))}")
    return dict(value)


def _result_identifier(value: Any, path: str) -> str:
    if (not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value)
            or _CONTROL_RE.search(value) or _PATH_RE.search(value)
            or _SECRET_RE.search(value)):
        raise KineticsContractError(f"{path} must be a safe identifier")
    return value


def _result_version(value: Any, path: str) -> str:
    if (not isinstance(value, str) or not _VERSION_RE.fullmatch(value)
            or _CONTROL_RE.search(value) or _SECRET_RE.search(value)):
        raise KineticsContractError(f"{path} must be a safe version token")
    return value


def _result_number(value: Any, path: str, *, minimum=None, maximum=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KineticsContractError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise KineticsContractError(f"{path} must be a finite number")
    if minimum is not None and result < minimum:
        raise KineticsContractError(f"{path} is below the allowed minimum")
    if maximum is not None and result > maximum:
        raise KineticsContractError(f"{path} is above the allowed maximum")
    return result


def _metric_list(value: Any, path: str, *, id_field: str, value_min=None,
                 value_max=None, extra_field: str | None = None) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise KineticsContractError(f"{path} must be an array")
    if len(value) > 10000:
        raise KineticsContractError(f"{path} contains too many records")
    records = []
    expected = {id_field, "value"} | ({extra_field} if extra_field else set())
    for index, raw in enumerate(value):
        item = _strict_object(raw, expected, f"{path}[{index}]")
        normalized = {
            id_field: _result_identifier(item.get(id_field), f"{path}[{index}].{id_field}"),
            "value": _result_number(
                item.get("value"), f"{path}[{index}].value",
                minimum=value_min, maximum=value_max),
        }
        if extra_field:
            normalized[extra_field] = _result_identifier(
                item.get(extra_field), f"{path}[{index}].{extra_field}")
        records.append(normalized)
    return records


def _in_range(value: float, bounds: Any) -> bool:
    return (isinstance(bounds, Sequence) and len(bounds) == 2
            and float(bounds[0]) <= value <= float(bounds[1]))


def _normalize_point(raw: Any, index: int, network: Mapping[str, Any]) -> dict[str, Any]:
    path = f"points[{index}]"
    keys = {
        "conditions", "tof", "coverage", "selectivity", "drc", "dsc",
        "reaction_order", "apparent_activation_energy", "free_energy_diagram",
        "convergence",
    }
    point = _strict_object(raw, keys, path)
    conditions = _strict_object(
        point.get("conditions"), {"temperature", "pressure", "potential"},
        f"{path}.conditions")
    temperature = _result_number(
        conditions.get("temperature"), f"{path}.conditions.temperature", minimum=1.0)
    pressure = _result_number(
        conditions.get("pressure"), f"{path}.conditions.pressure", minimum=0.0)
    potential = conditions.get("potential")
    if potential is not None:
        potential = _result_number(
            potential, f"{path}.conditions.potential", minimum=-10.0, maximum=10.0)
    ranges = network.get("operating_range") or {}
    if not _in_range(temperature, ranges.get("temperature_K")):
        raise KineticsContractError(
            f"{path}.conditions.temperature is outside the frozen operating range")
    if not _in_range(pressure, ranges.get("pressure_bar")):
        raise KineticsContractError(
            f"{path}.conditions.pressure is outside the frozen operating range")
    potential_range = ranges.get("potential_V")
    if potential is not None and not _in_range(potential, potential_range):
        raise KineticsContractError(
            f"{path}.conditions.potential is outside the frozen operating range")
    if potential is None and potential_range is not None:
        raise KineticsContractError(
            f"{path}.conditions.potential is required by the frozen operating range")

    tof = _metric_list(point.get("tof"), f"{path}.tof", id_field="species_id")
    coverage = _metric_list(
        point.get("coverage"), f"{path}.coverage", id_field="species_id",
        value_min=0.0, value_max=1.0, extra_field="site_type")
    selectivity = _metric_list(
        point.get("selectivity"), f"{path}.selectivity", id_field="species_id",
        value_min=0.0, value_max=1.0)
    drc = _metric_list(
        point.get("drc"), f"{path}.drc", id_field="step_id",
        value_min=-1000.0, value_max=1000.0)
    dsc = _metric_list(
        point.get("dsc"), f"{path}.dsc", id_field="step_id",
        value_min=-1000.0, value_max=1000.0)
    reaction_order = _metric_list(
        point.get("reaction_order"), f"{path}.reaction_order",
        id_field="species_id", value_min=-1000.0, value_max=1000.0)
    apparent = _metric_list(
        point.get("apparent_activation_energy"),
        f"{path}.apparent_activation_energy", id_field="species_id",
        value_min=-1000.0, value_max=1000.0)
    free_energy = _metric_list(
        point.get("free_energy_diagram"), f"{path}.free_energy_diagram",
        id_field="state_id", value_min=-1.0e6, value_max=1.0e6)
    known_species = {
        str(record.get("id")) for record in network.get("species") or []
        if isinstance(record, Mapping) and record.get("id")
    }
    known_steps = {
        str(record.get("id")) for record in network.get("elementary_steps") or []
        if isinstance(record, Mapping) and record.get("id")
    }
    known_sites = {
        str(site) for record in network.get("species") or []
        if isinstance(record, Mapping) for site in (record.get("sites") or {})
    }
    for metric_name, records in (
            ("tof", tof), ("coverage", coverage), ("selectivity", selectivity),
            ("reaction_order", reaction_order),
            ("apparent_activation_energy", apparent)):
        unknown = [record["species_id"] for record in records
                   if record["species_id"] not in known_species]
        if unknown:
            raise KineticsContractError(
                f"{path}.{metric_name} references species outside the frozen network")
    for metric_name, records in (("drc", drc), ("dsc", dsc)):
        if any(record["step_id"] not in known_steps for record in records):
            raise KineticsContractError(
                f"{path}.{metric_name} references steps outside the frozen network")
    if any(record["site_type"] not in known_sites for record in coverage):
        raise KineticsContractError(
            f"{path}.coverage references site types outside the frozen network")
    site_totals = defaultdict(float)
    for record in coverage:
        site_totals[record["site_type"]] += record["value"]
    if any(total > 1.0 + 1.0e-6 for total in site_totals.values()):
        raise KineticsContractError(f"{path}.coverage exceeds unit site coverage")
    if sum(record["value"] for record in selectivity) > 1.0 + 1.0e-6:
        raise KineticsContractError(f"{path}.selectivity exceeds one")

    convergence = _strict_object(
        point.get("convergence"), {"converged", "residual", "iterations", "solver"},
        f"{path}.convergence")
    if not isinstance(convergence.get("converged"), bool):
        raise KineticsContractError(f"{path}.convergence.converged must be boolean")
    residual = _result_number(
        convergence.get("residual"), f"{path}.convergence.residual", minimum=0.0)
    iterations = convergence.get("iterations")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise KineticsContractError(
            f"{path}.convergence.iterations must be a non-negative integer")
    solver = _result_identifier(
        convergence.get("solver"), f"{path}.convergence.solver")
    return {
        "conditions": {
            "temperature": temperature, "pressure": pressure, "potential": potential,
        },
        "tof": tof, "coverage": coverage, "selectivity": selectivity,
        "drc": drc, "dsc": dsc, "reaction_order": reaction_order,
        "apparent_activation_energy": apparent,
        "free_energy_diagram": free_energy,
        "convergence": {
            "converged": convergence["converged"], "residual": residual,
            "iterations": iterations, "solver": solver,
        },
    }


def _normalize_sensitivity(value: Any) -> dict[str, Any]:
    result = _strict_object(value, {"status", "analyses", "warnings"}, "sensitivity")
    status = result.get("status")
    if status not in {"passed", "warning", "unavailable"}:
        raise KineticsContractError("sensitivity.status is unsupported")
    analyses = result.get("analyses")
    if isinstance(analyses, (str, bytes)) or not isinstance(analyses, Sequence):
        raise KineticsContractError("sensitivity.analyses must be an array")
    normalized = []
    for index, raw in enumerate(analyses):
        item = _strict_object(
            raw, {"kind", "max_relative_change"}, f"sensitivity.analyses[{index}]")
        normalized.append({
            "kind": _result_identifier(
                item.get("kind"), f"sensitivity.analyses[{index}].kind"),
            "max_relative_change": _result_number(
                item.get("max_relative_change"),
                f"sensitivity.analyses[{index}].max_relative_change", minimum=0.0),
        })
    warnings = result.get("warnings")
    if isinstance(warnings, (str, bytes)) or not isinstance(warnings, Sequence):
        raise KineticsContractError("sensitivity.warnings must be an array")
    safe_warnings = []
    for index, warning in enumerate(warnings):
        local_issues: list[dict[str, str]] = []
        text = _safe_text(warning, f"sensitivity.warnings[{index}]", local_issues)
        if text is None:
            raise KineticsContractError(
                f"sensitivity.warnings[{index}] must be bounded safe text")
        safe_warnings.append(text)
    return {"status": status, "analyses": normalized, "warnings": safe_warnings}


def import_result(
        result: Mapping[str, Any],
        network_source: Mapping[str, Any] | KineticsInputProvider, *,
        expected_adapter: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an external result; never trust browser numerics."""
    network = _as_mapping(network_source)
    audit = audit_network(network)
    if audit["export_ready"] is not True:
        raise KineticsContractError("frozen network failed the kinetics input audit")
    payload = _strict_object(result, {
        "schema", "input_sha256", "adapter", "units", "points", "sensitivity",
    }, "result")
    if payload.get("schema") != RESULT_SCHEMA:
        raise KineticsContractError(f"result schema must be {RESULT_SCHEMA}")
    if payload.get("input_sha256") != audit["input_sha256"]:
        raise KineticsContractError("result input hash does not match the frozen network")
    units = _strict_object(payload.get("units"), set(RESULT_UNITS), "result.units")
    if units != RESULT_UNITS:
        raise KineticsContractError("result units do not match the normalized unit contract")
    adapter = _strict_object(
        payload.get("adapter"), {"id", "version", "tool_version", "tool_sha256"},
        "result.adapter")
    expected = _strict_object(
        expected_adapter, {"id", "version", "tool_sha256"}, "expected_adapter")
    for key in ("id", "version"):
        if adapter.get(key) != expected.get(key):
            raise KineticsContractError(f"result adapter {key} mismatch")
    if adapter.get("tool_sha256") != expected.get("tool_sha256"):
        raise KineticsContractError("result tool hash mismatch")
    _result_identifier(adapter.get("id"), "result.adapter.id")
    _result_version(adapter.get("version"), "result.adapter.version")
    _result_version(adapter.get("tool_version"), "result.adapter.tool_version")
    if not isinstance(adapter.get("tool_sha256"), str) or not _HEX_RE.fullmatch(
            adapter["tool_sha256"]):
        raise KineticsContractError("result.adapter.tool_sha256 must be SHA-256")

    points = payload.get("points")
    if (isinstance(points, (str, bytes)) or not isinstance(points, Sequence)
            or not 1 <= len(points) <= 10000):
        raise KineticsContractError("result.points must contain 1 to 10000 points")
    normalized_points = [
        _normalize_point(point, index, network) for index, point in enumerate(points)
    ]
    sensitivity = _normalize_sensitivity(payload.get("sensitivity"))
    converged = all(
        point["convergence"]["converged"] for point in normalized_points)
    sensitivity_available = sensitivity["status"] != "unavailable"
    reason_codes = []
    if not converged:
        reason_codes.append("NUMERICAL_NOT_CONVERGED")
    if not sensitivity_available:
        reason_codes.append("SENSITIVITY_UNAVAILABLE")
    return {
        "schema": NORMALIZED_RESULT_SCHEMA,
        "input_sha256": audit["input_sha256"],
        "adapter": copy.deepcopy(adapter),
        "units": copy.deepcopy(RESULT_UNITS),
        "scientific_status": "diagnostic",
        "eligible_final": False,
        "available": converged and sensitivity_available,
        "reason_codes": reason_codes,
        "points": normalized_points,
        "sensitivity": sensitivity,
        "denominator": {
            "condition_points": len(normalized_points),
            "converged_points": sum(
                1 for point in normalized_points
                if point["convergence"]["converged"]),
        },
        "limitations": {
            "mean_field": True,
            "steady_state": True,
            "uniform_sites": True,
            "lateral_interactions": (
                (network.get("assumptions") or {}).get("lateral_interactions")),
            "mechanism_completeness": (
                (network.get("assumptions") or {}).get("mechanism_completeness")),
            "browser_solves": False,
            "diagnostic_only": True,
            "may_enter_accepted_or_final": False,
        },
    }


__all__ = [
    "AUDIT_SCHEMA", "KineticsContractError", "KineticsInputProvider",
    "NETWORK_SCHEMA", "NORMALIZED_RESULT_SCHEMA", "RESULT_SCHEMA", "RESULT_UNITS",
    "audit_network", "compute_input_sha256", "import_result",
]
