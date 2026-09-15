"""One-shot reaction/spec projection into the strict kinetics input DTO.

The final Reaction Workbench schema is intentionally behind a narrow protocol:
the domain layer supplies one already server-validated, path-free frozen
mapping.  This module snapshots that mapping exactly once, snapshots one exact
``KineticsModelSpec``, and snapshots every referenced evidence artifact once.
It never reopens project files and never persists reaction facts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from vcstudio.project.kinetics_model_spec import (
    MAX_EVIDENCE_BINDINGS,
    KineticsModelSpec,
    KineticsModelSpecError,
    classify_sensitive_text,
)


_SOURCE_HASH_FIELDS = {
    ("vcstudio.reaction-domain-projection/v1", "1"): "projection_sha256",
    ("vcstudio.kinetics-adapted-reaction-source/v1", "1"):
        "adapter_projection_sha256",
}
FROZEN_REACTION_V2_SCHEMA = "vcstudio.frozen-reaction-network/v2"
ADAPTED_REACTION_SOURCE_SCHEMA = (
    "vcstudio.kinetics-adapted-reaction-source/v1")
ADAPTED_REACTION_SOURCE_VERSION = "1"
VALIDATED_FROZEN_AUTHORITY_SCHEMA = (
    "vcstudio.validated-frozen-reaction-authority/v1")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_TOTAL_EVIDENCE_BYTES = 64 * 1024 * 1024
_MAX_COLLECTION = 4096
_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
_OPAQUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:+-]{0,159}\Z")
_FORMULA_TOKEN_RE = re.compile(r"([A-Z][a-z]?)([1-9][0-9]*)?")
_KINETIC_SCIENTIFIC_STATUSES = frozenset({
    "machine_pass", "human_review", "verified", "release",
})
_SPEC_OWNED_TOP_LEVEL = frozenset({
    "assumptions", "feed_species", "target_products", "rate_law_policy",
    "site_population_totals", "model_spec",
})
_SPEC_OWNED_STEP_FIELDS = frozenset({
    "prefactors", "bep", "scaling", "uncertainty_eV", "evidence",
})


class KineticsProjectionError(ValueError):
    """A frozen source, spec binding, or evidence byte snapshot is invalid."""


@runtime_checkable
class ValidatedReactionProjection(Protocol):
    """Temporary seam until the Reaction Workbench projection API is final.

    Implementations must return a server-validated, path-free frozen DTO.  The
    provider invokes this method exactly once during construction.
    """

    def frozen_reaction_projection(self) -> Mapping[str, Any]:
        """Return one detached authoritative reaction projection snapshot."""


@runtime_checkable
class ValidatedReactionSnapshotIdentity(Protocol):
    """Path-free identity emitted by the production Reaction authority."""

    project_id: str
    source_projection_sha256: str
    domain_authority_id: str
    domain_generation: int
    network_id: str
    network_revision: str

    def detached_projection(self) -> Mapping[str, Any]:
        """Return the already validated canonical Reaction projection."""


@runtime_checkable
class EvidenceBindingResolver(Protocol):
    """Resolve one explicitly indexed opaque reference to immutable bytes."""

    def __call__(self, reference: str) -> bytes:
        """Return the exact artifact bytes for ``reference``."""


@runtime_checkable
class ValidatedFrozenReactionAuthority(Protocol):
    """Server-sealed identity for the exact frozen and condition revision."""

    def frozen_reaction_authority(self) -> Mapping[str, Any]:
        """Return the path-free expected frozen/condition hash identity."""


@dataclass(frozen=True)
class ValidatedFrozenReactionAuthoritySnapshot:
    """Detached narrow authority snapshot supplied by the Reaction server."""

    frozen_network_sha256: str
    condition_revision_id: str
    condition_revision_sha256: str
    conditions_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "frozen_network_sha256", _sha256(
            self.frozen_network_sha256, "validated frozen network sha256"))
        object.__setattr__(self, "condition_revision_id", _opaque(
            self.condition_revision_id, "validated condition revision id"))
        object.__setattr__(self, "condition_revision_sha256", _sha256(
            self.condition_revision_sha256,
            "validated condition revision sha256"))
        object.__setattr__(self, "conditions_sha256", _sha256(
            self.conditions_sha256, "validated conditions sha256"))

    def frozen_reaction_authority(self) -> Mapping[str, Any]:
        return {
            "schema": VALIDATED_FROZEN_AUTHORITY_SCHEMA,
            "frozen_network_sha256": self.frozen_network_sha256,
            "condition_revision_id": self.condition_revision_id,
            "condition_revision_sha256": self.condition_revision_sha256,
            "conditions_sha256": self.conditions_sha256,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KineticsProjectionError(
            "reaction projection must be canonical finite JSON") from exc


def _source_hash(value: Mapping[str, Any]) -> tuple[str, str]:
    identity = (value.get("schema"), value.get("version"))
    hash_field = _SOURCE_HASH_FIELDS.get(identity)
    if hash_field is None:
        raise KineticsProjectionError(
            "reaction projection schema/version is not supported by the seam")
    declared = value.get(hash_field)
    if not isinstance(declared, str) or not _SHA_RE.fullmatch(declared):
        raise KineticsProjectionError("reaction projection hash is invalid")
    copied = copy.deepcopy(dict(value))
    copied.pop(hash_field, None)
    computed = hashlib.sha256(_canonical_bytes(copied)).hexdigest()
    if declared != computed:
        raise KineticsProjectionError(
            "reaction projection hash does not match its frozen bytes")
    return hash_field, computed


def _source_binding_authority(
        value: Mapping[str, Any], adapter_hash: str,
        ) -> tuple[str, tuple[str, str]]:
    """Separate adapter integrity from the upstream source authority hash."""
    identity = (value.get("schema"), value.get("version"))
    if identity != (
            ADAPTED_REACTION_SOURCE_SCHEMA, ADAPTED_REACTION_SOURCE_VERSION):
        return adapter_hash, (str(identity[0]), str(identity[1]))
    authority = _strict_fields(
        value.get("authoritative_source"),
        frozenset({"schema", "version", "frozen_network_sha256"}),
        "adapted reaction authoritative_source")
    if (authority.get("schema") != FROZEN_REACTION_V2_SCHEMA
            or authority.get("version") != "2"):
        raise KineticsProjectionError(
            "adapted reaction source does not name frozen reaction v2 authority")
    return (
        _sha256(
            authority.get("frozen_network_sha256"),
            "adapted reaction frozen_network_sha256"),
        (FROZEN_REACTION_V2_SCHEMA, "2"),
    )


def _safe_tree(value: Any, path: str = "$", *, depth: int = 0) -> None:
    if depth > 20:
        raise KineticsProjectionError(f"{path} is nested too deeply")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 2**53 - 1:
            raise KineticsProjectionError(f"{path} integer is outside JSON-safe range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise KineticsProjectionError(f"{path} contains a non-finite number")
        return
    if isinstance(value, str):
        if (not value or len(value) > 4096 or _CONTROL_RE.search(value)
                or classify_sensitive_text(value) is not None):
            raise KineticsProjectionError(f"{path} contains unsafe text")
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION:
            raise KineticsProjectionError(f"{path} contains too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256 \
                    or _CONTROL_RE.search(key) \
                    or classify_sensitive_text(key) is not None:
                raise KineticsProjectionError(f"{path} contains an unsafe key")
            _safe_tree(item, f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > _MAX_COLLECTION:
            raise KineticsProjectionError(f"{path} contains too many items")
        for index, item in enumerate(value):
            _safe_tree(item, f"{path}[{index}]", depth=depth + 1)
        return
    raise KineticsProjectionError(f"{path} is not JSON")


def _required(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    missing = fields - set(value)
    if missing:
        raise KineticsProjectionError(
            f"{label} is missing required fields: {', '.join(sorted(missing))}")


def _evidence_index(value: Any, label: str) -> dict[str, str]:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or not value or len(value) > MAX_EVIDENCE_BINDINGS):
        raise KineticsProjectionError(f"{label} must be a bounded evidence array")
    output = {}
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {
                "reference", "artifact_sha256"}:
            raise KineticsProjectionError(f"{label}[{index}] is invalid")
        reference = raw.get("reference")
        digest = raw.get("artifact_sha256")
        if not isinstance(reference, str) or not reference \
                or not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
            raise KineticsProjectionError(f"{label}[{index}] is invalid")
        prior = output.get(reference)
        if prior is not None and prior != digest:
            raise KineticsProjectionError(
                f"evidence reference {reference!r} has conflicting hashes")
        if prior is not None:
            raise KineticsProjectionError(f"{label} contains duplicate references")
        output[reference] = digest
    return output


def _assert_source_records_declared(node: Any, declared: Mapping[str, str],
                                    path: str = "$") -> None:
    if isinstance(node, Mapping):
        if set(node) == {"kind", "reference", "evidence_sha256"}:
            reference = node.get("reference")
            digest = node.get("evidence_sha256")
            if declared.get(reference) != digest:
                raise KineticsProjectionError(
                    f"{path} is not bound by the declared evidence list")
        for key, item in node.items():
            _assert_source_records_declared(item, declared, f"{path}.{key}")
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, item in enumerate(node):
            _assert_source_records_declared(item, declared, f"{path}[{index}]")


def _activity_source(reservoir: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "condition",
        "reference": reservoir["source"],
        "evidence_sha256": reservoir["evidence_sha256"],
    }


_FROZEN_V2_FIELDS = frozenset({
    "schema", "version", "network_id", "domain_authority",
    "network_identity", "state_catalog", "source_projection_sha256",
    "reaction_graph_sha256", "condition_revision_id",
    "condition_revision_sha256", "conditions", "edges",
    "canonical_envelope_authority", "binding_status_gate",
    "scientific_status", "readiness", "microkinetics_ready",
    "authorizes_execution", "limitations", "frozen_network_sha256",
})
_EDGE_FIELDS = frozenset({
    "edge_id", "reactants", "transition_state", "products",
    "reactant_state_ids", "product_state_ids", "transition_state_id",
    "stoichiometry", "condition_set_id", "condition_set_sha256",
    "method_sha256", "evidence_sha256", "edge_compatibility_sha256",
    "evidence_refs", "conservation", "reaction_delta_g_eV",
    "observed_activation_delta_e_eV", "thermal_activation_delta_g_eV",
    "activation_delta_g_eV", "reverse_activation_delta_g_eV",
    "thermodynamic_status", "kinetic_status", "missing",
})
_PARTICIPANT_FIELDS = frozenset({
    "state_id", "coefficient", "phase", "charge", "site_stoichiometry",
})
_OBJECT_EVIDENCE_FIELDS = frozenset({
    "role", "object_type", "object_id", "object_revision_id",
    "semantic_sha256", "resolver_refs",
})
_RESOLVER_REF_FIELDS = frozenset({
    "opaque_id", "origin", "ref_type", "revision_id",
})
_SHA_EVIDENCE_FIELDS = frozenset({"role", "sha256"})
_GATE_FIELDS = frozenset({"status", "eligible", "entries", "blocking"})
_GATE_ENTRY_FIELDS = frozenset({
    "object_id", "entity_type", "scientific_status", "kinetic_eligible",
})
_CONSERVATION_FIELDS = frozenset({
    "schema", "status", "elemental", "charge", "surface_site_occupancy",
    "transition_state_status", "canonical_validator_error", "missing",
})


def _strict_fields(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise KineticsProjectionError(f"{label} contract is invalid")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise KineticsProjectionError(f"{label} must be a SHA-256 value")
    return value


def _opaque(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not _OPAQUE_RE.fullmatch(value)
            or value in {".", ".."}):
        raise KineticsProjectionError(f"{label} must be an opaque identifier")
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KineticsProjectionError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise KineticsProjectionError(f"{label} must be a finite number")
    return result


def _rational(value: Any, label: str, *, positive: bool = False) -> float:
    record = _strict_fields(
        value, frozenset({"numerator", "denominator"}), label)
    numerator = record.get("numerator")
    denominator = record.get("denominator")
    if (isinstance(numerator, bool) or not isinstance(numerator, int)
            or isinstance(denominator, bool) or not isinstance(denominator, int)
            or denominator <= 0):
        raise KineticsProjectionError(f"{label} is not an exact rational")
    result = numerator / denominator
    if positive and result <= 0.0:
        raise KineticsProjectionError(f"{label} must be positive")
    return float(result)


def _formula_composition(value: Any, label: str) -> dict[str, int]:
    if value == "*":
        return {}
    if not isinstance(value, str) or not value:
        raise KineticsProjectionError(f"{label} is invalid")
    composition: dict[str, int] = {}
    offset = 0
    for match in _FORMULA_TOKEN_RE.finditer(value):
        if match.start() != offset:
            raise KineticsProjectionError(f"{label} is unsupported")
        element = match.group(1)
        count = int(match.group(2) or "1")
        composition[element] = composition.get(element, 0) + count
        offset = match.end()
    if offset != len(value) or not composition:
        raise KineticsProjectionError(f"{label} is unsupported")
    return composition


def _scoped_evidence_reference(edge_id: str, role: str, identity: str) -> str:
    return f"frozen-v2:{edge_id}:{role}:{identity}"


def required_frozen_v2_evidence_references(
        frozen: Mapping[str, Any]) -> tuple[str, ...]:
    """List explicit external byte bindings required by a frozen v2 payload."""
    if not isinstance(frozen, Mapping):
        raise KineticsProjectionError("frozen v2 evidence source must be a mapping")
    edges = frozen.get("edges")
    if isinstance(edges, (str, bytes)) or not isinstance(edges, Sequence):
        raise KineticsProjectionError("frozen v2 edges must be an array")
    references: set[str] = set()
    for edge_index, edge in enumerate(edges):
        if not isinstance(edge, Mapping):
            raise KineticsProjectionError(
                f"frozen v2 edges[{edge_index}] is invalid")
        edge_id = _opaque(edge.get("edge_id"), f"edges[{edge_index}].edge_id")
        method_sha = _sha256(
            edge.get("method_sha256"), f"edges[{edge_index}].method_sha256")
        references.add(_scoped_evidence_reference(edge_id, "method", method_sha))
        evidence = edge.get("evidence_refs")
        if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
            raise KineticsProjectionError(
                f"edges[{edge_index}].evidence_refs is invalid")
        for item_index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                raise KineticsProjectionError(
                    f"edges[{edge_index}].evidence_refs[{item_index}] is invalid")
            if set(item) == _OBJECT_EVIDENCE_FIELDS:
                _opaque(item.get("role"), "frozen v2 object evidence role")
                _opaque(
                    item.get("object_type"),
                    "frozen v2 object evidence object_type")
                _opaque(
                    item.get("object_id"),
                    "frozen v2 object evidence object_id")
                _opaque(
                    item.get("object_revision_id"),
                    "frozen v2 object evidence revision")
                _sha256(
                    item.get("semantic_sha256"),
                    "frozen v2 object evidence semantic sha256")
                resolver_refs = item.get("resolver_refs")
                if (isinstance(resolver_refs, (str, bytes))
                        or not isinstance(resolver_refs, Sequence)
                        or not resolver_refs):
                    raise KineticsProjectionError(
                        "frozen v2 domain evidence lacks resolver references")
                for raw_ref in resolver_refs:
                    resolver_ref = _strict_fields(
                        raw_ref, _RESOLVER_REF_FIELDS,
                        "frozen v2 resolver reference")
                    references.add(_opaque(
                        resolver_ref.get("opaque_id"),
                        "frozen v2 resolver opaque_id"))
                    _opaque(
                        resolver_ref.get("origin"),
                        "frozen v2 resolver origin")
                    _opaque(
                        resolver_ref.get("ref_type"),
                        "frozen v2 resolver ref_type")
                    revision_id = resolver_ref.get("revision_id")
                    if revision_id is not None:
                        _opaque(
                            revision_id, "frozen v2 resolver revision_id")
            elif set(item) == _SHA_EVIDENCE_FIELDS:
                role = _opaque(item.get("role"), "frozen v2 evidence role")
                digest = _sha256(
                    item.get("sha256"), "frozen v2 evidence sha256")
                references.add(_scoped_evidence_reference(edge_id, role, digest))
            else:
                raise KineticsProjectionError(
                    "frozen v2 evidence reference contract is invalid")
    return tuple(sorted(references))


def _validate_edge_evidence(
        edge: Mapping[str, Any], states: Mapping[str, Mapping[str, Any]],
        sides: Mapping[str, Mapping[str, float]]) -> None:
    edge_id = str(edge["edge_id"])
    raw_evidence = edge.get("evidence_refs")
    if (isinstance(raw_evidence, (str, bytes))
            or not isinstance(raw_evidence, Sequence) or not raw_evidence):
        raise KineticsProjectionError(
            f"frozen v2 edge {edge_id} evidence is invalid")
    object_records: list[Mapping[str, Any]] = []
    hash_records: dict[str, str] = {}
    for item in raw_evidence:
        if not isinstance(item, Mapping):
            raise KineticsProjectionError(
                f"frozen v2 edge {edge_id} evidence is invalid")
        if set(item) == _OBJECT_EVIDENCE_FIELDS:
            object_records.append(item)
        elif set(item) == _SHA_EVIDENCE_FIELDS:
            role = str(item["role"])
            if role in hash_records:
                raise KineticsProjectionError(
                    f"frozen v2 edge {edge_id} has duplicate hash evidence roles")
            hash_records[role] = str(item["sha256"])
    required_hash_roles = {
        "edge_evidence": edge.get("evidence_sha256"),
        "edge_compatibility": edge.get("edge_compatibility_sha256"),
    }
    if any(hash_records.get(role) != digest
           for role, digest in required_hash_roles.items()):
        raise KineticsProjectionError(
            f"frozen v2 edge {edge_id} hash evidence identity disagrees")
    for role in (
            "transition_state_structure", "transition_state_evidence",
            "transition_state_binding"):
        if role not in hash_records:
            raise KineticsProjectionError(
                f"frozen v2 edge {edge_id} lacks mandatory saddle evidence")

    expected_participants = []
    for role, side_name in (
            ("reactant", "reactants"),
            ("transition_state", "transition_state"),
            ("product", "products")):
        expected_participants.extend(
            (role, state_id, states[state_id]["object_type"],
             states[state_id]["object_revision_id"],
             states[state_id]["semantic_sha256"])
            for state_id in sides[side_name]
        )
    actual_participants = [
        (str(item["role"]), str(item["object_id"]), str(item["object_type"]),
         str(item["object_revision_id"]), str(item["semantic_sha256"]))
        for item in object_records
        if item.get("role") in {"reactant", "transition_state", "product"}
    ]
    if actual_participants != expected_participants:
        raise KineticsProjectionError(
            f"frozen v2 edge {edge_id} participant evidence identity disagrees")
    step_records = [
        item for item in object_records if item.get("role") == "elementary_step"]
    condition_records = [
        item for item in object_records if item.get("role") == "condition_set"]
    if (len(step_records) != 1 or step_records[0].get("object_id") != edge_id
            or len(condition_records) != 1
            or condition_records[0].get("object_id")
            != edge.get("condition_set_id")
            or condition_records[0].get("semantic_sha256")
            != edge.get("condition_set_sha256")
            or len(object_records) != len(expected_participants) + 2):
        raise KineticsProjectionError(
            f"frozen v2 edge {edge_id} domain evidence identity disagrees")


def _linear_state_energies(
        states: Mapping[str, Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]]) -> dict[str, float] | None:
    state_ids = sorted(states)
    columns = {state_id: index for index, state_id in enumerate(state_ids)}
    rows: list[list[float]] = []

    def equation(
            positive: Mapping[str, float], negative: Mapping[str, float],
            value: float) -> None:
        row = [0.0] * (len(state_ids) + 1)
        for state_id, coefficient in positive.items():
            row[columns[state_id]] += coefficient
        for state_id, coefficient in negative.items():
            row[columns[state_id]] -= coefficient
        row[-1] = value
        rows.append(row)

    for edge in edges:
        sides = edge["_sides"]
        equation(sides["products"], sides["reactants"], edge["_delta_g"])
        equation(
            sides["transition_state"], sides["reactants"],
            edge["_forward_barrier"])
    elements = sorted({
        element for state in states.values()
        for element in state["_composition"]
    })

    def matrix_rank(matrix: Sequence[Sequence[float]]) -> int:
        work = [list(map(float, row)) for row in matrix]
        current_rank = 0
        for column in range(len(elements)):
            pivot = next((index for index in range(current_rank, len(work))
                          if abs(work[index][column]) > 1.0e-12), None)
            if pivot is None:
                continue
            work[current_rank], work[pivot] = work[pivot], work[current_rank]
            divisor = work[current_rank][column]
            work[current_rank] = [value / divisor for value in work[current_rank]]
            for index, row in enumerate(work):
                if index == current_rank:
                    continue
                factor = row[column]
                if abs(factor) > 1.0e-12:
                    work[index] = [
                        value - factor * basis
                        for value, basis in zip(row, work[current_rank])
                    ]
            current_rank += 1
        return current_rank

    reference_states = []
    reference_rows: list[list[float]] = []
    for state_id in sorted(states):
        state = states[state_id]
        if state["object_type"] != "FluidState":
            continue
        row = [float(state["_composition"].get(element, 0)) for element in elements]
        if matrix_rank([*reference_rows, row]) > len(reference_rows):
            reference_states.append(state_id)
            reference_rows.append(row)
    for state_id, state in states.items():
        if (state_id in reference_states
                or (state["object_type"] == "AdsorbateState"
                    and not state["_composition"])):
            equation({state_id: 1.0}, {}, 0.0)
    rank = 0
    pivots: dict[int, int] = {}
    tolerance = 1.0e-10
    for column in range(len(state_ids)):
        pivot = next((index for index in range(rank, len(rows))
                      if abs(rows[index][column]) > tolerance), None)
        if pivot is None:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        divisor = rows[rank][column]
        rows[rank] = [value / divisor for value in rows[rank]]
        for index, row in enumerate(rows):
            if index == rank:
                continue
            factor = row[column]
            if abs(factor) > tolerance:
                rows[index] = [
                    value - factor * basis
                    for value, basis in zip(row, rows[rank])
                ]
        pivots[column] = rank
        rank += 1
    if any(
            all(abs(value) <= tolerance for value in row[:-1])
            and abs(row[-1]) > tolerance
            for row in rows):
        raise KineticsProjectionError(
            "frozen v2 edge energies are algebraically inconsistent")
    if rank != len(state_ids):
        return None
    return {
        state_id: float(rows[pivots[column]][-1])
        for state_id, column in columns.items()
    }


class ValidatedFrozenReactionV2:
    """One-shot strict adapter from authoritative frozen v2 to source facts."""

    def __init__(
        self, frozen_reaction: Mapping[str, Any], *,
        validated_reaction: ValidatedReactionSnapshotIdentity,
        validated_frozen: ValidatedFrozenReactionAuthority,
        evidence_bindings: Mapping[str, str],
        evidence_resolver: EvidenceBindingResolver | Callable[[str], bytes],
    ):
        if not isinstance(frozen_reaction, Mapping):
            raise KineticsProjectionError("frozen v2 payload must be a mapping")
        frozen = copy.deepcopy(dict(frozen_reaction))
        _safe_tree(frozen)
        _strict_fields(frozen, _FROZEN_V2_FIELDS, "frozen reaction v2")
        if (frozen.get("schema") != FROZEN_REACTION_V2_SCHEMA
                or frozen.get("version") != "2"):
            raise KineticsProjectionError(
                "only vcstudio.frozen-reaction-network/v2 is formal")
        declared_frozen_hash = _sha256(
            frozen.get("frozen_network_sha256"), "frozen_network_sha256")
        frozen_body = copy.deepcopy(frozen)
        frozen_body.pop("frozen_network_sha256")
        frozen_bytes = _canonical_bytes(frozen_body)
        if len(frozen_bytes) > _MAX_TOTAL_EVIDENCE_BYTES:
            raise KineticsProjectionError(
                "frozen v2 payload exceeds the total evidence byte limit")
        if hashlib.sha256(frozen_bytes).hexdigest() != declared_frozen_hash:
            raise KineticsProjectionError("frozen v2 hash does not match its bytes")
        frozen_identity_loader = getattr(
            validated_frozen, "frozen_reaction_authority", None)
        if not callable(frozen_identity_loader):
            raise KineticsProjectionError(
                "a server-validated frozen/condition authority is required")
        try:
            frozen_identity = _strict_fields(
                frozen_identity_loader(),
                frozenset({
                    "schema", "frozen_network_sha256",
                    "condition_revision_id", "condition_revision_sha256",
                    "conditions_sha256",
                }),
                "validated frozen reaction authority")
        except KineticsProjectionError:
            raise
        except Exception as exc:  # noqa: BLE001 trusted seam fails closed
            raise KineticsProjectionError(
                "validated frozen reaction authority could not be detached") from exc
        if frozen_identity.get("schema") != VALIDATED_FROZEN_AUTHORITY_SCHEMA:
            raise KineticsProjectionError(
                "validated frozen reaction authority schema is unsupported")
        gate = _strict_fields(
            frozen.get("binding_status_gate"), _GATE_FIELDS,
            "frozen v2 binding_status_gate")
        gate_entries = gate.get("entries")
        if (isinstance(gate_entries, (str, bytes))
                or not isinstance(gate_entries, Sequence) or not gate_entries):
            raise KineticsProjectionError(
                "frozen v2 binding status entries are invalid")
        gate_object_ids = set()
        for index, raw_entry in enumerate(gate_entries):
            entry = _strict_fields(
                raw_entry, _GATE_ENTRY_FIELDS,
                f"frozen v2 binding status entries[{index}]")
            object_id = _opaque(
                entry.get("object_id"), "binding status object_id")
            if object_id in gate_object_ids:
                raise KineticsProjectionError(
                    "frozen v2 binding status object ids are duplicated")
            gate_object_ids.add(object_id)
            _opaque(entry.get("entity_type"), "binding status entity_type")
            if (entry.get("scientific_status")
                    not in _KINETIC_SCIENTIFIC_STATUSES
                    or entry.get("kinetic_eligible") is not True):
                raise KineticsProjectionError(
                    "frozen v2 binding status is not kinetically eligible")
        limitations = frozen.get("limitations")
        if (isinstance(limitations, (str, bytes))
                or not isinstance(limitations, Sequence) or not limitations):
            raise KineticsProjectionError(
                "frozen v2 limitations must remain explicit")
        if (frozen.get("canonical_envelope_authority") is not True
                or frozen.get("readiness") != "ready"
                or frozen.get("microkinetics_ready") is not True
                or frozen.get("authorizes_execution") is not False
                or gate.get("eligible") is not True
                or gate.get("status") != "available"
                or gate.get("blocking") != []
                or frozen.get("scientific_status")
                not in _KINETIC_SCIENTIFIC_STATUSES):
            raise KineticsProjectionError(
                "frozen v2 is blocked or lacks canonical microkinetics authority")

        identity_loader = getattr(validated_reaction, "detached_projection", None)
        if not callable(identity_loader):
            raise KineticsProjectionError(
                "a hash-bound ValidatedReactionSnapshot identity is required")
        try:
            validated_projection = identity_loader()
        except Exception as exc:  # noqa: BLE001 trusted identity fails closed
            raise KineticsProjectionError(
                "validated Reaction identity could not be detached") from exc
        project_id = _opaque(
            getattr(validated_reaction, "project_id", None),
            "validated_reaction.project_id")
        if (not isinstance(validated_projection, Mapping)
                or validated_projection.get("project_id") != project_id):
            raise KineticsProjectionError(
                "validated Reaction project identity is inconsistent")
        domain = _strict_fields(
            frozen.get("domain_authority"),
            frozenset({"authority_id", "generation", "snapshot_sha256"}),
            "frozen v2 domain_authority")
        network_identity = _strict_fields(
            frozen.get("network_identity"),
            frozenset({"network_id", "object_revision_id", "semantic_sha256"}),
            "frozen v2 network_identity")
        authority_id = domain.get("authority_id")
        generation = domain.get("generation")
        network_id = _opaque(frozen.get("network_id"), "frozen v2 network_id")
        network_revision = _opaque(
            network_identity.get("object_revision_id"),
            "frozen v2 network revision")
        source_projection_hash = _sha256(
            frozen.get("source_projection_sha256"),
            "frozen v2 source_projection_sha256")
        if (not isinstance(authority_id, str)
                or not _AUTHORITY_RE.fullmatch(authority_id)
                or isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 1
                or network_identity.get("network_id") != network_id
                or getattr(validated_reaction, "domain_authority_id", None)
                != authority_id
                or getattr(validated_reaction, "domain_generation", None)
                != generation
                or getattr(validated_reaction, "network_id", None) != network_id
                or getattr(validated_reaction, "network_revision", None)
                != network_revision
                or getattr(validated_reaction, "source_projection_sha256", None)
                != source_projection_hash):
            raise KineticsProjectionError(
                "frozen v2 authority/network identity is stale or forged")
        _sha256(domain.get("snapshot_sha256"), "domain snapshot sha256")
        _sha256(network_identity.get("semantic_sha256"), "network semantic sha256")
        _sha256(frozen.get("reaction_graph_sha256"), "reaction graph sha256")
        condition_revision_id = _opaque(
            frozen.get("condition_revision_id"), "condition revision id")
        condition_revision_sha = _sha256(
            frozen.get("condition_revision_sha256"), "condition revision sha256")
        conditions = frozen.get("conditions")
        if not isinstance(conditions, Mapping) or "temperature_k" not in conditions:
            raise KineticsProjectionError(
                "frozen v2 condition revision lacks an explicit temperature")
        normalized_conditions = {
            str(key): _finite(value, f"conditions.{key}")
            for key, value in conditions.items()
        }
        conditions_sha = hashlib.sha256(
            _canonical_bytes(dict(conditions))).hexdigest()
        if (frozen_identity.get("frozen_network_sha256")
                != declared_frozen_hash
                or frozen_identity.get("condition_revision_id")
                != condition_revision_id
                or frozen_identity.get("condition_revision_sha256")
                != condition_revision_sha
                or frozen_identity.get("conditions_sha256") != conditions_sha):
            raise KineticsProjectionError(
                "frozen v2 network or condition identity is stale or forged")
        if set(normalized_conditions) - {
                "temperature_k", "pressure_pa", "ph",
                "electrode_potential_v", "coverage"}:
            raise KineticsProjectionError("frozen v2 conditions are unsupported")
        if normalized_conditions["temperature_k"] <= 0.0:
            raise KineticsProjectionError(
                "frozen v2 temperature must be strictly positive")
        if ("pressure_pa" in normalized_conditions
                and normalized_conditions["pressure_pa"] <= 0.0):
            raise KineticsProjectionError(
                "frozen v2 pressure must be strictly positive")
        if ("electrode_potential_v" in normalized_conditions
                and abs(normalized_conditions["electrode_potential_v"]) > 1.0e-12):
            raise KineticsProjectionError(
                "nonzero frozen v2 potential lacks an electrode-reference contract")

        states_raw = frozen.get("state_catalog")
        if (isinstance(states_raw, (str, bytes))
                or not isinstance(states_raw, Sequence) or not states_raw
                or len(states_raw) > _MAX_COLLECTION):
            raise KineticsProjectionError("frozen v2 state_catalog is invalid")
        states: dict[str, dict[str, Any]] = {}
        adsorbate_surface_ids: set[str] = set()
        for index, raw_state in enumerate(states_raw):
            if not isinstance(raw_state, Mapping):
                raise KineticsProjectionError(
                    f"state_catalog[{index}] is invalid")
            object_type = raw_state.get("object_type")
            base_fields = {
                "state_id", "object_type", "object_revision_id", "semantic_sha256",
                "phase", "chemical_formula", "charge", "multiplicity",
            }
            fields = (
                frozenset(base_fields | {"standard_state"})
                if object_type == "FluidState"
                else frozenset(base_fields | {
                    "surface_id", "site_stoichiometry"})
                if object_type == "AdsorbateState" else frozenset()
            )
            state = _strict_fields(raw_state, fields, f"state_catalog[{index}]")
            state_id = _opaque(state.get("state_id"), f"state_catalog[{index}].state_id")
            if state_id in states:
                raise KineticsProjectionError("frozen v2 state ids are duplicated")
            _opaque(state.get("object_revision_id"), "state object revision")
            _sha256(state.get("semantic_sha256"), "state semantic sha256")
            charge = state.get("charge")
            multiplicity = state.get("multiplicity")
            if (isinstance(charge, bool) or not isinstance(charge, int)
                    or isinstance(multiplicity, bool) or not isinstance(multiplicity, int)
                    or multiplicity < 1):
                raise KineticsProjectionError("frozen v2 state quantum identity is invalid")
            composition = _formula_composition(
                state.get("chemical_formula"), f"state_catalog[{index}].formula")
            sites = {}
            standard_state = None
            if object_type == "FluidState":
                phase = state.get("phase")
                if phase not in {"gas", "liquid", "aqueous"}:
                    raise KineticsProjectionError("frozen v2 fluid phase is unsupported")
                standard_state = _strict_fields(
                    state.get("standard_state"),
                    frozenset({"schema", "phase", "kind", "value", "unit"}),
                    "fluid standard_state")
                expected = (
                    {
                        ("1-bar", "Pa", 100000.0),
                        ("1-atm", "Pa", 101325.0),
                    }
                    if phase == "gas"
                    else {("1-molar", "mol/L", 1.0)}
                )
                actual_standard_state = (
                    standard_state.get("kind"),
                    standard_state.get("unit"),
                    _finite(
                        standard_state.get("value"),
                        "fluid standard_state.value",
                    ),
                )
                if (standard_state.get("schema")
                        != "vcstudio.fluid-standard-state/v1"
                        or standard_state.get("phase") != phase
                        or not any(
                            actual_standard_state[:2] == candidate[:2]
                            and abs(actual_standard_state[2] - candidate[2])
                            <= 1.0e-12
                            for candidate in expected
                        )):
                    raise KineticsProjectionError("fluid standard_state is inconsistent")
            else:
                if state.get("phase") != "adsorbed":
                    raise KineticsProjectionError(
                        "AdsorbateState phase must remain adsorbed")
                adsorbate_surface_ids.add(
                    _opaque(state.get("surface_id"), "adsorbate surface_id"))
                raw_sites = state.get("site_stoichiometry")
                if not isinstance(raw_sites, Mapping) or not raw_sites:
                    raise KineticsProjectionError(
                        "AdsorbateState requires exact site stoichiometry")
                sites = {
                    _opaque(site_id, "adsorbate site id"):
                    _rational(value, "adsorbate site count", positive=True)
                    for site_id, value in raw_sites.items()
                }
            states[state_id] = {
                **copy.deepcopy(dict(state)), "_composition": composition,
                "_sites": sites, "_standard_state": (
                    None if standard_state is None else copy.deepcopy(dict(standard_state))),
            }
        if len(adsorbate_surface_ids) != 1:
            raise KineticsProjectionError(
                "frozen v2 requires exactly one catalyst surface axis; "
                "multi-surface site populations cannot be projected without loss")

        edges_raw = frozen.get("edges")
        if (isinstance(edges_raw, (str, bytes))
                or not isinstance(edges_raw, Sequence) or not edges_raw
                or len(edges_raw) > _MAX_COLLECTION):
            raise KineticsProjectionError("frozen v2 edges are invalid")
        edges: list[dict[str, Any]] = []
        edge_ids = set()
        transition_ids = set()
        endpoint_ids: set[str] = set()
        method_hashes = set()
        condition_identities = set()
        for index, raw_edge in enumerate(edges_raw):
            edge = _strict_fields(raw_edge, _EDGE_FIELDS, f"edges[{index}]")
            edge_id = _opaque(edge.get("edge_id"), f"edges[{index}].edge_id")
            if edge_id in edge_ids:
                raise KineticsProjectionError("frozen v2 edge ids are duplicated")
            edge_ids.add(edge_id)
            missing = edge.get("missing")
            conservation = _strict_fields(
                edge.get("conservation"), _CONSERVATION_FIELDS,
                f"edge {edge_id} conservation")
            if (edge.get("thermodynamic_status") != "available"
                    or edge.get("kinetic_status") != "available"
                    or missing != []
                    or conservation.get("status") != "available"
                    or conservation.get("transition_state_status") != "available"
                    or conservation.get("canonical_validator_error") is not None
                    or conservation.get("missing") != []):
                raise KineticsProjectionError(
                    f"frozen v2 edge {edge_id} is blocked or incomplete")
            transition_id = _opaque(
                edge.get("transition_state_id"),
                f"edge {edge_id} transition_state_id")
            if transition_id in transition_ids:
                raise KineticsProjectionError(
                    "frozen v2 saddle identities are duplicated")
            transition_ids.add(transition_id)
            sides: dict[str, dict[str, float]] = {}
            for side_name in ("reactants", "transition_state", "products"):
                raw_side = edge.get(side_name)
                if (isinstance(raw_side, (str, bytes))
                        or not isinstance(raw_side, Sequence) or not raw_side):
                    raise KineticsProjectionError(
                        f"edge {edge_id} {side_name} is invalid")
                side = {}
                for participant_index, raw_participant in enumerate(raw_side):
                    participant = _strict_fields(
                        raw_participant, _PARTICIPANT_FIELDS,
                        f"edge {edge_id} {side_name}[{participant_index}]")
                    state_id = _opaque(
                        participant.get("state_id"), "participant state_id")
                    state = states.get(state_id)
                    if state is None or state_id in side:
                        raise KineticsProjectionError(
                            f"edge {edge_id} participant identity is invalid")
                    coefficient = _rational(
                        participant.get("coefficient"),
                        f"edge {edge_id} coefficient", positive=True)
                    domain_phase = (
                        state["phase"] if state["object_type"] == "FluidState"
                        else "adsorbed")
                    if (participant.get("phase") != domain_phase
                            or participant.get("charge") != state["charge"]):
                        raise KineticsProjectionError(
                            f"edge {edge_id} participant authority disagrees")
                    raw_participant_sites = participant.get("site_stoichiometry")
                    if not isinstance(raw_participant_sites, Mapping):
                        raise KineticsProjectionError(
                            f"edge {edge_id} participant sites are invalid")
                    participant_sites = {
                        _opaque(site_id, "participant site id"):
                        _rational(value, "participant site count", positive=True)
                        for site_id, value in raw_participant_sites.items()
                    }
                    if participant_sites != state["_sites"]:
                        raise KineticsProjectionError(
                            f"edge {edge_id} participant sites disagree")
                    side[state_id] = coefficient
                sides[side_name] = side
            endpoint_ids.update(sides["reactants"])
            endpoint_ids.update(sides["products"])
            reactant_ids = edge.get("reactant_state_ids")
            product_ids = edge.get("product_state_ids")
            if (isinstance(reactant_ids, (str, bytes))
                    or not isinstance(reactant_ids, Sequence)
                    or list(reactant_ids) != list(sides["reactants"])):
                raise KineticsProjectionError("frozen v2 reactant identity list disagrees")
            if (isinstance(product_ids, (str, bytes))
                    or not isinstance(product_ids, Sequence)
                    or list(product_ids) != list(sides["products"])):
                raise KineticsProjectionError("frozen v2 product identity list disagrees")
            expected_stoichiometry = {
                state_id: sides["products"].get(state_id, 0.0)
                - sides["reactants"].get(state_id, 0.0)
                for state_id in set(sides["reactants"]) | set(sides["products"])
                if abs(sides["products"].get(state_id, 0.0)
                       - sides["reactants"].get(state_id, 0.0)) > 1.0e-12
            }
            raw_stoichiometry = edge.get("stoichiometry")
            if (not isinstance(raw_stoichiometry, Mapping)
                    or set(raw_stoichiometry) != set(expected_stoichiometry)):
                raise KineticsProjectionError(
                    "frozen v2 net stoichiometry identity disagrees")
            for state_id, expected_coefficient in expected_stoichiometry.items():
                actual_coefficient = _rational(
                    raw_stoichiometry[state_id],
                    f"edge {edge_id} stoichiometry")
                if abs(actual_coefficient - expected_coefficient) > 1.0e-12:
                    raise KineticsProjectionError(
                        "frozen v2 net stoichiometry identity disagrees")
            delta_g = _finite(edge.get("reaction_delta_g_eV"), "reaction delta G")
            _finite(
                edge.get("observed_activation_delta_e_eV"),
                "observed activation barrier")
            forward = _finite(edge.get("activation_delta_g_eV"), "forward barrier")
            reverse = _finite(
                edge.get("reverse_activation_delta_g_eV"), "reverse barrier")
            thermal = _finite(
                edge.get("thermal_activation_delta_g_eV"), "thermal barrier")
            if (abs(thermal - forward) > 1.0e-9
                    or abs((forward - reverse) - delta_g) > 1.0e-6
                    or forward < -1.0e-9 or reverse < -1.0e-9):
                raise KineticsProjectionError(
                    f"frozen v2 edge {edge_id} energy identities disagree")
            method_sha = _sha256(edge.get("method_sha256"), "edge method sha256")
            _sha256(edge.get("evidence_sha256"), "edge evidence sha256")
            _sha256(
                edge.get("edge_compatibility_sha256"),
                "edge compatibility sha256")
            condition_id = _opaque(
                edge.get("condition_set_id"), "edge condition_set_id")
            condition_sha = _sha256(
                edge.get("condition_set_sha256"), "edge condition_set_sha256")
            method_hashes.add(method_sha)
            condition_identities.add((condition_id, condition_sha))
            _validate_edge_evidence(edge, states, sides)
            edges.append({
                **copy.deepcopy(dict(edge)), "_sides": sides,
                "_delta_g": delta_g, "_forward_barrier": forward,
                "_reverse_barrier": reverse,
            })
        if len(method_hashes) != 1 or len(condition_identities) != 1:
            raise KineticsProjectionError(
                "frozen v2 requires one exact method and condition identity")
        if any(states[state_id]["object_type"] == "FluidState"
               and states[state_id]["phase"] == "gas" for state_id in states) \
                and "pressure_pa" not in normalized_conditions:
            raise KineticsProjectionError(
                "gas frozen v2 conditions require explicit pressure_pa")

        saddle_state_ids: set[str] = set()
        for edge in edges:
            candidates = [
                state_id for state_id, coefficient in edge["_sides"][
                    "transition_state"].items()
                if states[state_id]["object_type"] == "AdsorbateState"
                and states[state_id]["_composition"]
                and state_id not in endpoint_ids
                and abs(coefficient - 1.0) <= 1.0e-12
            ]
            if len(candidates) != 1:
                raise KineticsProjectionError(
                    f"edge {edge['edge_id']} requires one explicit AdsorbateState saddle")
            candidate = candidates[0]
            invalid_extras = [
                state_id for state_id in edge["_sides"]["transition_state"]
                if state_id != candidate and (
                    states[state_id]["object_type"] != "AdsorbateState"
                    or states[state_id]["_composition"])
            ]
            if invalid_extras:
                raise KineticsProjectionError(
                    f"edge {edge['edge_id']} transition side is not phase-1 representable")
            saddle_state_ids.add(candidate)

        required_bindings = set(required_frozen_v2_evidence_references(frozen))
        if (not isinstance(evidence_bindings, Mapping)
                or len(evidence_bindings) > MAX_EVIDENCE_BINDINGS - 1):
            raise KineticsProjectionError("frozen v2 evidence binding index is invalid")
        normalized_bindings = {}
        for reference, digest in evidence_bindings.items():
            if (not isinstance(reference, str) or not reference
                    or classify_sensitive_text(reference) is not None
                    or _CONTROL_RE.search(reference) or len(reference) > 512):
                raise KineticsProjectionError(
                    "frozen v2 evidence reference is unsafe")
            normalized_bindings[reference] = _sha256(
                digest, "frozen v2 artifact sha256")
        if set(normalized_bindings) != required_bindings:
            raise KineticsProjectionError(
                "frozen v2 evidence binding index is incomplete or excessive")
        if not callable(evidence_resolver):
            raise KineticsProjectionError("frozen v2 evidence resolver is required")
        frozen_reference = f"frozen-v2:{declared_frozen_hash}"
        evidence_bytes = {frozen_reference: frozen_bytes}
        total_evidence_bytes = len(frozen_bytes)
        for reference, digest in sorted(normalized_bindings.items()):
            try:
                raw_artifact = evidence_resolver(reference)
            except Exception as exc:  # noqa: BLE001 evidence boundary fails closed
                raise KineticsProjectionError(
                    "frozen v2 evidence could not be resolved") from exc
            if not isinstance(raw_artifact, (bytes, bytearray, memoryview)):
                raise KineticsProjectionError(
                    "frozen v2 evidence resolver must return bytes")
            artifact = bytes(raw_artifact)
            total_evidence_bytes += len(artifact)
            if (not artifact or total_evidence_bytes > _MAX_TOTAL_EVIDENCE_BYTES
                    or hashlib.sha256(artifact).hexdigest() != digest):
                raise KineticsProjectionError(
                    "frozen v2 evidence bytes do not match their binding")
            evidence_bytes[reference] = artifact

        method_sha = next(iter(method_hashes))
        method_id = f"method:{method_sha}"
        formation_energies = _linear_state_energies(states, edges)
        species = []
        for state_id in sorted(states):
            state = states[state_id]
            if state["object_type"] == "FluidState":
                phase = {
                    "gas": "gas", "liquid": "liquid", "aqueous": "solution",
                }[state["phase"]]
            elif state_id in saddle_state_ids:
                phase = "transition_state"
            elif not state["_composition"]:
                phase = "surface"
            else:
                phase = "adsorbate"
            energy = None
            if formation_energies is not None:
                energy = {
                    "value": formation_energies[state_id], "unit": "eV",
                    "method_id": method_id,
                    "source": {
                        "kind": "frozen_reaction_v2",
                        "reference": frozen_reference,
                        "evidence_sha256": declared_frozen_hash,
                    },
                }
            species.append({
                "id": state_id, "phase": phase,
                "composition": copy.deepcopy(state["_composition"]),
                "charge": state["charge"], "sites": copy.deepcopy(state["_sites"]),
                "formation_energy": energy, "frequencies_cm1": [],
                "standard_state": copy.deepcopy(state["_standard_state"]),
            })

        def energy_record(value: float) -> dict[str, Any]:
            return {
                "value": value, "unit": "eV", "method_id": method_id,
                "source": {
                    "kind": "frozen_reaction_v2", "reference": frozen_reference,
                    "evidence_sha256": declared_frozen_hash,
                },
            }

        elementary_steps = []
        for edge in edges:
            elementary_steps.append({
                "id": edge["edge_id"],
                "reactants": copy.deepcopy(edge["_sides"]["reactants"]),
                "transition_state": copy.deepcopy(
                    edge["_sides"]["transition_state"]),
                "products": copy.deepcopy(edge["_sides"]["products"]),
                "reversible": True,
                "delta_g": energy_record(edge["_delta_g"]),
                "forward_barrier": energy_record(edge["_forward_barrier"]),
                "reverse_barrier": energy_record(edge["_reverse_barrier"]),
            })

        fluid_phases = {
            record["phase"] for record in species
            if record["phase"] in {"gas", "liquid", "solution"}
        }
        standard_state: dict[str, Any] = {
            "temperature": {
                "value": normalized_conditions["temperature_k"], "unit": "K"},
            "potential": None,
        }
        operating_range: dict[str, Any] = {
            "temperature_K": [
                normalized_conditions["temperature_k"],
                normalized_conditions["temperature_k"],
            ],
            "potential_V": None,
        }
        if "gas" in fluid_phases:
            pressure_bar = normalized_conditions["pressure_pa"] / 100000.0
            standard_state["pressure"] = {"value": pressure_bar, "unit": "bar"}
            operating_range["pressure_bar"] = [pressure_bar, pressure_bar]
        solution_standard_values = {
            float(record["standard_state"]["value"])
            for record in species
            if record["phase"] in {"liquid", "solution"}
        }
        if solution_standard_values:
            if len(solution_standard_values) != 1:
                raise KineticsProjectionError(
                    "fluid molar standard states are inconsistent")
            standard_state["concentration"] = {
                "value": next(iter(solution_standard_values)), "unit": "mol/L"}

        evidence_refs = [{
            "reference": frozen_reference,
            "artifact_sha256": declared_frozen_hash,
        }, *[
            {"reference": reference, "artifact_sha256": digest}
            for reference, digest in sorted(normalized_bindings.items())
        ]]
        authority_extension = {
            "schema": "vcstudio.kinetics-frozen-reaction-authority/v1",
            "frozen_network_sha256": declared_frozen_hash,
            "source_projection_sha256": source_projection_hash,
            "reaction_graph_sha256": frozen["reaction_graph_sha256"],
            "condition_revision_id": condition_revision_id,
            "condition_revision_sha256": condition_revision_sha,
            "conditions": copy.deepcopy(normalized_conditions),
            "direct_step_energies": True,
            "qualified_stationary_points": True,
        }
        source = {
            "schema": ADAPTED_REACTION_SOURCE_SCHEMA,
            "version": ADAPTED_REACTION_SOURCE_VERSION,
            "adapter_projection_sha256": "",
            "authoritative_source": {
                "schema": FROZEN_REACTION_V2_SCHEMA, "version": "2",
                "frozen_network_sha256": declared_frozen_hash,
            },
            "project_id": project_id, "domain_authority_id": authority_id,
            "domain_generation": generation, "network_id": network_id,
            "network_revision": network_revision,
            "evidence_refs": evidence_refs,
            "standard_state": standard_state,
            "operating_range": operating_range,
            "methodology": {
                "method_id": method_id, "energy_basis": "gibbs_free_energy",
                "thermochemistry": "frozen_reaction_v2_direct_delta_g",
                "solvation": (
                    "frozen_fluid" if {"liquid", "solution"} & fluid_phases
                    else "none"),
                "potential_model": "none", "compatibility_status": "verified",
                "identity_sha256": method_sha,
            },
            "species": species, "elementary_steps": elementary_steps,
            "extensions": {"frozen_reaction_v2": authority_extension},
        }
        source["adapter_projection_sha256"] = hashlib.sha256(_canonical_bytes({
            key: value for key, value in source.items()
            if key != "adapter_projection_sha256"
        })).hexdigest()
        _safe_tree(source)
        self._source = source
        self._evidence = evidence_bytes

    def frozen_reaction_projection(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._source)

    def kinetics_evidence(self, reference: str) -> bytes:
        try:
            return bytes(self._evidence[reference])
        except KeyError as exc:
            raise KineticsProjectionError(
                "evidence reference is outside the frozen v2 adapter") from exc


class KineticsProjectionProvider:
    """Frozen provider binding one reaction snapshot, spec snapshot and evidence."""

    def __init__(
        self, reaction_projection: ValidatedReactionProjection,
        spec_snapshot: KineticsModelSpec | Mapping[str, Any],
        evidence_resolver: Callable[[str], bytes],
    ):
        loader = getattr(reaction_projection, "frozen_reaction_projection", None)
        if not callable(loader):
            raise KineticsProjectionError(
                "a ValidatedReactionProjection is required; raw mappings are untrusted")
        try:
            raw_source = loader()
        except Exception as exc:  # noqa: BLE001 server seam must fail closed
            raise KineticsProjectionError(
                "frozen reaction projection could not be snapshotted") from exc
        if not isinstance(raw_source, Mapping):
            raise KineticsProjectionError(
                "frozen_reaction_projection must return a mapping")
        source = copy.deepcopy(dict(raw_source))
        _safe_tree(source)
        hash_field, adapter_hash = _source_hash(source)
        source_hash, source_identity = _source_binding_authority(
            source, adapter_hash)
        required_fields = {
            "schema", "version", hash_field, "evidence_refs", "project_id",
            "domain_authority_id", "domain_generation", "network_id",
            "network_revision", "standard_state", "operating_range",
            "methodology", "species", "elementary_steps", "extensions",
        }
        if (source.get("schema"), source.get("version")) == (
                ADAPTED_REACTION_SOURCE_SCHEMA,
                ADAPTED_REACTION_SOURCE_VERSION):
            required_fields.add("authoritative_source")
        _required(source, required_fields, "reaction projection")
        if (source.get("schema"), source.get("version")) == (
                ADAPTED_REACTION_SOURCE_SCHEMA,
                ADAPTED_REACTION_SOURCE_VERSION) \
                and set(source) != required_fields:
            raise KineticsProjectionError(
                "adapted reaction projection fields do not match the protocol")
        forbidden = _SPEC_OWNED_TOP_LEVEL & set(source)
        if forbidden:
            raise KineticsProjectionError(
                "reaction projection contains spec-owned fields: "
                + ", ".join(sorted(forbidden)))
        generation = source.get("domain_generation")
        if (isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 0):
            raise KineticsProjectionError(
                "reaction projection domain_generation is invalid")
        if not isinstance(source.get("species"), Sequence) \
                or isinstance(source.get("species"), (str, bytes)):
            raise KineticsProjectionError("reaction projection species must be an array")
        if not isinstance(source.get("elementary_steps"), Sequence) \
                or isinstance(source.get("elementary_steps"), (str, bytes)):
            raise KineticsProjectionError(
                "reaction projection elementary_steps must be an array")

        try:
            spec = KineticsModelSpec.from_dict(
                spec_snapshot.to_dict()
                if isinstance(spec_snapshot, KineticsModelSpec)
                else spec_snapshot)
        except KineticsModelSpecError as exc:
            raise KineticsProjectionError("model spec snapshot is invalid") from exc
        binding = spec.source_binding
        comparisons = {
            "project_id": (spec.project_id, source.get("project_id")),
            "domain_authority_id": (
                binding["domain_authority_id"], source.get("domain_authority_id")),
            "domain_generation": (
                binding["domain_generation"], source.get("domain_generation")),
            "network_revision": (
                binding["network_revision"], source.get("network_revision")),
            "source_projection_sha256": (
                binding["source_projection_sha256"], source_hash),
        }
        if (source.get("schema"), source.get("version")) == (
                ADAPTED_REACTION_SOURCE_SCHEMA,
                ADAPTED_REACTION_SOURCE_VERSION):
            comparisons["network_id"] = (
                binding.get("network_id"), source.get("network_id"))
        elif "network_id" in binding:
            comparisons["network_id"] = (
                binding["network_id"], source.get("network_id"))
        stale = [name for name, pair in comparisons.items() if pair[0] != pair[1]]
        if stale:
            raise KineticsProjectionError(
                "model spec is stale for the frozen reaction source: "
                + ", ".join(stale))

        source_evidence = _evidence_index(
            source["evidence_refs"], "reaction_projection.evidence_refs")
        spec_evidence_records = spec.evidence_bindings()
        spec_evidence = {
            item["reference"]: item["artifact_sha256"]
            for item in spec_evidence_records
        }
        combined = dict(source_evidence)
        for reference, digest in spec_evidence.items():
            prior = combined.get(reference)
            if prior is not None and prior != digest:
                raise KineticsProjectionError(
                    f"evidence reference {reference!r} crosses hash authorities")
            combined[reference] = digest
        if len(combined) > MAX_EVIDENCE_BINDINGS:
            raise KineticsProjectionError(
                "source/spec evidence chain exceeds the binding limit")
        _assert_source_records_declared(source, source_evidence)

        if not callable(evidence_resolver):
            raise KineticsProjectionError("evidence_resolver must be callable")
        evidence_bytes = {}
        total_evidence_bytes = 0
        for reference, digest in sorted(combined.items()):
            try:
                artifact = evidence_resolver(reference)
            except Exception as exc:  # noqa: BLE001 opaque evidence boundary
                raise KineticsProjectionError(
                    f"evidence reference {reference!r} could not be resolved") from exc
            if not isinstance(artifact, (bytes, bytearray, memoryview)):
                raise KineticsProjectionError(
                    "evidence resolver must return artifact bytes")
            frozen = bytes(artifact)
            if not frozen or len(frozen) > _MAX_TOTAL_EVIDENCE_BYTES:
                raise KineticsProjectionError("resolved evidence has an invalid size")
            total_evidence_bytes += len(frozen)
            if total_evidence_bytes > _MAX_TOTAL_EVIDENCE_BYTES:
                raise KineticsProjectionError(
                    "resolved evidence artifacts exceed the total byte limit")
            if hashlib.sha256(frozen).hexdigest() != digest:
                raise KineticsProjectionError(
                    f"evidence byte mismatch for {reference!r}")
            evidence_bytes[reference] = frozen

        species = []
        species_by_id = {}
        for index, raw in enumerate(source["species"]):
            if not isinstance(raw, Mapping):
                raise KineticsProjectionError(
                    f"reaction projection species[{index}] must be an object")
            record = copy.deepcopy(dict(raw))
            if "activity" in record:
                raise KineticsProjectionError(
                    "reaction projection species must not duplicate spec-owned activity")
            species_id = record.get("id")
            if not isinstance(species_id, str) or species_id in species_by_id:
                raise KineticsProjectionError(
                    "reaction projection species ids must be unique strings")
            species_by_id[species_id] = record
            species.append(record)
        reservoirs = {item["species_id"]: item for item in spec.feed_reservoirs}
        fluid_ids = {
            species_id for species_id, record in species_by_id.items()
            if record.get("phase") in {"gas", "liquid", "solution"}
        }
        explicit_surface_state_ids = {
            species_id for species_id, record in species_by_id.items()
            if record.get("phase") in {"surface", "adsorbate"}
        }
        if (not fluid_ids <= set(reservoirs)
                or not set(reservoirs) <= fluid_ids | explicit_surface_state_ids):
            raise KineticsProjectionError(
                "model spec feed_reservoirs must cover every fluid species and may "
                "add only explicit surface-state reservoirs")
        for species_id, reservoir in reservoirs.items():
            record = species_by_id[species_id]
            phase = record.get("phase")
            expected_unit = "bar" if phase == "gas" else "mol/L"
            if phase in {"surface", "adsorbate"}:
                if reservoir["unit"] != "dimensionless":
                    raise KineticsProjectionError(
                        "surface-state feed reservoirs must use dimensionless activity")
                continue
            if reservoir["unit"] != expected_unit:
                raise KineticsProjectionError(
                    f"feed reservoir unit is incompatible with {species_id!r}")
            record["activity"] = {
                "value": reservoir["activity"],
                "unit": reservoir["unit"],
                "source": _activity_source(reservoir),
            }

        steps = []
        steps_by_id = {}
        for index, raw in enumerate(source["elementary_steps"]):
            if not isinstance(raw, Mapping):
                raise KineticsProjectionError(
                    f"reaction projection elementary_steps[{index}] must be an object")
            duplicated = _SPEC_OWNED_STEP_FIELDS & set(raw)
            if duplicated:
                raise KineticsProjectionError(
                    "reaction projection step contains spec-owned fields: "
                    + ", ".join(sorted(duplicated)))
            record = copy.deepcopy(dict(raw))
            step_id = record.get("id")
            if not isinstance(step_id, str) or step_id in steps_by_id:
                raise KineticsProjectionError(
                    "reaction projection step ids must be unique strings")
            steps_by_id[step_id] = record
            steps.append(record)
        spec_steps = {item["step_id"]: item for item in spec.steps}
        if set(spec_steps) != set(steps_by_id):
            raise KineticsProjectionError(
                "model spec steps must cover frozen reaction steps exactly")
        for step_id, spec_step in spec_steps.items():
            record = steps_by_id[step_id]
            record.update({
                "prefactors": copy.deepcopy(spec_step["prefactors"]),
                "bep": copy.deepcopy(spec_step["bep"]),
                "scaling": copy.deepcopy(spec_step["scaling"]),
                "uncertainty_eV": spec_step["uncertainty_eV"],
                "evidence": copy.deepcopy(spec_step["evidence"]),
            })
        if spec.saddle_selector is not None:
            for step_id, species_id in spec.saddle_selector["by_step"].items():
                transition = steps_by_id[step_id].get("transition_state")
                if not isinstance(transition, Mapping) or species_id not in transition:
                    raise KineticsProjectionError(
                        f"saddle selector for {step_id!r} is not in its transition state")

        site_types = {
            str(site_type)
            for record in species
            for site_type, count in (record.get("sites") or {}).items()
            if isinstance(count, (int, float)) and not isinstance(count, bool)
            and float(count) > 0.0
        }
        spec_site_types = {
            item["site_type"] for item in spec.site_population_totals
        }
        if spec_site_types != site_types:
            raise KineticsProjectionError(
                "site_population_totals must cover frozen site types exactly")
        missing_targets = set(spec.target_products) - set(species_by_id)
        if missing_targets:
            raise KineticsProjectionError(
                "target_products contain species absent from the frozen source")

        model_spec = {
            "schema": spec.schema,
            "project_id": spec.project_id,
            "spec_id": spec.spec_id,
            "revision": spec.revision,
            "spec_sha256": spec.semantic_sha256,
            "source_projection_sha256": source_hash,
            "evidence_refs": copy.deepcopy(spec_evidence_records),
        }
        network = {
            "schema": "vcstudio.kinetics-network/v3",
            "input_sha256": "",
            "network_id": source["network_id"],
            "revision": source["network_revision"],
            "source_projection": {
                "schema": source_identity[0],
                "version": source_identity[1],
                "projection_sha256": source_hash,
                "evidence_refs": copy.deepcopy(source["evidence_refs"]),
            },
            "model_spec": model_spec,
            "rate_law_policy": copy.deepcopy(dict(spec.rate_law_policy)),
            "assumptions": copy.deepcopy(dict(spec.assumptions)),
            "standard_state": copy.deepcopy(source["standard_state"]),
            "operating_range": copy.deepcopy(source["operating_range"]),
            "methodology": copy.deepcopy(source["methodology"]),
            "feed_species": [
                item["species_id"] for item in spec.feed_reservoirs
                if float(item["activity"]) > 0.0
            ],
            "target_products": list(spec.target_products),
            "species": species,
            "elementary_steps": steps,
            "site_population_totals": copy.deepcopy(
                list(spec.site_population_totals)),
            "extensions": copy.deepcopy(source["extensions"]),
        }
        network["input_sha256"] = hashlib.sha256(_canonical_bytes({
            key: value for key, value in network.items() if key != "input_sha256"
        })).hexdigest()
        _safe_tree(network)
        self._source = source
        self._spec = spec.to_dict()
        self._network = network
        self._evidence = evidence_bytes

    def kinetics_input(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._network)

    def kinetics_evidence(self, reference: str) -> bytes:
        try:
            return bytes(self._evidence[reference])
        except KeyError as exc:
            raise KineticsProjectionError(
                f"evidence reference {reference!r} is outside the frozen snapshot") from exc

    def canonical_source_projection(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._source)

    def kinetics_model_spec(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._spec)


__all__ = [
    "ADAPTED_REACTION_SOURCE_SCHEMA", "EvidenceBindingResolver",
    "FROZEN_REACTION_V2_SCHEMA", "KineticsProjectionError",
    "KineticsProjectionProvider", "ValidatedFrozenReactionV2",
    "ValidatedFrozenReactionAuthority",
    "ValidatedFrozenReactionAuthoritySnapshot", "ValidatedReactionProjection",
    "ValidatedReactionSnapshotIdentity",
    "required_frozen_v2_evidence_references",
]
