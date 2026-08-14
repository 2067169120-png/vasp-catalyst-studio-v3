"""Project-local Research Notebook and Human Review Ledger.

The notebook is intentionally independent from report qualification.  It stores
plain research notes, decisions, and self-attributed local human review records,
but it never creates or upgrades :class:`ValidationResult` and it never grants a
``human_scientific_reviewed`` qualification.

Records are an append-only hash chain.  A stable project-scoped OS lock protects
revision compare-and-swap across processes, and each JSONL append is one
``O_APPEND`` write followed by ``fsync``.  Edits append a ``supersedes`` record;
deletions append a tombstone.  Attachment bytes remain on the project filesystem
and browser-facing values contain metadata only.
"""
from __future__ import annotations

import errno
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


NOTEBOOK_SCHEMA = "vcstudio.research-notebook/v1"
PUBLIC_SCHEMA = "vcstudio.research-notebook-public/v1"
ARCHIVE_SCHEMA = "vcstudio.research-notebook-archive/v1"
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

_PROJECT_ID_RE = re.compile(r"(?:project-[a-f0-9]{32}|registry-[a-f0-9]{24})")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ABS_WINDOWS_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:[a-z]:[\\/]|\\\\)[^\s,;，；]+")
_FILE_URI_RE = re.compile(r"(?i)\bfile:(?://+|\\+)[^\s,;，；]+")
_REMOTE_URL_RE = re.compile(r"(?i)\b(?:https?|s3)://[^\s,;，；]+")
_ABS_POSIX_RE = re.compile(
    r"(?<![:A-Za-z0-9])/(?!/)(?:[^/\s,;，；]+/)+[^\s,;，；]+"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+\S+"
    r"|\b(?:password|passwd|secret|token|api[_-]?key|authorization)"
    r"\s*[:=]\s*[\"']?[^\s,\"'}]+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|https?://[^\s/:]+:[^\s/@]+@)"
)
_RESERVED_ACTOR_FIELDS = frozenset({
    "actor_type", "reviewer_type", "made_by", "identity_assurance",
    "entry_method", "cryptographic_signature", "signature_verified",
})

_PROCESS_LOCK = threading.RLock()


class NotebookError(ValueError):
    """Base class for notebook contract failures."""


class NotebookRevisionConflict(NotebookError):
    """Optimistic revision compare-and-swap failed."""

    def __init__(self, current_revision: int):
        super().__init__("research notebook revision conflict")
        self.current_revision = int(current_revision)


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
    if _SECRET_VALUE_RE.search(text):
        raise NotebookError(f"{field} contains credential-like material")
    return text


def _validate_token(value: Any, field: str) -> str:
    text = _validate_text(value, field, required=True, limit=160)
    if not _TOKEN_RE.fullmatch(text):
        raise NotebookError(f"{field} must be an opaque identifier")
    return text


def _validate_project_id(value: Any) -> str:
    text = _validate_text(value, "project_id", required=True, limit=64).lower()
    if not _PROJECT_ID_RE.fullmatch(text):
        raise NotebookError("project_id must be a registered opaque identity")
    return text


