"""Project-local authority for explicit catalysis network projections.

The authority binds exactly one caller-selected ``ReactionNetwork`` head.  It
never chooses a network by name, insertion order, or apparent uniqueness, and
it never reads or writes job facts.  Public values are path-free DTOs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from vcstudio.project.catalysis_contracts import (
    AdsorbateState,
    CatalysisContractError,
    CatalystSurface,
    DomainEnvelope,
    ElementaryStep,
    FluidState,
    ReactionNetwork,
    AuthoritativeParticipantState,
    reject_sensitive,
    validate_elementary_step_conservation,
)
from vcstudio.project.catalysis_domain_store import (
    DomainEnvelopeStore,
    DomainHeadSnapshot,
    _exclusive_store_lock,
    _path_is_linklike,
    _read_bounded_regular,
    _write_store,
)


ACTIVE_BINDING_SCHEMA = "vcstudio.catalysis-active-network-binding/v1"
BINDING_AUTHORITY_SCHEMA = "vcstudio.catalysis-binding-authority/v1"
BINDING_SNAPSHOT_SCHEMA = "vcstudio.catalysis-binding-authority-snapshot/v1"
BINDING_WRITE_SCHEMA = "vcstudio.catalysis-binding-write-result/v1"
NETWORK_HEAD_CAS_SCHEMA = "vcstudio.catalysis-network-head-cas/v1"
PROJECTION_SNAPSHOT_SCHEMA = "vcstudio.catalysis-projection-snapshot/v1"
DOMAIN_STORE_FILENAME = "domain-envelopes.json"
BINDING_STORE_FILENAME = "active-network-binding.json"
MAX_BINDING_STORE_BYTES = 4 * 1024 * 1024
MAX_BINDING_REVISIONS = 10_000
MAX_NETWORK_SURFACES = 256
MAX_NETWORK_STATES = 4_096
MAX_NETWORK_STEPS = 2_048
MAX_NETWORK_CONDITIONS = 512
MAX_NETWORK_MEMBERS = 6_000
MAX_STEP_PARTICIPANTS = 512
MAX_CLOSURE_REFERENCES = 12_000
MAX_PROJECTION_GAPS = 256
MAX_PUBLIC_SNAPSHOT_BYTES = 2 * 1024 * 1024

_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]{0,159}\Z")
_PROCESS_LOCK = threading.RLock()


class CatalysisProjectionError(CatalysisContractError):
    """A projection authority or caller contract is malformed or tampered."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalysisProjectionError(
            "catalysis projection is not canonical JSON") from exc


