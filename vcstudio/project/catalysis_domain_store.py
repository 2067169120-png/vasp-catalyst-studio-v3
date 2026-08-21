"""Create-only/CAS persistence for immutable catalysis domain envelopes."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from vcstudio.project.catalysis_contracts import (
    CatalysisContractError,
    DomainEnvelope,
)


STORE_SCHEMA = "vcstudio.catalysis-domain-store/v1"
HEAD_SNAPSHOT_SCHEMA = "vcstudio.catalysis-domain-head-snapshot/v1"
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_STORE_HEADS = 5_000
MAX_STORE_REVISIONS = 20_000
_MAX_LOCK_BYTES = 4_096
_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_LOCK = threading.RLock()


class DomainRevisionConflict(CatalysisContractError):
    """An immutable revision identity or current-head CAS check conflicted."""

    def __init__(self, reason: str, envelope: DomainEnvelope):
        self.reason = reason
        self.object_type = envelope.object_type
        self.object_id = envelope.object_id
        self.object_revision_id = envelope.object_revision_id
        super().__init__(
            f"domain revision conflict ({reason}) for "
            f"{self.object_type}/{self.object_id}/{self.object_revision_id}"
        )


@dataclass(frozen=True)
class DomainWriteResult:
    action: str
    generation: int
    envelope: DomainEnvelope
    job_source_of_truth: str = "job.yaml"
    authorizes_execution: bool = False

    def __post_init__(self) -> None:
        if self.action not in {"created", "advanced", "replayed"}:
            raise CatalysisContractError("domain write action is invalid")
        if (isinstance(self.generation, bool) or not isinstance(self.generation, int)
                or self.generation < 1):
            raise CatalysisContractError("domain store generation is invalid")
        if self.job_source_of_truth != "job.yaml" or self.authorizes_execution is not False:
            raise CatalysisContractError("domain write authority boundary is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "generation": self.generation,
            "envelope": self.envelope.to_dict(),
            "job_source_of_truth": self.job_source_of_truth,
            "authorizes_execution": self.authorizes_execution,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalysisContractError("domain snapshot is not canonical JSON") from exc


@dataclass(frozen=True)
class DomainHeadSnapshot:
    """One atomic, immutable and path-free view of every current domain head."""

    authority_id: str
    generation: int
    heads: tuple[DomainEnvelope, ...]
    snapshot_sha256: str = ""
    schema: str = HEAD_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != HEAD_SNAPSHOT_SCHEMA:
            raise CatalysisContractError("domain head snapshot schema is unsupported")
        if not _AUTHORITY_RE.fullmatch(str(self.authority_id or "")):
            raise CatalysisContractError("domain head snapshot authority is invalid")
        if (isinstance(self.generation, bool)
                or not isinstance(self.generation, int)
                or not 0 <= self.generation <= MAX_STORE_REVISIONS):
            raise CatalysisContractError("domain head snapshot generation is invalid")
        if (isinstance(self.heads, (str, bytes))
                or not isinstance(self.heads, Sequence)
                or len(self.heads) > MAX_STORE_HEADS):
            raise CatalysisContractError("domain head snapshot heads are invalid")
        heads = tuple(self.heads)
        if any(not isinstance(item, DomainEnvelope) for item in heads):
            raise CatalysisContractError("domain head snapshot requires envelopes")
        ordered = tuple(sorted(
            heads,
            key=lambda item: (
                item.object_type, item.object_id, item.object_revision_id,
            ),
        ))
        identities = {(item.object_type, item.object_id) for item in ordered}
        if len(identities) != len(ordered) or ordered != heads:
            raise CatalysisContractError(
                "domain head snapshot heads must be sorted unique identities")
        material = {
            "schema": self.schema,
            "authority_id": self.authority_id,
            "generation": self.generation,
            "heads": [item.to_dict() for item in ordered],
        }
        digest = hashlib.sha256(_canonical_bytes(material)).hexdigest()
        if self.snapshot_sha256 not in {"", digest}:
            raise CatalysisContractError("domain head snapshot hash is invalid")
        object.__setattr__(self, "heads", ordered)
        object.__setattr__(self, "snapshot_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "authority_id": self.authority_id,
            "generation": self.generation,
            "heads": [item.to_dict() for item in self.heads],
            "snapshot_sha256": self.snapshot_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DomainHeadSnapshot":
        if not isinstance(value, Mapping) or set(value) != {
                "schema", "authority_id", "generation", "heads",
                "snapshot_sha256"}:
            raise CatalysisContractError("domain head snapshot contract is invalid")
        raw_heads = value["heads"]
        if isinstance(raw_heads, (str, bytes)) or not isinstance(raw_heads, Sequence):
            raise CatalysisContractError("domain head snapshot heads are invalid")
        return cls(
            schema=value["schema"], authority_id=value["authority_id"],
            generation=value["generation"],
            heads=tuple(DomainEnvelope.from_dict(item) for item in raw_heads),
            snapshot_sha256=value["snapshot_sha256"],
        )


def _identity_key(object_type: str, object_id: str) -> str:
    wire = json.dumps(
        [object_type, object_id], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(wire).hexdigest()


def _revision_key(envelope: DomainEnvelope) -> str:
    wire = json.dumps(
        [envelope.object_type, envelope.object_id, envelope.object_revision_id],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(wire).hexdigest()


def _path_is_linklike(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CatalysisContractError("domain store entity is unavailable") from exc
    return bool(
        stat.S_ISLNK(details.st_mode)
        or int(getattr(details, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _ensure_safe_parent(path: Path) -> None:
    authored = Path(os.path.abspath(path.parent))
    cursor = authored
    missing: list[Path] = []
    while not os.path.lexists(cursor):
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            raise CatalysisContractError("domain store directory is unavailable")
        cursor = parent
    existing_real = Path(os.path.realpath(cursor))
    if (_path_is_linklike(cursor)
            or os.path.normcase(str(cursor))
            != os.path.normcase(str(existing_real))):
        raise CatalysisContractError(
            "domain store directory must not use a link or reparse point")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise CatalysisContractError("domain store directory is unavailable") from exc
        if _path_is_linklike(directory):
            raise CatalysisContractError(
                "domain store directory must not use a link or reparse point")
    canonical = Path(os.path.realpath(authored))
    if (os.path.normcase(str(authored)) != os.path.normcase(str(canonical))
            or _path_is_linklike(authored)):
        raise CatalysisContractError(
            "domain store directory must not use a link or reparse point")
    try:
        details = os.lstat(authored)
    except OSError as exc:
        raise CatalysisContractError("domain store directory is unavailable") from exc
    if not stat.S_ISDIR(details.st_mode):
        raise CatalysisContractError("domain store directory is not a directory")


def _opened_regular_identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        int(details.st_dev), int(details.st_ino), int(details.st_size),
        int(details.st_mtime_ns), int(details.st_ctime_ns),
    )


def _read_bounded_regular(path: Path, *, maximum: int) -> bytes | None:
    try:
        before_path = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CatalysisContractError("domain store is unreadable; refusing overwrite") from exc
    if (_path_is_linklike(path) or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size < 0 or before_path.st_size > maximum):
        raise CatalysisContractError("domain store is unsafe or exceeds its resource limit")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CatalysisContractError("domain store is unreadable; refusing overwrite") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size < 0
                or opened.st_size > maximum):
            raise CatalysisContractError(
                "domain store is unsafe or exceeds its resource limit")
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (len(payload) != opened.st_size
                or _opened_regular_identity(opened)
                != _opened_regular_identity(after)
                or (int(before_path.st_dev), int(before_path.st_ino))
                != (int(opened.st_dev), int(opened.st_ino))):
            raise CatalysisContractError("domain store changed while it was read")
        return payload
    except CatalysisContractError:
        raise
    except OSError as exc:
        raise CatalysisContractError(
            "domain store is unreadable; refusing overwrite") from exc
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _exclusive_store_lock(lock_path: Path) -> Iterator[None]:
    with _PROCESS_LOCK:
        _ensure_safe_parent(lock_path)
        if _path_is_linklike(lock_path):
            raise CatalysisContractError(
                "domain store lock must not be a link or reparse point")
        flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise CatalysisContractError("domain store lock is unavailable") from exc
        with os.fdopen(descriptor, "a+b") as handle:
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_LOCK_BYTES:
                raise CatalysisContractError("domain store lock is unsafe")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_store() -> dict[str, Any]:
    return {
        "schema": STORE_SCHEMA,
        "authority_id": secrets.token_hex(16),
        "generation": 0,
        "heads": {},
        "revisions": {},
    }


def _validated_store(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
            "schema", "authority_id", "generation", "heads", "revisions"}:
        raise CatalysisContractError("domain store contract is invalid")
    if value["schema"] != STORE_SCHEMA or not _AUTHORITY_RE.fullmatch(
            str(value["authority_id"] or "")):
        raise CatalysisContractError("domain store authority is invalid")
    generation = value["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise CatalysisContractError("domain store generation is invalid")
    if not isinstance(value["heads"], Mapping) or not isinstance(value["revisions"], Mapping):
        raise CatalysisContractError("domain store index is invalid")
    if (len(value["heads"]) > MAX_STORE_HEADS
            or len(value["revisions"]) > MAX_STORE_REVISIONS):
        raise CatalysisContractError("domain store exceeds its resource limit")

    revisions: dict[str, dict[str, Any]] = {}
    parsed: dict[str, DomainEnvelope] = {}
    for key, raw in value["revisions"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(key)) or not isinstance(raw, Mapping):
            raise CatalysisContractError("domain store revision entry is invalid")
        envelope = DomainEnvelope.from_dict(raw)
        if envelope.object_type == "WorkflowRecipe" or _revision_key(envelope) != key:
            raise CatalysisContractError("domain store revision identity is invalid")
        revisions[key] = envelope.to_dict()
        parsed[key] = envelope

    heads: dict[str, str] = {}
    for key, revision_key in value["heads"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(key)) or revision_key not in parsed:
            raise CatalysisContractError("domain store head is invalid")
        envelope = parsed[revision_key]
        if _identity_key(envelope.object_type, envelope.object_id) != key:
            raise CatalysisContractError("domain store head identity is invalid")
        heads[str(key)] = str(revision_key)

    if generation != len(revisions):
        raise CatalysisContractError("domain store generation does not match revisions")

    groups: dict[str, set[str]] = {}
    for revision_key, envelope in parsed.items():
        identity = _identity_key(envelope.object_type, envelope.object_id)
        groups.setdefault(identity, set()).add(revision_key)
    if set(heads) != set(groups):
        raise CatalysisContractError("domain store must have exactly one head per identity")

    for identity, revision_keys in groups.items():
        by_revision = {
            parsed[key].object_revision_id: key for key in revision_keys
        }
        children: dict[str, str] = {}
        roots: list[str] = []
        for revision_key in revision_keys:
            envelope = parsed[revision_key]
            parent_id = envelope.parent_revision
            if parent_id is None:
                roots.append(revision_key)
                continue
            parent_key = by_revision.get(parent_id)
            if parent_key is None:
                raise CatalysisContractError("domain store revision parent is missing")
            parent = parsed[parent_key]
            if envelope.expected_current_hash != parent.semantic_sha256:
                raise CatalysisContractError("domain store revision parent hash is invalid")
            if parent_key in children:
                raise CatalysisContractError("domain store revision history contains a fork")
            children[parent_key] = revision_key
        if len(roots) != 1:
            raise CatalysisContractError("domain store revision history requires one root")
        visited: set[str] = set()
        cursor: str | None = roots[0]
        while cursor is not None:
            if cursor in visited:
                raise CatalysisContractError("domain store revision history contains a cycle")
            visited.add(cursor)
            cursor = children.get(cursor)
        if visited != revision_keys:
            raise CatalysisContractError("domain store revision history is disconnected")
        leaf = next(key for key in revision_keys if key not in children)
        if heads[identity] != leaf:
            raise CatalysisContractError("domain store head is not the revision chain leaf")

    return {
        "schema": STORE_SCHEMA,
        "authority_id": str(value["authority_id"]),
        "generation": generation,
        "heads": heads,
        "revisions": revisions,
    }


def _read_store(path: Path) -> dict[str, Any]:
    payload = _read_bounded_regular(path, maximum=MAX_STORE_BYTES)
    if payload is None:
        return _empty_store()
    return _store_from_payload(payload)


def _store_from_payload(payload: bytes) -> dict[str, Any]:
    try:
        return _validated_store(json.loads(payload.decode("utf-8")))
    except CatalysisContractError:
        raise
    except Exception as exc:
        raise CatalysisContractError("domain store is unreadable; refusing overwrite") from exc


def _write_store(path: Path, value: Mapping[str, Any]) -> None:
    _ensure_safe_parent(path)
    if _path_is_linklike(path):
        raise CatalysisContractError(
            "domain store file must not be a link or reparse point")
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(payload) > MAX_STORE_BYTES:
        raise CatalysisContractError("domain store exceeds its resource limit")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class DomainEnvelopeStore:
    """Single-authority immutable-revision store with atomic head CAS."""

    def __init__(
            self, path: str | os.PathLike[str], *,
            boundary_validator: Callable[[], None] | None = None):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        if boundary_validator is not None and not callable(boundary_validator):
            raise CatalysisContractError("domain store boundary validator is invalid")
        self._boundary_validator = boundary_validator

    def _verify_boundary(self) -> None:
        if self._boundary_validator is not None:
            self._boundary_validator()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._verify_boundary()
        try:
            with _exclusive_store_lock(self.lock_path):
                self._verify_boundary()
                try:
                    yield
                finally:
                    self._verify_boundary()
        finally:
            self._verify_boundary()

    @staticmethod
    def _head_snapshot(store: Mapping[str, Any]) -> DomainHeadSnapshot:
        heads = tuple(sorted(
            (
                DomainEnvelope.from_dict(store["revisions"][revision_key])
                for revision_key in store["heads"].values()
            ),
            key=lambda item: (
                item.object_type, item.object_id, item.object_revision_id,
            ),
        ))
        return DomainHeadSnapshot(
            authority_id=store["authority_id"],
            generation=store["generation"], heads=heads,
        )

    def put(self, envelope: DomainEnvelope) -> DomainWriteResult:
        if not isinstance(envelope, DomainEnvelope):
            raise CatalysisContractError("domain store requires a DomainEnvelope")
        if envelope.object_type == "WorkflowRecipe":
            raise CatalysisContractError("workflow recipes are versioned by the built-in catalog")
        with self._locked():
            store = _read_store(self.path)
            identity_key = _identity_key(envelope.object_type, envelope.object_id)
            revision_key = _revision_key(envelope)
            existing_raw = store["revisions"].get(revision_key)
            if existing_raw is not None:
                existing = DomainEnvelope.from_dict(existing_raw)
                if existing.semantic_sha256 != envelope.semantic_sha256:
                    raise DomainRevisionConflict("immutable_revision_reused", envelope)
                return DomainWriteResult("replayed", store["generation"], existing)

            head_key = store["heads"].get(identity_key)
            if head_key is None:
                if envelope.parent_revision is not None or envelope.expected_current_hash is not None:
                    raise DomainRevisionConflict("unexpected_parent_for_create", envelope)
                action = "created"
            else:
                current = DomainEnvelope.from_dict(store["revisions"][head_key])
                if envelope.parent_revision != current.object_revision_id:
                    raise DomainRevisionConflict("stale_parent_revision", envelope)
                if envelope.expected_current_hash != current.semantic_sha256:
                    raise DomainRevisionConflict("stale_current_hash", envelope)
                action = "advanced"

            candidate = {
                **store,
                "generation": store["generation"] + 1,
                "heads": {**store["heads"], identity_key: revision_key},
                "revisions": {**store["revisions"], revision_key: envelope.to_dict()},
            }
            validated = _validated_store(candidate)
            _write_store(self.path, validated)
            return DomainWriteResult(action, validated["generation"], envelope)

    def snapshot_heads(self) -> DomainHeadSnapshot:
        """Read all heads under one cross-process lock and pin their authority."""

        with self.locked_snapshot_heads(initialize=True) as snapshot:
            if snapshot is None:
                raise CatalysisContractError("domain authority initialization failed")
            return snapshot

    @contextlib.contextmanager
    def locked_snapshot_heads(
            self, *, initialize: bool = False,
            ) -> Iterator[DomainHeadSnapshot | None]:
        """Yield one snapshot while retaining the cross-process store lock.

        Projection confirmation uses this boundary so a domain writer cannot
        advance any head between validating the caller's expected snapshot and
        committing its binding CAS.
        """

        if not isinstance(initialize, bool):
            raise CatalysisContractError("domain snapshot initialization flag is invalid")
        with self._locked():
            payload = _read_bounded_regular(self.path, maximum=MAX_STORE_BYTES)
            if payload is None:
                if not initialize:
                    yield None
                    return
                store = _empty_store()
                # Persist even the empty authority so repeated snapshots cannot
                # observe a new authority ID without an actual store rebuild.
                _write_store(self.path, store)
            else:
                store = _store_from_payload(payload)
            yield self._head_snapshot(store)

    def authority_exists(self) -> bool:
        """Return whether a safe persisted authority exists, without creating it."""

        with self._locked():
            return _read_bounded_regular(
                self.path, maximum=MAX_STORE_BYTES) is not None

    def head(self, object_type: str, object_id: str) -> DomainEnvelope | None:
        with self._locked():
            store = _read_store(self.path)
            key = store["heads"].get(_identity_key(object_type, object_id))
            return None if key is None else DomainEnvelope.from_dict(store["revisions"][key])

    def get(self, object_type: str, object_id: str,
            object_revision_id: str) -> DomainEnvelope | None:
        probe = hashlib.sha256(json.dumps(
            [object_type, object_id, object_revision_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        with self._locked():
            raw = _read_store(self.path)["revisions"].get(probe)
            return None if raw is None else DomainEnvelope.from_dict(raw)


__all__ = [
    "DomainEnvelopeStore", "DomainHeadSnapshot", "DomainRevisionConflict",
    "DomainWriteResult", "HEAD_SNAPSHOT_SCHEMA", "MAX_STORE_BYTES",
    "MAX_STORE_HEADS", "MAX_STORE_REVISIONS", "STORE_SCHEMA",
]
