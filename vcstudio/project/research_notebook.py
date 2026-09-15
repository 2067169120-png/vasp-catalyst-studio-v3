"""Project-local Research Notebook and Human Review Ledger.

The notebook is intentionally independent from report qualification.  It stores
plain research notes, decisions, and self-attributed local human review records,
but it never creates or upgrades :class:`ValidationResult` and it never grants a
``human_scientific_reviewed`` qualification.

Records are an append-only hash chain.  A stable external project-identity lock
protects revision/head/project compare-and-swap across processes, and a separate
durable head/sequence anchor exposes journal truncation or deletion.  Each JSONL
append is one ``O_APPEND`` write followed by ``fsync``.  Edits append a
``supersedes`` record; deletions append a tombstone.  Attachment bytes remain on
the project filesystem and browser-facing values contain metadata only.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import mimetypes
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from vcstudio.shared.credential_classifier import (
    classify_credential_structure,
    is_sensitive_key,
    looks_like_credential,
    redact_credential_structure,
    redact_credentials,
    redact_local_paths,
)


NOTEBOOK_SCHEMA = "vcstudio.research-notebook/v1"
PUBLIC_SCHEMA = "vcstudio.research-notebook-public/v1"
ARCHIVE_SCHEMA = "vcstudio.research-notebook-archive/v1"
ANCHOR_SCHEMA = "vcstudio.research-notebook-anchor/v1"
PENDING_SCHEMA = "vcstudio.research-notebook-anchor-pending/v1"
JOURNAL_NAME = "ledger.jsonl"
LOCK_NAME = ".ledger.lock"
ATTACHMENTS_DIR = "attachments"

RECORD_TYPES = ("note", "decision", "review", "tombstone")
NOTE_CATEGORIES = (
    "problem",
    "hypothesis",
    "observation",
    "interpretation",
    "limitation",
    "next_step",
)
REVIEW_DECISIONS = ("approved", "request_changes", "rejected", "comment")
LINK_KINDS = ("project", "job", "source", "report_revision")

MAX_BODY_CHARS = 200_000
MAX_RECORDS = 100_000
MAX_LINKS = 64
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL_BYTES = 50 * 1024 * 1024
MAX_NOTEBOOK_UNIQUE_BLOBS = 1_024
MAX_NOTEBOOK_BLOB_BYTES = 2 * 1024 * 1024 * 1024
MAX_JOB_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_BYTES = 512 * 1024 * 1024
MAX_JOURNAL_ROW_BYTES = 4 * 1024 * 1024
MAX_PENDING_BYTES = MAX_JOURNAL_ROW_BYTES * 2 + 64 * 1024

_PROJECT_ID_RE = re.compile(r"(?:project-[a-f0-9]{32}|registry-[a-f0-9]{24})")
_RECORD_ID_RE = re.compile(r"rn-[a-f0-9]{32}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RESERVED_ACTOR_FIELDS = frozenset({
    "actor_type", "reviewer_type", "made_by", "identity_assurance",
    "entry_method", "cryptographic_signature", "signature_verified",
})
_RECORD_FIELDS = frozenset({
    "schema", "revision", "record_id", "record_type", "category",
    "project_id", "created_at_utc", "body", "actor", "review", "links",
    "attachments", "supersedes", "tombstones", "previous_digest",
    "idempotency_key", "request_digest", "record_digest",
})
_ACTOR_FIELDS = frozenset({
    "id", "display_name", "role", "actor_type", "entry_method",
    "identity_assurance",
})
_REVIEW_FIELDS = frozenset({
    "decision", "requested_changes", "signature_attribution",
    "signature_kind", "cryptographic_signature", "identity_assurance",
})
_LINK_FIELDS = frozenset({
    "kind", "id", "report_revision_id", "bound_digest",
})
_ATTACHMENT_FIELDS = frozenset({
    "attachment_id", "name", "sha256", "size", "media_type",
})
_ANCHOR_FIELDS = frozenset({
    "schema", "project_id", "project_identity_digest", "sequence",
    "head_digest", "anchor_digest",
})
_PENDING_FIELDS = frozenset({
    "schema", "project_id", "project_identity_digest",
    "old_sequence", "old_head_digest", "old_anchor_digest",
    "new_sequence", "new_head_digest", "journal_size_before",
    "journal_size_after", "journal_prefix_sha256", "row_payload_sha256",
    "successor_row_payload", "pending_digest",
})
_MANIFEST_LOCATOR_FIELDS = frozenset({
    "path", "root", "directory", "dir", "locator", "destination",
    "remote_dir", "local_dir", "workdir", "cwd", "hostname", "host",
    "username", "user", "uri", "url", "endpoint", "remote_root",
    "scheduler_bin", "template_path", "key_path", "keypath",
})

_PROCESS_LOCK = threading.RLock()


class NotebookError(ValueError):
    """Base class for notebook contract failures."""


class NotebookRevisionConflict(NotebookError):
    """Optimistic revision compare-and-swap failed."""

    def __init__(self, current_revision: int, current_head_digest: str | None = None):
        super().__init__("research notebook revision conflict")
        self.current_revision = int(current_revision)
        self.current_head_digest = current_head_digest


class NotebookIntegrityError(NotebookError):
    """The append-only journal failed structural or digest verification."""

    def __init__(self, message: str, *, line_number: int | None = None):
        super().__init__(message)
        self.line_number = line_number


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_text(value: Any, field: str, *, required: bool = False,
                   limit: int = 2_000) -> str:
    if not isinstance(value, str):
        raise NotebookError(f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise NotebookError(f"{field} must not be empty")
    if len(text) > limit:
        raise NotebookError(f"{field} is too long")
    if looks_like_credential(text):
        raise NotebookError(f"{field} contains credential-like material")
    return text


def _validate_token(value: Any, field: str) -> str:
    text = _validate_text(value, field, required=True, limit=160)
    if not _TOKEN_RE.fullmatch(text):
        raise NotebookError(f"{field} must be an opaque identifier")
    return text


def _bounded_items(value: Iterable[Any] | None, *, limit: int,
                   error: str) -> list[Any]:
    """Materialize at most *limit* items, inspecting only one excess item."""

    items: list[Any] = []
    try:
        iterator = iter(value or ())
    except TypeError as exc:
        raise NotebookError(error) from exc
    for item in iterator:
        if len(items) == limit:
            raise NotebookError(error)
        items.append(item)
    return items


def _validate_project_id(value: Any) -> str:
    text = _validate_text(value, "project_id", required=True, limit=64).lower()
    if not _PROJECT_ID_RE.fullmatch(text):
        raise NotebookError("project_id must be a registered opaque identity")
    return text


def redact_public_text(value: Any) -> str:
    """Redact local paths and credential-like values while retaining prose."""

    text = redact_credentials(value)
    return redact_local_paths(text, replacement="<local-path>")


def _reject_credential_structure(value: Any, field: str) -> None:
    hit = classify_credential_structure(value)
    if hit:
        raise NotebookError(f"{field} contains credential-like material ({hit})")


def _redact_public_structure(value: Any) -> Any:
    """Apply credential and local-path redaction to a complete public DTO."""

    credential_safe = redact_credential_structure(value)

    def redact_paths(child: Any) -> Any:
        if isinstance(child, str):
            return redact_public_text(child)
        if isinstance(child, Mapping):
            return {str(key): redact_paths(item) for key, item in child.items()}
        if isinstance(child, (list, tuple)):
            return [redact_paths(item) for item in child]
        return child

    return redact_paths(credential_safe)


def _manifest_locator_field(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    return (
        normalized in _MANIFEST_LOCATOR_FIELDS
        or normalized.endswith((
            "_path", "_root", "_directory", "_dir", "_locator",
            "_destination", "_hostname", "_host", "_uri", "_url",
        ))
    )


def _public_manifest_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return redact_public_text(value) if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NotebookIntegrityError("job manifest contains a non-finite value")
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        public = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise NotebookIntegrityError("job manifest keys must be text")
            if _manifest_locator_field(key) or is_sensitive_key(key):
                continue
            public[key] = _public_manifest_value(child)
        return public
    if isinstance(value, (list, tuple)):
        return [_public_manifest_value(child) for child in value]
    raise NotebookIntegrityError("job manifest contains an unsupported public value")


def public_job_manifest_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project the complete manifest, removing only locator/credential material."""

    if not isinstance(manifest, Mapping):
        raise NotebookIntegrityError("job manifest must be an object")
    projected = _public_manifest_value(manifest)
    if not isinstance(projected, dict):  # pragma: no cover - guarded above
        raise NotebookIntegrityError("job manifest projection is invalid")
    return projected


