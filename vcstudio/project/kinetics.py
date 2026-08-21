"""Fail-closed microkinetic projection audit and result normalization.

This module deliberately does not define or persist a reaction-network domain
model.  The Reaction Map/Catalysis Model layers remain authoritative.  Kinetics
accepts only a server-injected structural ``KineticsInputProvider`` and audits
one evidence-resolved frozen projection before an external adapter may use it.

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
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from vcstudio.project.kinetics_model_spec import (
    MAX_EVIDENCE_BINDINGS,
    KineticsModelSpec,
    classify_sensitive_text,
)


NETWORK_SCHEMA = "vcstudio.kinetics-network/v3"
AUDIT_SCHEMA = "vcstudio.kinetics-audit/v2"
RESULT_SCHEMA = "vcstudio.kinetics-result/v2"
NORMALIZED_RESULT_SCHEMA = "vcstudio.kinetics-normalized-result/v2"
SOURCE_PROJECTION_PROTOCOLS = frozenset({
    ("vcstudio.frozen-reaction-network/v2", "2"),
    ("vcstudio.reaction-domain-projection/v1", "1"),
})
_SOURCE_PROJECTION_HASH_FIELDS = {
    ("vcstudio.frozen-reaction-network/v2", "2"): "frozen_network_sha256",
    ("vcstudio.reaction-domain-projection/v1", "1"): "projection_sha256",
    ("vcstudio.kinetics-adapted-reaction-source/v1", "1"):
        "adapter_projection_sha256",
}
CONVERGENCE_RESIDUAL_MAX = 1.0e-8
CONVERGENCE_ITERATIONS_MAX = 1_000_000
SIGNIFICANT_IMAGINARY_FREQUENCY_CM1 = 50.0
_FREE_ENERGY_AGGREGATE_STATES = frozenset({"reactants", "products"})

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
    "model_spec", "rate_law_policy", "assumptions", "standard_state",
    "operating_range", "methodology", "feed_species", "target_products",
    "species", "elementary_steps", "site_population_totals", "extensions",
})
_SPECIES_KEYS = frozenset({
    "id", "phase", "composition", "charge", "sites", "formation_energy",
    "frequencies_cm1", "activity", "standard_state",
})
_STEP_KEYS = frozenset({
    "id", "reactants", "transition_state", "products", "reversible", "delta_g",
    "forward_barrier", "reverse_barrier", "prefactors", "bep", "scaling",
    "uncertainty_eV", "evidence",
})
_ENERGY_KEYS = frozenset({
    "value", "unit", "method_id", "source", "uncertainty_eV",
})
_SOURCE_KEYS = frozenset({"kind", "reference", "evidence_sha256"})
_MODEL_SPEC_KEYS = frozenset({
    "schema", "project_id", "spec_id", "revision", "spec_sha256",
    "source_projection_sha256", "evidence_refs",
})
_RATE_LAW_POLICY_KEYS = frozenset({
    "activity", "reversibility", "detailed_balance", "prefactor",
    "electrochemical", "reactor",
})
_SITE_TOTAL_KEYS = frozenset({
    "site_type", "value", "unit", "basis", "evidence",
})
_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_ELEMENT_RE = re.compile(r"^[A-Z][a-z]?$|^e-$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BARRIER_TOL_EV = 1.0e-3
_BALANCE_TOL = 1.0e-8
_MAX_TOTAL_EVIDENCE_BYTES = 64 * 1024 * 1024
_CANONICAL_INPUT_SEAL = object()


class KineticsContractError(ValueError):
    """A frozen input or imported result violates the public contract."""


@runtime_checkable
class KineticsInputProvider(Protocol):
    """Structural seam implemented by an upstream frozen projection adapter."""

    def kinetics_input(self) -> Mapping[str, Any]:
        """Return one immutable-by-convention kinetics projection mapping."""

    def kinetics_evidence(self, reference: str) -> bytes:
        """Resolve an opaque evidence reference to the authoritative bytes."""

    def canonical_source_projection(self) -> Mapping[str, Any]:
        """Return the authoritative upstream frozen projection before adaptation."""

    def kinetics_model_spec(self) -> Mapping[str, Any]:
        """Return the exact immutable model-spec snapshot used by the projection."""


class CanonicalKineticsInput(Mapping[str, Any]):
    """Ephemeral verified projection; never a second persistent domain DTO."""

    def __init__(self, value: Mapping[str, Any], *, _seal=None):
        if _seal is not _CANONICAL_INPUT_SEAL:
            raise KineticsContractError(
                "canonical kinetics inputs may only be created by a trusted provider")
        self._value = copy.deepcopy(dict(value))

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(self._value[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def to_mapping(self) -> dict[str, Any]:
        return copy.deepcopy(self._value)


def _as_mapping(source: CanonicalKineticsInput | KineticsInputProvider) -> dict[str, Any]:
    return canonicalize_kinetics_input(source).to_mapping()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_input_sha256(
        source: Mapping[str, Any] | KineticsInputProvider) -> str:
    """Hash an exact adapter projection, excluding its self-declared hash."""
    if isinstance(source, CanonicalKineticsInput):
        value = source.to_mapping()
    elif isinstance(source, Mapping):
        value = copy.deepcopy(dict(source))
    else:
        value = canonicalize_kinetics_input(source).to_mapping()
    value.pop("input_sha256", None)
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def compute_source_projection_sha256(source: Mapping[str, Any]) -> str:
    """Hash an authoritative upstream projection under its whitelisted policy."""
    if not isinstance(source, Mapping):
        raise KineticsContractError("source projection hash input must be a mapping")
    value = copy.deepcopy(dict(source))
    identity = (value.get("schema"), value.get("version"))
    hash_field = _SOURCE_PROJECTION_HASH_FIELDS.get(identity)
    if hash_field is None:
        raise KineticsContractError("source projection schema/version is not trusted")
    value.pop(hash_field, None)
    try:
        return hashlib.sha256(_canonical_bytes(value)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise KineticsContractError(
            "projection must be canonical JSON without non-finite numbers") from exc


def _authoritative_source_identity(
        upstream: Mapping[str, Any], adapter_hash: str,
        ) -> tuple[tuple[str, str], str]:
    identity = (upstream.get("schema"), upstream.get("version"))
    if identity != ("vcstudio.kinetics-adapted-reaction-source/v1", "1"):
        return (str(identity[0]), str(identity[1])), adapter_hash
    authority = upstream.get("authoritative_source")
    if (not isinstance(authority, Mapping)
            or set(authority) != {
                "schema", "version", "frozen_network_sha256"}
            or authority.get("schema") != "vcstudio.frozen-reaction-network/v2"
            or authority.get("version") != "2"
            or not isinstance(authority.get("frozen_network_sha256"), str)
            or not _HEX_RE.fullmatch(authority["frozen_network_sha256"])):
        raise KineticsContractError(
            "adapted source does not bind an exact frozen reaction v2 authority")
    return (
        ("vcstudio.frozen-reaction-network/v2", "2"),
        authority["frozen_network_sha256"],
    )


def compute_projection_sha256(source: KineticsInputProvider) -> str:
    """Compatibility name for hashing a provider's authoritative projection."""
    loader = getattr(source, "canonical_source_projection", None)
    if not callable(loader):
        raise KineticsContractError(
            "projection hash requires a trusted KineticsInputProvider")
    upstream = loader()
    computed = compute_source_projection_sha256(upstream)
    _, authority_hash = _authoritative_source_identity(upstream, computed)
    return authority_hash