def redact_public_text(value: Any) -> str:
    """Redact local paths and credential-like values while retaining prose."""

    text = str(value or "")
    remote_urls: list[str] = []

    def preserve_remote_url(match: re.Match[str]) -> str:
        remote_urls.append(match.group(0))
        return f"<research-notebook-remote-url-{len(remote_urls) - 1}>"

    text = _REMOTE_URL_RE.sub(preserve_remote_url, text)
    text = _SECRET_VALUE_RE.sub("[redacted-secret]", text)
    text = _FILE_URI_RE.sub("<local-path>", text)
    text = _ABS_WINDOWS_RE.sub("<local-path>", text)
    text = _ABS_POSIX_RE.sub("<local-path>", text)
    for index, url in enumerate(remote_urls):
        text = text.replace(f"<research-notebook-remote-url-{index}>", url)
    return text


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
    links = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise NotebookIntegrityError("record link is not an object")
        kind = str(raw.get("kind") or "")
        identifier = str(raw.get("id") or "")
        revision = raw.get("report_revision_id")
        digest = str(raw.get("bound_digest") or "")
        if (kind not in LINK_KINDS or not _TOKEN_RE.fullmatch(identifier)
                or (revision is not None and not _TOKEN_RE.fullmatch(str(revision)))
                or not _SHA256_RE.fullmatch(digest)):
            raise NotebookIntegrityError("record link contract is invalid")
        if (kind == "source") != (revision is not None):
            raise NotebookIntegrityError("record source revision binding is invalid")
        if kind == "project" and project_id is not None and identifier != project_id:
            raise NotebookIntegrityError("record project link binding is invalid")
        links.append({
            "kind": kind,
            "id": identifier,
            "report_revision_id": None if revision is None else str(revision),
            "bound_digest": digest,
        })
    return links


def _notebook_root(project_locator: str | os.PathLike[str]) -> Path:
    locator = Path(project_locator).expanduser()
    if locator.name.lower() in {"project.yaml", "project.yml"} or locator.suffix.lower() in {
            ".yaml", ".yml"}:
        project_root = locator.parent
    else:
        project_root = locator
    return project_root.resolve() / ".vcstudio" / "research-notebook"


@contextmanager
def _exclusive_lock(lock_path: Path, *, timeout: float = 10.0):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.1, float(timeout))
    with _PROCESS_LOCK, lock_path.open("a+b") as handle:
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
    ]


