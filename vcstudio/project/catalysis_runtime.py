"""Fail-closed production wiring for catalysis and microkinetics.

This module is deliberately an adapter, not another scientific authority.  It
reads one project-local :class:`CatalysisProjectionAuthority` snapshot, checks
its CAS and canonical-envelope closure, and adds Workbench-only evidence only
through an explicit server adapter.  Kinetics is constructed only from that
validated value, an explicitly selected model-spec head, and an evidence-byte
resolver.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


REACTION_PROJECTION_SCHEMA = "vcstudio.reaction-domain-projection/v2"
ACTIVE_SPEC_SELECTION_SCHEMA = "vcstudio.kinetics-active-spec-selection/v2"
ACTIVE_SPEC_SELECTION_PATH = (".vcstudio", "kinetics", "active-model-spec.json")
MAX_KINETICS_EVIDENCE_BINDINGS = 256
MAX_KINETICS_EVIDENCE_BYTES = 64 * 1024 * 1024

_SNAPSHOT_SCHEMA = "vcstudio.catalysis-projection-snapshot/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
_OPAQUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:+-]{0,159}\Z")
_SCIENTIFIC_STATUSES = frozenset({
    "unknown", "unavailable", "blocked", "candidate", "machine_pass",
    "human_review", "verified", "release",
})
_ORIGINS = frozenset({"observed", "imported", "derived", "inferred", "unknown"})
_SNAPSHOT_FIELDS = frozenset({
    "schema", "status", "binding_authority", "domain_authority", "network",
    "surfaces", "states", "transition_states", "steps", "conditions",
    "evidence_gaps", "gap_summary", "job_source_of_truth",
    "authorizes_execution", "projection_sha256",
})
_DEFAULT_EVIDENCE_ADAPTER = object()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _semantic_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _detached_json_tree(value: Any) -> Any:
    """Detach JSON mappings, including immutable ``mappingproxy`` instances."""
    if isinstance(value, Mapping):
        return {str(key): _detached_json_tree(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_detached_json_tree(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("validated reaction projection contains a non-JSON value")


def _opaque(value: Any) -> str:
    text = str(value or "").strip()
    if not _OPAQUE_RE.fullmatch(text) or text in {".", ".."}:
        raise ValueError("opaque identity is invalid")
    return text


def _sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError("SHA-256 identity is invalid")
    return text


def _authority_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _AUTHORITY_RE.fullmatch(text):
        raise ValueError("authority identity is invalid")
    return text


def _is_reparse_point(path: Path) -> bool:
    details = path.lstat()
    return bool(
        path.is_symlink()
        or getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _validated_root(value: Any) -> tuple[Path, tuple[int, int]]:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("project root is unavailable")
    root = Path(value)
    if not root.is_absolute() or not root.exists() or not root.is_dir():
        raise ValueError("project root is unavailable")
    if _is_reparse_point(root):
        raise ValueError("project root is unavailable")
    resolved = root.resolve(strict=True)
    details = resolved.stat()
    return resolved, (int(details.st_dev), int(details.st_ino))


def _project_root(project: Mapping[str, Any], explicit_root: Any = None) -> Any:
    if explicit_root not in (None, ""):
        return explicit_root
    if not isinstance(project, Mapping):
        raise ValueError("private project context is unavailable")
    return project.get("root")


@runtime_checkable
class ReactionProjectionEvidenceAdapter(Protocol):
    """Server adapter for non-persistent Workbench evidence projections."""

    def projection_evidence(
        self, *, project_id: str, project: Mapping[str, Any],
        catalysis_snapshot: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Return exactly ``bindings`` and ``applicability``, or ``None``."""