def compute_condition_sha256(conditions: Mapping[str, Any]) -> str:
    """Bind a sensitivity record to one exact normalized condition point."""
    if not isinstance(conditions, Mapping):
        raise KineticsContractError("conditions must be a mapping")
    try:
        return hashlib.sha256(_canonical_bytes(dict(conditions))).hexdigest()
    except (TypeError, ValueError) as exc:
        raise KineticsContractError(
            "conditions must be canonical JSON without non-finite numbers") from exc


def canonicalize_kinetics_input(
        source: CanonicalKineticsInput | KineticsInputProvider,
        ) -> CanonicalKineticsInput:
    """Verify one source/spec/evidence snapshot through the trusted seam."""
    if isinstance(source, CanonicalKineticsInput):
        return source
    input_loader = getattr(source, "kinetics_input", None)
    evidence_loader = getattr(source, "kinetics_evidence", None)
    source_loader = getattr(source, "canonical_source_projection", None)
    spec_loader = getattr(source, "kinetics_model_spec", None)
    if (not callable(input_loader) or not callable(evidence_loader)
            or not callable(source_loader) or not callable(spec_loader)):
        if isinstance(source, Mapping):
            raise KineticsContractError(
                "raw kinetics mappings are untrusted; a KineticsInputProvider is required")
        raise KineticsContractError(
            "trusted KineticsInputProvider must supply exact source, spec, input, "
            "and evidence snapshots")
    try:
        upstream_raw = source_loader()
    except Exception as exc:  # noqa: BLE001 trusted seam must fail closed
        raise KineticsContractError(
            "canonical source projection could not be resolved") from exc
    if not isinstance(upstream_raw, Mapping):
        raise KineticsContractError(
            "canonical_source_projection must return a mapping")
    upstream = copy.deepcopy(dict(upstream_raw))
    upstream_identity = (upstream.get("schema"), upstream.get("version"))
    upstream_hash_field = _SOURCE_PROJECTION_HASH_FIELDS.get(upstream_identity)
    if upstream_hash_field is None:
        raise KineticsContractError("source projection schema/version is not trusted")
    upstream_declared_hash = upstream.get(upstream_hash_field)
    upstream_computed_hash = compute_source_projection_sha256(upstream)
    if upstream_declared_hash != upstream_computed_hash:
        raise KineticsContractError(
            "authoritative source projection hash does not match its canonical bytes")
    source_projection_identity, source_authority_hash = (
        _authoritative_source_identity(upstream, upstream_computed_hash))
    try:
        spec_raw = spec_loader()
    except Exception as exc:  # noqa: BLE001 trusted seam must fail closed
        raise KineticsContractError("kinetics model spec could not be resolved") from exc
    if not isinstance(spec_raw, Mapping):
        raise KineticsContractError("kinetics_model_spec must return a mapping")
    try:
        spec = KineticsModelSpec.from_dict(spec_raw)
        spec_evidence_refs = spec.evidence_bindings()
    except Exception as exc:  # noqa: BLE001 translate the strict DTO boundary
        raise KineticsContractError("kinetics model spec snapshot is invalid") from exc
    source_binding_checks = {
        "project_id": (upstream.get("project_id"), spec.project_id),
        "domain_authority_id": (
            upstream.get("domain_authority_id"),
            spec.source_binding["domain_authority_id"]),
        "domain_generation": (
            upstream.get("domain_generation"),
            spec.source_binding["domain_generation"]),
        "network_revision": (
            upstream.get("network_revision"),
            spec.source_binding["network_revision"]),
        "source_projection_sha256": (
            source_authority_hash,
            spec.source_binding["source_projection_sha256"]),
    }
    if upstream_identity == ("vcstudio.kinetics-adapted-reaction-source/v1", "1"):
        source_binding_checks["network_id"] = (
            upstream.get("network_id"), spec.source_binding.get("network_id"))
    elif "network_id" in spec.source_binding:
        source_binding_checks["network_id"] = (
            upstream.get("network_id"), spec.source_binding["network_id"])
    for field, (authoritative, declared) in source_binding_checks.items():
        if authoritative != declared:
            raise KineticsContractError(
                f"kinetics model spec {field} binding is stale or forged")
    try:
        raw = input_loader()
    except Exception as exc:  # noqa: BLE001 trusted seam must fail closed
        raise KineticsContractError("kinetics projection could not be resolved") from exc
    if not isinstance(raw, Mapping):
        raise KineticsContractError("KineticsInputProvider must return a mapping")
    value = copy.deepcopy(dict(raw))
    if ("network_id" in upstream
            and value.get("network_id") != upstream.get("network_id")):
        raise KineticsContractError(
            "kinetics network_id does not match the authoritative source projection")
    projection = value.get("source_projection")
    if not isinstance(projection, Mapping):
        raise KineticsContractError("source_projection must be an object")
    projection = dict(projection)
    if set(projection) != {
            "schema", "version", "projection_sha256", "evidence_refs"}:
        raise KineticsContractError("source_projection fields do not match the protocol")
    identity = (projection.get("schema"), projection.get("version"))
    if identity not in SOURCE_PROJECTION_PROTOCOLS:
        raise KineticsContractError("source projection schema/version is not trusted")
    if identity != source_projection_identity:
        raise KineticsContractError(
            "adapted source projection identity does not match the provider source")
    refs = projection.get("evidence_refs")
    if (isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence)
            or not 1 <= len(refs) <= MAX_EVIDENCE_BINDINGS):
        raise KineticsContractError(
            "source projection requires bounded artifact evidence references")
    seen: set[str] = set()
    source_seen: set[str] = set()
    model_seen: set[str] = set()
    declared: dict[str, str] = {}
    resolved_artifacts: dict[str, str] = {}
    resolved_total_bytes = 0

    def resolve(reference: str, digest: str) -> None:
        nonlocal resolved_total_bytes
        prior = resolved_artifacts.get(reference)
        if prior is not None:
            if prior != digest:
                raise KineticsContractError(
                    f"conflicting artifact hashes for evidence reference {reference}")
            return
        try:
            artifact = evidence_loader(reference)
        except Exception as exc:  # noqa: BLE001 opaque evidence boundary
            raise KineticsContractError(
                f"evidence reference {reference} could not be resolved") from exc
        if not isinstance(artifact, (bytes, bytearray, memoryview)):
            raise KineticsContractError(
                "trusted evidence resolver must return artifact bytes")
        artifact_bytes = bytes(artifact)
        if not artifact_bytes or len(artifact_bytes) > _MAX_TOTAL_EVIDENCE_BYTES:
            raise KineticsContractError("resolved evidence artifact has an invalid size")
        resolved_total_bytes += len(artifact_bytes)
        if resolved_total_bytes > _MAX_TOTAL_EVIDENCE_BYTES:
            raise KineticsContractError(
                "resolved evidence artifacts exceed the total byte limit")
        actual = hashlib.sha256(artifact_bytes).hexdigest()
        if actual != digest:
            raise KineticsContractError(
                f"resolved evidence artifact hash mismatch for {reference}")
        resolved_artifacts[reference] = actual

    def declare(item: Any, path: str, scope_seen: set[str]) -> None:
        if not isinstance(item, Mapping) or set(item) != {
                "reference", "artifact_sha256"}:
            raise KineticsContractError(f"{path} is invalid")
        reference = item.get("reference")
        digest = item.get("artifact_sha256")
        local_issues: list[dict[str, str]] = []
        if _safe_text(reference, f"{path}.reference", local_issues) is None:
            raise KineticsContractError(f"{path}.reference is unsafe")
        if reference in scope_seen:
            raise KineticsContractError(
                f"{path}.reference duplicates an evidence declaration")
        scope_seen.add(reference)
        if reference in seen:
            if declared.get(reference) != digest:
                raise KineticsContractError(
                    f"evidence reference {reference} has conflicting declared hashes")
            return
        seen.add(reference)
        if len(seen) > MAX_EVIDENCE_BINDINGS:
            raise KineticsContractError(
                "source/spec evidence chain exceeds the binding limit")
        if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest):
            raise KineticsContractError(f"{path}.artifact_sha256 is invalid")
        declared[reference] = digest

    for index, item in enumerate(refs):
        declare(
            item, f"source_projection.evidence_refs[{index}]", source_seen)

    model_spec = value.get("model_spec")
    if not isinstance(model_spec, Mapping) or set(model_spec) != _MODEL_SPEC_KEYS:
        raise KineticsContractError("model_spec fields do not match the protocol")
    model_refs = model_spec.get("evidence_refs")
    if (isinstance(model_refs, (str, bytes)) or not isinstance(model_refs, Sequence)
            or not model_refs or len(model_refs) > MAX_EVIDENCE_BINDINGS):
        raise KineticsContractError("model_spec requires bounded evidence references")
    for index, item in enumerate(model_refs):
        declare(item, f"model_spec.evidence_refs[{index}]", model_seen)
    if copy.deepcopy(list(model_refs)) != copy.deepcopy(spec_evidence_refs):
        raise KineticsContractError(
            "adapted model-spec evidence references do not match the exact spec")
    expected_metadata = {
        "schema": spec.schema,
        "project_id": spec.project_id,
        "spec_id": spec.spec_id,
        "revision": spec.revision,
        "spec_sha256": spec.semantic_sha256,
        "source_projection_sha256": source_authority_hash,
        "evidence_refs": copy.deepcopy(spec_evidence_refs),
    }
    if copy.deepcopy(dict(model_spec)) != expected_metadata:
        raise KineticsContractError(
            "adapted model_spec does not match the exact provider spec snapshot")
    if spec.source_binding["source_projection_sha256"] != source_authority_hash:
        raise KineticsContractError(
            "kinetics model spec is stale for the authoritative source projection")

    def exact(left: Any, right: Any, message: str) -> None:
        try:
            matches = _canonical_bytes(copy.deepcopy(left)) == _canonical_bytes(
                copy.deepcopy(right))
        except (TypeError, ValueError) as exc:
            raise KineticsContractError(message) from exc
        if not matches:
            raise KineticsContractError(message)

    exact(value.get("rate_law_policy"), spec.rate_law_policy,
          "rate_law_policy does not match the exact model spec")
    exact(value.get("assumptions"), spec.assumptions,
          "assumptions do not match the exact model spec")
    exact(value.get("target_products"), spec.target_products,
          "target_products do not match the exact model spec")
    exact(value.get("site_population_totals"), spec.site_population_totals,
          "site_population_totals do not match the exact model spec")
    species_records = value.get("species")
    if (isinstance(species_records, (str, bytes))
            or not isinstance(species_records, Sequence)):
        raise KineticsContractError("species must be an array")
    species_by_id = {
        item.get("id"): item for item in species_records if isinstance(item, Mapping)
    }
    if len(species_by_id) != len(species_records):
        raise KineticsContractError("species ids must be unique")
    expected_feeds = []
    for reservoir in spec.feed_reservoirs:
        species_record = species_by_id.get(reservoir["species_id"])
        if species_record is None:
            raise KineticsContractError(
                "model-spec feed reservoir species is absent from the projection")
        if float(reservoir["activity"]) > 0.0:
            expected_feeds.append(reservoir["species_id"])
        if species_record.get("phase") in {"surface", "adsorbate"}:
            if species_record.get("activity") is not None:
                raise KineticsContractError(
                    "surface-state reservoirs must not become fluid activity records")
            continue
        expected_activity = {
            "value": reservoir["activity"],
            "unit": reservoir["unit"],
            "source": {
                "kind": "condition",
                "reference": reservoir["source"],
                "evidence_sha256": reservoir["evidence_sha256"],
            },
        }
        exact(species_record.get("activity"), expected_activity,
              "species activity does not match the exact model spec")
    exact(value.get("feed_species"), expected_feeds,
          "feed_species do not match the exact model spec reservoirs")
    step_records = value.get("elementary_steps")
    if (isinstance(step_records, (str, bytes))
            or not isinstance(step_records, Sequence)):
        raise KineticsContractError("elementary_steps must be an array")
    steps_by_id = {
        item.get("id"): item for item in step_records if isinstance(item, Mapping)
    }
    if len(steps_by_id) != len(step_records):
        raise KineticsContractError("elementary step ids must be unique")
    for decision in spec.steps:
        step = steps_by_id.get(decision["step_id"])
        if step is None:
            raise KineticsContractError(
                "model-spec step is absent from the reaction projection")
        for field in ("prefactors", "bep", "scaling", "uncertainty_eV", "evidence"):
            exact(step.get(field), decision[field],
                  f"elementary step {field} does not match the exact model spec")

    source_revision = upstream.get("network_revision", upstream.get("revision"))
    if (value.get("network_id") != upstream.get("network_id")
            or value.get("revision") != source_revision):
        raise KineticsContractError(
            "network identity does not match the authoritative source projection")
    for field in ("standard_state", "operating_range", "methodology", "extensions"):
        exact(value.get(field), upstream.get(field),
              f"{field} does not match the authoritative source projection")
    source_species = upstream.get("species")
    if isinstance(source_species, Sequence) and not isinstance(source_species, (str, bytes)):
        stripped_species = []
        for record in species_records:
            copied = copy.deepcopy(dict(record))
            copied.pop("activity", None)
            stripped_species.append(copied)
        exact(stripped_species, source_species,
              "species facts do not match the authoritative source projection")
    source_steps = upstream.get("elementary_steps")
    if isinstance(source_steps, Sequence) and not isinstance(source_steps, (str, bytes)):
        stripped_steps = []
        for record in step_records:
            copied = copy.deepcopy(dict(record))
            for field in ("prefactors", "bep", "scaling", "uncertainty_eV", "evidence"):
                copied.pop(field, None)
            stripped_steps.append(copied)
        exact(stripped_steps, source_steps,
              "reaction step facts do not match the authoritative source projection")

    def verify_source_records(node: Any, path: str = "$") -> None:
        if isinstance(node, Mapping):
            if set(node) == _SOURCE_KEYS:
                reference = node.get("reference")
                digest = node.get("evidence_sha256")
                if not isinstance(reference, str) or reference not in declared:
                    raise KineticsContractError(
                        f"{path}.reference is not declared by the source/spec evidence chain")
                if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest):
                    raise KineticsContractError(f"{path}.evidence_sha256 is invalid")
                if declared[reference] != digest:
                    raise KineticsContractError(
                        f"{path}.evidence_sha256 conflicts with its declared hash")
            for key, item in node.items():
                verify_source_records(item, f"{path}.{key}")
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for index, item in enumerate(node):
                verify_source_records(item, f"{path}[{index}]")

    verify_source_records(value)
    for reference, digest in sorted(declared.items()):
        resolve(reference, digest)
    if projection.get("projection_sha256") != source_authority_hash:
        raise KineticsContractError(
            "source projection hash does not match the canonical provider projection")
    if copy.deepcopy(list(refs)) != copy.deepcopy(upstream.get("evidence_refs")):
        raise KineticsContractError(
            "adapted evidence references do not match the provider source projection")
    computed_input = compute_input_sha256(value)
    if value.get("input_sha256") != computed_input:
        raise KineticsContractError(
            "input hash does not match the canonical provider projection")
    return CanonicalKineticsInput(value, _seal=_CANONICAL_INPUT_SEAL)


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
    for key in sorted(set(value) - allowed, key=lambda item: (type(item).__name__, str(item))):
        if (not isinstance(key, str) or not key or len(key) > 128
                or _CONTROL_RE.search(key)
                or classify_sensitive_text(key) is not None):
            _issue(issues, "UNKNOWN_FIELD", path,
                   f"{path} contains an unknown or unsafe field")
        else:
            _issue(issues, "UNKNOWN_FIELD", f"{path}.{key}",
                   f"unknown field {path}.{key}")
    return True