class ResearchNotebook:
    """One project-local append-only notebook journal."""

    def __init__(self, project_locator: str | os.PathLike[str], project_id: str,
                 *, lock_timeout: float = 10.0):
        self.project_id = _validate_project_id(project_id)
        self.root = _notebook_root(project_locator)
        self.journal_path = self.root / JOURNAL_NAME
        self.lock_path = self.root / LOCK_NAME
        self.attachments_path = self.root / ATTACHMENTS_DIR
        self.lock_timeout = float(lock_timeout)

    def _read_locked(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        try:
            size = self.journal_path.stat().st_size
        except OSError as exc:
            raise NotebookIntegrityError("notebook journal is unreadable") from exc
        if size > 512 * 1024 * 1024:
            raise NotebookIntegrityError("notebook journal exceeds the safety limit")
        rows: list[dict[str, Any]] = []
        previous_digest = ""
        try:
            with self.journal_path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if not raw_line.endswith("\n"):
                        raise NotebookIntegrityError(
                            "notebook journal has a partial final record",
                            line_number=line_number,
                        )
                    try:
                        row = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        raise NotebookIntegrityError(
                            "notebook journal contains invalid JSON",
                            line_number=line_number,
                        ) from exc
                    if not isinstance(row, dict):
                        raise NotebookIntegrityError(
                            "notebook journal record is not an object",
                            line_number=line_number,
                        )
                    expected_revision = len(rows) + 1
                    checks = {
                        "schema": NOTEBOOK_SCHEMA,
                        "project_id": self.project_id,
                        "revision": expected_revision,
                        "previous_digest": previous_digest,
                    }
                    for key, expected in checks.items():
                        if row.get(key) != expected:
                            raise NotebookIntegrityError(
                                f"notebook journal {key} binding mismatch",
                                line_number=line_number,
                            )
                    if row.get("record_type") not in RECORD_TYPES:
                        raise NotebookIntegrityError(
                            "notebook record_type is invalid", line_number=line_number
                        )
                    if not _TOKEN_RE.fullmatch(str(row.get("record_id") or "")):
                        raise NotebookIntegrityError(
                            "notebook record_id is invalid", line_number=line_number
                        )
                    _validate_bound_links(row.get("links"), project_id=self.project_id)
                    actual_digest = str(row.get("record_digest") or "")
                    if actual_digest != _record_digest(row):
                        raise NotebookIntegrityError(
                            "notebook record digest mismatch", line_number=line_number
                        )
                    previous_digest = actual_digest
                    rows.append(row)
                    if len(rows) > MAX_RECORDS:
                        raise NotebookIntegrityError("notebook contains too many records")
        except UnicodeDecodeError as exc:
            raise NotebookIntegrityError("notebook journal is not valid UTF-8") from exc
        return rows

    def _append_locked(self, row: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = _canonical_bytes(row) + b"\n"
        descriptor = os.open(
            str(self.journal_path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
        )
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError(f"research notebook short write: {written}/{len(payload)}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _store_attachments_locked(self, attachments: Iterable[Mapping[str, Any]]) \
            -> list[dict[str, Any]]:
        raw_items = list(attachments or [])
        if len(raw_items) > MAX_ATTACHMENTS:
            raise NotebookError("too many attachments")
        total = 0
        normalized = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise NotebookError("attachment must be an object")
            name = _safe_attachment_name(raw.get("name"))
            data = raw.get("data")
            if not isinstance(data, (bytes, bytearray)):
                raise NotebookError("attachment.data must be bytes")
            content = bytes(data)
            if not content or len(content) > MAX_ATTACHMENT_BYTES:
                raise NotebookError("attachment size is outside the allowed range")
            total += len(content)
            if total > MAX_ATTACHMENTS_TOTAL_BYTES:
                raise NotebookError("attachments exceed the total size limit")
            sha256 = hashlib.sha256(content).hexdigest()
            self.attachments_path.mkdir(parents=True, exist_ok=True)
            target = self.attachments_path / f"{sha256}.bin"
            if target.exists():
                if target.stat().st_size != len(content):
                    raise NotebookIntegrityError("content-addressed attachment size mismatch")
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
                    os.replace(temporary_path, target)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
            media_type = _validate_text(
                raw.get("media_type", ""), "attachment.media_type", limit=100
            ) or mimetypes.guess_type(name)[0] or "application/octet-stream"
            normalized.append({
                "attachment_id": f"attachment-{sha256[:24]}",
                "name": name,
                "sha256": sha256,
                "size": len(content),
                "media_type": media_type,
            })
        return normalized

    @staticmethod
    def _active_ids(rows: list[dict[str, Any]]) -> set[str]:
        superseded = {
            str(row.get("supersedes")) for row in rows if row.get("supersedes")
        }
        tombstoned = {
            str(row.get("tombstones")) for row in rows if row.get("tombstones")
        }
        return {
            str(row["record_id"]) for row in rows
            if row.get("record_type") != "tombstone"
            and row.get("record_id") not in superseded
            and row.get("record_id") not in tombstoned
        }

    def append(self, *, record_type: str, category: str, body: str,
               actor: Mapping[str, Any], expected_revision: int,
               links: list[dict[str, Any]] | None = None,
               supersedes: str | None = None,
               review: Mapping[str, Any] | None = None,
               ai_proposal: bool = False,
               attachments: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
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
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) \
                or expected_revision < 0:
            raise NotebookError("expected_revision must be a non-negative integer")
        supersedes_id = None if supersedes is None else _validate_token(
            supersedes, "supersedes"
        )
        try:
            bound_links = _validate_bound_links(
                list(links or []), project_id=self.project_id)
        except NotebookIntegrityError as exc:
            raise NotebookError(str(exc)) from exc

        with _exclusive_lock(self.lock_path, timeout=self.lock_timeout):
            rows = self._read_locked()
            current_revision = len(rows)
            if current_revision != expected_revision:
                raise NotebookRevisionConflict(current_revision)
            if supersedes_id:
                by_id = {str(item["record_id"]): item for item in rows}
                target = by_id.get(supersedes_id)
                if target is None or supersedes_id not in self._active_ids(rows):
                    raise NotebookError("supersedes must reference an active record")
                if target.get("record_type") != record_type:
                    raise NotebookError("supersedes must preserve record_type")
            attachment_rows = self._store_attachments_locked(attachments)
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
            }
            row["record_digest"] = _record_digest(row)
            self._append_locked(row)
            return self.public_record(row, resolver=None, active=True)

    def tombstone(self, *, record_id: str, reason: str, actor: Mapping[str, Any],
                  expected_revision: int) -> dict[str, Any]:
        target_id = _validate_token(record_id, "record_id")
        reason_text = _validate_text(reason, "reason", required=True, limit=2_000)
        actor_value = _actor(actor)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) \
                or expected_revision < 0:
            raise NotebookError("expected_revision must be a non-negative integer")
        with _exclusive_lock(self.lock_path, timeout=self.lock_timeout):
            rows = self._read_locked()
            if len(rows) != expected_revision:
                raise NotebookRevisionConflict(len(rows))
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
        return {
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
            "active": bool(active),
        }

    def read(self, *, resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) \
            -> dict[str, Any]:
        try:
            with _exclusive_lock(self.lock_path, timeout=self.lock_timeout):
                rows = self._read_locked()
        except NotebookIntegrityError as exc:
            return {
                "schema": PUBLIC_SCHEMA,
                "ok": False,
                "project_id": self.project_id,
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
        return {
            "schema": PUBLIC_SCHEMA,
            "ok": True,
            "project_id": self.project_id,
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

    def archive_payload(self, *, report_revision_id: str | None = None,
                        resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) \
            -> dict[str, Any]:
        snapshot = self.read(resolver=resolver)
        return {
            "schema": ARCHIVE_SCHEMA,
            "project_id": self.project_id,
            "bound_report_revision_id": report_revision_id,
            "ledger_revision": snapshot["revision"],
            "ledger_head_digest": snapshot["head_digest"],
            "integrity_status": snapshot["integrity_status"],
            "records": snapshot["records"],
            "denominator": snapshot["denominator"],
            "limitations": notebook_limitations(),
        }


class AttachmentSelections:
    """Bounded, single-use server-side attachment selections."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = 15 * 60, max_items: int = 64):
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._max_items = int(max_items)
        self._lock = threading.RLock()
        self._items: dict[str, tuple[float, str, list[dict[str, Any]]]] = {}

    def _prune(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if now - value[0] > self._ttl]
        for key in expired:
            self._items.pop(key, None)
        while len(self._items) >= self._max_items:
            oldest = min(self._items, key=lambda key: self._items[key][0])
            self._items.pop(oldest, None)

    def register(self, paths: Iterable[str | os.PathLike[str]], *, project_id: str) \
            -> dict[str, Any]:
        identifier = _validate_project_id(project_id)
        selected = []
        for raw in list(paths or [])[:MAX_ATTACHMENTS]:
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                raise NotebookError("selected attachment is not a readable file")
            size = path.stat().st_size
            if size <= 0 or size > MAX_ATTACHMENT_BYTES:
                raise NotebookError("selected attachment size is outside the allowed range")
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            selected.append({
                "path": str(path),
                "name": _safe_attachment_name(path.name),
                "size": size,
                "sha256": sha256,
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            })
        if not selected:
            raise NotebookError("no attachments were selected")
        if sum(item["size"] for item in selected) > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise NotebookError("selected attachments exceed the total size limit")
        token = "notebook-attachment." + uuid.uuid4().hex
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            self._items[token] = (now, identifier, selected)
        return {
            "selection_token": token,
            "files": [
                {key: value for key, value in item.items() if key != "path"}
                for item in selected
            ],
        }

    def consume(self, token: str, *, project_id: str) -> list[dict[str, Any]]:
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            selected = self._items.pop(str(token or ""), None)
        if selected is None:
            raise NotebookError("attachment selection token is invalid or expired")
        if selected[1] != _validate_project_id(project_id):
            raise NotebookError("attachment selection project binding mismatch")
        attachments = []
        for metadata in selected[2]:
            path = Path(metadata["path"])
            try:
                data = path.read_bytes()
            except OSError as exc:
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