def _actor(value: Any, *, human_review: bool = False,
           ai_proposal: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NotebookError("actor must be an object")
    forbidden = sorted(set(value) & _RESERVED_ACTOR_FIELDS)
    if forbidden:
        raise NotebookError(
            "actor assurance fields are server-controlled: " + ", ".join(forbidden)
        )
    actor_id = _validate_token(value.get("id"), "actor.id")
    display_name = _validate_text(
        value.get("display_name"), "actor.display_name", required=True, limit=160
    )
    role = _validate_text(value.get("role", "researcher"), "actor.role",
                          required=True, limit=120)
    if human_review and ai_proposal:
        raise NotebookError("AI proposals cannot be recorded as human review")
    if ai_proposal:
        return {
            "id": actor_id,
            "display_name": display_name,
            "role": role,
            "actor_type": "ai",
            "entry_method": "ai-proposal",
            "identity_assurance": "declared-software-agent",
        }
    return {
        "id": actor_id,
        "display_name": display_name,
        "role": role,
        "actor_type": "human",
        "entry_method": "local-explicit",
        "identity_assurance": "self-asserted-local",
    }


def _review(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NotebookError("review must be an object")
    allowed = {"decision", "requested_changes", "signature_attribution",
               "local_human_attestation"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise NotebookError("review contains unsupported fields: " + ", ".join(unknown))
    if value.get("local_human_attestation") is not True:
        raise NotebookError("human review requires explicit local human attestation")
    decision = _validate_text(
        value.get("decision"), "review.decision", required=True, limit=32
    )
    if decision not in REVIEW_DECISIONS:
        raise NotebookError("review.decision is unsupported")
    requested = value.get("requested_changes") or []
    if isinstance(requested, str) or not isinstance(requested, (list, tuple)):
        raise NotebookError("review.requested_changes must be a list")
    if len(requested) > 64:
        raise NotebookError("review.requested_changes contains too many entries")
    changes = [
        _validate_text(item, "review.requested_changes", required=True, limit=2_000)
        for item in requested
    ]
    if decision == "request_changes" and not changes:
        raise NotebookError("request_changes review requires requested changes")
    attribution = _validate_text(
        value.get("signature_attribution", ""),
        "review.signature_attribution",
        required=False,
        limit=500,
    )
    return {
        "decision": decision,
        "requested_changes": changes,
        "signature_attribution": attribution or None,
        "signature_kind": "typed-attribution" if attribution else "none",
        "cryptographic_signature": False,
        "identity_assurance": "self-asserted-local",
    }


def bind_links(raw_links: Any, resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]]) \
        -> list[dict[str, Any]]:
    """Resolve opaque evidence links and freeze their current digest."""

    if raw_links is None:
        return []
    if isinstance(raw_links, (str, bytes)) or not isinstance(raw_links, (list, tuple)):
        raise NotebookError("links must be a list")
    if len(raw_links) > MAX_LINKS:
        raise NotebookError("links contains too many entries")
    bound: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_links:
        if not isinstance(raw, Mapping):
            raise NotebookError("each link must be an object")
        unknown = sorted(set(raw) - {"kind", "id", "report_revision_id"})
        if unknown:
            raise NotebookError("link contains unsupported fields: " + ", ".join(unknown))
        kind = _validate_text(raw.get("kind"), "link.kind", required=True, limit=32)
        if kind not in LINK_KINDS:
            raise NotebookError("link.kind is unsupported")
        identifier = _validate_token(raw.get("id"), "link.id")
        report_revision_id = None
        if raw.get("report_revision_id") is not None:
            report_revision_id = _validate_token(
                raw.get("report_revision_id"), "link.report_revision_id"
            )
        if kind == "source" and not report_revision_id:
            raise NotebookError("source links require report_revision_id")
        if kind != "source" and report_revision_id:
            raise NotebookError("report_revision_id is only valid for source links")
        key = (kind, identifier, report_revision_id or "")
        if key in seen:
            raise NotebookError("links must not contain duplicates")
        seen.add(key)
        resolved = resolver({
            "kind": kind,
            "id": identifier,
            "report_revision_id": report_revision_id,
        })
        if not isinstance(resolved, Mapping) or resolved.get("status") != "current":
            raise NotebookError(f"linked {kind} evidence is missing or stale")
        evidence_digest = str(resolved.get("digest") or "").lower()
        if not _SHA256_RE.fullmatch(evidence_digest):
            raise NotebookError("linked evidence did not provide a valid digest")
        bound.append({
            "kind": kind,
            "id": identifier,
            "report_revision_id": report_revision_id,
            "bound_digest": evidence_digest,
        })
    return bound


def _validate_bound_links(value: Any, *, project_id: str | None = None) \
        -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise NotebookIntegrityError("record links must be a list")
    if len(value) > MAX_LINKS:
        raise NotebookIntegrityError("record links exceed the safety limit")
    links = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise NotebookIntegrityError("record link is not an object")
        if set(raw) != _LINK_FIELDS:
            raise NotebookIntegrityError("record link fields are invalid")
        kind = raw.get("kind")
        identifier = raw.get("id")
        revision = raw.get("report_revision_id")
        digest = raw.get("bound_digest")
        if (not isinstance(kind, str) or kind not in LINK_KINDS
                or not isinstance(identifier, str) or not _TOKEN_RE.fullmatch(identifier)
                or looks_like_credential(identifier)
                or (revision is not None and (
                    not isinstance(revision, str) or not _TOKEN_RE.fullmatch(revision)
                    or looks_like_credential(revision)))
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)):
            raise NotebookIntegrityError("record link contract is invalid")
        if (kind == "source") != (revision is not None):
            raise NotebookIntegrityError("record source revision binding is invalid")
        if kind == "project" and project_id is not None and identifier != project_id:
            raise NotebookIntegrityError("record project link binding is invalid")
        key = (kind, identifier, revision or "")
        if key in seen:
            raise NotebookIntegrityError("record links contain a duplicate binding")
        seen.add(key)
        links.append({
            "kind": kind,
            "id": identifier,
            "report_revision_id": revision,
            "bound_digest": digest,
        })
    return links


def _stored_text(value: Any, field: str, *, required: bool = False,
                 limit: int = 2_000) -> str:
    try:
        return _validate_text(value, field, required=required, limit=limit)
    except NotebookError as exc:
        raise NotebookIntegrityError(f"stored {field} is invalid") from exc


def _validate_stored_actor(value: Any, *, human_required: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ACTOR_FIELDS:
        raise NotebookIntegrityError("stored actor fields are invalid")
    actor_id = value.get("id")
    if (not isinstance(actor_id, str) or not _TOKEN_RE.fullmatch(actor_id)
            or looks_like_credential(actor_id)):
        raise NotebookIntegrityError("stored actor id is invalid")
    display_name = _stored_text(
        value.get("display_name"), "actor.display_name", required=True, limit=160)
    role = _stored_text(value.get("role"), "actor.role", required=True, limit=120)
    actor_type = value.get("actor_type")
    if actor_type == "human":
        expected = ("local-explicit", "self-asserted-local")
    elif actor_type == "ai" and not human_required:
        expected = ("ai-proposal", "declared-software-agent")
    else:
        raise NotebookIntegrityError("stored actor type is invalid")
    if (value.get("entry_method"), value.get("identity_assurance")) != expected:
        raise NotebookIntegrityError("stored actor assurance is invalid")
    return {
        "id": actor_id, "display_name": display_name, "role": role,
        "actor_type": actor_type, "entry_method": expected[0],
        "identity_assurance": expected[1],
    }


def _validate_stored_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REVIEW_FIELDS:
        raise NotebookIntegrityError("stored review fields are invalid")
    decision = value.get("decision")
    if decision not in REVIEW_DECISIONS:
        raise NotebookIntegrityError("stored review decision is invalid")
    requested = value.get("requested_changes")
    if not isinstance(requested, list) or len(requested) > 64:
        raise NotebookIntegrityError("stored requested changes are invalid")
    changes = [
        _stored_text(item, "review.requested_changes", required=True, limit=2_000)
        for item in requested
    ]
    if decision == "request_changes" and not changes:
        raise NotebookIntegrityError("stored request_changes review has no changes")
    attribution = value.get("signature_attribution")
    if attribution is not None:
        attribution = _stored_text(
            attribution, "review.signature_attribution", required=True, limit=500)
    expected_kind = "typed-attribution" if attribution else "none"
    if (value.get("signature_kind") != expected_kind
            or value.get("cryptographic_signature") is not False
            or value.get("identity_assurance") != "self-asserted-local"):
        raise NotebookIntegrityError("stored review assurance is invalid")
    return {
        "decision": decision, "requested_changes": changes,
        "signature_attribution": attribution, "signature_kind": expected_kind,
        "cryptographic_signature": False,
        "identity_assurance": "self-asserted-local",
    }


def _is_reparse(st: os.stat_result) -> bool:
    attributes = int(getattr(st, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(st.st_mode) or bool(attributes & reparse_flag)


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def _assert_secure_chain(path: Path, *, require_exists: bool = True,
                         expected: str | None = None) -> None:
    """Reject symlinks/junctions/reparse points in every existing component."""

    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    missing = False
    for part in parts:
        current = current / part
        try:
            item_stat = os.lstat(current)
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise NotebookIntegrityError("secure notebook path is unreadable") from exc
        if missing:
            raise NotebookIntegrityError("secure notebook path changed during validation")
        if _is_reparse(item_stat):
            raise NotebookIntegrityError(
                "notebook path must not contain a symlink, junction, or reparse point")
    if require_exists and missing:
        raise NotebookIntegrityError("secure notebook path is missing")
    if not missing and expected:
        final_stat = os.lstat(absolute)
        if expected == "directory" and not stat.S_ISDIR(final_stat.st_mode):
            raise NotebookIntegrityError("secure notebook path is not a directory")
        if expected == "file" and not stat.S_ISREG(final_stat.st_mode):
            raise NotebookIntegrityError("secure notebook path is not a regular file")


def _ensure_secure_directory(path: Path) -> None:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in (absolute.parts[1:] if absolute.anchor else absolute.parts):
        current = current / part
        try:
            item_stat = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass
            item_stat = os.lstat(current)
        if _is_reparse(item_stat) or not stat.S_ISDIR(item_stat.st_mode):
            raise NotebookIntegrityError(
                "notebook directory chain must contain only real directories")
    if os.path.normcase(os.path.realpath(absolute)) != os.path.normcase(str(absolute)):
        raise NotebookIntegrityError("notebook directory chain changed during validation")


def _assert_contained(root: Path, path: Path) -> None:
    root_text = os.path.normcase(str(_absolute_path(root)))
    path_text = os.path.normcase(str(_absolute_path(path)))
    try:
        contained = os.path.commonpath([root_text, path_text]) == root_text
    except ValueError:
        contained = False
    if not contained:
        raise NotebookIntegrityError("notebook path escaped its canonical project root")


def _normalize_handle_path(value: str) -> str:
    path = str(value)
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.abspath(path))


def _windows_handle_path(handle: int) -> str:  # pragma: no cover - Windows-only helper
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    final_name = kernel.GetFinalPathNameByHandleW
    final_name.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                           ctypes.c_uint, ctypes.c_uint]
    final_name.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32_768)
    length = final_name(ctypes.c_void_p(handle), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return _normalize_handle_path(buffer.value)


def _opened_file_path(descriptor: int) -> str | None:
    if os.name == "nt":
        import msvcrt

        return _windows_handle_path(msvcrt.get_osfhandle(descriptor))
    proc_path = f"/proc/self/fd/{descriptor}"
    try:
        return _normalize_handle_path(os.readlink(proc_path))
    except OSError:  # pragma: no cover - non-Linux POSIX fallback
        return None


def _directory_handle_path(path: Path) -> str:
    """Resolve an existing directory from its no-follow OS handle."""

    path = _absolute_path(path)
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p]
        create.restype = ctypes.c_void_p
        handle = create(
            str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3,
            0x02000000 | 0x00200000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return _windows_handle_path(handle)
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    descriptor = os.open(
        str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0))
    try:
        return _opened_file_path(descriptor) or _normalize_handle_path(str(path))
    finally:
        os.close(descriptor)