def _safe_identifier(value: Any, path: str, issues: list[dict[str, str]]) -> str | None:
    if (not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value)
            or _CONTROL_RE.search(value)
            or classify_sensitive_text(value) is not None):
        _issue(issues, "UNSAFE_IDENTIFIER", path,
               f"{path} must be a safe opaque identifier")
        return None
    return value


def _safe_text(value: Any, path: str, issues: list[dict[str, str]],
               *, maximum: int = 512) -> str | None:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or _CONTROL_RE.search(value)
            or classify_sensitive_text(value) is not None):
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


def _audit_evidence_list(value: Any, path: str,
                         issues: list[dict[str, str]]) -> None:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or not value or len(value) > 256):
        _issue(issues, "MODEL_EVIDENCE_REQUIRED", path,
               f"{path} must be a non-empty bounded evidence array")
        return
    seen = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        _audit_source(item, item_path, issues)
        if isinstance(item, Mapping):
            identity = (item.get("reference"), item.get("evidence_sha256"))
            if identity in seen:
                _issue(issues, "DUPLICATE_EVIDENCE", item_path,
                       "evidence records must be unique")
            seen.add(identity)


def _audit_rate_law_policy(value: Any,
                           issues: list[dict[str, str]]) -> dict[str, str]:
    if not _unknown_fields(value, _RATE_LAW_POLICY_KEYS, "rate_law_policy", issues):
        return {}
    policy = {}
    supported = {
        "activity": "ideal",
        "reversibility": "explicit_reverse",
        "detailed_balance": "enforced",
        "prefactor": "explicit_per_step",
        "reactor": "mean_field_steady_state",
    }
    for key, expected in supported.items():
        current = _safe_identifier(
            value.get(key), f"rate_law_policy.{key}", issues)
        if current is not None:
            policy[key] = current
            if current != expected:
                code = ("NONIDEAL_ACTIVITY_UNSUPPORTED" if key == "activity"
                        else "RATE_LAW_POLICY_UNSUPPORTED")
                _issue(issues, code, f"rate_law_policy.{key}",
                       f"rate_law_policy.{key} must be {expected}")
    electrochemical = _safe_identifier(
        value.get("electrochemical"), "rate_law_policy.electrochemical", issues)
    if electrochemical is not None:
        policy["electrochemical"] = electrochemical
        if electrochemical not in {"none", "explicit_potential"}:
            _issue(issues, "RATE_LAW_POLICY_UNSUPPORTED",
                   "rate_law_policy.electrochemical",
                   "electrochemical policy must be none or explicit_potential")
    return policy