class CanonicalEnvelopeBlockedEvidenceAdapter:
    """Expose canonical topology without claiming missing scientific evidence.

    The envelope semantic hash binds the exact canonical object and its opaque
    evidence-reference declarations.  It is not promoted to a structure,
    thermochemistry, frequency, or applicability observation; every binding is
    explicitly blocked.
    """

    def projection_evidence(
        self, *, project_id: str, project: Mapping[str, Any],
        catalysis_snapshot: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        del project_id, project
        from vcstudio.project.catalysis_contracts import DomainEnvelope

        records = [catalysis_snapshot.get("network")]
        for collection in ("surfaces", "states", "steps", "conditions"):
            values = catalysis_snapshot.get(collection)
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                return None
            records.extend(values)
        bindings = {}
        try:
            for raw in records:
                envelope = DomainEnvelope.from_dict(raw)
                provenance = str(envelope.payload.get("provenance") or "")
                evidence_refs = envelope.payload.get("evidence_refs")
                if (provenance not in _ORIGINS
                        or isinstance(evidence_refs, (str, bytes))
                        or not isinstance(evidence_refs, Sequence)
                        or not evidence_refs):
                    return None
                bindings[envelope.object_id] = {
                    "scientific_status": "blocked",
                    "origin": provenance,
                    "evidence_sha256": envelope.semantic_sha256,
                }
        except Exception:  # noqa: BLE001 - malformed authority remains unavailable
            return None
        return {"bindings": bindings, "applicability": {}}


@runtime_checkable
class EvidenceResolverFactory(Protocol):
    """Resolve one project-scoped immutable evidence-byte reader."""

    def for_project(
        self, private_context: Mapping[str, Any],
    ) -> Callable[[str], bytes] | None:
        """Return an opaque-reference resolver or ``None``."""


@dataclass(frozen=True)
class ValidatedReactionSnapshot:
    """Path-free in-memory hand-off shared by the production adapters."""

    project_id: str
    projection: Mapping[str, Any]
    source_projection_sha256: str
    domain_authority_id: str
    domain_generation: int
    network_id: str
    network_revision: str

    def detached_projection(self) -> dict[str, Any]:
        return _detached_json_tree(self.projection)


class FixedActiveKineticsSpecSelector:
    """Read the fixed, explicit active-spec selector; never create a default."""

    def select(
        self, *, project_root: str | os.PathLike[str], project_id: str,
    ) -> Any | None:
        root, root_identity = _validated_root(project_root)
        current = root
        for component in ACTIVE_SPEC_SELECTION_PATH[:-1]:
            current = current / component
            if not current.exists():
                return None
            if not current.is_dir() or _is_reparse_point(current):
                return None
            if current.resolve(strict=True).parent != (
                    root if component == ACTIVE_SPEC_SELECTION_PATH[0]
                    else current.parent.resolve(strict=True)):
                return None
        try:
            _same_root, current_root_identity = _validated_root(root)
            if current_root_identity != root_identity:
                return None
            from vcstudio.project.kinetics_authoring import (
                ActiveKineticsModelSpecSelectorStore,
            )

            # Production selection is delegated to the independent monotonic
            # selector authority.  Reading the JSON record alone cannot detect
            # a byte-for-byte rollback to an older, otherwise valid revision.
            selection = ActiveKineticsModelSpecSelectorStore(root).select(
                project_id=project_id)
        except Exception:  # noqa: BLE001 - fixed selector is a fail-closed seam
            return None
        return selection


@dataclass
class _AuthorityEntry:
    root: Path
    identity: tuple[int, int]
    authority: Any


class ProductionReactionDomainSource:
    """Production ``ReactionDomainSource`` over one strict catalysis snapshot."""

    def __init__(
        self, *, evidence_adapter: ReactionProjectionEvidenceAdapter | Callable[..., Any] | None | object = _DEFAULT_EVIDENCE_ADAPTER,
        authority_factory: Callable[[str | os.PathLike[str]], Any] | None = None,
    ):
        self._evidence_adapter = (
            CanonicalEnvelopeBlockedEvidenceAdapter()
            if evidence_adapter is _DEFAULT_EVIDENCE_ADAPTER
            else evidence_adapter
        )
        self._authority_factory = authority_factory
        self._authorities: dict[str, _AuthorityEntry] = {}
        self._root_owners: dict[tuple[str, tuple[int, int]], str] = {}
        self._lock = threading.RLock()

    def _authority(self, project_id: str, project_root: Any) -> Any | None:
        if project_root in (None, ""):
            with self._lock:
                existing = self._authorities.get(project_id)
                if existing is None:
                    return None
                _same_root, current_identity = _validated_root(existing.root)
                if current_identity != existing.identity:
                    return None
                return existing.authority
        root, identity = _validated_root(project_root)
        key = (os.path.normcase(str(root)), identity)
        with self._lock:
            existing = self._authorities.get(project_id)
            if existing is not None:
                if existing.root != root or existing.identity != identity:
                    return None
                _same_root, current_identity = _validated_root(existing.root)
                if current_identity != existing.identity:
                    return None
                return existing.authority
            owner = self._root_owners.get(key)
            if owner is not None and owner != project_id:
                return None
            factory = self._authority_factory
            if factory is None:
                authority_dir = root / ".vcstudio" / "catalysis"
                if (not authority_dir.exists() or not authority_dir.is_dir()
                        or _is_reparse_point(authority_dir)):
                    return None
                from vcstudio.project.catalysis_projection import (
                    CatalysisProjectionAuthority,
                )

                factory = CatalysisProjectionAuthority
            authority = factory(root)
            self._authorities[project_id] = _AuthorityEntry(root, identity, authority)
            self._root_owners[key] = project_id
            return authority

    def _evidence(
        self, *, project_id: str, project: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        adapter = self._evidence_adapter
        if adapter is None:
            return None
        loader = getattr(adapter, "projection_evidence", None)
        if callable(loader):
            value = loader(
                project_id=project_id, project=copy.deepcopy(dict(project)),
                catalysis_snapshot=copy.deepcopy(dict(snapshot)),
            )
        elif callable(adapter):
            value = adapter(
                project_id=project_id, project=copy.deepcopy(dict(project)),
                catalysis_snapshot=copy.deepcopy(dict(snapshot)),
            )
        else:
            return None
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def _validated_projection(
        *, project_id: str, snapshot: Mapping[str, Any], evidence: Mapping[str, Any],
    ) -> ValidatedReactionSnapshot:
        from vcstudio.project.catalysis_contracts import (
            DomainEnvelope,
            ReactionNetwork,
        )
        from vcstudio.project.catalysis_projection import (
            ActiveBindingAuthoritySnapshot,
        )
        from vcstudio.project.reaction_workbench import (
            build_reaction_workbench_view,
        )

        if not isinstance(snapshot, Mapping) or set(snapshot) != _SNAPSHOT_FIELDS:
            raise ValueError("catalysis snapshot is invalid")
        frozen = copy.deepcopy(dict(snapshot))
        if frozen.get("schema") != _SNAPSHOT_SCHEMA:
            raise ValueError("catalysis snapshot is invalid")
        declared_hash = _sha256(frozen.pop("projection_sha256"))
        if _semantic_sha256(frozen) != declared_hash:
            raise ValueError("catalysis snapshot is invalid")
        frozen["projection_sha256"] = declared_hash
        if (frozen.get("status") != "available"
                or frozen.get("job_source_of_truth") != "job.yaml"
                or frozen.get("authorizes_execution") is not False
                or frozen.get("transition_states") != []
                or frozen.get("evidence_gaps") != []):
            raise ValueError("catalysis snapshot is unavailable")
        summary = frozen.get("gap_summary")
        if not isinstance(summary, Mapping) or dict(summary) != {
                "total": 0, "returned": 0, "omitted": 0, "by_code": {}}:
            raise ValueError("catalysis snapshot is unavailable")

        binding_snapshot = ActiveBindingAuthoritySnapshot.from_dict(
            frozen.get("binding_authority"))
        binding = binding_snapshot.binding
        domain = frozen.get("domain_authority")
        if binding is None or not isinstance(domain, Mapping) or set(domain) != {
                "authority_id", "generation", "snapshot_sha256"}:
            raise ValueError("catalysis authority binding is unavailable")
        if (
            binding.authority_id != binding_snapshot.authority_id
            or binding.revision != binding_snapshot.revision
            or binding.binding_sha256 != binding_snapshot.current_hash
            or binding.domain_authority_id != _authority_id(domain.get("authority_id"))
            or binding.domain_generation != domain.get("generation")
            or binding.domain_snapshot_sha256 != _sha256(domain.get("snapshot_sha256"))
        ):
            raise ValueError("catalysis authority binding is stale")

        network_envelope = DomainEnvelope.from_dict(frozen.get("network"))
        if (
            network_envelope.object_type != "ReactionNetwork"
            or network_envelope.object_id != binding.network_id
            or network_envelope.object_revision_id != binding.network_revision_id
            or network_envelope.semantic_sha256 != binding.network_semantic_sha256
        ):
            raise ValueError("catalysis network binding is stale")
        network = ReactionNetwork.from_dict(network_envelope.payload)

        expected_types = {
            "surfaces": "CatalystSurface",
            "steps": "ElementaryStep", "conditions": "ConditionSet",
        }
        envelopes: dict[str, list[DomainEnvelope]] = {}
        for collection, object_type in expected_types.items():
            raw_items = frozen.get(collection)
            if isinstance(raw_items, (str, bytes)) or not isinstance(raw_items, Sequence):
                raise ValueError("catalysis closure is invalid")
            parsed = [DomainEnvelope.from_dict(item) for item in raw_items]
            if any(item.object_type != object_type for item in parsed):
                raise ValueError("catalysis closure is invalid")
            if len({item.object_id for item in parsed}) != len(parsed):
                raise ValueError("catalysis closure is invalid")
            envelopes[collection] = parsed
        raw_states = frozen.get("states")
        if (isinstance(raw_states, (str, bytes))
                or not isinstance(raw_states, Sequence)):
            raise ValueError("catalysis state closure is invalid")
        states = [DomainEnvelope.from_dict(item) for item in raw_states]
        if any(item.object_type not in {"AdsorbateState", "FluidState"}
               for item in states):
            raise ValueError("catalysis state closure is invalid")
        # One opaque state_id has exactly one canonical type.  This single ID
        # uniqueness check also rejects AdsorbateState/FluidState collisions.
        if len({item.object_id for item in states}) != len(states):
            raise ValueError("catalysis state closure contains a type collision")
        envelopes["states"] = states
        expected_ids = {
            "surfaces": list(network.surface_ids), "states": list(network.state_ids),
            "steps": list(network.step_ids),
            "conditions": list(network.condition_set_ids),
        }
        for collection, identifiers in expected_ids.items():
            if [item.object_id for item in envelopes[collection]] != identifiers:
                raise ValueError("catalysis closure is invalid")

        if not isinstance(evidence, Mapping) or set(evidence) != {
                "bindings", "applicability"}:
            raise ValueError("reaction evidence projection is invalid")
        bindings = evidence.get("bindings")
        applicability = evidence.get("applicability")
        if not isinstance(bindings, Mapping) or not isinstance(applicability, Mapping):
            raise ValueError("reaction evidence projection is invalid")
        required_binding_ids = {
            network.network_id,
            *(item.object_id for values in envelopes.values() for item in values),
        }
        if set(bindings) != required_binding_ids:
            raise ValueError("reaction evidence bindings are incomplete")
        for object_id, raw_binding in bindings.items():
            _opaque(object_id)
            if not isinstance(raw_binding, Mapping):
                raise ValueError("reaction evidence bindings are invalid")
            if str(raw_binding.get("scientific_status") or "") not in _SCIENTIFIC_STATUSES:
                raise ValueError("reaction evidence bindings are invalid")
            if str(raw_binding.get("origin") or "") not in _ORIGINS:
                raise ValueError("reaction evidence bindings are invalid")
            _sha256(raw_binding.get("evidence_sha256"))

        projection = {
            "schema": REACTION_PROJECTION_SCHEMA,
            "project_id": project_id,
            "authority": {
                "domain_authority_id": binding.domain_authority_id,
                "domain_generation": binding.domain_generation,
                "domain_snapshot_sha256": binding.domain_snapshot_sha256,
                "network_revision_id": binding.network_revision_id,
                "network_semantic_sha256": binding.network_semantic_sha256,
            },
            "network": network_envelope.to_dict(),
            "surfaces": [item.to_dict() for item in envelopes["surfaces"]],
            "states": [item.to_dict() for item in envelopes["states"]],
            "steps": [item.to_dict() for item in envelopes["steps"]],
            "conditions": [item.to_dict() for item in envelopes["conditions"]],
            "bindings": copy.deepcopy(dict(bindings)),
            "applicability": copy.deepcopy(dict(applicability)),
        }
        view = build_reaction_workbench_view(
            projection, project_id=project_id, conditions={})
        if (
            view.get("source_projection_schema") != REACTION_PROJECTION_SCHEMA
            or view.get("migration_only") is not False
            or not isinstance(view.get("graph"), Mapping)
            or view["graph"].get("canonical_envelope_authority") is not True
        ):
            raise ValueError("reaction projection did not retain canonical authority")
        return ValidatedReactionSnapshot(
            project_id=project_id,
            projection=copy.deepcopy(projection),
            source_projection_sha256=_sha256(view.get("source_projection_sha256")),
            domain_authority_id=binding.domain_authority_id,
            domain_generation=binding.domain_generation,
            network_id=binding.network_id,
            network_revision=binding.network_revision_id,
        )

    def validated_for_project(
        self, private_context: Mapping[str, Any],
    ) -> ValidatedReactionSnapshot | None:
        """Return one validated path-free snapshot, swallowing private failures."""
        try:
            if not isinstance(private_context, Mapping):
                return None
            project_id = _opaque(private_context.get("project_id"))
            project = private_context.get("project")
            if not isinstance(project, Mapping):
                return None
            root_value = _project_root(project, private_context.get("project_root"))
            authority = self._authority(project_id, root_value)
            if authority is None:
                return None
            # Exactly one authority read belongs to this resolved request.
            snapshot = authority.build_snapshot()
            if (
                not isinstance(snapshot, Mapping)
                or snapshot.get("schema") != _SNAPSHOT_SCHEMA
                or snapshot.get("status") != "available"
                or snapshot.get("evidence_gaps") != []
            ):
                return None
            evidence = self._evidence(
                project_id=project_id, project=project, snapshot=snapshot)
            if evidence is None:
                return None
            return self._validated_projection(
                project_id=project_id, snapshot=snapshot, evidence=evidence)
        except Exception:  # noqa: BLE001 - private paths/errors never cross the API
            return None

    def load_reaction_projection(
        self, *, project_id: str, project: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        validated = self.validated_for_project({
            "project_id": project_id,
            "project": project,
            "project_root": project.get("root") if isinstance(project, Mapping) else None,
        })
        return None if validated is None else validated.detached_projection()

    def for_project(
        self, private_context: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Consume the full server-private project context when it is available."""
        validated = self.validated_for_project(private_context)
        return None if validated is None else validated.detached_projection()


def _authoritative_workbench_conditions(
        validated: ValidatedReactionSnapshot) -> dict[str, float]:
    """Project one exact canonical ConditionSet value for Workbench freezing.

    Frozen reaction v2 has one condition-revision axis.  A network declaring
    multiple condition-set heads is therefore eligible only when their numeric
    conditions are identical.  No browser-supplied condition is accepted here.
    """
    from vcstudio.project.catalysis_contracts import (
        ConditionSet,
        DomainEnvelope,
        ReactionNetwork,
    )

    projection = validated.detached_projection()
    network_envelope = DomainEnvelope.from_dict(projection.get("network"))
    network = ReactionNetwork.from_dict(network_envelope.payload)
    by_id = {
        envelope.object_id: envelope
        for envelope in (
            DomainEnvelope.from_dict(item)
            for item in projection.get("conditions", ())
        )
    }
    values: list[dict[str, float]] = []
    for condition_id in network.condition_set_ids:
        envelope = by_id.get(condition_id)
        if envelope is None or envelope.object_type != "ConditionSet":
            raise ValueError("canonical condition authority is incomplete")
        condition = ConditionSet.from_dict(envelope.payload)
        record = {
            key: float(value)
            for key, value in (
                ("temperature_k", condition.temperature_k),
                ("pressure_pa", condition.pressure_pa),
                ("ph", condition.ph),
                ("electrode_potential_v", condition.electrode_potential_v),
            )
            if value is not None
        }
        values.append(record)
    if not values or "temperature_k" not in values[0]:
        raise ValueError("canonical temperature authority is unavailable")
    first = _canonical_bytes(values[0])
    if any(_canonical_bytes(item) != first for item in values[1:]):
        raise ValueError("network condition heads do not share one revision axis")
    return values[0]


def _validated_workbench_frozen(
        validated: ValidatedReactionSnapshot,
        *, workbench_builder: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Build frozen v2 and its server-sealed condition/hash authority."""
    from vcstudio.project.kinetics_projection import (
        FROZEN_REACTION_V2_SCHEMA,
        ValidatedFrozenReactionAuthoritySnapshot,
    )
    from vcstudio.project.reaction_workbench import build_reaction_workbench_view

    builder = workbench_builder or build_reaction_workbench_view
    conditions = _authoritative_workbench_conditions(validated)
    view = builder(
        validated.detached_projection(),
        project_id=validated.project_id,
        conditions=conditions,
    )
    if not isinstance(view, Mapping):
        raise ValueError("Reaction Workbench view is unavailable")
    raw_frozen = view.get("frozen_network")
    if not isinstance(raw_frozen, Mapping):
        raise ValueError("Reaction Workbench frozen v2 is unavailable")
    frozen = copy.deepcopy(dict(raw_frozen))
    if (
        frozen.get("schema") != FROZEN_REACTION_V2_SCHEMA
        or frozen.get("version") != "2"
        or frozen.get("source_projection_sha256")
        != validated.source_projection_sha256
    ):
        raise ValueError("Reaction Workbench frozen v2 authority is inconsistent")
    frozen_hash = _sha256(frozen.get("frozen_network_sha256"))
    body = copy.deepcopy(frozen)
    body.pop("frozen_network_sha256", None)
    if _semantic_sha256(body) != frozen_hash:
        raise ValueError("Reaction Workbench frozen v2 hash is invalid")
    frozen_authority = ValidatedFrozenReactionAuthoritySnapshot(
        frozen_network_sha256=frozen_hash,
        condition_revision_id=frozen.get("condition_revision_id"),
        condition_revision_sha256=frozen.get("condition_revision_sha256"),
        conditions_sha256=_semantic_sha256(frozen.get("conditions")),
    )
    return frozen, frozen_authority


def _frozen_surface_axes(frozen: Mapping[str, Any]) -> frozenset[str]:
    """Return explicit surface axes that carry site populations."""
    return frozenset(
        str(item.get("surface_id"))
        for item in frozen.get("state_catalog", ())
        if (
            isinstance(item, Mapping)
            and item.get("object_type") == "AdsorbateState"
            and item.get("surface_id")
            and item.get("site_stoichiometry")
        )
    )


class _RequestEvidenceSnapshot:
    """Snapshot resolver bytes once and expose an exact binding index."""

    def __init__(self, resolver: Callable[[str], bytes]):
        if not callable(resolver):
            raise ValueError("evidence resolver is unavailable")
        self._resolver = resolver
        self._artifacts: dict[str, bytes] = {}
        self._total_bytes = 0

    def __call__(self, reference: str) -> bytes:
        if reference not in self._artifacts:
            value = self._resolver(reference)
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise ValueError("evidence resolver must return bytes")
            artifact = bytes(value)
            total = self._total_bytes + len(artifact)
            if (
                not artifact
                or len(artifact) > MAX_KINETICS_EVIDENCE_BYTES
                or total > MAX_KINETICS_EVIDENCE_BYTES
            ):
                raise ValueError("evidence artifacts exceed their byte limit")
            self._artifacts[reference] = artifact
            self._total_bytes = total
        return bytes(self._artifacts[reference])

    def bindings(self, references: Sequence[str]) -> dict[str, str]:
        if (
            isinstance(references, (str, bytes))
            or len(references) > MAX_KINETICS_EVIDENCE_BINDINGS - 1
            or len(set(references)) != len(references)
        ):
            raise ValueError("evidence binding references are invalid")
        return {
            reference: hashlib.sha256(self(reference)).hexdigest()
            for reference in references
        }


class _ProviderEvidenceResolver:
    def __init__(
        self, adapted: Any, snapshot: _RequestEvidenceSnapshot,
        adapted_references: Sequence[str],
    ):
        self._adapted = adapted
        self._snapshot = snapshot
        self._adapted_references = frozenset(adapted_references)

    def __call__(self, reference: str) -> bytes:
        if reference in self._adapted_references:
            loader = getattr(self._adapted, "kinetics_evidence", None)
            if not callable(loader):
                raise ValueError("adapted evidence authority is unavailable")
            return loader(reference)
        return self._snapshot(reference)


class ProductionKineticsProviderFactory:
    """Project-aware factory exposed through ``Api._kinetics_projection``."""

    def __init__(
        self, *, reaction_source: ProductionReactionDomainSource,
        active_spec_selector: Any | None = None,
        evidence_resolver_factory: EvidenceResolverFactory | Callable[..., Any] | None = None,
        spec_store_factory: Callable[[str | os.PathLike[str]], Any] | None = None,
        provider_factory: Callable[..., Any] | None = None,
        workbench_builder: Callable[..., Any] | None = None,
    ):
        self._reaction_source = reaction_source
        self._active_spec_selector = (
            active_spec_selector or FixedActiveKineticsSpecSelector())
        self._evidence_resolver_factory = evidence_resolver_factory
        self._spec_store_factory = spec_store_factory
        self._provider_factory = provider_factory
        self._workbench_builder = workbench_builder

    def _resolver(self, private_context: Mapping[str, Any]) -> Callable[[str], bytes] | None:
        factory = self._evidence_resolver_factory
        if factory is None:
            return None
        loader = getattr(factory, "for_project", None)
        value = loader(private_context) if callable(loader) else factory(private_context)
        return value if callable(value) else None

    @staticmethod
    def _validated_reaction_matches(
        private_context: Mapping[str, Any], validated: Any,
    ) -> bool:
        """Accept only the exact path-free object emitted by the production source."""
        if type(validated) is not ValidatedReactionSnapshot:
            return False
        try:
            project_id = _opaque(private_context.get("project_id"))
            authority = validated.projection.get("authority")
            if (
                validated.project_id != project_id
                or validated.projection.get("schema") != REACTION_PROJECTION_SCHEMA
                or validated.projection.get("project_id") != project_id
                or not isinstance(authority, Mapping)
                or set(authority) != {
                    "domain_authority_id", "domain_generation",
                    "domain_snapshot_sha256", "network_revision_id",
                    "network_semantic_sha256",
                }
                or _authority_id(validated.domain_authority_id)
                != validated.domain_authority_id
                or authority.get("domain_authority_id")
                != validated.domain_authority_id
                or isinstance(validated.domain_generation, bool)
                or not isinstance(validated.domain_generation, int)
                or validated.domain_generation < 1
                or authority.get("domain_generation")
                != validated.domain_generation
                or _sha256(authority.get("domain_snapshot_sha256"))
                != authority.get("domain_snapshot_sha256")
                or _opaque(validated.network_id) != validated.network_id
                or _opaque(validated.network_revision) != validated.network_revision
                or authority.get("network_revision_id")
                != validated.network_revision
                or _sha256(authority.get("network_semantic_sha256"))
                != authority.get("network_semantic_sha256")
                or _sha256(validated.source_projection_sha256)
                != validated.source_projection_sha256
            ):
                return False
            from vcstudio.project.catalysis_contracts import DomainEnvelope

            network = DomainEnvelope.from_dict(validated.projection.get("network"))
            return bool(
                network.object_type == "ReactionNetwork"
                and network.object_id == validated.network_id
                and network.object_revision_id == validated.network_revision
            )
        except Exception:  # noqa: BLE001 - untrusted reuse hint fails closed
            return False

    def _for_validated_project(
        self, private_context: Mapping[str, Any],
        validated: ValidatedReactionSnapshot,
    ) -> Any | None:
        project_root, _identity = _validated_root(private_context.get("project_root"))
        selection = self._active_spec_selector.select(
            project_root=project_root, project_id=validated.project_id)
        if selection is None:
            return None

        store_factory = self._spec_store_factory
        if store_factory is None:
            store_path = project_root.joinpath(
                ".vcstudio", "kinetics", "model-spec", "store.json")
            if not store_path.exists() or not store_path.is_file():
                return None
            from vcstudio.project.kinetics_model_spec import KineticsModelSpecStore

            store_factory = KineticsModelSpecStore
        store = store_factory(project_root)
        spec = store.head(validated.project_id, selection.spec_id)
        if (
            spec is None
            or spec.project_id != validated.project_id
            or spec.revision != selection.spec_revision
            or spec.semantic_sha256 != selection.spec_sha256
        ):
            return None
        frozen, frozen_authority = _validated_workbench_frozen(
            validated, workbench_builder=self._workbench_builder)
        if len(_frozen_surface_axes(frozen)) > 1:
            # Kinetics v3/site_population_totals currently has only a bare
            # site_type key.  It cannot preserve independent (surface, site)
            # axes without silently merging same-named sites.
            return None
        source_hash = _sha256(frozen.get("frozen_network_sha256"))
        binding = spec.source_binding
        if (
            binding.get("domain_authority_id") != validated.domain_authority_id
            or binding.get("domain_generation") != validated.domain_generation
            or binding.get("network_id") != validated.network_id
            or binding.get("network_revision") != validated.network_revision
            or binding.get("source_projection_sha256") != source_hash
        ):
            return None
        resolver = self._resolver(private_context)
        if resolver is None:
            return None
        from vcstudio.project.kinetics_projection import (
            ValidatedFrozenReactionV2,
            required_frozen_v2_evidence_references,
        )

        evidence_snapshot = _RequestEvidenceSnapshot(resolver)
        references = required_frozen_v2_evidence_references(frozen)
        evidence_bindings = evidence_snapshot.bindings(references)
        adapted = ValidatedFrozenReactionV2(
            frozen,
            validated_reaction=validated,
            validated_frozen=frozen_authority,
            evidence_bindings=evidence_bindings,
            evidence_resolver=evidence_snapshot,
        )
        adapted_projection = adapted.frozen_reaction_projection()
        adapted_references = tuple(
            item["reference"]
            for item in adapted_projection.get("evidence_refs", ())
            if isinstance(item, Mapping) and isinstance(item.get("reference"), str)
        )
        provider_factory = self._provider_factory
        if provider_factory is None:
            from vcstudio.project.kinetics_projection import KineticsProjectionProvider

            provider_factory = KineticsProjectionProvider
        return provider_factory(
            adapted,
            spec,
            _ProviderEvidenceResolver(
                adapted, evidence_snapshot, adapted_references),
        )

    def for_project_with_reaction(
        self, private_context: Mapping[str, Any], *, validated_reaction: Any,
    ) -> Any | None:
        """Reuse one request-frozen source, or re-read if the hint is invalid."""
        try:
            if validated_reaction is None:
                return None
            if not self._validated_reaction_matches(
                    private_context, validated_reaction):
                return self.for_project(private_context)
            return self._for_validated_project(
                private_context, validated_reaction)
        except Exception:  # noqa: BLE001 - unavailable is the production contract
            return None

    def for_project(self, private_context: Mapping[str, Any]) -> Any | None:
        try:
            validated = self._reaction_source.validated_for_project(private_context)
            if validated is None:
                return None
            if not self._validated_reaction_matches(private_context, validated):
                return None
            return self._for_validated_project(private_context, validated)
        except Exception:  # noqa: BLE001 - unavailable is the production contract
            return None


class _ProductionCatalysisAuthoringSnapshotAdapter:
    """Read binding and all domain heads under one authority/domain lock pair."""

    def __init__(self, authority: Any, project_id: str):
        self._authority = authority
        self._project_id = project_id

    @staticmethod
    def _gap(raw: Mapping[str, Any]) -> Any:
        from vcstudio.project.catalysis_authoring import CatalysisAuthoringGap

        return CatalysisAuthoringGap(
            status=raw["status"],
            code=raw["code"],
            object_type=raw["object_type"],
            object_id=raw.get("object_id"),
            context={},
        )

    def _candidate(
        self, binding: Any, domain: Any, index: Any, network_id: str,
    ) -> Any:
        from vcstudio.project import catalysis_projection as projection_module
        from vcstudio.project.catalysis_authoring import (
            CatalysisAuthoringCandidateSnapshot,
            CatalysisAuthoringGapSummary,
            MAX_GAPS,
        )

        network_cas = projection_module._network_cas(  # noqa: SLF001
            domain, index, network_id)
        network = index.head("ReactionNetwork", network_id)
        if network is None:
            raw_gaps = [{
                "status": "missing",
                "code": "network_missing",
                "object_type": "ReactionNetwork",
                "object_id": network_id,
            }]
            status = "unavailable"
        else:
            parsed = projection_module.ReactionNetwork.from_dict(network.payload)
            resource = projection_module._network_resource_gap(parsed)  # noqa: SLF001
            if resource is not None:
                raw_gaps = [resource]
                status = "unavailable"
            else:
                projection = projection_module._build_closure(  # noqa: SLF001
                    binding, domain, network, index)
                raw_gaps = list(projection["evidence_gaps"])
                status = projection["status"]
        gaps = tuple(sorted(
            (self._gap(item) for item in raw_gaps[:MAX_GAPS]),
            key=lambda item: _canonical_bytes(item.to_dict()),
        ))
        by_code: dict[str, int] = {}
        for item in raw_gaps:
            code = str(item["code"])
            by_code[code] = by_code.get(code, 0) + 1
        return CatalysisAuthoringCandidateSnapshot(
            project_id=self._project_id,
            binding=binding,
            network_head=network_cas,
            projection_status=status,
            gaps=gaps,
            gap_summary=CatalysisAuthoringGapSummary(
                total=len(raw_gaps),
                returned=len(gaps),
                omitted=len(raw_gaps) - len(gaps),
                by_code=by_code,
            ),
        )

    def _bootstrap_from_domain(
        self, binding: Any, domain: Any, index: Any,
    ) -> Any:
        from vcstudio.project.catalysis_authoring import (
            CatalysisAuthoringBootstrap,
            CatalysisAuthoringDomainCAS,
            MAX_CANDIDATES,
        )

        if domain is None:
            return CatalysisAuthoringBootstrap(
                project_id=self._project_id,
                status="missing",
                reason="domain_authority_missing",
                domain_cas=None,
                binding_cas=binding,
                candidates=(),
            )
        domain_cas = CatalysisAuthoringDomainCAS(
            authority_id=domain.authority_id,
            generation=domain.generation,
            snapshot_sha256=domain.snapshot_sha256,
        )
        network_ids = sorted(
            envelope.object_id
            for envelope in domain.heads
            if envelope.object_type == "ReactionNetwork"
        )
        if len(network_ids) > MAX_CANDIDATES:
            return CatalysisAuthoringBootstrap(
                project_id=self._project_id,
                status="unavailable",
                reason="network_catalog_resource_limit",
                domain_cas=domain_cas,
                binding_cas=binding,
                candidates=(),
            )
        candidates = tuple(
            self._candidate(binding, domain, index, network_id).to_candidate()
            for network_id in network_ids
        )
        return CatalysisAuthoringBootstrap(
            project_id=self._project_id,
            status="available",
            reason=None,
            domain_cas=domain_cas,
            binding_cas=binding,
            candidates=candidates,
        )

    @staticmethod
    def _shared_authority(domain: Any, binding: Any) -> dict[str, Any] | None:
        if domain is None:
            return None
        selected = binding.binding
        return {
            "schema": "vcstudio.catalysis-authoring-shared-authority/v1",
            "domain_authority_id": domain.authority_id,
            "domain_generation": domain.generation,
            "domain_snapshot_sha256": domain.snapshot_sha256,
            "network_id": None if selected is None else selected.network_id,
            "network_revision_id": (
                None if selected is None else selected.network_revision_id),
            "network_semantic_sha256": (
                None if selected is None else selected.network_semantic_sha256),
        }

    def combined_bootstrap_snapshot(self) -> Mapping[str, Any]:
        """Freeze Active summary and selected closure under one domain lock."""
        from vcstudio.project import catalysis_projection as projection_module

        with self._authority._locked():  # noqa: SLF001
            store = self._authority._read_binding_store()  # noqa: SLF001
            binding = projection_module._snapshot_from_store(store)  # noqa: SLF001
            with self._authority.domain_store.locked_snapshot_heads(
                    initialize=False) as domain:
                index = (
                    None if domain is None
                    else projection_module._HeadIndex.build(domain)  # noqa: SLF001
                )
                active = self._bootstrap_from_domain(
                    binding, domain, index)
                shared = self._shared_authority(domain, binding)
                selected = binding.binding
                if domain is None:
                    return {
                        "status": "unavailable",
                        "reason": "domain_authority_missing",
                        "active_network": active,
                        "catalysis_snapshot": None,
                        "shared_authority": shared,
                    }
                if selected is None:
                    return {
                        "status": "missing_prerequisite",
                        "reason": "active_network_binding_missing",
                        "active_network": active,
                        "catalysis_snapshot": None,
                        "shared_authority": shared,
                    }
                network = index.head("ReactionNetwork", selected.network_id)
                stale = bool(
                    selected.domain_authority_id != domain.authority_id
                    or selected.domain_generation != domain.generation
                    or selected.domain_snapshot_sha256 != domain.snapshot_sha256
                    or network is None
                    or network.object_revision_id != selected.network_revision_id
                    or network.semantic_sha256 != selected.network_semantic_sha256
                )
                if stale:
                    return {
                        "status": "conflict",
                        "reason": "stale_active_network_binding",
                        "active_network": active,
                        "catalysis_snapshot": None,
                        "shared_authority": shared,
                    }
                parsed = projection_module.ReactionNetwork.from_dict(
                    network.payload)
                if projection_module._network_resource_gap(parsed) is not None:  # noqa: SLF001
                    return {
                        "status": "unavailable",
                        "reason": "network_resource_limit",
                        "active_network": active,
                        "catalysis_snapshot": None,
                        "shared_authority": shared,
                    }
                snapshot = projection_module._build_closure(  # noqa: SLF001
                    binding, domain, network, index)
                if snapshot.get("status") != "available":
                    return {
                        "status": "unavailable",
                        "reason": "active_network_projection_unavailable",
                        "active_network": active,
                        "catalysis_snapshot": None,
                        "shared_authority": shared,
                    }
                return {
                    "status": "available",
                    "reason": None,
                    "active_network": active,
                    "catalysis_snapshot": snapshot,
                    "shared_authority": shared,
                }

    def authoring_bootstrap_snapshot(self) -> Any:
        return self.combined_bootstrap_snapshot()["active_network"]

    def authoring_candidate_snapshot(self, network_id: str) -> Any | None:
        from vcstudio.project import catalysis_projection as projection_module

        with self._authority._locked():  # noqa: SLF001
            store = self._authority._read_binding_store()  # noqa: SLF001
            binding = projection_module._snapshot_from_store(store)  # noqa: SLF001
            with self._authority.domain_store.locked_snapshot_heads(
                    initialize=False) as domain:
                if domain is None:
                    return None
                index = projection_module._HeadIndex.build(domain)  # noqa: SLF001
                return self._candidate(binding, domain, index, network_id)


@dataclass
class _AuthoringServiceEntry:
    root: Path
    identity: tuple[int, int]
    service: Any


class ProductionCatalysisAuthoringServiceFactory:
    """Project-pinned Active Network service factory with one process key."""

    def __init__(
        self, *, reaction_source: ProductionReactionDomainSource,
        preview_master_key: bytes | None = None,
    ):
        self._reaction_source = reaction_source
        self._master_key = bytes(preview_master_key or os.urandom(32))
        if len(self._master_key) < 32:
            raise ValueError("authoring preview master key is too short")
        self._entries: dict[str, _AuthoringServiceEntry] = {}
        self._snapshot_adapters: dict[
            str, _ProductionCatalysisAuthoringSnapshotAdapter] = {}
        self._lock = threading.RLock()

    def for_project(self, private_context: Mapping[str, Any]) -> Any | None:
        try:
            project_id = _opaque(private_context.get("project_id"))
            root, identity = _validated_root(private_context.get("project_root"))
            authority = self._reaction_source._authority(  # noqa: SLF001
                project_id, root)
            if authority is None:
                return None
            with self._lock:
                entry = self._entries.get(project_id)
                if entry is not None:
                    if entry.root != root or entry.identity != identity:
                        return None
                    _same_root, current_identity = _validated_root(entry.root)
                    return entry.service if current_identity == entry.identity else None
                from vcstudio.project.catalysis_authoring import (
                    CatalysisActiveNetworkAuthoringService,
                )

                key = hmac.new(
                    self._master_key,
                    b"vcstudio.catalysis-authoring-preview/v1\0"
                    + project_id.encode("utf-8"),
                    hashlib.sha256,
                ).digest()
                owner = "process-" + hmac.new(
                    self._master_key,
                    b"vcstudio.catalysis-authoring-owner/v1\0"
                    + project_id.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:48]
                snapshot_adapter = _ProductionCatalysisAuthoringSnapshotAdapter(
                    authority, project_id)
                service = CatalysisActiveNetworkAuthoringService(
                    project_id=project_id,
                    owner_id=owner,
                    snapshot_authority=snapshot_adapter,
                    binding_authority=authority,
                    preview_seal_key=key,
                )
                self._entries[project_id] = _AuthoringServiceEntry(
                    root=root, identity=identity, service=service)
                self._snapshot_adapters[project_id] = snapshot_adapter
                return service
        except Exception:  # noqa: BLE001 - private factory boundary fails closed
            return None

    def combined_bootstrap(
        self, private_context: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Return one Active/Reaction value frozen at one domain generation."""
        service = self.for_project(private_context)
        if service is None:
            return None
        project_id = _opaque(private_context.get("project_id"))
        project = private_context.get("project")
        if not isinstance(project, Mapping):
            return None
        with self._lock:
            entry = self._entries.get(project_id)
            adapter = self._snapshot_adapters.get(project_id)
            if entry is None or entry.service is not service or adapter is None:
                return None
        sealed = dict(adapter.combined_bootstrap_snapshot())
        sealed["validated_reaction"] = None
        if sealed.get("status") != "available":
            return sealed
        snapshot = sealed.get("catalysis_snapshot")
        if not isinstance(snapshot, Mapping):
            sealed.update({
                "status": "unavailable",
                "reason": "active_network_projection_unavailable",
            })
            return sealed
        try:
            evidence = self._reaction_source._evidence(  # noqa: SLF001
                project_id=project_id,
                project=project,
                snapshot=snapshot,
            )
            if evidence is None:
                raise ValueError("reaction evidence authority is unavailable")
            sealed["validated_reaction"] = (
                self._reaction_source._validated_projection(  # noqa: SLF001
                    project_id=project_id,
                    snapshot=snapshot,
                    evidence=evidence,
                ))
        except Exception:  # noqa: BLE001 - combined authority fails closed
            sealed.update({
                "status": "unavailable",
                "reason": "reaction_evidence_authority_unavailable",
                "validated_reaction": None,
            })
        return sealed


class _ProductionKineticsAuthoringSourceAuthority:
    def __init__(
        self, *, reaction_source: ProductionReactionDomainSource,
        private_context: Mapping[str, Any], resolver_available: bool,
        workbench_builder: Callable[..., Any] | None = None,
    ):
        self._reaction_source = reaction_source
        self._private_context = copy.deepcopy(dict(private_context))
        self._resolver_available = resolver_available
        self._workbench_builder = workbench_builder

    @staticmethod
    def _evidence_catalog(frozen: Mapping[str, Any]) -> list[dict[str, Any]]:
        from vcstudio.project.kinetics_projection import (
            required_frozen_v2_evidence_references,
        )

        catalog: dict[str, dict[str, Any]] = {}
        for edge in frozen.get("edges", ()):
            if not isinstance(edge, Mapping):
                continue
            for evidence in edge.get("evidence_refs", ()):
                if not isinstance(evidence, Mapping):
                    continue
                for resolver_ref in evidence.get("resolver_refs", ()):
                    if not isinstance(resolver_ref, Mapping):
                        continue
                    reference = str(resolver_ref.get("opaque_id") or "")
                    kind = str(resolver_ref.get("ref_type") or "evidence")
                    if reference:
                        catalog.setdefault(reference, {
                            "reference_id": reference,
                            "kind": kind,
                            "artifact_sha256": None,
                        })
        for reference in required_frozen_v2_evidence_references(frozen):
            catalog.setdefault(reference, {
                "reference_id": reference,
                "kind": "frozen_reaction_evidence",
                "artifact_sha256": None,
            })
        return [catalog[key] for key in sorted(catalog)]

    def snapshot_from_validated(self, validated: Any) -> Any:
        """Derive authoring choices from an already request-frozen source."""
        from vcstudio.project.kinetics_authoring import AuthoringSourceSnapshot

        if validated is None:
            raise ValueError("authoritative Reaction source is unavailable")
        frozen, _authority = _validated_workbench_frozen(
            validated, workbench_builder=self._workbench_builder)
        states = {
            str(item.get("state_id")): item
            for item in frozen.get("state_catalog", ())
            if isinstance(item, Mapping)
        }
        feed_ids = sorted(
            state_id for state_id, item in states.items()
            if item.get("object_type") == "FluidState")
        edges = [
            item for item in frozen.get("edges", ()) if isinstance(item, Mapping)]
        target_ids = sorted({
            str(state_id)
            for edge in edges
            for state_id in edge.get("product_state_ids", ())
            if state_id in states
        })
        step_ids = [str(edge.get("edge_id")) for edge in edges]
        site_ids = sorted({
            str(site_id)
            for item in states.values()
            for site_id in (item.get("site_stoichiometry") or {})
        })
        saddles = {}
        for edge in edges:
            candidates = []
            for participant in edge.get("transition_state", ()):
                if not isinstance(participant, Mapping):
                    continue
                state_id = str(participant.get("state_id") or "")
                state = states.get(state_id) or {}
                if (state.get("object_type") == "AdsorbateState"
                        and state.get("chemical_formula") != "*"):
                    candidates.append(state_id)
            saddles[str(edge.get("edge_id"))] = sorted(set(candidates))
        solver_reasons = []
        if frozen.get("microkinetics_ready") is not True:
            solver_reasons.append("frozen_reaction_not_solver_ready")
        if len(_frozen_surface_axes(frozen)) > 1:
            solver_reasons.append("multiple_surface_axes_unsupported")
        if not self._resolver_available:
            solver_reasons.append("evidence_resolver_unavailable")
        domain = frozen.get("domain_authority") or {}
        network = frozen.get("network_identity") or {}
        return AuthoringSourceSnapshot(
            project_id=validated.project_id,
            domain_authority_id=domain.get("authority_id"),
            domain_generation=domain.get("generation"),
            domain_snapshot_sha256=domain.get("snapshot_sha256"),
            network_id=frozen.get("network_id"),
            network_revision=network.get("object_revision_id"),
            network_semantic_sha256=network.get("semantic_sha256"),
            source_projection_sha256=frozen.get("frozen_network_sha256"),
            required_feed_reservoir_ids=feed_ids,
            allowed_target_product_ids=target_ids,
            required_step_ids=step_ids,
            required_site_type_ids=site_ids,
            saddle_candidates_by_step=saddles,
            evidence_catalog=self._evidence_catalog(frozen),
            solver_ready=not solver_reasons,
            solver_readiness_reasons=solver_reasons,
        )

    def authoring_source_snapshot(self) -> Any:
        validated = self._reaction_source.validated_for_project(
            self._private_context)
        return self.snapshot_from_validated(validated)


@dataclass
class _KineticsAuthoringEntry(_AuthoringServiceEntry):
    model_store: Any
    source_authority: Any


class ProductionKineticsAuthoringServiceFactory:
    """Pinned coordinator factory; only bootstrap may initialize its store."""

    def __init__(
        self, *, reaction_source: ProductionReactionDomainSource,
        evidence_resolver_factory: EvidenceResolverFactory | Callable[..., Any] | None = None,
        workbench_builder: Callable[..., Any] | None = None,
    ):
        self._reaction_source = reaction_source
        self._evidence_resolver_factory = evidence_resolver_factory
        self._workbench_builder = workbench_builder
        self._entries: dict[str, _KineticsAuthoringEntry] = {}
        self._lock = threading.RLock()

    def _resolver(self, private_context: Mapping[str, Any]) -> Callable[[str], bytes] | None:
        factory = self._evidence_resolver_factory
        if factory is None:
            return None
        loader = getattr(factory, "for_project", None)
        value = loader(private_context) if callable(loader) else factory(private_context)
        return value if callable(value) else None

    def for_project(
        self, private_context: Mapping[str, Any], *, initialize: bool = False,
    ) -> Any | None:
        try:
            project_id = _opaque(private_context.get("project_id"))
            root, identity = _validated_root(private_context.get("project_root"))
            store_path = root.joinpath(
                ".vcstudio", "kinetics", "model-spec", "store.json")
            with self._lock:
                entry = self._entries.get(project_id)
                if entry is not None:
                    if entry.root != root or entry.identity != identity:
                        return None
                    _same_root, current_identity = _validated_root(entry.root)
                    return entry.service if current_identity == entry.identity else None
                if not initialize and not store_path.is_file():
                    return None
                from vcstudio.project.kinetics_authoring import (
                    KineticsAuthoringCoordinator,
                )
                from vcstudio.project.kinetics_model_spec import KineticsModelSpecStore

                resolver = self._resolver(private_context)
                source_authority = _ProductionKineticsAuthoringSourceAuthority(
                    reaction_source=self._reaction_source,
                    private_context=private_context,
                    resolver_available=resolver is not None,
                    workbench_builder=self._workbench_builder,
                )
                model_store = KineticsModelSpecStore(root)
                service = KineticsAuthoringCoordinator(
                    root,
                    source_authority=source_authority,
                    evidence_resolver=resolver,
                    model_store=model_store,
                )
                entry = _KineticsAuthoringEntry(
                    root=root,
                    identity=identity,
                    service=service,
                    model_store=model_store,
                    source_authority=source_authority,
                )
                self._entries[project_id] = entry
                return service
        except Exception:  # noqa: BLE001 - private factory boundary fails closed
            return None

    def bootstrap(self, private_context: Mapping[str, Any]) -> Mapping[str, Any] | None:
        service = self.for_project(private_context, initialize=True)
        if service is None:
            return None
        project_id = _opaque(private_context.get("project_id"))
        with self._lock:
            entry = self._entries.get(project_id)
            if entry is None or entry.service is not service:
                return None
        store = entry.model_store.snapshot()
        selector = service.selector_snapshot()
        try:
            source = entry.source_authority.authoring_source_snapshot()
            if "evidence_resolver_unavailable" in source.solver_readiness_reasons:
                status, reason = (
                    "missing_prerequisite", "evidence_resolver_unavailable")
            else:
                status, reason = "available", None
        except Exception:  # noqa: BLE001 - unavailable source remains path-free
            source = None
            status, reason = "unavailable", "source_authority_unavailable"
        return {
            "status": status,
            "reason": reason,
            "source": source,
            "model_store": store,
            "selector": selector,
        }

    def bootstrap_from_validated(
        self,
        private_context: Mapping[str, Any],
        validated_reaction: Any,
        *,
        upstream_status: str = "available",
        upstream_reason: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Bootstrap without re-reading live Reaction/domain authority state."""
        service = self.for_project(private_context, initialize=True)
        if service is None:
            return None
        project_id = _opaque(private_context.get("project_id"))
        with self._lock:
            entry = self._entries.get(project_id)
            if entry is None or entry.service is not service:
                return None
        store = entry.model_store.snapshot()
        selector = service.selector_snapshot()
        if upstream_status != "available" or validated_reaction is None:
            return {
                "status": upstream_status,
                "reason": upstream_reason or "source_authority_unavailable",
                "source": None,
                "model_store": store,
                "selector": selector,
            }
        try:
            source = entry.source_authority.snapshot_from_validated(
                validated_reaction)
            if "evidence_resolver_unavailable" in source.solver_readiness_reasons:
                status, reason = (
                    "missing_prerequisite", "evidence_resolver_unavailable")
            else:
                status, reason = "available", None
        except Exception:  # noqa: BLE001 - frozen source failure is unavailable
            source = None
            status, reason = "unavailable", "source_authority_unavailable"
        return {
            "status": status,
            "reason": reason,
            "source": source,
            "model_store": store,
            "selector": selector,
        }


def create_production_catalysis_services() -> tuple[
        ProductionReactionDomainSource, ProductionKineticsProviderFactory]:
    """Create one pair sharing the same project/root authority cache."""
    reaction_source = ProductionReactionDomainSource()
    kinetics_provider = ProductionKineticsProviderFactory(
        reaction_source=reaction_source)
    return reaction_source, kinetics_provider


def create_production_authoring_services(
    reaction_source: ProductionReactionDomainSource,
    *, evidence_resolver_factory: EvidenceResolverFactory | Callable[..., Any] | None = None,
) -> tuple[
    ProductionCatalysisAuthoringServiceFactory,
    ProductionKineticsAuthoringServiceFactory,
]:
    """Create process-stable, project-pinned authoring service factories."""
    return (
        ProductionCatalysisAuthoringServiceFactory(
            reaction_source=reaction_source),
        ProductionKineticsAuthoringServiceFactory(
            reaction_source=reaction_source,
            evidence_resolver_factory=evidence_resolver_factory,
        ),
    )


__all__ = [
    "ACTIVE_SPEC_SELECTION_PATH", "ACTIVE_SPEC_SELECTION_SCHEMA",
    "CanonicalEnvelopeBlockedEvidenceAdapter",
    "EvidenceResolverFactory",
    "FixedActiveKineticsSpecSelector",
    "ProductionCatalysisAuthoringServiceFactory",
    "ProductionKineticsAuthoringServiceFactory",
    "ProductionKineticsProviderFactory", "ProductionReactionDomainSource",
    "REACTION_PROJECTION_SCHEMA", "ReactionProjectionEvidenceAdapter",
    "ValidatedReactionSnapshot", "create_production_authoring_services",
    "create_production_catalysis_services",
]