def _digest_material(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _opaque(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _OPAQUE_RE.fullmatch(text) or text in {".", ".."}:
        raise CatalysisProjectionError(f"{field} must be an opaque identifier")
    try:
        reject_sensitive(text, field=field)
    except CatalysisContractError as exc:
        raise CatalysisProjectionError(
            f"{field} must be an opaque identifier") from exc
    return text


def _sha256(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise CatalysisProjectionError(f"{field} must be a SHA-256 digest")
    return text


def _authority_id(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    text = str(value or "").strip().lower()
    if not _AUTHORITY_RE.fullmatch(text):
        raise CatalysisProjectionError(f"{field} must be an authority identifier")
    return text


def _revision(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if (isinstance(value, bool) or not isinstance(value, int)
            or not minimum <= value <= MAX_BINDING_REVISIONS):
        raise CatalysisProjectionError(f"{field} is invalid")
    return value


def _strict(value: Any, allowed: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != allowed:
        raise CatalysisProjectionError(f"{label} contract is invalid")
    try:
        reject_sensitive(value, field=label)
    except CatalysisContractError as exc:
        raise CatalysisProjectionError(f"{label} contract is invalid") from exc
    return value


@dataclass(frozen=True)
class ActiveNetworkBinding:
    """Strict CAS identity for the one explicitly active network head."""

    authority_id: str
    revision: int
    parent_revision: int | None
    expected_current_hash: str | None
    domain_authority_id: str
    domain_generation: int
    domain_snapshot_sha256: str
    network_id: str
    network_revision_id: str
    network_semantic_sha256: str
    intent_id: str
    confirmed: bool
    binding_sha256: str = ""
    schema: str = ACTIVE_BINDING_SCHEMA
    job_source_of_truth: str = "job.yaml"
    authorizes_execution: bool = False

    def __post_init__(self) -> None:
        if self.schema != ACTIVE_BINDING_SCHEMA:
            raise CatalysisProjectionError("active binding schema is unsupported")
        object.__setattr__(self, "authority_id", _authority_id(
            self.authority_id, "binding.authority_id"))
        revision = _revision(self.revision, "binding.revision")
        if revision == 1:
            if self.parent_revision is not None or self.expected_current_hash is not None:
                raise CatalysisProjectionError(
                    "initial active binding must not declare a parent")
        else:
            if self.parent_revision != revision - 1:
                raise CatalysisProjectionError("active binding parent revision is invalid")
            object.__setattr__(self, "expected_current_hash", _sha256(
                self.expected_current_hash, "binding.expected_current_hash"))
        object.__setattr__(self, "domain_authority_id", _authority_id(
            self.domain_authority_id, "binding.domain_authority_id"))
        if (isinstance(self.domain_generation, bool)
                or not isinstance(self.domain_generation, int)
                or self.domain_generation < 1):
            raise CatalysisProjectionError("binding domain generation is invalid")
        object.__setattr__(self, "domain_snapshot_sha256", _sha256(
            self.domain_snapshot_sha256, "binding.domain_snapshot_sha256"))
        object.__setattr__(self, "network_id", _opaque(
            self.network_id, "binding.network_id"))
        object.__setattr__(self, "network_revision_id", _opaque(
            self.network_revision_id, "binding.network_revision_id"))
        object.__setattr__(self, "network_semantic_sha256", _sha256(
            self.network_semantic_sha256, "binding.network_semantic_sha256"))
        object.__setattr__(self, "intent_id", _opaque(
            self.intent_id, "binding.intent_id"))
        if self.confirmed is not True:
            raise CatalysisProjectionError("active binding requires explicit confirmation")
        if (self.job_source_of_truth != "job.yaml"
                or self.authorizes_execution is not False):
            raise CatalysisProjectionError("active binding authority boundary is invalid")
        material = self._hash_material()
        digest = _digest_material(material)
        if self.binding_sha256 not in {"", digest}:
            raise CatalysisProjectionError("active binding hash is invalid")
        object.__setattr__(self, "binding_sha256", digest)

    def _hash_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "authority_id": self.authority_id,
            "revision": self.revision,
            "parent_revision": self.parent_revision,
            "expected_current_hash": self.expected_current_hash,
            "domain_authority_id": self.domain_authority_id,
            "domain_generation": self.domain_generation,
            "domain_snapshot_sha256": self.domain_snapshot_sha256,
            "network_id": self.network_id,
            "network_revision_id": self.network_revision_id,
            "network_semantic_sha256": self.network_semantic_sha256,
            "intent_id": self.intent_id,
            "confirmed": self.confirmed,
            "job_source_of_truth": self.job_source_of_truth,
            "authorizes_execution": self.authorizes_execution,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_material(), "binding_sha256": self.binding_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActiveNetworkBinding":
        allowed = {
            "schema", "authority_id", "revision", "parent_revision",
            "expected_current_hash", "domain_authority_id", "domain_generation",
            "domain_snapshot_sha256", "network_id", "network_revision_id",
            "network_semantic_sha256",
            "intent_id", "confirmed", "binding_sha256", "job_source_of_truth",
            "authorizes_execution",
        }
        _strict(value, allowed, "active_binding")
        return cls(**dict(value))


@dataclass(frozen=True)
class ActiveBindingAuthoritySnapshot:
    """Path-free public view of the current projection CAS authority."""

    authority_id: str | None
    revision: int
    current_hash: str | None
    binding: ActiveNetworkBinding | None
    snapshot_sha256: str = ""
    schema: str = BINDING_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != BINDING_SNAPSHOT_SCHEMA:
            raise CatalysisProjectionError("binding snapshot schema is unsupported")
        revision = _revision(
            self.revision, "binding_snapshot.revision", allow_zero=True)
        if revision == 0:
            if any(value is not None for value in (
                    self.authority_id, self.current_hash, self.binding)):
                raise CatalysisProjectionError("empty binding snapshot is invalid")
        else:
            authority = _authority_id(
                self.authority_id, "binding_snapshot.authority_id")
            current = _sha256(
                self.current_hash, "binding_snapshot.current_hash")
            if (not isinstance(self.binding, ActiveNetworkBinding)
                    or self.binding.authority_id != authority
                    or self.binding.revision != revision
                    or self.binding.binding_sha256 != current):
                raise CatalysisProjectionError("binding snapshot current binding is invalid")
        material = self._hash_material()
        digest = _digest_material(material)
        if self.snapshot_sha256 not in {"", digest}:
            raise CatalysisProjectionError("binding snapshot hash is invalid")
        object.__setattr__(self, "snapshot_sha256", digest)

    def _hash_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "authority_id": self.authority_id,
            "revision": self.revision,
            "current_hash": self.current_hash,
            "binding": None if self.binding is None else self.binding.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_material(), "snapshot_sha256": self.snapshot_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActiveBindingAuthoritySnapshot":
        _strict(value, {
            "schema", "authority_id", "revision", "current_hash", "binding",
            "snapshot_sha256",
        }, "binding_snapshot")
        binding = value["binding"]
        return cls(
            schema=value["schema"], authority_id=value["authority_id"],
            revision=value["revision"], current_hash=value["current_hash"],
            binding=(None if binding is None
                     else ActiveNetworkBinding.from_dict(binding)),
            snapshot_sha256=value["snapshot_sha256"],
        )


@dataclass(frozen=True)
class NetworkHeadCAS:
    """Authoritative path-free domain/network identity returned with writes."""

    domain_authority_id: str
    domain_generation: int
    domain_snapshot_sha256: str
    network_id: str
    network_status: str
    network_revision_id: str | None
    network_semantic_sha256: str | None
    snapshot_sha256: str = ""
    schema: str = NETWORK_HEAD_CAS_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != NETWORK_HEAD_CAS_SCHEMA:
            raise CatalysisProjectionError("network head CAS schema is unsupported")
        object.__setattr__(self, "domain_authority_id", _authority_id(
            self.domain_authority_id, "network_cas.domain_authority_id"))
        if (isinstance(self.domain_generation, bool)
                or not isinstance(self.domain_generation, int)
                or self.domain_generation < 1):
            raise CatalysisProjectionError("network head CAS generation is invalid")
        object.__setattr__(self, "domain_snapshot_sha256", _sha256(
            self.domain_snapshot_sha256, "network_cas.domain_snapshot_sha256"))
        object.__setattr__(self, "network_id", _opaque(
            self.network_id, "network_cas.network_id"))
        if self.network_status not in {"available", "missing", "wrong_type"}:
            raise CatalysisProjectionError("network head CAS status is invalid")
        if self.network_status == "available":
            object.__setattr__(self, "network_revision_id", _opaque(
                self.network_revision_id, "network_cas.network_revision_id"))
            object.__setattr__(self, "network_semantic_sha256", _sha256(
                self.network_semantic_sha256,
                "network_cas.network_semantic_sha256"))
        elif (self.network_revision_id is not None
              or self.network_semantic_sha256 is not None):
            raise CatalysisProjectionError(
                "unavailable network head CAS must not claim a revision")
        material = self._hash_material()
        digest = _digest_material(material)
        if self.snapshot_sha256 not in {"", digest}:
            raise CatalysisProjectionError("network head CAS hash is invalid")
        object.__setattr__(self, "snapshot_sha256", digest)

    def _hash_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "domain_authority_id": self.domain_authority_id,
            "domain_generation": self.domain_generation,
            "domain_snapshot_sha256": self.domain_snapshot_sha256,
            "network_id": self.network_id,
            "network_status": self.network_status,
            "network_revision_id": self.network_revision_id,
            "network_semantic_sha256": self.network_semantic_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_material(), "snapshot_sha256": self.snapshot_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NetworkHeadCAS":
        _strict(value, {
            "schema", "domain_authority_id", "domain_generation",
            "domain_snapshot_sha256", "network_id", "network_status",
            "network_revision_id", "network_semantic_sha256", "snapshot_sha256",
        }, "network_head_cas")
        return cls(**dict(value))


@dataclass(frozen=True)
class BindingWriteResult:
    """A write outcome carrying the authoritative post-attempt snapshot."""

    action: str
    snapshot: ActiveBindingAuthoritySnapshot
    network_cas: NetworkHeadCAS | None = None
    reason: str | None = None
    schema: str = BINDING_WRITE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != BINDING_WRITE_SCHEMA:
            raise CatalysisProjectionError("binding write schema is unsupported")
        if self.action not in {"created", "advanced", "replayed", "conflict"}:
            raise CatalysisProjectionError("binding write action is invalid")
        if not isinstance(self.snapshot, ActiveBindingAuthoritySnapshot):
            raise CatalysisProjectionError("binding write snapshot is invalid")
        if self.network_cas is not None and not isinstance(
                self.network_cas, NetworkHeadCAS):
            raise CatalysisProjectionError("binding write network CAS is invalid")
        if self.action == "conflict":
            object.__setattr__(self, "reason", _opaque(
                self.reason, "binding_write.reason"))
        elif self.reason is not None:
            raise CatalysisProjectionError(
                "successful binding writes must not declare a conflict reason")

    @property
    def binding(self) -> ActiveNetworkBinding | None:
        return self.snapshot.binding

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "action": self.action,
            "reason": self.reason,
            "snapshot": self.snapshot.to_dict(),
            "network_cas": (
                None if self.network_cas is None else self.network_cas.to_dict()),
        }


def _empty_binding_snapshot() -> ActiveBindingAuthoritySnapshot:
    return ActiveBindingAuthoritySnapshot(
        authority_id=None, revision=0, current_hash=None, binding=None)


def _snapshot_from_store(
        store: Mapping[str, Any] | None) -> ActiveBindingAuthoritySnapshot:
    if store is None:
        return _empty_binding_snapshot()
    binding = ActiveNetworkBinding.from_dict(store["bindings"][-1])
    return ActiveBindingAuthoritySnapshot(
        authority_id=store["authority_id"], revision=store["revision"],
        current_hash=store["current_hash"], binding=binding,
    )


def _validated_binding_store(value: Any) -> dict[str, Any]:
    _strict(value, {
        "schema", "authority_id", "revision", "current_hash", "bindings",
    }, "binding_authority")
    if value["schema"] != BINDING_AUTHORITY_SCHEMA:
        raise CatalysisProjectionError("binding authority schema is unsupported")
    authority = _authority_id(value["authority_id"], "binding_authority.authority_id")
    revision = _revision(value["revision"], "binding_authority.revision")
    raw_bindings = value["bindings"]
    if (isinstance(raw_bindings, (str, bytes))
            or not isinstance(raw_bindings, Sequence)
            or len(raw_bindings) != revision
            or len(raw_bindings) > MAX_BINDING_REVISIONS):
        raise CatalysisProjectionError("binding authority history is invalid")
    bindings = tuple(ActiveNetworkBinding.from_dict(item) for item in raw_bindings)
    intents: set[str] = set()
    previous: ActiveNetworkBinding | None = None
    for index, binding in enumerate(bindings, 1):
        if (binding.authority_id != authority or binding.revision != index
                or binding.intent_id in intents):
            raise CatalysisProjectionError("binding authority history is invalid")
        if previous is None:
            if binding.parent_revision is not None or binding.expected_current_hash is not None:
                raise CatalysisProjectionError("binding authority anchor is invalid")
        elif (binding.parent_revision != previous.revision
              or binding.expected_current_hash != previous.binding_sha256):
            raise CatalysisProjectionError("binding authority chain is invalid")
        intents.add(binding.intent_id)
        previous = binding
    current_hash = _sha256(value["current_hash"], "binding_authority.current_hash")
    if bindings[-1].binding_sha256 != current_hash:
        raise CatalysisProjectionError("binding authority current hash is invalid")
    return {
        "schema": BINDING_AUTHORITY_SCHEMA,
        "authority_id": authority,
        "revision": revision,
        "current_hash": current_hash,
        "bindings": [item.to_dict() for item in bindings],
    }


def _root_directory(project_root: str | os.PathLike[str]) -> Path:
    authored = Path(os.path.abspath(os.fspath(project_root)))
    try:
        details = os.lstat(authored)
    except OSError as exc:
        raise CatalysisProjectionError("project root is unavailable") from exc
    canonical = Path(os.path.realpath(authored))
    if (not stat.S_ISDIR(details.st_mode) or _path_is_linklike(authored)
            or os.path.normcase(str(authored)) != os.path.normcase(str(canonical))):
        raise CatalysisProjectionError(
            "project root must be a real directory without link aliases")
    return canonical


def _ensure_child_directory(parent: Path, name: str, root: Path) -> Path:
    candidate = parent / name
    try:
        details = os.lstat(candidate)
    except FileNotFoundError:
        try:
            candidate.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise CatalysisProjectionError(
                "catalysis authority directory is unavailable") from exc
        try:
            details = os.lstat(candidate)
        except OSError as exc:
            raise CatalysisProjectionError(
                "catalysis authority directory is unavailable") from exc
    except OSError as exc:
        raise CatalysisProjectionError(
            "catalysis authority directory is unavailable") from exc
    resolved = Path(os.path.realpath(candidate))
    if (not stat.S_ISDIR(details.st_mode) or _path_is_linklike(candidate)
            or os.path.normcase(str(candidate)) != os.path.normcase(str(resolved))):
        raise CatalysisProjectionError(
            "catalysis authority directory must not be a link or reparse point")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CatalysisProjectionError(
            "catalysis authority directory escaped the project root") from exc
    return resolved


def _directory_identity(path: Path, label: str) -> tuple[int, int]:
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise CatalysisProjectionError(f"{label} entity is unavailable") from exc
    canonical = Path(os.path.realpath(path))
    if (not stat.S_ISDIR(details.st_mode) or _path_is_linklike(path)
            or os.path.normcase(str(path)) != os.path.normcase(str(canonical))):
        raise CatalysisProjectionError(
            f"{label} entity must not be a link, junction, or reparse point")
    return int(details.st_dev), int(details.st_ino)


@dataclass(frozen=True)
class _HeadIndex:
    by_identity: Mapping[tuple[str, str], DomainEnvelope]
    types_by_id: Mapping[str, tuple[str, ...]]

    @classmethod
    def build(cls, snapshot: DomainHeadSnapshot) -> "_HeadIndex":
        by_identity: dict[tuple[str, str], DomainEnvelope] = {}
        types: dict[str, list[str]] = {}
        for envelope in snapshot.heads:
            by_identity[(envelope.object_type, envelope.object_id)] = envelope
            types.setdefault(envelope.object_id, []).append(envelope.object_type)
        return cls(
            by_identity=by_identity,
            types_by_id={
                object_id: tuple(sorted(values))
                for object_id, values in types.items()
            },
        )

    def head(self, object_type: str, object_id: str) -> DomainEnvelope | None:
        return self.by_identity.get((object_type, object_id))


def _network_cas(
        domain: DomainHeadSnapshot, index: _HeadIndex,
        network_id: str) -> NetworkHeadCAS:
    network = index.head("ReactionNetwork", network_id)
    if network is not None:
        status = "available"
        revision_id = network.object_revision_id
        semantic_sha256 = network.semantic_sha256
    else:
        status = "wrong_type" if index.types_by_id.get(network_id) else "missing"
        revision_id = None
        semantic_sha256 = None
    return NetworkHeadCAS(
        domain_authority_id=domain.authority_id,
        domain_generation=domain.generation,
        domain_snapshot_sha256=domain.snapshot_sha256,
        network_id=network_id, network_status=status,
        network_revision_id=revision_id,
        network_semantic_sha256=semantic_sha256,
    )


def _network_resource_gap(network: ReactionNetwork) -> dict[str, Any] | None:
    counts = {
        "surfaces": len(network.surface_ids),
        "states": len(network.state_ids),
        "steps": len(network.step_ids),
        "conditions": len(network.condition_set_ids),
    }
    total = sum(counts.values())
    if (counts["surfaces"] <= MAX_NETWORK_SURFACES
            and counts["states"] <= MAX_NETWORK_STATES
            and counts["steps"] <= MAX_NETWORK_STEPS
            and counts["conditions"] <= MAX_NETWORK_CONDITIONS
            and total <= MAX_NETWORK_MEMBERS):
        return None
    return _gap(
        "unavailable", "network_closure_resource_limit",
        "ReactionNetwork", network.network_id,
        member_counts={**counts, "total": total},
        member_limits={
            "surfaces": MAX_NETWORK_SURFACES,
            "states": MAX_NETWORK_STATES,
            "steps": MAX_NETWORK_STEPS,
            "conditions": MAX_NETWORK_CONDITIONS,
            "total": MAX_NETWORK_MEMBERS,
        },
    )


class CatalysisProjectionAuthority:
    """Own the fixed ``.vcstudio/catalysis`` binding and domain authorities."""

    def __init__(self, project_root: str | os.PathLike[str]):
        self._project_root = _root_directory(project_root)
        internal = _ensure_child_directory(
            self._project_root, ".vcstudio", self._project_root)
        self._authority_directory = _ensure_child_directory(
            internal, "catalysis", self._project_root)
        self._root_identity = _directory_identity(
            self._project_root, "project root")
        self._authority_identity = _directory_identity(
            self._authority_directory, "catalysis authority directory")
        self._binding_path = self._authority_directory / BINDING_STORE_FILENAME
        self._binding_lock_path = (
            self._authority_directory / f".{BINDING_STORE_FILENAME}.lock")
        self.domain_store = DomainEnvelopeStore(
            self._authority_directory / DOMAIN_STORE_FILENAME,
            boundary_validator=self._verify_entities,
        )

    def _verify_entities(self) -> None:
        if (_directory_identity(self._project_root, "project root")
                != self._root_identity):
            raise CatalysisProjectionError("project root entity identity changed")
        if (_directory_identity(
                self._authority_directory, "catalysis authority directory")
                != self._authority_identity):
            raise CatalysisProjectionError(
                "catalysis authority directory identity changed")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._verify_entities()
        with _PROCESS_LOCK:
            self._verify_entities()
            try:
                with _exclusive_store_lock(self._binding_lock_path):
                    self._verify_entities()
                    try:
                        yield
                    finally:
                        self._verify_entities()
                self._verify_entities()
            except CatalysisProjectionError:
                raise
            except CatalysisContractError as exc:
                raise CatalysisProjectionError(
                    "catalysis binding authority lock is unavailable") from exc
            except OSError as exc:
                raise CatalysisProjectionError(
                    "catalysis binding authority lock is unavailable") from exc
            finally:
                self._verify_entities()

    def _read_binding_store(self) -> dict[str, Any] | None:
        self._verify_entities()
        try:
            try:
                payload = _read_bounded_regular(
                    self._binding_path, maximum=MAX_BINDING_STORE_BYTES)
            except (CatalysisContractError, OSError) as exc:
                raise CatalysisProjectionError(
                    "catalysis binding authority is unsafe or unreadable") from exc
            if payload is None:
                return None
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise CatalysisProjectionError(
                    "catalysis binding authority is invalid") from exc
            return _validated_binding_store(value)
        finally:
            self._verify_entities()

    def _write_binding_store(self, value: Mapping[str, Any]) -> dict[str, Any]:
        self._verify_entities()
        try:
            validated = _validated_binding_store(value)
            if len(_canonical_bytes(validated)) + 1 > MAX_BINDING_STORE_BYTES:
                raise CatalysisProjectionError(
                    "catalysis binding authority exceeds its resource limit")
            try:
                _write_store(self._binding_path, validated)
            except (CatalysisContractError, OSError) as exc:
                raise CatalysisProjectionError(
                    "catalysis binding authority could not be committed") from exc
            return validated
        finally:
            self._verify_entities()

    def binding_snapshot(self) -> ActiveBindingAuthoritySnapshot:
        with self._locked():
            return _snapshot_from_store(self._read_binding_store())

    def network_head_cas(self, network_id: str) -> NetworkHeadCAS | None:
        """Return the exact domain/network CAS values a confirmation must echo."""

        network_id = _opaque(network_id, "network_id")
        with self._locked():
            try:
                with self.domain_store.locked_snapshot_heads(
                        initialize=False) as domain:
                    if domain is None:
                        return None
                    return _network_cas(
                        domain, _HeadIndex.build(domain), network_id)
            except CatalysisContractError as exc:
                if isinstance(exc, CatalysisProjectionError):
                    raise
                raise CatalysisProjectionError(
                    "domain authority is unavailable or invalid") from exc

    def _conflict(
            self, reason: str,
            store: Mapping[str, Any] | None, *,
            network_cas: NetworkHeadCAS | None = None) -> BindingWriteResult:
        return BindingWriteResult(
            action="conflict", reason=reason,
            snapshot=_snapshot_from_store(store),
            network_cas=network_cas,
        )

    def bind_network(
            self, *, network_id: str, intent_id: str, confirmed: bool,
            expected_authority_id: str | None,
            expected_revision: int,
            expected_current_hash: str | None,
            expected_domain_authority_id: str,
            expected_domain_generation: int,
            expected_domain_snapshot_sha256: str,
            expected_network_revision_id: str,
            expected_network_semantic_sha256: str) -> BindingWriteResult:
        """Create, advance, or replay the explicit active-network binding."""

        network_id = _opaque(network_id, "network_id")
        intent_id = _opaque(intent_id, "intent_id")
        expected_revision = _revision(
            expected_revision, "expected_revision", allow_zero=True)
        expected_authority_id = _authority_id(
            expected_authority_id, "expected_authority_id", optional=True)
        expected_current_hash = _sha256(
            expected_current_hash, "expected_current_hash", optional=True)
        expected_domain_authority_id = _authority_id(
            expected_domain_authority_id, "expected_domain_authority_id")
        if (isinstance(expected_domain_generation, bool)
                or not isinstance(expected_domain_generation, int)
                or expected_domain_generation < 1):
            raise CatalysisProjectionError(
                "expected_domain_generation is invalid")
        expected_domain_snapshot_sha256 = _sha256(
            expected_domain_snapshot_sha256,
            "expected_domain_snapshot_sha256")
        expected_network_revision_id = _opaque(
            expected_network_revision_id, "expected_network_revision_id")
        expected_network_semantic_sha256 = _sha256(
            expected_network_semantic_sha256,
            "expected_network_semantic_sha256")
        if expected_revision == 0:
            if expected_authority_id is not None or expected_current_hash is not None:
                raise CatalysisProjectionError("initial binding CAS identity is invalid")
        elif expected_authority_id is None or expected_current_hash is None:
            raise CatalysisProjectionError("binding CAS identity is incomplete")

        with self._locked():
            store = self._read_binding_store()
            if confirmed is not True:
                return self._conflict("confirmation_required", store)
            try:
                with self.domain_store.locked_snapshot_heads(
                        initialize=False) as domain:
                    if domain is None:
                        return self._conflict("domain_authority_missing", store)
                    index = _HeadIndex.build(domain)
                    network_cas = _network_cas(domain, index, network_id)
                    if (domain.authority_id != expected_domain_authority_id
                            or domain.generation != expected_domain_generation
                            or domain.snapshot_sha256
                            != expected_domain_snapshot_sha256):
                        return self._conflict(
                            "stale_domain_snapshot", store,
                            network_cas=network_cas)
                    if network_cas.network_status != "available":
                        return self._conflict(
                            "network_wrong_type"
                            if network_cas.network_status == "wrong_type"
                            else "network_missing",
                            store, network_cas=network_cas)
                    if (network_cas.network_revision_id
                            != expected_network_revision_id
                            or network_cas.network_semantic_sha256
                            != expected_network_semantic_sha256):
                        return self._conflict(
                            "stale_network_head", store,
                            network_cas=network_cas)

                    current_revision = 0 if store is None else store["revision"]
                    current_authority = (
                        None if store is None else store["authority_id"])
                    current_hash = None if store is None else store["current_hash"]
                    if store is not None:
                        prior = next((
                            ActiveNetworkBinding.from_dict(item)
                            for item in store["bindings"]
                            if item["intent_id"] == intent_id
                        ), None)
                        current = ActiveNetworkBinding.from_dict(
                            store["bindings"][-1])
                        if prior is not None:
                            if (prior.revision == current.revision
                                    and prior.network_id == network_id):
                                return BindingWriteResult(
                                    action="replayed",
                                    snapshot=_snapshot_from_store(store),
                                    network_cas=network_cas)
                            return self._conflict(
                                "intent_reused", store,
                                network_cas=network_cas)
                    if (current_revision != expected_revision
                            or current_authority != expected_authority_id
                            or current_hash != expected_current_hash):
                        return self._conflict(
                            "stale_cas", store, network_cas=network_cas)

                    network = index.head("ReactionNetwork", network_id)
                    if network is None:
                        raise CatalysisProjectionError(
                            "confirmed network head resolution failed")
                    parsed_network = ReactionNetwork.from_dict(network.payload)
                    if _network_resource_gap(parsed_network) is not None:
                        return self._conflict(
                            "network_resource_limit", store,
                            network_cas=network_cas)

                    authority = current_authority or secrets.token_hex(16)
                    revision = current_revision + 1
                    binding = ActiveNetworkBinding(
                        authority_id=authority, revision=revision,
                        parent_revision=(
                            None if revision == 1 else current_revision),
                        expected_current_hash=current_hash,
                        domain_authority_id=domain.authority_id,
                        domain_generation=domain.generation,
                        domain_snapshot_sha256=domain.snapshot_sha256,
                        network_id=network.object_id,
                        network_revision_id=network.object_revision_id,
                        network_semantic_sha256=network.semantic_sha256,
                        intent_id=intent_id, confirmed=True,
                    )
                    candidate = {
                        "schema": BINDING_AUTHORITY_SCHEMA,
                        "authority_id": authority,
                        "revision": revision,
                        "current_hash": binding.binding_sha256,
                        "bindings": (
                            [] if store is None else list(store["bindings"]))
                        + [binding.to_dict()],
                    }
                    # The domain lock remains held through this atomic replace;
                    # no head can advance during confirmation.
                    committed = self._write_binding_store(candidate)
                    return BindingWriteResult(
                        action="created" if revision == 1 else "advanced",
                        snapshot=_snapshot_from_store(committed),
                        network_cas=network_cas,
                    )
            except CatalysisContractError as exc:
                if isinstance(exc, CatalysisProjectionError):
                    raise
                raise CatalysisProjectionError(
                    "domain authority is unavailable or invalid") from exc

    def create_binding(
            self, *, network_id: str, intent_id: str,
            confirmed: bool, expected_domain_authority_id: str,
            expected_domain_generation: int,
            expected_domain_snapshot_sha256: str,
            expected_network_revision_id: str,
            expected_network_semantic_sha256: str) -> BindingWriteResult:
        return self.bind_network(
            network_id=network_id, intent_id=intent_id, confirmed=confirmed,
            expected_authority_id=None, expected_revision=0,
            expected_current_hash=None,
            expected_domain_authority_id=expected_domain_authority_id,
            expected_domain_generation=expected_domain_generation,
            expected_domain_snapshot_sha256=expected_domain_snapshot_sha256,
            expected_network_revision_id=expected_network_revision_id,
            expected_network_semantic_sha256=expected_network_semantic_sha256,
        )

    def advance_binding(
            self, *, network_id: str, intent_id: str, confirmed: bool,
            expected_authority_id: str, expected_revision: int,
            expected_current_hash: str,
            expected_domain_authority_id: str,
            expected_domain_generation: int,
            expected_domain_snapshot_sha256: str,
            expected_network_revision_id: str,
            expected_network_semantic_sha256: str) -> BindingWriteResult:
        return self.bind_network(
            network_id=network_id, intent_id=intent_id, confirmed=confirmed,
            expected_authority_id=expected_authority_id,
            expected_revision=expected_revision,
            expected_current_hash=expected_current_hash,
            expected_domain_authority_id=expected_domain_authority_id,
            expected_domain_generation=expected_domain_generation,
            expected_domain_snapshot_sha256=expected_domain_snapshot_sha256,
            expected_network_revision_id=expected_network_revision_id,
            expected_network_semantic_sha256=expected_network_semantic_sha256,
        )

    def replay_binding(
            self, *, network_id: str, intent_id: str, confirmed: bool,
            expected_authority_id: str | None, expected_revision: int,
            expected_current_hash: str | None,
            expected_domain_authority_id: str,
            expected_domain_generation: int,
            expected_domain_snapshot_sha256: str,
            expected_network_revision_id: str,
            expected_network_semantic_sha256: str) -> BindingWriteResult:
        """Replay an idempotent create/advance request through the same CAS gate."""

        return self.bind_network(
            network_id=network_id, intent_id=intent_id, confirmed=confirmed,
            expected_authority_id=expected_authority_id,
            expected_revision=expected_revision,
            expected_current_hash=expected_current_hash,
            expected_domain_authority_id=expected_domain_authority_id,
            expected_domain_generation=expected_domain_generation,
            expected_domain_snapshot_sha256=expected_domain_snapshot_sha256,
            expected_network_revision_id=expected_network_revision_id,
            expected_network_semantic_sha256=expected_network_semantic_sha256,
        )

    def build_snapshot(self) -> dict[str, Any]:
        """Build the pinned closure, retaining every unsupported fact as a gap."""

        with self._locked():
            store = self._read_binding_store()
            binding_snapshot = _snapshot_from_store(store)
            if store is None:
                return _projection_snapshot(
                    binding_snapshot=binding_snapshot, domain=None,
                    network=None, surfaces=(), states=(), steps=(), conditions=(),
                    gaps=(_gap(
                        "missing", "active_network_binding_missing",
                        "ReactionNetwork", None),),
                )
            binding = binding_snapshot.binding
            if binding is None:
                raise CatalysisProjectionError(
                    "binding authority has no current binding")
            try:
                with self.domain_store.locked_snapshot_heads(
                        initialize=False) as domain:
                    if domain is None:
                        return _projection_snapshot(
                            binding_snapshot=binding_snapshot, domain=None,
                            network=None, surfaces=(), states=(), steps=(),
                            conditions=(), gaps=(_gap(
                                "unavailable", "domain_authority_missing",
                                "ReactionNetwork", binding.network_id),),
                        )
                    index = _HeadIndex.build(domain)
                    stale: list[dict[str, Any]] = []
                    if domain.authority_id != binding.domain_authority_id:
                        stale.append(_gap(
                            "unavailable", "domain_authority_replaced",
                            "ReactionNetwork", binding.network_id))
                    if domain.snapshot_sha256 != binding.domain_snapshot_sha256:
                        stale.append(_gap(
                            "unavailable", "domain_snapshot_changed",
                            "ReactionNetwork", binding.network_id))
                    current_network = index.head(
                        "ReactionNetwork", binding.network_id)
                    if current_network is None:
                        stale.append(_gap(
                            "missing", "active_network_head_missing",
                            "ReactionNetwork", binding.network_id))
                    elif (current_network.object_revision_id
                          != binding.network_revision_id
                          or current_network.semantic_sha256
                          != binding.network_semantic_sha256):
                        stale.append(_gap(
                            "unavailable", "active_network_head_advanced",
                            "ReactionNetwork", binding.network_id))
                    if domain.generation != binding.domain_generation:
                        stale.append(_gap(
                            "unavailable", "domain_generation_advanced",
                            "ReactionNetwork", binding.network_id))
                    if stale:
                        return _projection_snapshot(
                            binding_snapshot=binding_snapshot, domain=domain,
                            network=None, surfaces=(), states=(), steps=(),
                            conditions=(), gaps=tuple(stale),
                        )
                    if current_network is None:
                        raise CatalysisProjectionError(
                            "active network resolution failed")
                    parsed_network = ReactionNetwork.from_dict(
                        current_network.payload)
                    resource_gap = _network_resource_gap(parsed_network)
                    if resource_gap is not None:
                        return _projection_snapshot(
                            binding_snapshot=binding_snapshot, domain=domain,
                            network=current_network, surfaces=(), states=(), steps=(),
                            conditions=(), gaps=(resource_gap,),
                        )
                    return _build_closure(
                        binding_snapshot, domain, current_network, index)
            except CatalysisContractError as exc:
                if isinstance(exc, CatalysisProjectionError):
                    raise
                raise CatalysisProjectionError(
                    "domain authority is unavailable or invalid") from exc


def _gap(
        status: str, code: str, object_type: str,
        object_id: str | None, **context: Any) -> dict[str, Any]:
    value = {
        "status": status,
        "code": code,
        "object_type": object_type,
        "object_id": object_id,
        **context,
    }
    try:
        reject_sensitive(value, field="projection_gap")
    except CatalysisContractError as exc:
        raise CatalysisProjectionError("projection gap is invalid") from exc
    return value


def _member(
        index: _HeadIndex, object_type: str,
        object_id: str, gaps: list[dict[str, Any]]) -> DomainEnvelope | None:
    match = index.head(object_type, object_id)
    if match is not None:
        return match
    available_types = index.types_by_id.get(object_id, ())
    if available_types:
        gaps.append(_gap(
            "unavailable", "network_member_wrong_type", object_type, object_id,
            available_object_types=available_types,
        ))
    else:
        gaps.append(_gap(
            "missing", "network_member_missing", object_type, object_id))
    return None


def _state_member(
        index: _HeadIndex, state_id: str,
        gaps: list[dict[str, Any]]) -> DomainEnvelope | None:
    matches = tuple(filter(None, (
        index.head("AdsorbateState", state_id),
        index.head("FluidState", state_id),
    )))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        gaps.append(_gap(
            "unavailable", "network_state_type_collision",
            "ParticipantState", state_id,
            available_object_types=["AdsorbateState", "FluidState"],
        ))
        return None
    available_types = index.types_by_id.get(state_id, ())
    if available_types:
        gaps.append(_gap(
            "unavailable", "network_member_wrong_type",
            "ParticipantState", state_id,
            available_object_types=available_types,
        ))
    else:
        gaps.append(_gap(
            "missing", "network_member_missing", "ParticipantState", state_id))
    return None


def _build_closure(
        binding_snapshot: ActiveBindingAuthoritySnapshot,
        domain: DomainHeadSnapshot,
        network_envelope: DomainEnvelope,
        index: _HeadIndex) -> dict[str, Any]:
    network = ReactionNetwork.from_dict(network_envelope.payload)
    gaps: list[dict[str, Any]] = []
    surfaces = tuple(filter(None, (
        _member(index, "CatalystSurface", item, gaps)
        for item in network.surface_ids
    )))
    states = tuple(filter(None, (
        _state_member(index, item, gaps)
        for item in network.state_ids
    )))
    steps = tuple(filter(None, (
        _member(index, "ElementaryStep", item, gaps)
        for item in network.step_ids
    )))
    conditions = tuple(filter(None, (
        _member(index, "ConditionSet", item, gaps)
        for item in network.condition_set_ids
    )))
    parsed_steps = tuple(
        ElementaryStep.from_dict(item.payload) for item in steps)
    participant_counts = tuple(
        len(step.reactants) + len(step.transition_state) + len(step.products)
        for step in parsed_steps)
    participant_total = sum(participant_counts)
    closure_references = (
        len(network.surface_ids) + len(network.state_ids)
        + len(network.step_ids) + len(network.condition_set_ids)
        + participant_total)
    if (any(count > MAX_STEP_PARTICIPANTS for count in participant_counts)
            or closure_references > MAX_CLOSURE_REFERENCES):
        gaps.append(_gap(
            "unavailable", "network_closure_reference_limit",
            "ReactionNetwork", network.network_id,
            participant_references=participant_total,
            closure_references=closure_references,
            participant_limit_per_step=MAX_STEP_PARTICIPANTS,
            closure_reference_limit=MAX_CLOSURE_REFERENCES,
        ))
        return _projection_snapshot(
            binding_snapshot=binding_snapshot, domain=domain,
            network=network_envelope, surfaces=(), states=(), steps=(),
            conditions=(), gaps=tuple(gaps),
        )

    surface_by_id = {
        item.object_id: CatalystSurface.from_dict(item.payload) for item in surfaces
    }
    state_by_id: dict[str, AdsorbateState | FluidState] = {}
    state_type_by_id: dict[str, str] = {}
    authoritative_states: dict[str, AuthoritativeParticipantState] = {}
    for envelope in states:
        if envelope.object_type == "AdsorbateState":
            state = AdsorbateState.from_dict(envelope.payload)
            if state.charge is not None and state.geometric_site_id is not None:
                authoritative_states[state.state_id] = AuthoritativeParticipantState(
                    state_id=state.state_id, chemical_formula=state.chemical_formula,
                    phase="adsorbed", charge=state.charge,
                    site_stoichiometry={state.geometric_site_id: 1},
                    surface_id=state.surface_id,
                )
        elif envelope.object_type == "FluidState":
            state = FluidState.from_dict(envelope.payload)
            authoritative_states[state.state_id] = AuthoritativeParticipantState(
                state_id=state.state_id, chemical_formula=state.chemical_formula,
                phase=state.phase, charge=state.charge, site_stoichiometry={},
                surface_id=None,
            )
        else:
            raise CatalysisProjectionError("resolved network state type is invalid")
        state_by_id[state.state_id] = state
        state_type_by_id[state.state_id] = envelope.object_type
    condition_ids = {item.object_id for item in conditions}
    network_state_ids = set(network.state_ids)
    network_surface_ids = set(network.surface_ids)
    network_condition_ids = set(network.condition_set_ids)

    for state_id, state in sorted(state_by_id.items()):
        if isinstance(state, FluidState):
            continue
        if state.surface_id not in network_surface_ids:
            gaps.append(_gap(
                "unavailable", "state_surface_not_in_network",
                "AdsorbateState", state_id,
                referenced_object_type="CatalystSurface",
                referenced_object_id=state.surface_id,
            ))
            continue
        surface = surface_by_id.get(state.surface_id)
        if surface is None:
            continue
        if (state.geometric_site_id is not None
                and state.geometric_site_id not in surface.geometric_site_ids):
            gaps.append(_gap(
                "unavailable", "state_site_not_on_surface",
                "AdsorbateState", state_id,
                referenced_object_type="CatalystSurface",
                referenced_object_id=state.surface_id,
            ))

    for step in parsed_steps:
        if (step.condition_set_id is not None
                and step.condition_set_id not in network_condition_ids):
            gaps.append(_gap(
                "unavailable", "step_condition_not_in_network",
                "ElementaryStep", step.step_id,
                referenced_object_type="ConditionSet",
                referenced_object_id=step.condition_set_id,
            ))
        elif (step.condition_set_id is not None
              and step.condition_set_id not in condition_ids):
            # The missing member gap already records the absent head.  This
            # additional edge gap identifies why the closure cannot be used.
            gaps.append(_gap(
                "missing", "step_condition_head_missing",
                "ElementaryStep", step.step_id,
                referenced_object_type="ConditionSet",
                referenced_object_id=step.condition_set_id,
            ))
        for role, participants in (
                ("reactant", step.reactants),
                ("transition_state", step.transition_state),
                ("product", step.products)):
            for participant in participants:
                state_id = participant.state_id
                if state_id not in network_state_ids:
                    gaps.append(_gap(
                        "missing", "participant_state_not_in_network",
                        "ElementaryStep", step.step_id,
                        participant_role=role,
                        referenced_object_type="ParticipantState",
                        referenced_object_id=state_id,
                    ))
                state = state_by_id.get(state_id)
                if state is None:
                    continue
                state_type = state_type_by_id[state_id]
                if state.charge != participant.charge:
                    gaps.append(_gap(
                        "unavailable", "participant_state_charge_disagrees",
                        state_type, state_id,
                        participant_role=role,
                    ))
                if isinstance(state, FluidState):
                    if participant.phase != state.phase:
                        gaps.append(_gap(
                            "unavailable", "participant_fluid_phase_disagrees",
                            "FluidState", state_id, participant_role=role,
                            participant_phase=participant.phase,
                            authoritative_phase=state.phase,
                        ))
                    if participant.site_stoichiometry:
                        gaps.append(_gap(
                            "unavailable",
                            "participant_fluid_site_stoichiometry_disagrees",
                            "FluidState", state_id, participant_role=role,
                        ))
                else:
                    if participant.phase != "adsorbed":
                        gaps.append(_gap(
                            "unavailable", "participant_phase_model_unavailable",
                            "ElementaryStep", step.step_id,
                            participant_role=role,
                            participant_phase=participant.phase,
                            referenced_object_type="AdsorbateState",
                            referenced_object_id=state_id,
                        ))
                    if state.surface_id not in network_surface_ids:
                        gaps.append(_gap(
                            "unavailable", "participant_surface_not_in_network",
                            "ElementaryStep", step.step_id,
                            participant_role=role,
                            referenced_object_type="CatalystSurface",
                            referenced_object_id=state.surface_id,
                        ))
                    if state.charge is None:
                        gaps.append(_gap(
                            "unavailable", "participant_charge_model_unavailable",
                            "AdsorbateState", state_id, participant_role=role,
                        ))
                    if state.geometric_site_id is None:
                        gaps.append(_gap(
                            "unavailable", "participant_site_authority_unavailable",
                            "AdsorbateState", state_id, participant_role=role,
                        ))
                        continue
                    site_amount = participant.site_stoichiometry.get(
                        state.geometric_site_id)
                    if (set(participant.site_stoichiometry)
                            != {state.geometric_site_id}
                            or site_amount is None
                            or site_amount.to_fraction() != 1):
                        gaps.append(_gap(
                            "unavailable",
                            "participant_site_stoichiometry_disagrees",
                            "AdsorbateState", state_id,
                            participant_role=role,
                        ))
        participant_ids = {
            item.state_id for side in (
                step.reactants, step.transition_state, step.products)
            for item in side
        }
        if not participant_ids.issubset(authoritative_states):
            gaps.append(_gap(
                "unavailable", "formal_conservation_unavailable",
                "ElementaryStep", step.step_id,
            ))
        else:
            try:
                validate_elementary_step_conservation(step, authoritative_states)
            except CatalysisContractError:
                gaps.append(_gap(
                    "unavailable", "formal_conservation_failed",
                    "ElementaryStep", step.step_id,
                ))

    return _projection_snapshot(
        binding_snapshot=binding_snapshot, domain=domain,
        network=network_envelope, surfaces=surfaces, states=states,
        steps=steps, conditions=conditions, gaps=tuple(gaps),
    )


def _projection_snapshot(
        *, binding_snapshot: ActiveBindingAuthoritySnapshot,
        domain: DomainHeadSnapshot | None, network: DomainEnvelope | None,
        surfaces: Sequence[DomainEnvelope], states: Sequence[DomainEnvelope],
        steps: Sequence[DomainEnvelope], conditions: Sequence[DomainEnvelope],
        gaps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unique_gaps = {
        _canonical_bytes(item): dict(item) for item in gaps
    }
    all_gaps = [unique_gaps[key] for key in sorted(unique_gaps)]
    code_counts: dict[str, int] = {}
    for gap in all_gaps:
        code = str(gap["code"])
        code_counts[code] = code_counts.get(code, 0) + 1
    ordered_gaps = all_gaps[:MAX_PROJECTION_GAPS]
    gap_summary = {
        "total": len(all_gaps),
        "returned": len(ordered_gaps),
        "omitted": len(all_gaps) - len(ordered_gaps),
        "by_code": dict(sorted(code_counts.items())),
    }
    value: dict[str, Any] = {
        "schema": PROJECTION_SNAPSHOT_SCHEMA,
        "status": "available" if not all_gaps else "unavailable",
        "binding_authority": binding_snapshot.to_dict(),
        "domain_authority": None if domain is None else {
            "authority_id": domain.authority_id,
            "generation": domain.generation,
            "snapshot_sha256": domain.snapshot_sha256,
        },
        "network": None if network is None else network.to_dict(),
        "surfaces": [item.to_dict() for item in surfaces],
        "states": [item.to_dict() for item in states],
        "transition_states": [],
        "steps": [item.to_dict() for item in steps],
        "conditions": [item.to_dict() for item in conditions],
        "evidence_gaps": ordered_gaps,
        "gap_summary": gap_summary,
        "job_source_of_truth": "job.yaml",
        "authorizes_execution": False,
    }
    value["projection_sha256"] = _digest_material(value)
    if len(_canonical_bytes(value)) > MAX_PUBLIC_SNAPSHOT_BYTES:
        resource_gap = _gap(
            "unavailable", "projection_snapshot_resource_limit",
            "ReactionNetwork", None,
            byte_limit=MAX_PUBLIC_SNAPSHOT_BYTES,
        )
        resource_counts = dict(code_counts)
        resource_counts[resource_gap["code"]] = (
            resource_counts.get(resource_gap["code"], 0) + 1)
        value = {
            "schema": PROJECTION_SNAPSHOT_SCHEMA,
            "status": "unavailable",
            "binding_authority": binding_snapshot.to_dict(),
            "domain_authority": None if domain is None else {
                "authority_id": domain.authority_id,
                "generation": domain.generation,
                "snapshot_sha256": domain.snapshot_sha256,
            },
            "network": None,
            "surfaces": [], "states": [], "transition_states": [],
            "steps": [], "conditions": [],
            "evidence_gaps": [resource_gap],
            "gap_summary": {
                "total": len(all_gaps) + 1,
                "returned": 1,
                "omitted": len(all_gaps),
                "by_code": dict(sorted(resource_counts.items())),
            },
            "job_source_of_truth": "job.yaml",
            "authorizes_execution": False,
        }
        value["projection_sha256"] = _digest_material(value)
        if len(_canonical_bytes(value)) > MAX_PUBLIC_SNAPSHOT_BYTES:
            raise CatalysisProjectionError(
                "bounded projection snapshot exceeds its fixed byte limit")
    try:
        reject_sensitive(value, field="projection_snapshot")
    except CatalysisContractError as exc:
        raise CatalysisProjectionError(
            "projection snapshot crossed the public-data boundary") from exc
    return value


__all__ = [
    "ACTIVE_BINDING_SCHEMA", "ActiveBindingAuthoritySnapshot",
    "ActiveNetworkBinding", "BINDING_AUTHORITY_SCHEMA",
    "BINDING_SNAPSHOT_SCHEMA", "BINDING_STORE_FILENAME",
    "BINDING_WRITE_SCHEMA", "BindingWriteResult", "CatalysisProjectionAuthority",
    "CatalysisProjectionError", "DOMAIN_STORE_FILENAME",
    "MAX_BINDING_REVISIONS", "MAX_BINDING_STORE_BYTES",
    "MAX_CLOSURE_REFERENCES", "MAX_NETWORK_CONDITIONS",
    "MAX_NETWORK_MEMBERS", "MAX_NETWORK_STATES", "MAX_NETWORK_STEPS",
    "MAX_NETWORK_SURFACES", "MAX_PROJECTION_GAPS",
    "MAX_PUBLIC_SNAPSHOT_BYTES", "MAX_STEP_PARTICIPANTS",
    "NETWORK_HEAD_CAS_SCHEMA", "NetworkHeadCAS", "PROJECTION_SNAPSHOT_SCHEMA",
]