def _audit_site_population_totals(
        value: Any, species: Mapping[str, Mapping[str, Any]],
        issues: list[dict[str, str]]) -> dict[str, float]:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or not value or len(value) > 256):
        _issue(issues, "SITE_POPULATION_TOTALS_REQUIRED", "site_population_totals",
               "site_population_totals must be a non-empty bounded array")
        return {}
    totals = {}
    for index, raw in enumerate(value):
        path = f"site_population_totals[{index}]"
        if not _unknown_fields(raw, _SITE_TOTAL_KEYS, path, issues):
            continue
        site_type = _safe_identifier(raw.get("site_type"), f"{path}.site_type", issues)
        number = _number(raw.get("value"), f"{path}.value", issues,
                         minimum=0.0, code="SITE_POPULATION_TOTALS_REQUIRED")
        if number is not None and number <= 0.0:
            _issue(issues, "SITE_POPULATION_TOTALS_REQUIRED", f"{path}.value",
                   "site population total must be strictly positive")
        if raw.get("unit") not in {"sites", "dimensionless"}:
            _issue(issues, "SITE_POPULATION_UNIT_UNSUPPORTED", f"{path}.unit",
                   "site population unit must be sites or dimensionless")
        if raw.get("basis") not in {
                "surface_unit_cell", "normalized_site_population"}:
            _issue(issues, "SITE_POPULATION_BASIS_UNSUPPORTED", f"{path}.basis",
                   "site population basis is unsupported")
        _audit_evidence_list(raw.get("evidence"), f"{path}.evidence", issues)
        if site_type in totals:
            _issue(issues, "DUPLICATE_SITE_POPULATION_TOTAL", f"{path}.site_type",
                   f"duplicate population total for site type {site_type}")
        elif site_type and number is not None:
            totals[site_type] = number
    used_sites = {
        site_type for record in species.values()
        if record.get("phase") in {"surface", "adsorbate", "transition_state"}
        for site_type in (record.get("_sites") or {})
    }
    if set(totals) != used_sites:
        _issue(issues, "SITE_POPULATION_TOTALS_INCOMPLETE", "site_population_totals",
               "site_population_totals must cover every frozen site type exactly")
    return totals


def _audit_energy(value: Any, path: str, issues: list[dict[str, str]],
                  *, method_id: str | None,
                  require_uncertainty: bool = True) -> float | None:
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
    if require_uncertainty:
        uncertainty = _number(
            value.get("uncertainty_eV"), f"{path}.uncertainty_eV", issues,
            minimum=0.0, code="ENERGY_UNCERTAINTY_REQUIRED")
        if uncertainty is None:
            _issue(issues, "ENERGY_UNCERTAINTY_REQUIRED", f"{path}.uncertainty_eV",
                   "every energy requires a finite non-negative uncertainty_eV")
    elif "uncertainty_eV" in value:
        _number(
            value.get("uncertainty_eV"), f"{path}.uncertainty_eV", issues,
            minimum=0.0, code="ENERGY_UNCERTAINTY_REQUIRED")
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
            _issue(
                issues, "CATMAP_STOICHIOMETRY_UNSUPPORTED", f"{path}.{raw_id}",
                "phase-1 CatMAP export requires integer elementary-step coefficients",
                severity="warning")
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
        "lateral_interactions", "mechanism_completeness", "evidence",
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
    _audit_evidence_list(value.get("evidence"), "assumptions.evidence", issues)


def _audit_standard_state(
        value: Any, issues: list[dict[str, str]], *, phases: set[str]) -> None:
    allowed = frozenset({"temperature", "pressure", "concentration", "potential"})
    if not _unknown_fields(value, allowed, "standard_state", issues):
        return
    dimensions = {
        "temperature": ("K", 1.0, 5000.0, True),
        "pressure": ("bar", 0.0, 1.0e6, "gas" in phases),
        "concentration": (
            "mol/L", 0.0, 1.0e4,
            bool({"liquid", "solution"} & phases)),
    }
    for key, (unit, minimum, maximum, required) in dimensions.items():
        record = value.get(key)
        path = f"standard_state.{key}"
        if record is None and not required:
            continue
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


