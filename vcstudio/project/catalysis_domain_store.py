"""Create-only/CAS persistence for immutable catalysis domain envelopes."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from vcstudio.project.catalysis_contracts import (
    CatalysisContractError,
    DomainEnvelope,
)


STORE_SCHEMA = "vcstudio.catalysis-domain-store/v1"
_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
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


@contextlib.contextmanager
def _exclusive_store_lock(lock_path: Path) -> Iterator[None]:
    with _PROCESS_LOCK:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
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
    if not path.exists():
        return _empty_store()
    try:
        with open(path, encoding="utf-8") as handle:
            return _validated_store(json.load(handle))
    except CatalysisContractError:
        raise
    except Exception as exc:
        raise CatalysisContractError("domain store is unreadable; refusing overwrite") from exc


def _write_store(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8") + b"\n"
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

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def put(self, envelope: DomainEnvelope) -> DomainWriteResult:
        if not isinstance(envelope, DomainEnvelope):
            raise CatalysisContractError("domain store requires a DomainEnvelope")
        if envelope.object_type == "WorkflowRecipe":
            raise CatalysisContractError("workflow recipes are versioned by the built-in catalog")
        with _exclusive_store_lock(self.lock_path):
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

    def head(self, object_type: str, object_id: str) -> DomainEnvelope | None:
        with _exclusive_store_lock(self.lock_path):
            store = _read_store(self.path)
            key = store["heads"].get(_identity_key(object_type, object_id))
            return None if key is None else DomainEnvelope.from_dict(store["revisions"][key])

    def get(self, object_type: str, object_id: str,
            object_revision_id: str) -> DomainEnvelope | None:
        probe = hashlib.sha256(json.dumps(
            [object_type, object_id, object_revision_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        with _exclusive_store_lock(self.lock_path):
            raw = _read_store(self.path)["revisions"].get(probe)
            return None if raw is None else DomainEnvelope.from_dict(raw)


__all__ = [
    "DomainEnvelopeStore", "DomainRevisionConflict", "DomainWriteResult", "STORE_SCHEMA",
]