def _secure_open(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a regular file without following a final link and verify the inode."""

    path = _absolute_path(path)
    _assert_secure_chain(path.parent, expected="directory")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    if before is not None and (_is_reparse(before) or not stat.S_ISREG(before.st_mode)):
        raise NotebookIntegrityError("notebook file must be a non-reparse regular file")
    descriptor = os.open(
        str(path), flags | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0), mode)
    try:
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        opened_path = _opened_file_path(descriptor)
        if (_is_reparse(after) or not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(after.st_mode)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or (opened_path is not None
                    and opened_path != _normalize_handle_path(str(path)))):
            raise NotebookIntegrityError("notebook file changed during no-follow open")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_regular_bytes(path: Path, *, maximum: int | None = None) -> bytes:
    descriptor = _secure_open(path, os.O_RDONLY)
    try:
        size = os.fstat(descriptor).st_size
        if maximum is not None and size > maximum:
            raise NotebookIntegrityError("notebook file exceeds the safety limit")
        chunks = []
        remaining = size
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise NotebookIntegrityError("notebook file changed during read")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise NotebookIntegrityError("notebook file grew during read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _regular_file_size_and_sha256(path: Path, *, maximum: int) -> tuple[int, str]:
    """Hash one bounded regular file without materializing it."""

    descriptor = _secure_open(path, os.O_RDONLY)
    try:
        initial = os.fstat(descriptor)
        if initial.st_size > maximum:
            raise NotebookIntegrityError("notebook file exceeds the safety limit")
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise NotebookIntegrityError("notebook file exceeds the safety limit")
            digest.update(block)
        final = os.fstat(descriptor)
        if (total != initial.st_size or final.st_size != initial.st_size
                or (final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)):
            raise NotebookIntegrityError("notebook file changed during read")
        return total, digest.hexdigest()
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write all bytes while preserving a durable WAL for any torn suffix."""

    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("research notebook write made no progress")
        offset += written


def _iter_regular_utf8_lines(path: Path) -> Iterable[tuple[int, str]]:
    """Yield a bounded journal one line at a time, rejecting on limit + 1."""

    descriptor = _secure_open(path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as handle:
        initial = os.fstat(handle.fileno())
        if initial.st_size > MAX_JOURNAL_BYTES:
            raise NotebookIntegrityError("notebook journal exceeds the safety limit")
        total = 0
        line_number = 0
        while True:
            raw_line = handle.readline(MAX_JOURNAL_ROW_BYTES + 1)
            if not raw_line:
                break
            line_number += 1
            total += len(raw_line)
            if total > MAX_JOURNAL_BYTES:
                raise NotebookIntegrityError("notebook journal exceeds the safety limit")
            if line_number > MAX_RECORDS:
                raise NotebookIntegrityError(
                    "notebook contains too many records",
                    line_number=line_number)
            if len(raw_line) > MAX_JOURNAL_ROW_BYTES:
                raise NotebookIntegrityError(
                    "notebook journal record exceeds the safety limit",
                    line_number=line_number)
            try:
                decoded = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise NotebookIntegrityError(
                    "notebook journal is not valid UTF-8",
                    line_number=line_number) from exc
            yield line_number, decoded
        final = os.fstat(handle.fileno())
        if (total != initial.st_size or final.st_size != initial.st_size
                or (final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)):
            raise NotebookIntegrityError("notebook journal changed during replay")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, target: Path) -> None:
    """Replace a regular file and durably publish its directory entry."""

    _assert_secure_chain(source, expected="file")
    _assert_secure_chain(target.parent, expected="directory")
    try:
        target_stat = os.lstat(target)
    except FileNotFoundError:
        target_stat = None
    if target_stat is not None and (
            _is_reparse(target_stat) or not stat.S_ISREG(target_stat.st_mode)):
        raise NotebookIntegrityError("replace target is not a regular file")
    if os.name == "nt":
        import ctypes

        move = ctypes.windll.kernel32.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        # REPLACE_EXISTING | WRITE_THROUGH makes the metadata replacement
        # durable before the ledger can reference it.
        if not move(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError()
    else:
        os.replace(source, target)
        _fsync_directory(target.parent)
    _assert_secure_chain(target, expected="file")


def _canonical_project_root(project_locator: str | os.PathLike[str]) -> Path:
    locator = _absolute_path(project_locator)
    if locator.name.lower() in {"project.yaml", "project.yml"} or locator.suffix.lower() in {
            ".yaml", ".yml"}:
        if locator.exists():
            _assert_secure_chain(locator, expected="file")
        project_root = locator.parent
    else:
        project_root = locator
    _assert_secure_chain(project_root, expected="directory")
    canonical = Path(_directory_handle_path(project_root))
    if _normalize_handle_path(str(canonical)) != _normalize_handle_path(str(project_root)):
        raise NotebookIntegrityError("canonical project root traverses a link or reparse point")
    return canonical


def authoritative_job_manifest_evidence(
    project_locator: str | os.PathLike[str],
    job_dir: str | os.PathLike[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read one authoritative ``job.yaml`` through a contained no-follow handle.

    The first result is used only for opaque job-id matching.  The second is a
    complete public projection suitable for canonical evidence hashing.
    """

    project_root = _canonical_project_root(project_locator)
    member = _absolute_path(job_dir)
    _assert_contained(project_root, member)
    _assert_secure_chain(member, expected="directory")
    canonical_member = Path(_directory_handle_path(member))
    if _normalize_handle_path(str(canonical_member)) != _normalize_handle_path(str(member)):
        raise NotebookIntegrityError("job directory traverses a link or reparse point")
    manifest_path = canonical_member / "job.yaml"
    _assert_contained(project_root, manifest_path)
    raw = _read_regular_bytes(manifest_path, maximum=MAX_JOB_MANIFEST_BYTES)
    try:
        decoded = raw.decode("utf-8")
        manifest = yaml.safe_load(decoded)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise NotebookIntegrityError("authoritative job manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise NotebookIntegrityError("authoritative job manifest is missing")
    return manifest, public_job_manifest_projection(manifest)


def _default_anchor_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return _absolute_path(base) / "VASP Catalyst Studio" / "research-notebook-anchors"


@contextmanager
def _exclusive_lock(lock_path: Path, *, timeout: float = 10.0):
    _ensure_secure_directory(lock_path.parent)
    deadline = time.monotonic() + max(0.1, float(timeout))
    descriptor = _secure_open(lock_path, os.O_CREAT | os.O_RDWR)
    with _PROCESS_LOCK, os.fdopen(descriptor, "r+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("research notebook lock timed out") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised by Linux CI
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("research notebook lock timed out") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record_digest(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("record_digest", None)
    return digest_json(payload)


def _safe_attachment_name(value: Any) -> str:
    raw = _validate_text(value, "attachment.name", required=True, limit=255)
    if raw in {".", ".."} or Path(raw).name != raw or "/" in raw or "\\" in raw:
        raise NotebookError("attachment.name must not contain a path")
    return raw


def _prepare_attachment_inputs(
    attachments: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Bound and validate attachment inputs before copying any body bytes."""

    raw_items = _bounded_items(
        attachments, limit=MAX_ATTACHMENTS, error="too many attachments")
    validated: list[tuple[str, bytes | bytearray, str]] = []
    total = 0
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise NotebookError("attachment must be an object")
        unknown = sorted(set(raw) - {"name", "data", "media_type"})
        if unknown:
            raise NotebookError(
                "attachment contains unsupported fields: " + ", ".join(unknown))
        _reject_credential_structure(
            {key: value for key, value in raw.items() if key != "data"},
            "attachment",
        )
        name = _safe_attachment_name(raw.get("name"))
        data = raw.get("data")
        if not isinstance(data, (bytes, bytearray)):
            raise NotebookError("attachment.data must be bytes")
        size = len(data)
        if size <= 0 or size > MAX_ATTACHMENT_BYTES:
            raise NotebookError("attachment size is outside the allowed range")
        total += size
        if total > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise NotebookError("attachments exceed the total size limit")
        media_type = _validate_text(
            raw.get("media_type", ""), "attachment.media_type", limit=100
        ) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        validated.append((name, data, media_type))

    prepared: list[dict[str, Any]] = []
    input_digests: set[str] = set()
    for name, data, media_type in validated:
        content = data if isinstance(data, bytes) else bytes(data)
        sha256 = hashlib.sha256(content).hexdigest()
        if sha256 in input_digests:
            raise NotebookError("attachments must not contain duplicate blobs")
        input_digests.add(sha256)
        prepared.append({
            "name": name, "content": content, "sha256": sha256,
            "size": len(content), "media_type": media_type,
        })
    return prepared


def notebook_limitations() -> list[dict[str, str]]:
    return [
        {
            "code": "self_asserted_actor",
            "zh": "审阅者身份为本机明确录入的自声明归属，不是认证身份。",
            "en": "Reviewer identity is explicit local self-attribution, not authentication.",
        },
        {
            "code": "no_cryptographic_signature",
            "zh": "signature attribution 只是署名说明，不是密码学电子签名。",
            "en": "Signature attribution is a label, not a cryptographic signature.",
        },
        {
            "code": "report_gate_unchanged",
            "zh": "Notebook 审阅不会创建或提升 ValidationResult、claims 或 final 门禁。",
            "en": "Notebook review does not create or elevate ValidationResult, claims, or final gates.",
        },
        {
            "code": "attachment_bytes_local",
            "zh": "附件正文保留在项目本地；共享归档只含安全元数据与哈希。",
            "en": "Attachment bytes remain project-local; shared archives include safe metadata and hashes.",
        },
        {
            "code": "local_rollback_anchor",
            "zh": "journal head/sequence 由项目外本机锚点检测回滚；它不是远程见证或硬件证明。",
            "en": "A machine-local external head/sequence anchor detects rollback; it is not a remote witness or hardware attestation.",
        },
    ]


class ResearchNotebook:
    """One project-local append-only notebook journal."""

    def __init__(self, project_locator: str | os.PathLike[str], project_id: str,
                 *, lock_timeout: float = 10.0,
                 anchor_root: str | os.PathLike[str] | None = None,
                 project_identity_digest: str | None = None):
        self.project_id = _validate_project_id(project_id)
        self.project_root = _canonical_project_root(project_locator)
        self.root = self.project_root / ".vcstudio" / "research-notebook"
        _assert_contained(self.project_root, self.root)
        self.journal_path = self.root / JOURNAL_NAME
        self.attachments_path = self.root / ATTACHMENTS_DIR
        derived_identity = digest_json({
            "project_id": self.project_id,
            "canonical_project_root": os.path.normcase(str(self.project_root)),
        })
        if project_identity_digest is not None and (
                not isinstance(project_identity_digest, str)
                or not _SHA256_RE.fullmatch(project_identity_digest)):
            raise NotebookError("project_identity_digest must be a SHA-256 digest")
        self.project_identity_digest = project_identity_digest or derived_identity
        self.anchor_root = _absolute_path(anchor_root or _default_anchor_root())
        anchor_key = hashlib.sha256(
            self.project_identity_digest.encode("ascii")).hexdigest()
        self.anchor_path = self.anchor_root / f"{anchor_key}.json"
        self.pending_path = self.anchor_root / f"{anchor_key}.pending.json"
        self.lock_path = self.anchor_root / f"{anchor_key}{LOCK_NAME}"
        self.lock_timeout = float(lock_timeout)

    @staticmethod
    def _anchor_digest(anchor: Mapping[str, Any]) -> str:
        payload = dict(anchor)
        payload.pop("anchor_digest", None)
        return digest_json(payload)

    @staticmethod
    def _pending_digest(pending: Mapping[str, Any]) -> str:
        payload = dict(pending)
        payload.pop("pending_digest", None)
        return digest_json(payload)

    def _durable_write_external_locked(self, target: Path,
                                       value: Mapping[str, Any]) -> None:
        _ensure_secure_directory(self.anchor_root)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(self.anchor_root))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(_canonical_bytes(value) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            _durable_replace(temporary_path, target)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _read_anchor_locked(self) -> dict[str, Any] | None:
        try:
            os.lstat(self.anchor_path)
        except FileNotFoundError:
            return None
        raw = _read_regular_bytes(self.anchor_path, maximum=64 * 1024)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotebookIntegrityError("notebook head anchor is invalid") from exc
        if not isinstance(value, dict) or set(value) != _ANCHOR_FIELDS:
            raise NotebookIntegrityError("notebook head anchor fields are invalid")
        if (value.get("schema") != ANCHOR_SCHEMA
                or value.get("project_id") != self.project_id
                or value.get("project_identity_digest") != self.project_identity_digest
                or isinstance(value.get("sequence"), bool)
                or not isinstance(value.get("sequence"), int)
                or value.get("sequence") < 1
                or not isinstance(value.get("head_digest"), str)
                or not _SHA256_RE.fullmatch(value.get("head_digest"))
                or value.get("anchor_digest") != self._anchor_digest(value)):
            raise NotebookIntegrityError("notebook head anchor contract is invalid")
        return value

    def _read_pending_locked(self) -> dict[str, Any] | None:
        try:
            os.lstat(self.pending_path)
        except FileNotFoundError:
            return None
        raw = _read_regular_bytes(self.pending_path, maximum=MAX_PENDING_BYTES)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotebookIntegrityError("notebook anchor transaction is invalid") from exc
        if not isinstance(value, dict) or set(value) != _PENDING_FIELDS:
            raise NotebookIntegrityError("notebook anchor transaction fields are invalid")
        old_sequence = value.get("old_sequence")
        new_sequence = value.get("new_sequence")
        old_head = value.get("old_head_digest")
        old_anchor_digest = value.get("old_anchor_digest")
        sizes = (value.get("journal_size_before"), value.get("journal_size_after"))
        successor_payload = value.get("successor_row_payload")
        if (value.get("schema") != PENDING_SCHEMA
                or value.get("project_id") != self.project_id
                or value.get("project_identity_digest") != self.project_identity_digest
                or isinstance(old_sequence, bool) or not isinstance(old_sequence, int)
                or old_sequence < 0
                or isinstance(new_sequence, bool) or not isinstance(new_sequence, int)
                or new_sequence != old_sequence + 1
                or any(isinstance(item, bool) or not isinstance(item, int) or item < 0
                       for item in sizes)
                or sizes[1] <= sizes[0]
                or (old_sequence == 0) != (old_head is None)
                or (old_sequence == 0) != (old_anchor_digest is None)
                or (old_head is not None and (
                    not isinstance(old_head, str) or not _SHA256_RE.fullmatch(old_head)))
                or (old_anchor_digest is not None and (
                    not isinstance(old_anchor_digest, str)
                    or not _SHA256_RE.fullmatch(old_anchor_digest)))
                or not isinstance(value.get("new_head_digest"), str)
                or not _SHA256_RE.fullmatch(value.get("new_head_digest"))
                or not isinstance(value.get("journal_prefix_sha256"), str)
                or not _SHA256_RE.fullmatch(value.get("journal_prefix_sha256"))
                or not isinstance(value.get("row_payload_sha256"), str)
                or not _SHA256_RE.fullmatch(value.get("row_payload_sha256"))
                or not isinstance(successor_payload, str)
                or value.get("pending_digest") != self._pending_digest(value)):
            raise NotebookIntegrityError("notebook anchor transaction contract is invalid")
        try:
            successor_bytes = successor_payload.encode("utf-8")
            successor_row = json.loads(successor_payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise NotebookIntegrityError(
                "notebook anchor transaction successor is invalid") from exc
        if (not isinstance(successor_row, dict)
                or set(successor_row) != _RECORD_FIELDS
                or successor_row.get("schema") != NOTEBOOK_SCHEMA
                or successor_row.get("project_id") != self.project_id
                or isinstance(successor_row.get("revision"), bool)
                or successor_bytes != _canonical_bytes(successor_row) + b"\n"
                or len(successor_bytes) > MAX_JOURNAL_ROW_BYTES
                or sizes[1] != sizes[0] + len(successor_bytes)
                or hashlib.sha256(successor_bytes).hexdigest()
                != value["row_payload_sha256"]
                or successor_row.get("revision") != new_sequence
                or successor_row.get("record_digest") != value["new_head_digest"]
                or successor_row.get("previous_digest") != (old_head or "")
                or not isinstance(successor_row.get("record_id"), str)
                or not _RECORD_ID_RE.fullmatch(successor_row["record_id"])
                or not isinstance(successor_row.get("idempotency_key"), str)
                or not _TOKEN_RE.fullmatch(successor_row["idempotency_key"])
                or looks_like_credential(successor_row["idempotency_key"])
                or not isinstance(successor_row.get("request_digest"), str)
                or not _SHA256_RE.fullmatch(successor_row["request_digest"])
                or _record_digest(successor_row) != value["new_head_digest"]):
            raise NotebookIntegrityError(
                "notebook anchor transaction successor contract is invalid")
        return value

    def _clear_pending_locked(self) -> None:
        try:
            pending_stat = os.lstat(self.pending_path)
        except FileNotFoundError:
            return
        if _is_reparse(pending_stat) or not stat.S_ISREG(pending_stat.st_mode):
            raise NotebookIntegrityError("notebook anchor transaction is not regular")
        os.unlink(self.pending_path)
        _fsync_directory(self.anchor_root)

    def _write_anchor_locked(self, row: Mapping[str, Any]) -> None:
        previous = self._read_anchor_locked()
        revision = int(row["revision"])
        expected_previous = str(row["previous_digest"])
        if previous is None:
            if revision != 1 or expected_previous:
                raise NotebookIntegrityError("notebook head anchor is missing")
        elif (previous["sequence"] != revision - 1
              or previous["head_digest"] != expected_previous):
            raise NotebookIntegrityError("notebook head anchor cannot be rolled back")
        anchor = {
            "schema": ANCHOR_SCHEMA,
            "project_id": self.project_id,
            "project_identity_digest": self.project_identity_digest,
            "sequence": revision,
            "head_digest": str(row["record_digest"]),
        }
        anchor["anchor_digest"] = self._anchor_digest(anchor)
        self._durable_write_external_locked(self.anchor_path, anchor)

    @staticmethod
    def _anchor_is_old_for_pending(anchor: Mapping[str, Any] | None,
                                   pending: Mapping[str, Any]) -> bool:
        if pending["old_sequence"] == 0:
            return anchor is None
        return bool(
            anchor
            and anchor.get("sequence") == pending["old_sequence"]
            and anchor.get("head_digest") == pending["old_head_digest"]
            and anchor.get("anchor_digest") == pending["old_anchor_digest"]
        )

    @staticmethod
    def _anchor_is_new_for_pending(anchor: Mapping[str, Any] | None,
                                   pending: Mapping[str, Any]) -> bool:
        return bool(
            anchor
            and anchor.get("sequence") == pending["new_sequence"]
            and anchor.get("head_digest") == pending["new_head_digest"]
        )

    def _recover_pending_before_replay_locked(self) -> dict[str, Any] | None:
        pending = self._read_pending_locked()
        if pending is None:
            return None
        anchor = self._read_anchor_locked()
        anchor_is_old = self._anchor_is_old_for_pending(anchor, pending)
        anchor_is_new = self._anchor_is_new_for_pending(anchor, pending)
        if not anchor_is_old and not anchor_is_new:
            raise NotebookIntegrityError(
                "notebook anchor transaction does not match the durable anchor")
        before = pending["journal_size_before"]
        after = pending["journal_size_after"]
        expected_payload = pending["successor_row_payload"].encode("utf-8")
        try:
            os.lstat(self.journal_path)
        except FileNotFoundError:
            descriptor = None
            size = 0
            prefix_digest = hashlib.sha256(b"").hexdigest()
            tail = b""
        else:
            descriptor = _secure_open(self.journal_path, os.O_RDONLY)
            try:
                initial = os.fstat(descriptor)
                size = initial.st_size
                if size < before or size > after or size > MAX_JOURNAL_BYTES:
                    raise NotebookIntegrityError(
                        "notebook anchor transaction does not match the journal length")
                prefix_hash = hashlib.sha256()
                remaining = before
                while remaining:
                    block = os.read(descriptor, min(1024 * 1024, remaining))
                    if not block:
                        raise NotebookIntegrityError(
                            "notebook anchor transaction journal prefix is truncated")
                    prefix_hash.update(block)
                    remaining -= len(block)
                prefix_digest = prefix_hash.hexdigest()
                tail_chunks: list[bytes] = []
                tail_remaining = size - before
                while tail_remaining:
                    block = os.read(descriptor, min(1024 * 1024, tail_remaining))
                    if not block:
                        raise NotebookIntegrityError(
                            "notebook anchor transaction journal tail is truncated")
                    tail_chunks.append(block)
                    tail_remaining -= len(block)
                tail = b"".join(tail_chunks)
                final = os.fstat(descriptor)
                if (os.read(descriptor, 1) or final.st_size != initial.st_size
                        or (final.st_dev, final.st_ino)
                        != (initial.st_dev, initial.st_ino)):
                    raise NotebookIntegrityError(
                        "notebook anchor transaction journal changed during recovery")
            finally:
                os.close(descriptor)
        if size < before or size > after:
            raise NotebookIntegrityError(
                "notebook anchor transaction does not match the journal length")
        if prefix_digest != pending["journal_prefix_sha256"]:
            raise NotebookIntegrityError(
                "notebook anchor transaction does not match the journal prefix")
        if anchor_is_new:
            if size != after or tail != expected_payload:
                raise NotebookIntegrityError(
                    "anchored notebook journal record was truncated or replaced")
            return pending
        if tail != expected_payload[:len(tail)]:
            raise NotebookIntegrityError(
                "notebook anchor transaction row payload was replaced")
        suffix = expected_payload[len(tail):]
        if suffix:
            _ensure_secure_directory(self.root)
            created = not self.journal_path.exists()
            descriptor = _secure_open(
                self.journal_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
            try:
                _write_all(descriptor, suffix)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if created:
                _fsync_directory(self.root)
        return pending

    def _finish_pending_after_replay_locked(
        self, rows: list[dict[str, Any]], pending: Mapping[str, Any] | None,
    ) -> None:
        if pending is None:
            return
        if (len(rows) != pending["new_sequence"] or not rows
                or rows[-1]["record_digest"] != pending["new_head_digest"]
                or rows[-1]["revision"] != pending["new_sequence"]
                or (rows[-1]["previous_digest"] or None)
                != pending["old_head_digest"]):
            raise NotebookIntegrityError(
                "notebook anchor transaction is not an exact journal successor")
        anchor = self._read_anchor_locked()
        if self._anchor_is_old_for_pending(anchor, pending):
            self._write_anchor_locked(rows[-1])
        elif not self._anchor_is_new_for_pending(anchor, pending):
            raise NotebookIntegrityError(
                "notebook anchor transaction changed during recovery")
        self._clear_pending_locked()

    def _attachment_store_usage_locked(self) -> dict[str, int]:
        """Validate and count every physical content-addressed blob, including orphans."""

        try:
            os.lstat(self.attachments_path)
        except FileNotFoundError:
            return {}
        _assert_secure_chain(self.attachments_path, expected="directory")
        canonical = _directory_handle_path(self.attachments_path)
        if _normalize_handle_path(canonical) != _normalize_handle_path(
                str(self.attachments_path)):
            raise NotebookIntegrityError("attachment store traverses a reparse point")
        usage: dict[str, int] = {}
        try:
            entries = os.scandir(self.attachments_path)
        except OSError as exc:
            raise NotebookIntegrityError("attachment store is unreadable") from exc
        total_bytes = 0
        with entries:
            for entry in entries:
                if len(usage) == MAX_NOTEBOOK_UNIQUE_BLOBS:
                    raise NotebookIntegrityError(
                        "attachment store exceeds the notebook blob quota")
                name = entry.name
                digest = name[:-4] if name.endswith(".bin") else ""
                if not _SHA256_RE.fullmatch(digest):
                    raise NotebookIntegrityError(
                        "attachment store contains a non-content-addressed entry")
                target = self.attachments_path / name
                _assert_contained(self.root, target)
                try:
                    item_stat = os.lstat(target)
                except OSError as exc:
                    raise NotebookIntegrityError(
                        "attachment store entry is unreadable") from exc
                if (_is_reparse(item_stat) or not stat.S_ISREG(item_stat.st_mode)
                        or item_stat.st_size <= 0
                        or item_stat.st_size > MAX_ATTACHMENT_BYTES):
                    raise NotebookIntegrityError(
                        "attachment store entry is not a bounded regular file blob")
                total_bytes += int(item_stat.st_size)
                if total_bytes > MAX_NOTEBOOK_BLOB_BYTES:
                    raise NotebookIntegrityError(
                        "attachment store exceeds the notebook blob quota")
                usage[digest] = int(item_stat.st_size)
        return usage

    def _verify_attachment_locked(
        self,
        raw: Any,
        verification_cache: dict[tuple[str, int], bool],
        digest_sizes: dict[str, int],
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != _ATTACHMENT_FIELDS:
            raise NotebookIntegrityError("stored attachment fields are invalid")
        digest = raw.get("sha256")
        size = raw.get("size")
        attachment_id = raw.get("attachment_id")
        if (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
                or isinstance(size, bool) or not isinstance(size, int)
                or size <= 0 or size > MAX_ATTACHMENT_BYTES
                or attachment_id != f"attachment-{digest[:24]}"):
            raise NotebookIntegrityError("stored attachment contract is invalid")
        try:
            name = _safe_attachment_name(raw.get("name"))
            media_type = _validate_text(
                raw.get("media_type"), "attachment.media_type", required=True, limit=100)
        except NotebookError as exc:
            raise NotebookIntegrityError("stored attachment metadata is invalid") from exc
        known_size = digest_sizes.setdefault(digest, size)
        if known_size != size:
            raise NotebookIntegrityError("stored attachment digest has conflicting sizes")
        cache_key = (digest, size)
        if cache_key not in verification_cache:
            target = self.attachments_path / f"{digest}.bin"
            _assert_contained(self.root, target)
            try:
                content = _read_regular_bytes(target, maximum=MAX_ATTACHMENT_BYTES)
            except (OSError, NotebookIntegrityError) as exc:
                raise NotebookIntegrityError(
                    "stored attachment blob is missing or invalid") from exc
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                raise NotebookIntegrityError("stored attachment blob digest mismatch")
            verification_cache[cache_key] = True
        return {
            "attachment_id": attachment_id, "name": name, "sha256": digest,
            "size": size, "media_type": media_type,
        }

    def _read_locked(self) -> list[dict[str, Any]]:
        _assert_secure_chain(self.root, require_exists=False)
        self._attachment_store_usage_locked()
        pending = self._recover_pending_before_replay_locked()
        try:
            os.lstat(self.journal_path)
        except FileNotFoundError:
            journal_exists = False
        else:
            journal_exists = True
        rows: list[dict[str, Any]] = []
        previous_digest = ""
        active: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        attachment_verifications: dict[tuple[str, int], bool] = {}
        attachment_digest_sizes: dict[str, int] = {}
        attachment_unique_total = 0
        operation_keys: set[str] = set()
        if journal_exists:
            for line_number, raw_line in _iter_regular_utf8_lines(self.journal_path):
                if not raw_line.endswith("\n"):
                    raise NotebookIntegrityError(
                        "notebook journal has a partial final record",
                        line_number=line_number)
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise NotebookIntegrityError(
                        "notebook journal contains invalid JSON",
                        line_number=line_number) from exc
                if raw_line.encode("utf-8") != _canonical_bytes(row) + b"\n":
                    raise NotebookIntegrityError(
                        "notebook journal record is not canonical",
                        line_number=line_number)
                if not isinstance(row, dict) or set(row) != _RECORD_FIELDS:
                    raise NotebookIntegrityError(
                        "notebook journal record fields are invalid",
                        line_number=line_number)
                credential_hit = classify_credential_structure(row)
                if credential_hit:
                    raise NotebookIntegrityError(
                        "notebook journal contains credential-like material",
                        line_number=line_number)
                expected_revision = len(rows) + 1
                checks = {
                    "schema": NOTEBOOK_SCHEMA, "project_id": self.project_id,
                    "revision": expected_revision, "previous_digest": previous_digest,
                }
                for key, expected in checks.items():
                    if row.get(key) != expected or (
                            key == "revision" and isinstance(row.get(key), bool)):
                        raise NotebookIntegrityError(
                            f"notebook journal {key} binding mismatch",
                            line_number=line_number)
                record_id = row.get("record_id")
                if (not isinstance(record_id, str)
                        or not _RECORD_ID_RE.fullmatch(record_id) or record_id in seen):
                    raise NotebookIntegrityError(
                        "notebook record_id is invalid or duplicated",
                        line_number=line_number)
                seen.add(record_id)
                idempotency_key = row.get("idempotency_key")
                request_digest = row.get("request_digest")
                if (not isinstance(idempotency_key, str)
                        or not _TOKEN_RE.fullmatch(idempotency_key)
                        or looks_like_credential(idempotency_key)
                        or idempotency_key in operation_keys
                        or not isinstance(request_digest, str)
                        or not _SHA256_RE.fullmatch(request_digest)):
                    raise NotebookIntegrityError(
                        "notebook operation receipt is invalid or duplicated",
                        line_number=line_number)
                operation_keys.add(idempotency_key)
                record_type = row.get("record_type")
                category = row.get("category")
                if record_type not in RECORD_TYPES or not isinstance(category, str):
                    raise NotebookIntegrityError(
                        "notebook record type/category is invalid",
                        line_number=line_number)
                _stored_text(row.get("body"), "body", required=True,
                             limit=MAX_BODY_CHARS)
                timestamp = row.get("created_at_utc")
                try:
                    parsed = datetime.fromisoformat(timestamp)
                except (TypeError, ValueError) as exc:
                    raise NotebookIntegrityError(
                        "notebook record timestamp is invalid",
                        line_number=line_number) from exc
                if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                    raise NotebookIntegrityError(
                        "notebook record timestamp must be UTC",
                        line_number=line_number)
                human_required = record_type in {"review", "tombstone"}
                _validate_stored_actor(row.get("actor"), human_required=human_required)
                links = _validate_bound_links(
                    row.get("links"), project_id=self.project_id)
                attachments_value = row.get("attachments")
                if not isinstance(attachments_value, list) \
                        or len(attachments_value) > MAX_ATTACHMENTS:
                    raise NotebookIntegrityError("stored attachments are invalid")
                attachment_ids: set[str] = set()
                attachment_total = 0
                for attachment in attachments_value:
                    unique_before = len(attachment_digest_sizes)
                    verified = self._verify_attachment_locked(
                        attachment, attachment_verifications, attachment_digest_sizes)
                    if verified["attachment_id"] in attachment_ids:
                        raise NotebookIntegrityError("stored attachments contain duplicates")
                    attachment_ids.add(verified["attachment_id"])
                    attachment_total += verified["size"]
                    if len(attachment_digest_sizes) != unique_before:
                        attachment_unique_total += verified["size"]
                    if (len(attachment_digest_sizes) > MAX_NOTEBOOK_UNIQUE_BLOBS
                            or attachment_unique_total > MAX_NOTEBOOK_BLOB_BYTES):
                        raise NotebookIntegrityError(
                            "stored attachments exceed the notebook blob quota")
                if attachment_total > MAX_ATTACHMENTS_TOTAL_BYTES:
                    raise NotebookIntegrityError(
                        "stored attachments exceed the total safety limit")
                supersedes = row.get("supersedes")
                tombstones = row.get("tombstones")
                if supersedes is not None and (
                        not isinstance(supersedes, str)
                        or not _TOKEN_RE.fullmatch(supersedes)):
                    raise NotebookIntegrityError("stored supersedes target is invalid")
                if tombstones is not None and (
                        not isinstance(tombstones, str)
                        or not _TOKEN_RE.fullmatch(tombstones)):
                    raise NotebookIntegrityError("stored tombstone target is invalid")
                if record_type == "tombstone":
                    if (category != "tombstone" or supersedes is not None
                            or not tombstones or row.get("review") is not None
                            or links or attachments_value or tombstones not in active):
                        raise NotebookIntegrityError(
                            "tombstone state transition is invalid",
                            line_number=line_number)
                    active.pop(tombstones)
                else:
                    expected_category = (
                        category in NOTE_CATEGORIES if record_type == "note"
                        else category == record_type)
                    if not expected_category or tombstones is not None:
                        raise NotebookIntegrityError(
                            "record category/state is invalid", line_number=line_number)
                    if record_type == "review":
                        _validate_stored_review(row.get("review"))
                    elif row.get("review") is not None:
                        raise NotebookIntegrityError(
                            "non-review record contains review fields",
                            line_number=line_number)
                    if supersedes is not None:
                        target = active.get(supersedes)
                        if target is None or target.get("record_type") != record_type:
                            raise NotebookIntegrityError(
                                "supersedes state transition is invalid",
                                line_number=line_number)
                        active.pop(supersedes)
                    active[record_id] = row
                actual_digest = row.get("record_digest")
                if (not isinstance(actual_digest, str)
                        or not _SHA256_RE.fullmatch(actual_digest)
                        or actual_digest != _record_digest(row)):
                    raise NotebookIntegrityError(
                        "notebook record digest mismatch", line_number=line_number)
                previous_digest = actual_digest
                rows.append(row)
        self._finish_pending_after_replay_locked(rows, pending)
        anchor = self._read_anchor_locked()
        if rows:
            if (anchor is None or anchor["sequence"] != len(rows)
                    or anchor["head_digest"] != previous_digest):
                raise NotebookIntegrityError(
                    "notebook journal is truncated, deleted, or rolled back")
        elif anchor is not None:
            raise NotebookIntegrityError(
                "notebook journal is missing behind its durable head anchor")
        return rows

    def _append_locked(self, row: dict[str, Any]) -> None:
        _ensure_secure_directory(self.root)
        _assert_contained(self.project_root, self.journal_path)
        payload = _canonical_bytes(row) + b"\n"
        try:
            os.lstat(self.journal_path)
        except FileNotFoundError:
            created = True
            journal_size = 0
            journal_sha256 = hashlib.sha256(b"").hexdigest()
        else:
            created = False
            journal_size, journal_sha256 = _regular_file_size_and_sha256(
                self.journal_path, maximum=MAX_JOURNAL_BYTES)
        if len(payload) > MAX_JOURNAL_ROW_BYTES:
            raise NotebookError("notebook record exceeds the journal row limit")
        if journal_size + len(payload) > MAX_JOURNAL_BYTES:
            raise NotebookError("notebook journal exceeds the safety limit")
        if self._read_pending_locked() is not None:
            raise NotebookIntegrityError("notebook anchor transaction is already pending")
        previous_anchor = self._read_anchor_locked()
        revision = int(row["revision"])
        expected_previous = str(row["previous_digest"])
        if previous_anchor is None:
            if revision != 1 or expected_previous:
                raise NotebookIntegrityError("notebook head anchor is missing")
            old_sequence = 0
            old_head = None
            old_anchor_digest = None
        else:
            if (previous_anchor["sequence"] != revision - 1
                    or previous_anchor["head_digest"] != expected_previous):
                raise NotebookIntegrityError("notebook head anchor cannot be rolled back")
            old_sequence = previous_anchor["sequence"]
            old_head = previous_anchor["head_digest"]
            old_anchor_digest = previous_anchor["anchor_digest"]
        pending = {
            "schema": PENDING_SCHEMA,
            "project_id": self.project_id,
            "project_identity_digest": self.project_identity_digest,
            "old_sequence": old_sequence,
            "old_head_digest": old_head,
            "old_anchor_digest": old_anchor_digest,
            "new_sequence": revision,
            "new_head_digest": str(row["record_digest"]),
            "journal_size_before": journal_size,
            "journal_size_after": journal_size + len(payload),
            "journal_prefix_sha256": journal_sha256,
            "row_payload_sha256": hashlib.sha256(payload).hexdigest(),
            "successor_row_payload": payload.decode("utf-8"),
        }
        pending["pending_digest"] = self._pending_digest(pending)
        self._durable_write_external_locked(self.pending_path, pending)
        descriptor = _secure_open(
            self.journal_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            _fsync_directory(self.root)
        self._write_anchor_locked(row)
        self._clear_pending_locked()

    def _store_attachments_locked(
        self,
        prepared: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        unique_sizes = self._attachment_store_usage_locked()
        for item in prepared:
            existing_size = unique_sizes.setdefault(
                item["sha256"], len(item["content"]))
            if existing_size != len(item["content"]):
                raise NotebookIntegrityError(
                    "content-addressed attachment size mismatch")
        if (len(unique_sizes) > MAX_NOTEBOOK_UNIQUE_BLOBS
                or sum(unique_sizes.values()) > MAX_NOTEBOOK_BLOB_BYTES):
            raise NotebookError("attachments exceed the notebook blob quota")

        normalized = []
        for item in prepared:
            name = item["name"]
            content = item["content"]
            sha256 = item["sha256"]
            _ensure_secure_directory(self.attachments_path)
            target = self.attachments_path / f"{sha256}.bin"
            _assert_contained(self.root, target)
            try:
                os.lstat(target)
            except FileNotFoundError:
                existing = None
            else:
                existing = _read_regular_bytes(target, maximum=MAX_ATTACHMENT_BYTES)
            if existing is not None:
                if (len(existing) != len(content)
                        or hashlib.sha256(existing).hexdigest() != sha256):
                    raise NotebookIntegrityError(
                        "content-addressed attachment digest mismatch")
            else:
                descriptor, temporary = tempfile.mkstemp(
                    prefix=f".{sha256}.", suffix=".tmp", dir=str(self.attachments_path)
                )
                temporary_path = Path(temporary)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        descriptor = -1
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    _durable_replace(temporary_path, target)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
            stored = _read_regular_bytes(target, maximum=MAX_ATTACHMENT_BYTES)
            if len(stored) != len(content) or hashlib.sha256(stored).hexdigest() != sha256:
                raise NotebookIntegrityError("stored attachment failed post-replace verification")
            normalized.append({
                "attachment_id": f"attachment-{sha256[:24]}",
                "name": name,
                "sha256": sha256,
                "size": len(content),
                "media_type": item["media_type"],
            })
        return normalized

    @staticmethod
    def _active_ids(rows: list[dict[str, Any]]) -> set[str]:
        active: set[str] = set()
        for row in rows:
            if row["record_type"] == "tombstone":
                active.remove(row["tombstones"])
                continue
            if row["supersedes"] is not None:
                active.remove(row["supersedes"])
            active.add(row["record_id"])
        return active

    @staticmethod
    def _validate_cas_inputs(*, expected_revision: int,
                             expected_head_digest: str | None,
                             expected_project_identity: str) -> None:
        if (isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int) or expected_revision < 0):
            raise NotebookError("expected_revision must be a non-negative integer")
        if (not isinstance(expected_project_identity, str)
                or not _SHA256_RE.fullmatch(expected_project_identity)):
            raise NotebookError("expected_project_identity must be a SHA-256 digest")
        if expected_head_digest is not None and (
                not isinstance(expected_head_digest, str)
                or not _SHA256_RE.fullmatch(expected_head_digest)):
            raise NotebookError("expected_head_digest must be null or a SHA-256 digest")

    def _assert_cas(self, rows: list[dict[str, Any]], *, expected_revision: int,
                    expected_head_digest: str | None,
                    expected_project_identity: str) -> None:
        self._validate_cas_inputs(
            expected_revision=expected_revision,
            expected_head_digest=expected_head_digest,
            expected_project_identity=expected_project_identity)
        current_head = str(rows[-1]["record_digest"]) if rows else None
        if (len(rows) != expected_revision or current_head != expected_head_digest
                or self.project_identity_digest != expected_project_identity):
            raise NotebookRevisionConflict(len(rows), current_head)

    def _operation_replay(
        self, rows: list[dict[str, Any]], *, idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        for row in rows:
            if row["idempotency_key"] != idempotency_key:
                continue
            if row["request_digest"] != request_digest:
                raise NotebookError(
                    "idempotency key was already used for a different request")
            public = self.public_record(row, resolver=None, active=(
                str(row["record_id"]) in self._active_ids(rows)))
            public["operation_receipt"]["replayed"] = True
            return public
        return None

    def append(self, *, record_type: str, category: str, body: str,
               actor: Mapping[str, Any], expected_revision: int,
               expected_head_digest: str | None,
               expected_project_identity: str,
               links: list[dict[str, Any]] | None = None,
               supersedes: str | None = None,
               review: Mapping[str, Any] | None = None,
               ai_proposal: bool = False,
               attachments: Iterable[Mapping[str, Any]] = (),
               idempotency_key: str | None = None) -> dict[str, Any]:
        _reject_credential_structure({
            "record_type": record_type,
            "category": category,
            "body": body,
            "actor": actor,
            "links": links,
            "review": review,
            "supersedes": supersedes,
            "idempotency_key": idempotency_key,
        }, "record")
        if record_type not in {"note", "decision", "review"}:
            raise NotebookError("record_type must be note, decision, or review")
        if record_type == "note" and category not in NOTE_CATEGORIES:
            raise NotebookError("note category is unsupported")
        if record_type == "decision" and category != "decision":
            raise NotebookError("decision records require category=decision")
        if record_type == "review" and category != "review":
            raise NotebookError("review records require category=review")
        if record_type == "review" and ai_proposal:
            raise NotebookError("AI proposals cannot be recorded as human review")
        body_text = _validate_text(body, "body", required=True, limit=MAX_BODY_CHARS)
        actor_value = _actor(
            actor, human_review=record_type == "review", ai_proposal=ai_proposal
        )
        review_value = _review(review) if record_type == "review" else None
        if record_type != "review" and review is not None:
            raise NotebookError("review payload is only valid for review records")
        supersedes_id = None if supersedes is None else _validate_token(
            supersedes, "supersedes"
        )
        operation_key = _validate_token(
            idempotency_key or f"notebook-operation.{uuid.uuid4().hex}",
            "idempotency_key")
        try:
            bound_links = _validate_bound_links(
                _bounded_items(
                    links, limit=MAX_LINKS,
                    error="links contains too many entries"),
                project_id=self.project_id)
        except NotebookIntegrityError as exc:
            raise NotebookError(str(exc)) from exc
        prepared_attachments = _prepare_attachment_inputs(attachments)
        request_digest = digest_json({
            "schema": "vcstudio.research-notebook-operation-request/v1",
            "action": "append",
            "project_id": self.project_id,
            "project_identity_digest": self.project_identity_digest,
            "record_type": record_type,
            "category": category,
            "body": body_text,
            "actor": actor_value,
            "review": review_value,
            # The user request contains opaque link identities.  The resolved
            # evidence digest may advance after a lost response and must not
            # turn an otherwise identical retry into a different operation.
            "links": [{
                "kind": item["kind"], "id": item["id"],
                "report_revision_id": item["report_revision_id"],
            } for item in bound_links],
            "supersedes": supersedes_id,
            "attachments": [{
                "name": item["name"], "sha256": item["sha256"],
                "size": item["size"], "media_type": item["media_type"],
            } for item in prepared_attachments],
        })

        with _exclusive_lock(self.lock_path, timeout=self.lock_timeout):
            rows = self._read_locked()
            current_revision = len(rows)
            self._validate_cas_inputs(
                expected_revision=expected_revision,
                expected_head_digest=expected_head_digest,
                expected_project_identity=expected_project_identity)
            current_head = str(rows[-1]["record_digest"]) if rows else None
            if expected_project_identity != self.project_identity_digest:
                raise NotebookRevisionConflict(current_revision, current_head)
            replay = self._operation_replay(
                rows, idempotency_key=operation_key,
                request_digest=request_digest)
            if replay is not None:
                return replay
            self._assert_cas(
                rows, expected_revision=expected_revision,
                expected_head_digest=expected_head_digest,
                expected_project_identity=expected_project_identity)
            if current_revision >= MAX_RECORDS:
                raise NotebookError("notebook contains too many records")
            if supersedes_id:
                by_id = {str(item["record_id"]): item for item in rows}
                target = by_id.get(supersedes_id)
                if target is None or supersedes_id not in self._active_ids(rows):
                    raise NotebookError("supersedes must reference an active record")
                if target.get("record_type") != record_type:
                    raise NotebookError("supersedes must preserve record_type")
            attachment_rows = self._store_attachments_locked(prepared_attachments)
            previous_digest = str(rows[-1]["record_digest"]) if rows else ""
            row = {
                "schema": NOTEBOOK_SCHEMA,
                "revision": current_revision + 1,
                "record_id": f"rn-{uuid.uuid4().hex}",
                "record_type": record_type,
                "category": category,
                "project_id": self.project_id,
                "created_at_utc": _utc_now(),
                "body": body_text,
                "actor": actor_value,
                "review": review_value,
                "links": bound_links,
                "attachments": attachment_rows,
                "supersedes": supersedes_id,
                "tombstones": None,
                "previous_digest": previous_digest,
                "idempotency_key": operation_key,
                "request_digest": request_digest,
            }
            row["record_digest"] = _record_digest(row)
            self._append_locked(row)
            return self.public_record(row, resolver=None, active=True)

    def tombstone(self, *, record_id: str, reason: str, actor: Mapping[str, Any],
                  expected_revision: int, expected_head_digest: str | None,
                  expected_project_identity: str,
                  idempotency_key: str | None = None) -> dict[str, Any]:
        _reject_credential_structure({
            "record_id": record_id, "reason": reason, "actor": actor,
            "idempotency_key": idempotency_key,
        }, "tombstone")
        target_id = _validate_token(record_id, "record_id")
        reason_text = _validate_text(reason, "reason", required=True, limit=2_000)
        actor_value = _actor(actor)
        operation_key = _validate_token(
            idempotency_key or f"notebook-operation.{uuid.uuid4().hex}",
            "idempotency_key")
        request_digest = digest_json({
            "schema": "vcstudio.research-notebook-operation-request/v1",
            "action": "tombstone",
            "project_id": self.project_id,
            "project_identity_digest": self.project_identity_digest,
            "record_id": target_id,
            "reason": reason_text,
            "actor": actor_value,
        })
        with _exclusive_lock(self.lock_path, timeout=self.lock_timeout):
            rows = self._read_locked()
            self._validate_cas_inputs(
                expected_revision=expected_revision,
                expected_head_digest=expected_head_digest,
                expected_project_identity=expected_project_identity)
            current_head = str(rows[-1]["record_digest"]) if rows else None
            if expected_project_identity != self.project_identity_digest:
                raise NotebookRevisionConflict(len(rows), current_head)
            replay = self._operation_replay(
                rows, idempotency_key=operation_key,
                request_digest=request_digest)
            if replay is not None:
                return replay
            self._assert_cas(
                rows, expected_revision=expected_revision,
                expected_head_digest=expected_head_digest,
                expected_project_identity=expected_project_identity)
            if len(rows) >= MAX_RECORDS:
                raise NotebookError("notebook contains too many records")
            if target_id not in self._active_ids(rows):
                raise NotebookError("tombstone must reference an active record")
            previous_digest = str(rows[-1]["record_digest"]) if rows else ""
            row = {
                "schema": NOTEBOOK_SCHEMA,
                "revision": len(rows) + 1,
                "record_id": f"rn-{uuid.uuid4().hex}",
                "record_type": "tombstone",
                "category": "tombstone",
                "project_id": self.project_id,
                "created_at_utc": _utc_now(),
                "body": reason_text,
                "actor": actor_value,
                "review": None,
                "links": [],
                "attachments": [],
                "supersedes": None,
                "tombstones": target_id,
                "previous_digest": previous_digest,
                "idempotency_key": operation_key,
                "request_digest": request_digest,
            }
            row["record_digest"] = _record_digest(row)
            self._append_locked(row)
            return self.public_record(row, resolver=None, active=False)

    @staticmethod
    def _link_public(link: Mapping[str, Any], resolver: Callable[[Mapping[str, Any]],
                                                                 Mapping[str, Any]] | None) \
            -> dict[str, Any]:
        public = {
            "kind": str(link.get("kind") or ""),
            "id": str(link.get("id") or ""),
            "report_revision_id": link.get("report_revision_id"),
            "bound_digest": str(link.get("bound_digest") or ""),
            "status": "unknown",
            "current_digest": None,
            "route": None,
        }
        if resolver is None:
            return public
        try:
            current = resolver(link)
        except Exception:  # noqa: BLE001 fail-closed public projection
            current = {"status": "missing"}
        if not isinstance(current, Mapping):
            public["status"] = "missing"
            return public
        if current.get("status") not in {"current", "stale"}:
            public["status"] = "missing"
            return public
        digest = str(current.get("digest") or "")
        public["current_digest"] = digest if _SHA256_RE.fullmatch(digest) else None
        if current.get("status") == "stale":
            public["status"] = "stale"
            return public
        public["status"] = (
            "current" if digest == public["bound_digest"] else "stale"
        )
        route = current.get("route")
        if isinstance(route, Mapping):
            public["route"] = {
                str(key): redact_public_text(value)
                for key, value in route.items()
                if key in {"id", "project_id", "revision_id", "job_id", "source_id"}
            }
        return public

    @classmethod
    def public_record(cls, row: Mapping[str, Any], *,
                      resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
                      active: bool) -> dict[str, Any]:
        actor = row.get("actor") if isinstance(row.get("actor"), Mapping) else {}
        review = row.get("review") if isinstance(row.get("review"), Mapping) else None
        operation_receipt = None
        if (isinstance(row.get("idempotency_key"), str)
                and isinstance(row.get("request_digest"), str)):
            operation_receipt = {
                "idempotency_key": row["idempotency_key"],
                "request_digest": row["request_digest"],
                "revision": int(row.get("revision") or 0),
                "record_id": str(row.get("record_id") or ""),
                "record_digest": str(row.get("record_digest") or ""),
                "replayed": False,
            }
        public_review = None
        if review is not None:
            public_review = {
                "decision": review.get("decision"),
                "requested_changes": [
                    redact_public_text(item) for item in review.get("requested_changes") or []
                ],
                "signature_attribution": redact_public_text(
                    review.get("signature_attribution") or ""
                ) or None,
                "signature_kind": review.get("signature_kind"),
                "cryptographic_signature": False,
                "identity_assurance": "self-asserted-local",
            }
        attachments = []
        for raw in row.get("attachments") or []:
            if not isinstance(raw, Mapping):
                continue
            attachments.append({
                "attachment_id": str(raw.get("attachment_id") or ""),
                "name": Path(str(raw.get("name") or "attachment")).name,
                "sha256": str(raw.get("sha256") or ""),
                "size": int(raw.get("size") or 0),
                "media_type": str(raw.get("media_type") or "application/octet-stream"),
            })
        public = {
            "record_id": str(row.get("record_id") or ""),
            "revision": int(row.get("revision") or 0),
            "record_type": str(row.get("record_type") or ""),
            "category": str(row.get("category") or ""),
            "project_id": str(row.get("project_id") or ""),
            "created_at_utc": str(row.get("created_at_utc") or ""),
            "body": redact_public_text(row.get("body") or ""),
            "actor": {
                "id": str(actor.get("id") or ""),
                "display_name": redact_public_text(actor.get("display_name") or ""),
                "role": redact_public_text(actor.get("role") or ""),
                "actor_type": str(actor.get("actor_type") or ""),
                "entry_method": str(actor.get("entry_method") or ""),
                "identity_assurance": str(actor.get("identity_assurance") or ""),
            },
            "review": public_review,
            "links": [cls._link_public(link, resolver) for link in row.get("links") or []],
            "attachments": attachments,
            "supersedes": row.get("supersedes"),
            "tombstones": row.get("tombstones"),
            "previous_digest": str(row.get("previous_digest") or ""),
            "record_digest": str(row.get("record_digest") or ""),
            "operation_receipt": operation_receipt,
            "active": bool(active),
        }
        return _redact_public_structure(public)

    def read(self, *, resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) \
            -> dict[str, Any]:
        try:
            with _exclusive_lock(self.lock_path, timeout=self.lock_timeout):
                rows = self._read_locked()
        except NotebookIntegrityError as exc:
            failure = {
                "schema": PUBLIC_SCHEMA,
                "ok": False,
                "project_id": self.project_id,
                "project_identity_digest": self.project_identity_digest,
                "revision": 0,
                "head_digest": None,
                "integrity_status": "tampered",
                "integrity_error": "notebook_integrity_error",
                "integrity_line": exc.line_number,
                "records": [],
                "active_records": [],
                "review_todo": [],
                "denominator": {"records": 0, "active": 0, "review_todo": 0},
                "limitations": notebook_limitations(),
            }
            return _redact_public_structure(failure)
        active_ids = self._active_ids(rows)
        records = [
            self.public_record(
                row, resolver=resolver, active=str(row["record_id"]) in active_ids
            )
            for row in rows
        ]
        active = [row for row in records if row["active"]]
        review_todo = [
            row for row in active
            if row["record_type"] == "review"
            and (row.get("review") or {}).get("decision") in {
                "request_changes", "rejected", "comment"
            }
        ]
        public = {
            "schema": PUBLIC_SCHEMA,
            "ok": True,
            "project_id": self.project_id,
            "project_identity_digest": self.project_identity_digest,
            "revision": len(rows),
            "head_digest": rows[-1]["record_digest"] if rows else None,
            "integrity_status": "current",
            "integrity_error": None,
            "integrity_line": None,
            "records": records,
            "active_records": active,
            "review_todo": review_todo,
            "denominator": {
                "records": len(records),
                "active": len(active),
                "review_todo": len(review_todo),
            },
            "limitations": notebook_limitations(),
        }
        return _redact_public_structure(public)

    def archive_payload(self, *, report_revision_id: str | None = None,
                        resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) \
            -> dict[str, Any]:
        bound_revision = None if report_revision_id is None else _validate_token(
            report_revision_id, "report_revision_id")
        snapshot = self.read(resolver=resolver)
        payload = {
            "schema": ARCHIVE_SCHEMA,
            "project_id": self.project_id,
            "project_identity_digest": self.project_identity_digest,
            "bound_report_revision_id": bound_revision,
            "ledger_revision": snapshot["revision"],
            "ledger_head_digest": snapshot["head_digest"],
            "integrity_status": snapshot["integrity_status"],
            "records": snapshot["records"],
            "denominator": snapshot["denominator"],
            "limitations": notebook_limitations(),
        }
        return _redact_public_structure(payload)


class AttachmentSelections:
    """Bounded selections, optionally replayable by one bound operation key."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = 15 * 60, max_items: int = 64):
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._max_items = max(1, int(max_items))
        self._lock = threading.RLock()
        self._items: dict[
            str, tuple[float, str, str, list[dict[str, Any]], str | None]
        ] = {}

    def _prune(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if now - value[0] > self._ttl]
        for key in expired:
            self._items.pop(key, None)
        while len(self._items) > self._max_items:
            oldest = min(self._items, key=lambda key: self._items[key][0])
            self._items.pop(oldest, None)

    def register(self, paths: Iterable[str | os.PathLike[str]], *, project_id: str,
                 project_identity_digest: str) \
            -> dict[str, Any]:
        identifier = _validate_project_id(project_id)
        if (not isinstance(project_identity_digest, str)
                or not _SHA256_RE.fullmatch(project_identity_digest)):
            raise NotebookError("attachment selection project identity is invalid")
        raw_paths = _bounded_items(
            paths, limit=MAX_ATTACHMENTS,
            error="too many attachments were selected")
        selected = []
        total_size = 0
        for raw in raw_paths:
            path = _absolute_path(raw)
            try:
                data = _read_regular_bytes(path, maximum=MAX_ATTACHMENT_BYTES)
            except (OSError, NotebookIntegrityError) as exc:
                raise NotebookError(
                    "selected attachment must be a non-reparse regular file") from exc
            size = len(data)
            if size <= 0 or size > MAX_ATTACHMENT_BYTES:
                raise NotebookError("selected attachment size is outside the allowed range")
            total_size += size
            if total_size > MAX_ATTACHMENTS_TOTAL_BYTES:
                raise NotebookError("selected attachments exceed the total size limit")
            sha256 = hashlib.sha256(data).hexdigest()
            selected.append({
                "path": str(path),
                "name": _safe_attachment_name(path.name),
                "size": size,
                "sha256": sha256,
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            })
        if not selected:
            raise NotebookError("no attachments were selected")
        token = "notebook-attachment." + uuid.uuid4().hex
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            while len(self._items) >= self._max_items:
                oldest = min(self._items, key=lambda key: self._items[key][0])
                self._items.pop(oldest, None)
            self._items[token] = (
                now, identifier, project_identity_digest, selected, None)
        return {
            "selection_token": token,
            "files": [
                {key: value for key, value in item.items() if key != "path"}
                for item in selected
            ],
        }

    def consume(self, token: str, *, project_id: str,
                project_identity_digest: str,
                idempotency_key: str | None = None) -> list[dict[str, Any]]:
        identifier = _validate_project_id(project_id)
        if (not isinstance(project_identity_digest, str)
                or not _SHA256_RE.fullmatch(project_identity_digest)):
            raise NotebookError("attachment selection project identity is invalid")
        operation_key = None if idempotency_key is None else _validate_token(
            idempotency_key, "idempotency_key")
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            token_key = str(token or "")
            selected = self._items.get(token_key)
            if selected is not None:
                if selected[1] != identifier:
                    raise NotebookError("attachment selection project binding mismatch")
                if selected[2] != project_identity_digest:
                    raise NotebookError(
                        "attachment selection project identity mismatch")
                if operation_key is None:
                    self._items.pop(token_key, None)
                elif selected[4] not in {None, operation_key}:
                    raise NotebookError(
                        "attachment selection is bound to a different operation")
                elif selected[4] is None:
                    selected = (*selected[:4], operation_key)
                    self._items[token_key] = selected
        if selected is None:
            raise NotebookError("attachment selection token is invalid or expired")
        attachments = []
        for metadata in selected[3]:
            path = Path(metadata["path"])
            try:
                data = _read_regular_bytes(path, maximum=MAX_ATTACHMENT_BYTES)
            except (OSError, NotebookIntegrityError) as exc:
                raise NotebookError("selected attachment is no longer available") from exc
            if (len(data) != metadata["size"]
                    or hashlib.sha256(data).hexdigest() != metadata["sha256"]):
                raise NotebookError("selected attachment changed after selection")
            attachments.append({
                "name": metadata["name"],
                "data": data,
                "media_type": metadata["media_type"],
            })
        return attachments


__all__ = [
    "ARCHIVE_SCHEMA",
    "ANCHOR_SCHEMA",
    "AttachmentSelections",
    "LINK_KINDS",
    "NOTEBOOK_SCHEMA",
    "NOTE_CATEGORIES",
    "NotebookError",
    "NotebookIntegrityError",
    "NotebookRevisionConflict",
    "PUBLIC_SCHEMA",
    "REVIEW_DECISIONS",
    "ResearchNotebook",
    "bind_links",
    "digest_json",
    "notebook_limitations",
    "redact_public_text",
]