def _audit_species_standard_state(
        value: Any, phase: Any, path: str, issues: list[dict[str, str]], *,
        required: bool) -> None:
    if phase not in {"gas", "liquid", "solution"}:
        if value is not None:
            _issue(
                issues, "FLUID_STANDARD_STATE_FORBIDDEN", path,
                "surface and transition states must not carry fluid standard state")
        return
    if value is None and not required:
        return
    fields = frozenset({"schema", "phase", "kind", "value", "unit"})
    if not _unknown_fields(value, fields, path, issues):
        return
    domain_phase = "aqueous" if phase == "solution" else phase
    accepted = (
        {
            ("1-bar", "Pa", 100000.0),
            ("1-atm", "Pa", 101325.0),
        }
        if phase == "gas"
        else {("1-molar", "mol/L", 1.0)}
    )
    if value.get("schema") != "vcstudio.fluid-standard-state/v1":
        _issue(issues, "FLUID_STANDARD_STATE_INVALID", f"{path}.schema",
               "fluid standard state schema is unsupported")
    if value.get("phase") != domain_phase:
        _issue(issues, "FLUID_STANDARD_STATE_INVALID", f"{path}.phase",
               "fluid standard state phase disagrees with the canonical phase")
    matching = [
        candidate for candidate in accepted
        if (value.get("kind"), value.get("unit")) == candidate[:2]
    ]
    if not matching:
        _issue(issues, "FLUID_STANDARD_STATE_INVALID", path,
               "fluid standard state kind/unit is inconsistent")
    number = _number(
        value.get("value"), f"{path}.value", issues, minimum=0.0,
        code="FLUID_STANDARD_STATE_INVALID")
    if (number is not None and matching
            and abs(number - matching[0][2]) > _BALANCE_TOL):
        _issue(issues, "FLUID_STANDARD_STATE_INVALID", f"{path}.value",
               "fluid standard state value is inconsistent")


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


def _audit_species(
        value: Any, method_id: str | None,
        issues: list[dict[str, str]], *, direct_energy_authority: bool = False,
        qualified_stationary_points: bool = False,
        require_fluid_standard_state: bool = False,
        ) -> dict[str, dict[str, Any]]:
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
        _audit_species_standard_state(
            raw.get("standard_state"), phase, f"{path}.standard_state", issues,
            required=require_fluid_standard_state)
        if raw.get("formation_energy") is None and direct_energy_authority:
            energy = None
        else:
            energy = _audit_energy(
                raw.get("formation_energy"), f"{path}.formation_energy", issues,
                method_id=method_id,
                require_uncertainty=not direct_energy_authority)
        frequencies = raw.get("frequencies_cm1")
        normalized_frequencies = []
        if (isinstance(frequencies, (str, bytes))
                or not isinstance(frequencies, Sequence) or len(frequencies) > 1000):
            _issue(issues, "INVALID_FREQUENCIES", f"{path}.frequencies_cm1",
                   "frequencies_cm1 must be a bounded array")
        else:
            for freq_index, frequency in enumerate(frequencies):
                number = _number(
                    frequency, f"{path}.frequencies_cm1[{freq_index}]", issues,
                    minimum=-1.0e5, maximum=1.0e5,
                    code="INVALID_FREQUENCIES")
                if number is not None:
                    normalized_frequencies.append(number)
        significant_imaginary = [
            frequency for frequency in normalized_frequencies
            if frequency < -SIGNIFICANT_IMAGINARY_FREQUENCY_CM1
        ]
        if (phase == "transition_state" and not (
                qualified_stationary_points and not normalized_frequencies)
                and len(significant_imaginary) != 1):
            _issue(
                issues, "TRANSITION_STATE_IMAGINARY_MODE_REQUIRED",
                f"{path}.frequencies_cm1",
                "transition-state species requires exactly one imaginary mode "
                f"below -{SIGNIFICANT_IMAGINARY_FREQUENCY_CM1:g} cm^-1")
        elif phase in {"gas", "liquid", "solution", "adsorbate", "surface"} \
                and significant_imaginary:
            _issue(
                issues, "STATIONARY_SPECIES_IMAGINARY_MODE_FORBIDDEN",
                f"{path}.frequencies_cm1",
                "non-transition-state species cannot contain an imaginary mode "
                f"below -{SIGNIFICANT_IMAGINARY_FREQUENCY_CM1:g} cm^-1")
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
                 issues: list[dict[str, str]], *,
                 direct_energy_authority: bool = False,
                 ) -> list[dict[str, Any]]:
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
        transition_species = [
            (species_id, coefficient)
            for species_id, coefficient in transition.items()
            if (species.get(species_id) or {}).get("phase") == "transition_state"
        ]
        if (len(transition_species) != 1
                or abs(transition_species[0][1] - 1.0) > _BALANCE_TOL):
            _issue(
                issues, "TRANSITION_STATE_SPECIES_REQUIRED",
                f"{path}.transition_state",
                "phase 1 requires exactly one transition-state species with "
                "stoichiometric coefficient one")
        invalid_transition_species = [
            species_id for species_id in transition
            if (species.get(species_id) or {}).get("phase") != "transition_state"
            and not (
                (species.get(species_id) or {}).get("phase") == "surface"
                and not (species.get(species_id) or {}).get("_composition")
                and _same_scalar(
                    (species.get(species_id) or {}).get("_charge"), 0.0))
        ]
        if invalid_transition_species:
            _issue(
                issues, "TRANSITION_STATE_PHASE_INVALID",
                f"{path}.transition_state",
                "transition states may contain only the transition-state species "
                "and explicit empty surface-site bookkeeping")
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
                                method_id=method_id,
                                require_uncertainty=not direct_energy_authority)
        barrier_f = _audit_energy(
            raw.get("forward_barrier"), f"{path}.forward_barrier", issues,
            method_id=method_id,
            require_uncertainty=not direct_energy_authority)
        barrier_r = _audit_energy(
            raw.get("reverse_barrier"), f"{path}.reverse_barrier", issues,
            method_id=method_id,
            require_uncertainty=not direct_energy_authority)
        _audit_prefactors(raw.get("prefactors"), f"{path}.prefactors", issues)
        _audit_empirical_model(raw.get("bep"), f"{path}.bep", issues)
        _audit_empirical_model(raw.get("scaling"), f"{path}.scaling", issues)
        _number(raw.get("uncertainty_eV"), f"{path}.uncertainty_eV", issues,
                minimum=0.0, code="STEP_UNCERTAINTY_REQUIRED")
        _audit_evidence_list(raw.get("evidence"), f"{path}.evidence", issues)

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


def _frozen_v2_authority(
        network: Mapping[str, Any], projection: Any,
        issues: list[dict[str, str]],
        ) -> tuple[bool, bool, Mapping[str, Any] | None]:
    if (not isinstance(projection, Mapping)
            or (projection.get("schema"), projection.get("version"))
            != ("vcstudio.frozen-reaction-network/v2", "2")):
        return False, False, None
    extensions = network.get("extensions")
    authority = (
        extensions.get("frozen_reaction_v2")
        if isinstance(extensions, Mapping) else None)
    fields = frozenset({
        "schema", "frozen_network_sha256", "source_projection_sha256",
        "reaction_graph_sha256", "condition_revision_id",
        "condition_revision_sha256", "conditions", "direct_step_energies",
        "qualified_stationary_points",
    })
    if not isinstance(authority, Mapping) or set(authority) != fields:
        _issue(
            issues, "FROZEN_REACTION_AUTHORITY_REQUIRED",
            "extensions.frozen_reaction_v2",
            "frozen reaction v2 requires its exact source/condition authority ledger")
        return False, False, None
    if authority.get("schema") != "vcstudio.kinetics-frozen-reaction-authority/v1":
        _issue(
            issues, "FROZEN_REACTION_AUTHORITY_REQUIRED",
            "extensions.frozen_reaction_v2.schema",
            "frozen reaction authority schema is unsupported")
    frozen_hash = _sha(
        authority.get("frozen_network_sha256"),
        "extensions.frozen_reaction_v2.frozen_network_sha256", issues)
    _sha(
        authority.get("source_projection_sha256"),
        "extensions.frozen_reaction_v2.source_projection_sha256", issues)
    _sha(
        authority.get("reaction_graph_sha256"),
        "extensions.frozen_reaction_v2.reaction_graph_sha256", issues)
    _safe_identifier(
        authority.get("condition_revision_id"),
        "extensions.frozen_reaction_v2.condition_revision_id", issues)
    _sha(
        authority.get("condition_revision_sha256"),
        "extensions.frozen_reaction_v2.condition_revision_sha256", issues)
    conditions = authority.get("conditions")
    if not isinstance(conditions, Mapping) or not conditions:
        _issue(
            issues, "FROZEN_REACTION_CONDITIONS_REQUIRED",
            "extensions.frozen_reaction_v2.conditions",
            "frozen reaction v2 requires an exact condition snapshot")
        conditions = None
    else:
        for key, raw in conditions.items():
            if key not in {
                    "temperature_k", "pressure_pa", "ph",
                    "electrode_potential_v", "coverage"}:
                _issue(
                    issues, "FROZEN_REACTION_CONDITIONS_INVALID",
                    "extensions.frozen_reaction_v2.conditions",
                    "frozen reaction v2 condition field is unsupported")
                continue
            _number(
                raw, f"extensions.frozen_reaction_v2.conditions.{key}", issues,
                minimum=-1.0e12, maximum=1.0e12,
                code="FROZEN_REACTION_CONDITIONS_INVALID")
    if frozen_hash != projection.get("projection_sha256"):
        _issue(
            issues, "FROZEN_REACTION_AUTHORITY_MISMATCH",
            "extensions.frozen_reaction_v2.frozen_network_sha256",
            "frozen reaction ledger disagrees with source_projection")
    direct = authority.get("direct_step_energies") is True
    qualified = authority.get("qualified_stationary_points") is True
    if not direct or not qualified:
        _issue(
            issues, "FROZEN_REACTION_AUTHORITY_REQUIRED",
            "extensions.frozen_reaction_v2",
            "frozen reaction v2 lacks direct energy/stationary-point authority")
    return direct, qualified, conditions


def audit_network(
        source: CanonicalKineticsInput | KineticsInputProvider) -> dict[str, Any]:
    """Audit a frozen reaction/thermochemistry projection without solving it."""
    issues: list[dict[str, str]] = []
    try:
        network = _as_mapping(source)
    except KineticsContractError as exc:
        _issue(issues, "INVALID_INPUT", "$", str(exc))
        # Preserve a provider's already-frozen in-memory input for diagnostic
        # issue enumeration only.  INVALID_INPUT remains blocking and the raw
        # mapping can never become canonical or exportable.
        loader = getattr(source, "kinetics_input", None)
        try:
            raw = loader() if callable(loader) else None
        except Exception:  # noqa: BLE001 diagnostic fallback must remain inert
            raw = None
        network = copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
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
        schema = _safe_text(
            projection.get("schema"), "source_projection.schema", issues)
        version = projection.get("version")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            _issue(issues, "UNSAFE_IDENTIFIER", "source_projection.version",
                   "source_projection.version must be a safe version token")
        elif (schema, version) not in SOURCE_PROJECTION_PROTOCOLS:
            _issue(issues, "UNSUPPORTED_SOURCE_PROJECTION",
                   "source_projection",
                   "source projection schema/version is not trusted")
        _sha(projection.get("projection_sha256"),
             "source_projection.projection_sha256", issues)
        refs = projection.get("evidence_refs")
        if (isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence)
                or not refs or len(refs) > 256):
            _issue(issues, "SOURCE_EVIDENCE_REQUIRED", "source_projection.evidence_refs",
                   "source projection requires bounded evidence references")
        else:
            for index, reference in enumerate(refs):
                path = f"source_projection.evidence_refs[{index}]"
                if _unknown_fields(
                        reference, frozenset({"reference", "artifact_sha256"}),
                        path, issues):
                    _safe_text(reference.get("reference"), f"{path}.reference", issues)
                    _sha(reference.get("artifact_sha256"),
                         f"{path}.artifact_sha256", issues)

    model_spec = network.get("model_spec")
    if _unknown_fields(model_spec, _MODEL_SPEC_KEYS, "model_spec", issues):
        if model_spec.get("schema") != "vcstudio.kinetics-model-spec/v1":
            _issue(issues, "UNSUPPORTED_MODEL_SPEC", "model_spec.schema",
                   "model_spec.schema must be vcstudio.kinetics-model-spec/v1")
        for key in ("project_id", "spec_id", "revision"):
            _safe_text(model_spec.get(key), f"model_spec.{key}", issues,
                       maximum=128)
        _sha(model_spec.get("spec_sha256"), "model_spec.spec_sha256", issues)
        _sha(model_spec.get("source_projection_sha256"),
             "model_spec.source_projection_sha256", issues)
    rate_policy = _audit_rate_law_policy(network.get("rate_law_policy"), issues)
    _audit_assumptions(network.get("assumptions"), issues)
    raw_species = network.get("species")
    phases = {
        item.get("phase") for item in raw_species
        if isinstance(item, Mapping)
    } if (isinstance(raw_species, Sequence)
          and not isinstance(raw_species, (str, bytes))) else set()
    direct_energy, qualified_points, frozen_conditions = _frozen_v2_authority(
        network, projection, issues)
    _audit_standard_state(
        network.get("standard_state"), issues, phases=phases)
    operating = network.get("operating_range")
    ranges = {}
    if _unknown_fields(
            operating, frozenset({"temperature_K", "pressure_bar", "potential_V"}),
            "operating_range", issues):
        ranges["temperature_K"] = _audit_range(
            operating.get("temperature_K"), "operating_range.temperature_K", issues,
            minimum=1.0, maximum=5000.0)
        pressure_range = operating.get("pressure_bar")
        ranges["pressure_bar"] = (
            None if pressure_range is None and "gas" not in phases else _audit_range(
                pressure_range, "operating_range.pressure_bar", issues,
                minimum=0.0, maximum=1.0e6))
        potential = operating.get("potential_V")
        ranges["potential_V"] = (None if potential is None else _audit_range(
            potential, "operating_range.potential_V", issues,
            minimum=-10.0, maximum=10.0))

    method_id = _audit_methodology(network.get("methodology"), issues)
    species = _audit_species(
        network.get("species"), method_id, issues,
        direct_energy_authority=direct_energy,
        qualified_stationary_points=qualified_points,
        require_fluid_standard_state=(
            isinstance(projection, Mapping)
            and (projection.get("schema"), projection.get("version"))
            == ("vcstudio.frozen-reaction-network/v2", "2")))
    _audit_site_population_totals(
        network.get("site_population_totals"), species, issues)
    standard = network.get("standard_state") or {}
    standard_temperature = (standard.get("temperature") or {}).get("value")
    standard_pressure = (standard.get("pressure") or {}).get("value")
    if isinstance(frozen_conditions, Mapping):
        condition_temperature = frozen_conditions.get("temperature_k")
        if (isinstance(condition_temperature, (int, float))
                and not isinstance(condition_temperature, bool)
                and isinstance(standard_temperature, (int, float))
                and not isinstance(standard_temperature, bool)
                and abs(float(condition_temperature)
                        - float(standard_temperature)) > _BALANCE_TOL):
            _issue(
                issues, "FROZEN_REACTION_CONDITION_MISMATCH",
                "standard_state.temperature",
                "kinetics temperature disagrees with the frozen condition revision")
        condition_pressure = frozen_conditions.get("pressure_pa")
        if ("gas" in phases and isinstance(condition_pressure, (int, float))
                and not isinstance(condition_pressure, bool)
                and isinstance(standard_pressure, (int, float))
                and not isinstance(standard_pressure, bool)
                and abs(float(condition_pressure) / 100000.0
                        - float(standard_pressure)) > _BALANCE_TOL):
            _issue(
                issues, "FROZEN_REACTION_CONDITION_MISMATCH",
                "standard_state.pressure",
                "kinetics pressure disagrees with the frozen condition revision")
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
    expected_electrochemical = "explicit_potential" if uses_potential else "none"
    if (rate_policy.get("electrochemical") is not None
            and rate_policy["electrochemical"] != expected_electrochemical):
        _issue(issues, "ELECTROCHEMICAL_POLICY_MISMATCH",
               "rate_law_policy.electrochemical",
               "rate-law electrochemical policy disagrees with methodology")
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
    steps = _audit_steps(
        network.get("elementary_steps"), method_id, species, issues,
        direct_energy_authority=direct_energy)
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
            "checks": 15,
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
            or _CONTROL_RE.search(value)
            or classify_sensitive_text(value) is not None):
        raise KineticsContractError(f"{path} must be a safe identifier")
    return value


def _result_version(value: Any, path: str) -> str:
    if (not isinstance(value, str) or not _VERSION_RE.fullmatch(value)
            or _CONTROL_RE.search(value)
            or classify_sensitive_text(value) is not None):
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


def _exact_metric_completeness(
        records: Sequence[Mapping[str, Any]], identity_fields: tuple[str, ...],
        required_records: int, path: str) -> dict[str, Any]:
    identities = [
        tuple(str(record[field]) for field in identity_fields)
        for record in records
    ]
    observed = set(identities)
    duplicate_records = len(identities) - len(observed)
    denominator = {
        "identity_fields": list(identity_fields),
        "required_records": required_records,
        "observed_records": len(identities),
        "unique_records": len(observed),
        "duplicate_records": duplicate_records,
        "complete": duplicate_records == 0 and len(observed) == required_records,
    }
    if duplicate_records:
        raise KineticsContractError(
            f"{path} contains duplicate composite metric records")
    if len(observed) != required_records:
        raise KineticsContractError(
            f"{path} does not completely cover its frozen metric axes")
    return denominator


def _coverage_completeness(
        records: Sequence[Mapping[str, Any]], required_site_types: set[str],
        path: str) -> dict[str, Any]:
    identities = [
        (str(record["species_id"]), str(record["site_type"]))
        for record in records
    ]
    observed_identities = set(identities)
    observed_site_types = {identity[1] for identity in observed_identities}
    duplicate_records = len(identities) - len(observed_identities)
    denominator = {
        "identity_fields": ["species_id", "site_type"],
        "required_records": len(required_site_types),
        "required_site_types": len(required_site_types),
        "observed_site_types": len(observed_site_types),
        "observed_records": len(identities),
        "unique_records": len(observed_identities),
        "duplicate_records": duplicate_records,
        "complete": (
            duplicate_records == 0
            and required_site_types <= observed_site_types),
    }
    if duplicate_records:
        raise KineticsContractError(
            f"{path} contains duplicate species/site coverage records")
    if not required_site_types <= observed_site_types:
        raise KineticsContractError(
            f"{path} does not cover every frozen site type")
    return denominator


def _free_energy_completeness(
        records: Sequence[Mapping[str, Any]], path: str) -> dict[str, Any]:
    identities = [str(record["state_id"]) for record in records]
    observed = set(identities)
    duplicate_records = len(identities) - len(observed)
    denominator = {
        "identity_fields": ["state_id"],
        "required_records": 2,
        "observed_records": len(identities),
        "unique_records": len(observed),
        "duplicate_records": duplicate_records,
        "complete": duplicate_records == 0 and len(observed) >= 2,
    }
    if duplicate_records:
        raise KineticsContractError(
            f"{path} contains duplicate free-energy states")
    if len(observed) < 2:
        raise KineticsContractError(
            f"{path} requires at least two distinct frozen states")
    return denominator


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
        value_min=-1000.0, value_max=1000.0,
        extra_field="target_species_id")
    dsc = _metric_list(
        point.get("dsc"), f"{path}.dsc", id_field="step_id",
        value_min=-1000.0, value_max=1000.0,
        extra_field="target_species_id")
    reaction_order = _metric_list(
        point.get("reaction_order"), f"{path}.reaction_order",
        id_field="species_id", value_min=-1000.0, value_max=1000.0,
        extra_field="target_species_id")
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
    coverage_species = {
        str(record["id"]): {
            "phase": record.get("phase"),
            "sites": frozenset(str(site) for site in (record.get("sites") or {})),
        }
        for record in network.get("species") or []
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }
    target_species = {
        str(species_id) for species_id in network.get("target_products") or []
        if isinstance(species_id, str)
    }
    feed_species = {
        str(species_id) for species_id in network.get("feed_species") or []
        if isinstance(species_id, str)
    }
    gas_feed_species = {
        species_id for species_id in feed_species
        if coverage_species.get(species_id, {}).get("phase") == "gas"
    }
    frozen_site_types = {
        str(site_type) for species in coverage_species.values()
        for site_type in species.get("sites", frozenset())
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
    for metric_name, records in (
            ("drc", drc), ("dsc", dsc),
            ("reaction_order", reaction_order)):
        if any(record["target_species_id"] not in target_species
               for record in records):
            raise KineticsContractError(
                f"{path}.{metric_name} references target species outside "
                "the frozen target products")
    for metric_name, records in (
            ("tof", tof), ("selectivity", selectivity),
            ("apparent_activation_energy", apparent)):
        if any(record["species_id"] not in target_species for record in records):
            raise KineticsContractError(
                f"{path}.{metric_name} references species outside "
                "the frozen target products")
    for record in reaction_order:
        species = coverage_species.get(record["species_id"], {})
        if (species.get("phase") != "gas"
                or record["species_id"] not in gas_feed_species):
            raise KineticsContractError(
                f"{path}.reaction_order perturbation species must be a frozen "
                "gas feed reservoir")
    for record in coverage:
        species = coverage_species.get(record["species_id"], {})
        if species.get("phase") not in {"surface", "adsorbate"}:
            raise KineticsContractError(
                f"{path}.coverage references species that cannot carry "
                "phase-1 coverage")
        if record["site_type"] not in species.get("sites", frozenset()):
            raise KineticsContractError(
                f"{path}.coverage site type is not assigned to its frozen species")
    allowed_free_energy_states = known_species | _FREE_ENERGY_AGGREGATE_STATES
    if any(record["state_id"] not in allowed_free_energy_states
           for record in free_energy):
        raise KineticsContractError(
            f"{path}.free_energy_diagram references a state outside the frozen "
            "species and reserved aggregate states")
    target_records = len(target_species)
    target_step_records = len(target_species) * len(known_steps)
    target_feed_records = len(target_species) * len(gas_feed_species)
    completeness = {
        "tof": _exact_metric_completeness(
            tof, ("species_id",), target_records, f"{path}.tof"),
        "coverage": _coverage_completeness(
            coverage, frozen_site_types, f"{path}.coverage"),
        "selectivity": _exact_metric_completeness(
            selectivity, ("species_id",), target_records,
            f"{path}.selectivity"),
        "drc": _exact_metric_completeness(
            drc, ("target_species_id", "step_id"), target_step_records,
            f"{path}.drc"),
        "dsc": _exact_metric_completeness(
            dsc, ("target_species_id", "step_id"), target_step_records,
            f"{path}.dsc"),
        "reaction_order": _exact_metric_completeness(
            reaction_order, ("target_species_id", "species_id"),
            target_feed_records, f"{path}.reaction_order"),
        "apparent_activation_energy": _exact_metric_completeness(
            apparent, ("species_id",), target_records,
            f"{path}.apparent_activation_energy"),
        "free_energy_diagram": _free_energy_completeness(
            free_energy, f"{path}.free_energy_diagram"),
    }
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
    if (isinstance(iterations, bool) or not isinstance(iterations, int)
            or not 0 <= iterations <= CONVERGENCE_ITERATIONS_MAX):
        raise KineticsContractError(
            f"{path}.convergence.iterations must be between 0 and "
            f"{CONVERGENCE_ITERATIONS_MAX}")
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
        "completeness": completeness,
        "convergence": {
            # A small residual is necessary but cannot override an explicit
            # solver failure.  Both independent signals must agree.
            "converged": (
                convergence["converged"] is True
                and residual <= CONVERGENCE_RESIDUAL_MAX),
            "reported_converged": convergence["converged"], "residual": residual,
            "residual_threshold": CONVERGENCE_RESIDUAL_MAX,
            "iteration_threshold": CONVERGENCE_ITERATIONS_MAX,
            "iterations": iterations, "solver": solver,
        },
    }


def _normalize_sensitivity(
        value: Any, points: Sequence[Mapping[str, Any]],
        network: Mapping[str, Any]) -> dict[str, Any]:
    result = _strict_object(value, {"analyses", "warnings"}, "sensitivity")
    analyses = result.get("analyses")
    if (isinstance(analyses, (str, bytes)) or not isinstance(analyses, Sequence)
            or not analyses or len(analyses) > 10000):
        raise KineticsContractError(
            "sensitivity.analyses must be a non-empty bounded array")
    step_uncertainties = {
        str(step["id"]): float(step["uncertainty_eV"])
        for step in network.get("elementary_steps") or []
        if isinstance(step, Mapping) and isinstance(step.get("id"), str)
    }
    expected = {
        (point_index, step_id)
        for point_index in range(len(points)) for step_id in step_uncertainties
    }
    observed = set()
    normalized = []
    for index, raw in enumerate(analyses):
        item = _strict_object(
            raw, {
                "kind", "point_index", "condition_sha256", "step_id",
                "perturbation_eV", "max_relative_change",
            }, f"sensitivity.analyses[{index}]")
        if item.get("kind") != "energy_uncertainty":
            raise KineticsContractError(
                f"sensitivity.analyses[{index}].kind is unsupported")
        point_index = item.get("point_index")
        if (isinstance(point_index, bool) or not isinstance(point_index, int)
                or not 0 <= point_index < len(points)):
            raise KineticsContractError(
                f"sensitivity.analyses[{index}].point_index is invalid")
        step_id = _result_identifier(
            item.get("step_id"), f"sensitivity.analyses[{index}].step_id")
        key = (point_index, step_id)
        if key not in expected:
            raise KineticsContractError(
                f"sensitivity.analyses[{index}] is outside the frozen steps/points")
        if key in observed:
            raise KineticsContractError("sensitivity coverage contains a duplicate")
        observed.add(key)
        condition_sha256 = item.get("condition_sha256")
        expected_condition_sha256 = compute_condition_sha256(
            points[point_index]["conditions"])
        if condition_sha256 != expected_condition_sha256:
            raise KineticsContractError(
                f"sensitivity.analyses[{index}].condition_sha256 mismatch")
        perturbation = _result_number(
            item.get("perturbation_eV"),
            f"sensitivity.analyses[{index}].perturbation_eV",
            minimum=0.0, maximum=10.0)
        if not math.isclose(
                perturbation, step_uncertainties[step_id],
                rel_tol=0.0, abs_tol=1.0e-12):
            raise KineticsContractError(
                f"sensitivity.analyses[{index}].perturbation_eV does not match "
                "the frozen step uncertainty")
        normalized.append({
            "kind": "energy_uncertainty", "point_index": point_index,
            "condition_sha256": condition_sha256, "step_id": step_id,
            "perturbation_eV": perturbation,
            "max_relative_change": _result_number(
                item.get("max_relative_change"),
                f"sensitivity.analyses[{index}].max_relative_change",
                minimum=0.0, maximum=1.0e12),
        })
    if observed != expected:
        raise KineticsContractError(
            "sensitivity analyses do not cover every frozen point and step")
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
    maximum_change = max(item["max_relative_change"] for item in normalized)
    return {
        "status": "warning" if maximum_change > 1.0 else "passed",
        "available": True,
        "analyses": normalized,
        "warnings": safe_warnings,
        "coverage": {
            "required": len(expected), "observed": len(observed),
            "complete": observed == expected,
        },
    }


def import_result(
        result: Mapping[str, Any],
        network_source: CanonicalKineticsInput | KineticsInputProvider, *,
        expected_adapter: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an external result; never trust browser numerics."""
    canonical = canonicalize_kinetics_input(network_source)
    network = canonical.to_mapping()
    audit = audit_network(canonical)
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
    expected_fields = {"id", "version", "tool_version", "tool_sha256"}
    expected = _strict_object(
        expected_adapter, expected_fields, "expected_adapter")
    if set(expected) != expected_fields:
        raise KineticsContractError(
            "expected_adapter must contain id, version, tool_version and tool_sha256")
    for key in ("id", "version", "tool_version"):
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
    sensitivity = _normalize_sensitivity(
        payload.get("sensitivity"), normalized_points, network)
    converged = all(
        point["convergence"]["converged"] for point in normalized_points)
    sensitivity_available = sensitivity["available"] is True
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
    "AUDIT_SCHEMA", "CanonicalKineticsInput", "CONVERGENCE_ITERATIONS_MAX",
    "CONVERGENCE_RESIDUAL_MAX", "KineticsContractError", "KineticsInputProvider",
    "NETWORK_SCHEMA", "NORMALIZED_RESULT_SCHEMA", "RESULT_SCHEMA", "RESULT_UNITS",
    "SIGNIFICANT_IMAGINARY_FREQUENCY_CM1",
    "SOURCE_PROJECTION_PROTOCOLS", "audit_network", "canonicalize_kinetics_input",
    "compute_condition_sha256", "compute_input_sha256", "compute_projection_sha256",
    "compute_source_projection_sha256", "import_result",
]
