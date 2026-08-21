"""Server-owned source resolution and stable Analysis Workbench projections.

This module is deliberately ignorant of browser selections.  A source can enter
an analysis view only when it is a project member or a ledger job whose manifest
forms a descendant chain from a member.  Public projections contain opaque job
identities and content hashes, never local filesystem locators.
"""
from __future__ import annotations

import codecs
import hashlib
import inspect
import json
import math
import os
import re
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from vcstudio.project.analysis_registry import AnalysisSpec


VIEW_SCHEMA = "vcstudio.analysis-view/v1"
_PATH_RE = re.compile(r"(?i)(?:[A-Z]:[\\/]|(?:^|\s)/(?:[^/\s]+/)+|file:/{1,3}|\\\\)")
_SECRET_RE = re.compile(
    r"(?i)(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{8,}"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_XDATCAR_FRAME_RE = re.compile(
    r"^[ \t]*(?:Direct|Cartesian)[ \t]+configuration"
    r"[ \t]*=[ \t]*\d+[ \t]*$", re.I)
_LOGICAL_LINE_BREAK_RE = re.compile(
    r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_MAX_XDATCAR_LINE_CHARS = 4096
_FORBIDDEN_KEYS = frozenset({
    "path", "dir", "directory", "job_dir", "project_path", "root",
    "source_job", "peer_dir", "vacuum_dir", "solvent_dir", "report_file",
    "figure", "files", "password", "passwd", "secret", "token", "api_key",
})

# Analysis evidence is intentionally bounded before any decoder or parser sees
# it.  The values are high enough for ordinary VASP text/grid artifacts but
# finite so a ledger entry cannot turn one view request into an unbounded read.
MAX_EVIDENCE_FILES = 2048
MAX_EVIDENCE_FILE_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_EVIDENCE_LINES = 2_000_000
MAX_XDATCAR_FRAMES = 200_000
_READ_CHUNK_BYTES = 1024 * 1024
_WINDOWS_REPARSE_POINT = 0x400


class SourceSnapshotChanged(RuntimeError):
    """A source file changed while one authoritative view was being built."""


@dataclass
class SourceSnapshotBudget:
    """One shared byte budget for every snapshot retained by one view."""

    max_bytes: int = field(default_factory=lambda: MAX_EVIDENCE_TOTAL_BYTES)
    used_bytes: int = 0

    @property
    def remaining_bytes(self) -> int:
        return max(int(self.max_bytes) - int(self.used_bytes), 0)

    def consume(self, size: int) -> None:
        amount = int(size)
        if amount < 0 or amount > self.remaining_bytes:
            raise ValueError("snapshot budget cannot consume unbounded evidence")
        self.used_bytes += amount


def _snapshot_name(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = Path(raw)
    if (not raw or path.is_absolute() or bool(path.drive)
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError("snapshot evidence name must stay inside the source root")
    return "/".join(path.parts)


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _fd_change_token(descriptor: int, metadata: os.stat_result) -> int:
    """Return a non-user-settable file change token when the OS exposes one."""
    if os.name != "nt":
        return int(getattr(metadata, "st_ctime_ns", 0))
    try:
        import ctypes
        import msvcrt

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [
                ("creation_time", ctypes.c_longlong),
                ("last_access_time", ctypes.c_longlong),
                ("last_write_time", ctypes.c_longlong),
                ("change_time", ctypes.c_longlong),
                ("file_attributes", ctypes.c_ulong),
            ]

        value = FileBasicInfo()
        success = ctypes.windll.kernel32.GetFileInformationByHandleEx(
            ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)),
            0, ctypes.byref(value), ctypes.sizeof(value),
        )
        if success:
            return int(value.change_time)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return int(getattr(metadata, "st_ctime_ns", 0))


def _open_metadata_signature(
    path: Path,
) -> tuple[int, int, int, int, int, int]:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        return (*_stat_signature(metadata), _fd_change_token(descriptor, metadata))
    finally:
        os.close(descriptor)


def _rehash_bounded_regular_file(
    root: Path, path: Path, name: str, *, expected_size: int,
    remaining_bytes: int,
) -> str:
    """Stream one captured file again under the original snapshot budget.

    Windows file change times are finite-resolution metadata.  A same-size
    overwrite followed by an mtime restore can therefore share the captured
    metadata token when both operations land in one clock tick.  This final
    content check reads at most the captured size plus one EOF sentinel, keeps
    only one fixed-size chunk in memory, and repeats the no-follow/identity
    checks around the read.
    """
    size = int(expected_size)
    allowance = int(remaining_bytes)
    if size < 0 or size + 1 > allowance:
        raise ValueError("snapshot revalidation exceeds bounded byte budget")
    parents_before = _parent_signatures(root, name)
    try:
        before_path = path.lstat()
    except OSError as exc:
        raise ValueError("file disappeared before snapshot revalidation") from exc
    if not _is_regular_no_follow(before_path) or int(before_path.st_size) != size:
        raise ValueError("evidence identity changed before snapshot revalidation")

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(
            "evidence could not be reopened without following links") from exc
    digest = hashlib.sha256()
    read_bytes = 0
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        before_fd = os.fstat(handle.fileno())
        before_change = _fd_change_token(handle.fileno(), before_fd)
        if (not _is_regular_no_follow(before_fd)
                or _stat_signature(before_fd) != _stat_signature(before_path)):
            raise ValueError(
                "evidence identity changed before snapshot revalidation")
        while read_bytes < size:
            chunk = handle.read(min(_READ_CHUNK_BYTES, size - read_bytes))
            if not chunk:
                raise ValueError("evidence was truncated during snapshot revalidation")
            read_bytes += len(chunk)
            digest.update(chunk)
        if handle.read(1):
            raise ValueError("evidence grew during snapshot revalidation")
        after_fd = os.fstat(handle.fileno())
        after_change = _fd_change_token(handle.fileno(), after_fd)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise ValueError("file disappeared after snapshot revalidation") from exc
    identity = _stat_signature(before_fd)
    if (not _is_regular_no_follow(after_path)
            or read_bytes != size
            or identity != _stat_signature(after_fd)
            or identity != _stat_signature(after_path)
            or before_change != after_change
            or _parent_signatures(root, name) != parents_before):
        raise ValueError("evidence changed during snapshot revalidation")
    return digest.hexdigest()


def _is_regular_no_follow(value: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(value.st_mode)
        and not (int(getattr(value, "st_file_attributes", 0))
                 & _WINDOWS_REPARSE_POINT)
    )


def _is_directory_no_follow(value: os.stat_result) -> bool:
    return bool(
        stat.S_ISDIR(value.st_mode)
        and not (int(getattr(value, "st_file_attributes", 0))
                 & _WINDOWS_REPARSE_POINT)
    )


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except OSError:
        return False
    return True


def _parent_signatures(
    root: Path, name: str,
) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
    current = root
    parents = [root]
    for part in Path(name).parts[:-1]:
        current = current / part
        parents.append(current)
    signatures = []
    for parent in parents:
        try:
            value = parent.lstat()
        except OSError as exc:
            raise ValueError("evidence parent directory is unavailable") from exc
        if not _is_directory_no_follow(value):
            raise ValueError(
                "evidence parent must be a no-follow regular directory")
        signatures.append((str(parent), _stat_signature(value)))
    return tuple(signatures)


def _read_bounded_regular_file(
    root: Path, path: Path, name: str, *, remaining_bytes: int,
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    """Read one stable, regular, non-link file once under hard limits."""
    parents_before = _parent_signatures(root, name)
    try:
        before_path = path.lstat()
    except OSError as exc:
        raise ValueError("file disappeared before snapshot") from exc
    if not _is_regular_no_follow(before_path):
        raise ValueError("evidence must be a no-follow regular file")
    if remaining_bytes < 0 or before_path.st_size > remaining_bytes:
        raise ValueError(
            "evidence exceeds remaining total byte limit "
            f"({before_path.st_size} > {max(remaining_bytes, 0)})")
    byte_limit = min(MAX_EVIDENCE_FILE_BYTES, remaining_bytes)
    if before_path.st_size > byte_limit:
        raise ValueError(
            f"evidence exceeds per-file byte limit "
            f"({before_path.st_size} > {byte_limit})")

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("evidence could not be opened without following links") from exc
    data = bytearray()
    logical_line_count = 0
    pending_cr = False
    current_line_has_content = False
    is_xdatcar = Path(name).name.upper() == "XDATCAR"
    frame_line = ""
    frames = 0
    line_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def append_line(value: str) -> None:
        nonlocal current_line_has_content, frame_line
        if not value:
            return
        current_line_has_content = True
        if is_xdatcar:
            if len(frame_line) + len(value) > _MAX_XDATCAR_LINE_CHARS:
                raise ValueError(
                    "XDATCAR logical line exceeds bounded parser limit "
                    f"({_MAX_XDATCAR_LINE_CHARS})")
            frame_line += value

    def finish_line() -> None:
        nonlocal current_line_has_content, frame_line, frames, logical_line_count
        logical_line_count += 1
        if logical_line_count > MAX_EVIDENCE_LINES:
            raise ValueError(
                f"evidence exceeds line limit ({MAX_EVIDENCE_LINES})")
        if is_xdatcar and _XDATCAR_FRAME_RE.fullmatch(frame_line):
            frames += 1
            if frames > MAX_XDATCAR_FRAMES:
                raise ValueError(
                    "XDATCAR exceeds frame limit "
                    f"({frames} > {MAX_XDATCAR_FRAMES})")
        current_line_has_content = False
        frame_line = ""

    def scan_decoded_lines(decoded: str, *, final: bool = False) -> None:
        nonlocal pending_cr
        if pending_cr:
            decoded = "\r" + decoded
            pending_cr = False
        if not final and decoded.endswith("\r"):
            decoded = decoded[:-1]
            pending_cr = True
        cursor = 0
        for match in _LOGICAL_LINE_BREAK_RE.finditer(decoded):
            append_line(decoded[cursor:match.start()])
            finish_line()
            cursor = match.end()
        append_line(decoded[cursor:])
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before_fd = os.fstat(handle.fileno())
            before_change = _fd_change_token(handle.fileno(), before_fd)
            if (not _is_regular_no_follow(before_fd)
                    or _stat_signature(before_fd) != _stat_signature(before_path)):
                raise ValueError("evidence identity changed before bounded read")
            while True:
                chunk = handle.read(min(_READ_CHUNK_BYTES, byte_limit - len(data) + 1))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > byte_limit:
                    raise ValueError(f"evidence exceeds byte limit ({byte_limit})")
                # Incremental UTF-8 decoding matches SourceSnapshot.text()
                # without materialising a second whole-file string or lines.
                scan_decoded_lines(line_decoder.decode(chunk, final=False))
            after_fd = os.fstat(handle.fileno())
            after_change = _fd_change_token(handle.fileno(), after_fd)
            scan_decoded_lines(line_decoder.decode(b"", final=True), final=True)
    except Exception:
        # ``fdopen`` owns and closes the descriptor after it succeeds.
        raise
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise ValueError("file disappeared after snapshot") from exc
    identity_signature = _stat_signature(before_fd)
    signature = (*identity_signature, before_change)
    if (not _is_regular_no_follow(after_path)
            or identity_signature != _stat_signature(after_fd)
            or identity_signature != _stat_signature(after_path)
            or before_change != after_change
            or len(data) != before_fd.st_size):
        raise ValueError("evidence changed during bounded read")
    if _parent_signatures(root, name) != parents_before:
        raise ValueError("evidence parent directory changed during bounded read")
    if pending_cr:
        finish_line()
    elif current_line_has_content:
        finish_line()
    return bytes(data), signature


@dataclass
class SourceSnapshot:
    """Immutable bytes plus public hashes for one server-resolved source.

    Parsers consume :meth:`text`/:meth:`bytes` from this object, never reopening
    the scientific input. :meth:`assert_unchanged` first checks metadata, then
    streams a bounded digest revalidation so finite-resolution change tokens
    cannot hide a same-size replacement.
    """

    root: Path
    source_id: str
    relation: str
    task_type: str
    manifest_state: str
    requested_names: tuple[str, ...]
    missing: tuple[str, ...]
    rejected: tuple[dict[str, str], ...]
    _bytes_by_name: dict[str, bytes] = field(repr=False)
    _signatures: dict[str, tuple[int, int, int, int, int, int] | None] = field(
        repr=False)
    _files: tuple[dict[str, Any], ...] = field(repr=False)
    _text_by_name: dict[str, str] = field(default_factory=dict, repr=False)

    def has(self, name: str) -> bool:
        return _snapshot_name(name) in self._bytes_by_name

    def bytes(self, name: str) -> bytes:
        return self._bytes_by_name.get(_snapshot_name(name), b"")

    def text(self, name: str) -> str:
        wanted = _snapshot_name(name)
        if wanted not in self._text_by_name:
            self._text_by_name[wanted] = self.bytes(wanted).decode(
                "utf-8", errors="replace")
        return self._text_by_name[wanted]

    def file(self, name: str) -> dict[str, Any] | None:
        wanted = _snapshot_name(name)
        return next((dict(item) for item in self._files
                     if item.get("name") == wanted), None)

    def files(self, names: Sequence[str] | None = None) -> list[dict[str, Any]]:
        if names is None:
            return [dict(item) for item in self._files]
        wanted = {_snapshot_name(name) for name in names}
        return [dict(item) for item in self._files if item.get("name") in wanted]

    def identity(self) -> dict[str, Any]:
        files = self.files()
        return {
            "source_id": self.source_id,
            "relation": self.relation,
            "task_type": self.task_type,
            "manifest_state": self.manifest_state,
            "manifest_sha256": next(
                (item["sha256"] for item in files if item["name"] == "job.yaml"),
                None,
            ),
            "files": files,
        }

    def evidence_issues(self) -> list[str]:
        return [
            f'{item["name"]}: {item["reason"]}'
            for item in self.rejected
        ]

    @contextmanager
    def materialized_directory(self):
        """Expose exactly the captured bytes to a legacy path-based parser."""
        with tempfile.TemporaryDirectory(prefix="vcstudio-analysis-snapshot-") as raw:
            temporary_root = Path(raw)
            for name, data in self._bytes_by_name.items():
                target = temporary_root / Path(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | int(getattr(os, "O_BINARY", 0)))
                descriptor = os.open(target, flags, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(data)
            yield temporary_root

    def manifest(self) -> dict[str, Any]:
        if not self.has("job.yaml"):
            return {}
        try:
            value = yaml.safe_load(self.text("job.yaml")) or {}
        except yaml.YAMLError as exc:
            raise SourceSnapshotChanged(
                f"{self.source_id}: job.yaml snapshot is invalid") from exc
        if not isinstance(value, Mapping):
            raise SourceSnapshotChanged(
                f"{self.source_id}: job.yaml snapshot is not an object")
        return dict(value)

    def assert_manifest_matches(self, expected: Mapping[str, Any]) -> None:
        current = self.manifest()
        if _canonical_hash(current) != _canonical_hash(dict(expected or {})):
            raise SourceSnapshotChanged(
                f"{self.source_id}: ledger manifest and job.yaml snapshot differ; retry")
        current_state = str(current.get("state") or "UNKNOWN").strip().upper()
        current_task_type = str(current.get("task_type") or "").strip().lower()
        if (current_state != str(self.manifest_state or "UNKNOWN").strip().upper()
                or current_task_type != str(self.task_type or "").strip().lower()):
            raise SourceSnapshotChanged(
                f"{self.source_id}: resolved target state/task_type and job.yaml differ; retry")

    def assert_unchanged(self) -> None:
        rejected_names = {item["name"] for item in self.rejected}
        evidence_by_name = {
            str(item.get("name") or ""): item for item in self._files
        }
        # Each captured byte may be streamed once more, plus one EOF sentinel
        # per bounded file.  Across snapshots in a view this stays within the
        # original shared snapshot budget plus MAX_EVIDENCE_FILES bytes.
        remaining_revalidation_bytes = sum(
            int(item.get("size_bytes") or 0) + 1 for item in self._files)
        for name in self.requested_names:
            if name in rejected_names:
                continue
            path = self.root / Path(name)
            expected_signature = self._signatures.get(name)
            if expected_signature is None:
                try:
                    path.lstat()
                except OSError:
                    continue
                else:
                    raise SourceSnapshotChanged(
                        f"{self.source_id}:{name} appeared during analysis; retry")
            try:
                current = path.lstat()
            except OSError as exc:
                raise SourceSnapshotChanged(
                    f"{self.source_id}:{name} disappeared during analysis; retry") from exc
            try:
                current_signature = _open_metadata_signature(path)
            except OSError as exc:
                raise SourceSnapshotChanged(
                    f"{self.source_id}:{name} could not be revalidated; retry") from exc
            if (not _is_regular_no_follow(current)
                    or current_signature != expected_signature):
                raise SourceSnapshotChanged(
                    f"{self.source_id}:{name} changed during analysis; retry")
            evidence = evidence_by_name.get(name)
            if evidence is None:
                raise SourceSnapshotChanged(
                    f"{self.source_id}:{name} lost its snapshot hash; retry")
            expected_size = int(evidence.get("size_bytes") or 0)
            try:
                current_sha256 = _rehash_bounded_regular_file(
                    self.root, path, name, expected_size=expected_size,
                    remaining_bytes=remaining_revalidation_bytes)
            except (OSError, ValueError) as exc:
                raise SourceSnapshotChanged(
                    f"{self.source_id}:{name} changed during analysis; retry") from exc
            remaining_revalidation_bytes -= expected_size + 1
            if current_sha256 != str(evidence.get("sha256") or ""):
                raise SourceSnapshotChanged(
                    f"{self.source_id}:{name} changed during analysis; retry")


def capture_source_snapshot(
    target: Mapping[str, Any], evidence_names: Sequence[Any], *,
    budget: SourceSnapshotBudget | None = None,
) -> SourceSnapshot:
    """Capture each requested file once and bind hashes/parsers to those bytes."""
    view_budget = budget or SourceSnapshotBudget()
    root = Path(str(target["path"]))
    names = ["job.yaml"]
    missing_labels = []
    for evidence in evidence_names:
        if isinstance(evidence, (list, tuple)):
            options = [_snapshot_name(name) for name in evidence]
            chosen = next(
                (name for name in options if _entry_exists(root / Path(name))), None)
            if chosen is None:
                missing_labels.append(" or ".join(options))
            elif chosen not in names:
                names.append(chosen)
        else:
            name = _snapshot_name(evidence)
            if name not in names:
                names.append(name)

    missing = list(missing_labels)
    rejected = []
    if len(names) > MAX_EVIDENCE_FILES:
        rejected.append({
            "name": "<snapshot>",
            "reason": f"evidence file count exceeds limit ({MAX_EVIDENCE_FILES})",
        })
        names = names[:MAX_EVIDENCE_FILES]
        missing.append(
            f"evidence file count exceeds limit ({MAX_EVIDENCE_FILES})")

    bytes_by_name: dict[str, bytes] = {}
    signatures: dict[str, tuple[int, int, int, int, int, int] | None] = {}
    files = []
    total_bytes = 0
    for name in names:
        path = root / Path(name)
        try:
            path.lstat()
        except OSError:
            signatures[name] = None
            missing.append(name)
            continue
        try:
            data, signature = _read_bounded_regular_file(
                root, path, name,
                remaining_bytes=min(
                    MAX_EVIDENCE_TOTAL_BYTES - total_bytes,
                    view_budget.remaining_bytes,
                ))
        except ValueError as exc:
            signatures[name] = None
            reason = str(exc)
            rejected.append({"name": name, "reason": reason})
            missing.append(f"{name} rejected: {reason}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        bytes_by_name[name] = data
        signatures[name] = signature
        total_bytes += len(data)
        view_budget.consume(len(data))
        files.append({"name": name, "sha256": digest, "size_bytes": len(data)})
    return SourceSnapshot(
        root=root,
        source_id=str(target.get("source_id") or ""),
        relation=str(target.get("relation") or ""),
        task_type=str(target.get("task_type") or ""),
        manifest_state=str(target.get("state") or "UNKNOWN"),
        requested_names=tuple(names),
        missing=tuple(dict.fromkeys(missing)),
        rejected=tuple(rejected),
        _bytes_by_name=bytes_by_name,
        _signatures=signatures,
        _files=tuple(files),
    )


def _path_key(value: Any) -> str:
    try:
        return os.path.normcase(os.path.realpath(os.fspath(value)))
    except (TypeError, ValueError, OSError):
        return ""


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "").replace("\x00", "").strip()[:limit]
    if _PATH_RE.search(text) or _SECRET_RE.search(text):
        return "<redacted>"
    return text


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _public_value(value: Any) -> Any:
    """Project parser output without locator/credential-shaped fields."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _public_value(item)
            for key, item in value.items()
            if str(key).lower() not in _FORBIDDEN_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    return _safe_text(value)


def _member_paths(project: Mapping[str, Any]) -> list[str]:
    members = project.get("members") or {}
    if not isinstance(members, Mapping):
        return []
    values: list[Any] = [members.get("clean_slab"), members.get("gas_ref")]
    values.extend(members.get("configs") or [])
    for refs in (members.get("molecules"), project.get("species_ref_jobs")):
        if isinstance(refs, Mapping):
            values.extend(refs.values())
        elif isinstance(refs, (list, tuple)):
            values.extend(refs)
    result: list[str] = []
    for value in values:
        key = _path_key(value)
        if value and key and key not in {_path_key(item) for item in result}:
            result.append(str(value))
    return result


def _manifest_parent(manifest: Mapping[str, Any]) -> str | None:
    inputs = manifest.get("inputs") or {}
    candidates = [manifest.get("parent_job")]
    if isinstance(inputs, Mapping):
        candidates.extend((inputs.get("parent_job"), inputs.get("source_job")))
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def resolve_project_targets(
    project: Mapping[str, Any],
    ledger_entries: Iterable[tuple[Any, Any]],
    *,
    manifest_loader: Callable[[str], Mapping[str, Any] | None],
    opaque_id: Callable[[str, Mapping[str, Any]], str],
) -> list[dict[str, Any]]:
    """Resolve project members and manifest-linked descendants, fail closed.

    Merely living below a project directory is not authority.  Descendants must
    be present in the server ledger and bind to a previously accepted source via
    ``parent_job`` (top-level or in ``inputs``).
    """
    members = _member_paths(project)
    member_keys = {_path_key(path) for path in members}
    ledger: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for raw_path, raw_manifest in ledger_entries:
        key = _path_key(raw_path)
        if not key or not isinstance(raw_manifest, Mapping):
            continue
        ledger[key] = (str(raw_path), dict(raw_manifest))

    accepted: dict[str, tuple[str, Mapping[str, Any], str]] = {}
    for path in members:
        key = _path_key(path)
        manifest = ledger.get(key, (path, None))[1]
        if not isinstance(manifest, Mapping):
            manifest = manifest_loader(path)
        if isinstance(manifest, Mapping):
            accepted[key] = (str(path), dict(manifest), "member")

    changed = True
    while changed:
        changed = False
        for key, (path, manifest) in ledger.items():
            if key in accepted:
                continue
            parent = _path_key(_manifest_parent(manifest))
            if parent and parent in accepted:
                accepted[key] = (path, manifest, "descendant")
                changed = True

    targets = []
    for key, (path, manifest, relation) in sorted(
            accepted.items(), key=lambda item: (str(item[1][1].get("task_type") or ""),
                                                str(item[1][1].get("job_uuid") or
                                                    item[1][1].get("job_id") or item[0]))):
        identifier = str(opaque_id(path, manifest) or "").strip()
        if not identifier:
            continue
        targets.append({
            "path": path,
            "path_key": key,
            "source_id": identifier,
            "relation": relation,
            "task_type": str(manifest.get("task_type") or "").strip().lower(),
            "state": str(manifest.get("state") or "UNKNOWN").strip().upper(),
            "manifest": dict(manifest),
            "member": key in member_keys,
        })
    return targets


def source_identity(
    target: Mapping[str, Any],
    evidence_names: Sequence[Any],
) -> tuple[dict[str, Any], list[str]]:
    snapshot = capture_source_snapshot(target, evidence_names)
    if snapshot.has("job.yaml"):
        snapshot.assert_manifest_matches(target.get("manifest") or {})
    identity = snapshot.identity()
    missing = list(snapshot.missing)
    snapshot.assert_unchanged()
    return identity, missing


def value_provenance(
    source_id: Any,
    files: Sequence[Mapping[str, Any]],
    parser: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the path-free provenance required by one quantitative value.

    The caller supplies already resolved file records.  Invalid or absent hashes
    are not upgraded into evidence, and parser identity fields remain explicit
    even when unavailable so downstream views can fail closed.
    """
    evidence = []
    for item in files or ():
        digest = str(item.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(digest):
            continue
        evidence.append({
            "name": _safe_text(item.get("name")),
            "sha256": digest,
        })
    return {
        "source_id": _safe_text(source_id),
        "file_hashes": evidence,
        "parser_module": _safe_text(parser.get("module")),
        "parser_callable": _safe_text(parser.get("callable")),
        "parser_version": _safe_text(parser.get("version")),
    }


def _method_fingerprint_hash(value: Any) -> str:
    if isinstance(value, str) and _SHA256_RE.fullmatch(value.lower()):
        return value.lower()
    if isinstance(value, Mapping) and value:
        return _canonical_hash(value)
    return ""


def verify_neb_endpoint_record(
    *,
    record: Mapping[str, Any],
    role: str,
    frame: str,
    neb_snapshot: SourceSnapshot,
    targets_by_source_id: Mapping[str, Mapping[str, Any]],
    method_resolver: Callable[[Mapping[str, Any], SourceSnapshot], Mapping[str, Any]],
    neb_method_fingerprint: Any,
    snapshot_budget: SourceSnapshotBudget | None = None,
) -> dict[str, Any]:
    """Verify one copied endpoint against the current ledger and file bytes.

    ``trusted``, ``source_state`` and any method fingerprint stored inside the
    NEB manifest are deliberately ignored. Authority comes from the opaque
    ``source_job_id`` lookup, the current source ``job.yaml`` snapshot, copied
    file hashes on both sides, and a freshly recomputed method record.
    """
    issues = []
    if not isinstance(record, Mapping):
        return {"ok": False, "issues": [f"{role} endpoint record is unavailable"]}
    if str(record.get("target_frame") or "") != frame:
        issues.append(f"{role} endpoint target_frame does not match {frame}")
    source_id = str(record.get("source_job_id") or "").strip()
    source_target = targets_by_source_id.get(source_id)
    if not source_id or source_target is None:
        issues.append(f"{role} endpoint source_job_id is not a resolved ledger identity")
        return {"ok": False, "issues": issues, "source_id": source_id}
    if source_id == neb_snapshot.source_id:
        issues.append(f"{role} endpoint source_job_id cannot refer to the NEB job itself")
        return {"ok": False, "issues": issues, "source_id": source_id}

    source_snapshot = capture_source_snapshot(source_target, [
        "INCAR", "KPOINTS", "POTCAR", "POSCAR", "CONTCAR",
        "OSZICAR", "OUTCAR", "vasprun.xml",
    ], budget=snapshot_budget)
    issues.extend(
        f"{role} endpoint ledger source {issue}"
        for issue in source_snapshot.evidence_issues()
    )
    if source_snapshot.has("job.yaml"):
        source_snapshot.assert_manifest_matches(source_target.get("manifest") or {})
    source_manifest = source_snapshot.manifest()
    from vcstudio.project.energy_gate import validate_done_completion_evidence
    try:
        validate_done_completion_evidence(
            source_manifest, f"{role} endpoint ledger source",
            outcar_text=source_snapshot.text("OUTCAR"),
            vasprun_text=source_snapshot.text("vasprun.xml"),
            require_current_completion=True,
            require_explicit_diagnosis=True,
            reject_explicit_unclean=True,
            require_outcar_completion=True,
        )
    except ValueError as exc:
        issues.append(str(exc))

    declared_files = record.get("files") or []
    if not isinstance(declared_files, (list, tuple)) or not declared_files:
        issues.append(f"{role} endpoint has no copied file-hash evidence")
        declared_files = []
    verified_files = []
    verified_pairs = []
    for item in declared_files:
        if not isinstance(item, Mapping):
            issues.append(f"{role} endpoint copied file record is invalid")
            continue
        try:
            name = _snapshot_name(item.get("name"))
            copied_name = _snapshot_name(item.get("copied_name") or name)
        except ValueError:
            issues.append(f"{role} endpoint copied file name is invalid")
            continue
        if "/" in name or "/" in copied_name:
            issues.append(f"{role} endpoint copied file must be a root-level source file")
            continue
        expected = str(item.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(expected):
            issues.append(f"{role} endpoint {name} has no valid recorded SHA-256")
            continue
        copied = neb_snapshot.file(f"{frame}/{copied_name}")
        original = source_snapshot.file(name)
        if copied is None or copied.get("sha256") != expected:
            issues.append(f"{role} endpoint copied {name} hash does not match the record")
            continue
        if original is None or original.get("sha256") != expected:
            issues.append(f"{role} endpoint source {name} no longer matches the copied bytes")
            continue
        verified_files.append(name)
        verified_pairs.append((name, copied_name))

    structure_verified = any(
        name in {"POSCAR", "CONTCAR"} and copied_name == "POSCAR"
        for name, copied_name in verified_pairs)
    if not structure_verified:
        issues.append(f"{role} endpoint structure is not hash-bound to its ledger source")

    endpoint_method = dict(method_resolver(source_target, source_snapshot) or {})
    endpoint_method_hash = _method_fingerprint_hash(endpoint_method.get("fingerprint"))
    neb_method_hash = _method_fingerprint_hash(neb_method_fingerprint)
    method_issues = any(endpoint_method.get(key) for key in (
        "missing", "warnings", "errors", "issues",
    ))
    if (endpoint_method.get("status") != "verified" or method_issues
            or not endpoint_method_hash):
        issues.append(f"{role} endpoint method evidence is not verified")
    elif not neb_method_hash or endpoint_method_hash != neb_method_hash:
        issues.append(f"{role} endpoint method differs from the NEB method")
    return {
        "ok": not issues,
        "issues": issues,
        "source_id": source_id,
        "verified_files": verified_files,
        "method": endpoint_method,
        "source_snapshot": source_snapshot,
    }


_ANALYSIS_TASKS = {
    "electronic-structure": ("dos_pdos", "bands", "workfunction"),
    "charge-wavefunction": ("bader", "chgdiff", "elf"),
}
_EVIDENCE = {
    "dos_pdos": ("vasprun.xml",),
    "bands": (("EIGENVAL", "vasprun.xml"),),
    "workfunction": ("LOCPOT", "OUTCAR"),
    "bader": ("ACF.dat",),
    "chgdiff": ("CHGDIFF.vasp",),
    "elf": ("ELFCAR",),
}
_METHOD_EVIDENCE = ("INCAR", "KPOINTS", "POTCAR", "POSCAR", "CONTCAR")
_PROPERTY_EVIDENCE = (
    "INCAR", "KPOINTS", "POTCAR", "POSCAR", "CONTCAR",
    "OSZICAR", "OUTCAR", "vasprun.xml",
)


def _display(value: Any, precision: int) -> str:
    number = _finite(value)
    if number is not None:
        return f"{number:.{precision}f}"
    if value is None:
        return "—"
    return _safe_text(value)


def _value_rows(kind: str, result: Mapping[str, Any], precision: int) -> list[dict[str, Any]]:
    source = result.get("result") or {}
    if not isinstance(source, Mapping):
        source = {}
    fields = {
        "dos_pdos": (
            ("efermi_ev", "E_F", "eV"), ("n_energy_points", "Energy points", ""),
            ("n_ions", "Ions", ""), ("spin_polarized", "Spin polarized", ""),
        ),
        "bands": (),
        "workfunction": (
            ("phi", "Work function", "eV"),
            ("vacuum_level", "Vacuum level", "eV"),
        ),
        "bader": (("n_atoms", "Atoms", ""), ("sum_delta_q", "ΣΔq", "e")),
        "chgdiff": (
            ("n_points", "Profile points", ""),
            ("rho_min_e_a3", "ρ minimum", "e/Å³"),
            ("rho_max_e_a3", "ρ maximum", "e/Å³"),
        ),
        "elf": (
            ("grid_shape", "Grid shape", ""),
            ("natoms", "Atoms", ""),
            ("minimum", "ELF minimum", ""),
            ("maximum", "ELF maximum", ""),
            ("mean", "ELF mean", ""),
            ("std", "ELF standard deviation", ""),
        ),
    }.get(kind, ())
    if kind == "bands":
        gap = source.get("gap") or {}
        if isinstance(gap, Mapping):
            fields = (("value", "Band gap", "eV"), ("direct", "Direct gap", ""),
                      ("metal", "Metal", ""))
            source = gap
    rows = []
    for key, label, unit in fields:
        value = _public_value(source.get(key))
        rows.append({
            "key": key, "label": label, "value": value,
            "display": _display(value, precision), "unit": unit,
        })
    return rows


def _parser_record(identity: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(identity or {})
    return {
        "module": _safe_text(source.get("module")),
        "callable": _safe_text(source.get("callable")),
        "version": _safe_text(source.get("version")),
        "version_source": _safe_text(source.get("version_source")),
    }


def _method_evidence_from_snapshot(
    resolver: Callable[..., Mapping[str, Any]], target: Mapping[str, Any],
    snapshot: SourceSnapshot,
) -> dict[str, Any]:
    try:
        inspect.signature(resolver).bind(target, snapshot)
    except (TypeError, ValueError):
        return dict(resolver(target) or {})
    return dict(resolver(target, snapshot) or {})


def _result_projection(kind: str, result: Any, precision: int) -> Any:
    public = _public_value(result)
    if kind != "elf" or not isinstance(public, dict):
        return public
    public["display_quantiles"] = [{
        "fraction": _display(item.get("fraction"), precision),
        "value": _display(item.get("value"), precision),
    } for item in public.get("quantiles") or [] if isinstance(item, Mapping)]
    public["display_histogram"] = [{
        "low": _display(item.get("low"), precision),
        "high": _display(item.get("high"), precision),
        "count": _display(item.get("count"), 0),
    } for item in public.get("histogram") or [] if isinstance(item, Mapping)]
    return public


def build_task_analysis_view(
    spec: AnalysisSpec,
    targets: Sequence[Mapping[str, Any]],
    *,
    runner: Callable[[str, str], Mapping[str, Any]],
    parser_identities: Mapping[str, Mapping[str, Any]],
    method_evidence: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    if spec.analysis_id not in {*_ANALYSIS_TASKS, "task-results"}:
        raise ValueError("task analysis view requires a matching analysis kind")
    allowed = set(_ANALYSIS_TASKS.get(spec.analysis_id, ()))
    selected = [item for item in targets if not allowed or item.get("task_type") in allowed]
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    blocking: list[str] = []
    warnings: list[str] = []
    evidence_files = 0
    parser_ready = 0
    not_implemented = 0
    snapshot_budget = SourceSnapshotBudget()

    for target in selected:
        kind = str(target.get("task_type") or "")
        identity = _parser_record(parser_identities.get(kind))
        source_root = Path(str(target.get("path") or ""))
        method_names = [
            name for name in _METHOD_EVIDENCE
            if _entry_exists(source_root / name)
        ]
        source_snapshot = capture_source_snapshot(
            target, (*_EVIDENCE.get(kind, ()), *method_names),
            budget=snapshot_budget)
        if source_snapshot.has("job.yaml"):
            source_snapshot.assert_manifest_matches(target.get("manifest") or {})
        source_manifest = source_snapshot.manifest()
        source = source_snapshot.identity()
        source_missing = list(source_snapshot.missing)
        source_rejected = source_snapshot.evidence_issues()
        evidence_files += len(source["files"])
        prefix = f'{kind or "unknown"} [{target.get("source_id")}]'
        if str(source_manifest.get("state") or "").strip().upper() != "DONE":
            message = f"{prefix}: manifest state is not DONE"
            missing.append(message)
            rows.append({"source": source, "parser": identity, "available": False,
                         "status": "missing_prerequisite", "summary": message, "values": []})
            continue
        if not identity["module"] or not identity["callable"] or not identity["version"]:
            message = f"{prefix}: parser identity/version evidence is unavailable"
            blocking.append(message)
            not_implemented += 1
            rows.append({"source": source, "parser": identity, "available": False,
                         "status": "not_implemented", "summary": message, "values": []})
            continue
        parser_ready += 1
        if source_rejected:
            message = f"{prefix}: bounded evidence rejected: " + "; ".join(
                _safe_text(item) for item in source_rejected)
            blocking.append(message)
            rows.append({"source": source, "parser": identity, "available": False,
                         "status": "unavailable", "summary": message, "values": []})
            continue
        if source_missing:
            message = f"{prefix}: missing evidence files: {', '.join(source_missing)}"
            missing.append(message)
            rows.append({"source": source, "parser": identity, "available": False,
                         "status": "missing_prerequisite", "summary": message, "values": []})
            continue
        method = _method_evidence_from_snapshot(
            method_evidence, target, source_snapshot)
        if method.get("status") != "verified":
            details = list(method.get("missing") or method.get("issues") or
                           method.get("warnings") or ["method evidence is incomplete"])
            message = f"{prefix}: " + "; ".join(_safe_text(item) for item in details)
            blocking.append(message)
            rows.append({"source": source, "parser": identity, "method": _public_value(method),
                         "available": False, "status": "unavailable", "summary": message,
                         "values": []})
            continue
        try:
            with source_snapshot.materialized_directory() as parser_root:
                try:
                    parsed = dict(runner(str(parser_root), kind) or {})
                except Exception as exc:  # parser failures are data, never invented values
                    parsed = {"ok": False, "error": str(exc), "result": None}
        finally:
            source_snapshot.assert_unchanged()
        if parsed.get("ok") is not True:
            message = f"{prefix}: {_safe_text(parsed.get('error') or 'parser returned no result')}"
            blocking.append(message)
            rows.append({"source": source, "parser": identity, "method": _public_value(method),
                         "available": False, "status": "unavailable", "summary": message,
                         "values": []})
            continue
        parsed_warnings = list((parsed.get("result") or {}).get("warnings") or []) \
            if isinstance(parsed.get("result"), Mapping) else []
        warnings.extend(_safe_text(item) for item in parsed_warnings)
        rows.append({
            "source": source, "parser": identity, "method": _public_value(method),
            "available": True, "status": "available",
            "summary": _safe_text(parsed.get("summary")),
            "values": _value_rows(kind, parsed, spec.precision),
            "result": _result_projection(kind, parsed.get("result"), spec.precision),
        })

    if not selected:
        missing.append("No project member or manifest-linked descendant matches this analysis")
    available_count = sum(row["available"] is True for row in rows)
    if available_count:
        scientific_status = "verified" if not warnings else "unverified"
        capability_status = "available"
    elif not_implemented and not_implemented == len(rows):
        scientific_status = "unavailable"
        capability_status = "not_implemented"
    else:
        scientific_status = "blocked"
        capability_status = "missing_prerequisite" if missing and not blocking else "unavailable"
    next_action = (
        "Inspect the server-resolved results and evidence hashes."
        if available_count else
        "Complete the listed prerequisite on a registered project job, then refresh."
    )
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": scientific_status,
        "capability_status": capability_status,
        "available": bool(available_count),
        "rows": rows,
        "missing": list(dict.fromkeys(missing)),
        "blocking": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
        "next_action": next_action,
        "denominator": {
            "resolved_targets": len(targets),
            "matching_targets": len(selected),
            "parser_ready_targets": parser_ready,
            "available_results": available_count,
            "blocked_results": len(rows) - available_count,
            "evidence_files": evidence_files,
            "visible_rows": len(rows),
        },
    }
    payload["data_fingerprint"] = _canonical_hash(payload)
    return payload


def build_property_view(
    spec: AnalysisSpec,
    targets: Sequence[Mapping[str, Any]],
    *,
    runner: Callable[[str, Sequence[Mapping[str, Any]]], Sequence[Mapping[str, Any]]],
    parser_version: str,
) -> dict[str, Any]:
    if spec.analysis_id != "property-calculators":
        raise ValueError("property view requires property-calculators")
    rows = []
    missing: list[str] = []
    blocking: list[str] = []
    raw_by_kind = {}
    source_by_id = {}
    snapshots = []
    snapshot_budget = SourceSnapshotBudget()
    with ExitStack() as stack:
        materialized_targets = []
        for target in targets:
            snapshot = capture_source_snapshot(
                target, _PROPERTY_EVIDENCE, budget=snapshot_budget)
            snapshots.append(snapshot)
            source_id = str(target.get("source_id") or "")
            source_by_id[source_id] = snapshot.identity()
            if snapshot.has("job.yaml"):
                snapshot.assert_manifest_matches(target.get("manifest") or {})
            if snapshot.rejected:
                blocking.extend(
                    f"{source_id}: {issue}" for issue in snapshot.evidence_issues())
                continue
            parser_root = stack.enter_context(snapshot.materialized_directory())
            materialized = dict(target)
            materialized["path"] = str(parser_root)
            materialized_targets.append(materialized)
        for kind in ("surface_energy", "formation_binding", "vaspsol"):
            try:
                raw_by_kind[kind] = list(
                    runner(kind, materialized_targets) or [])
            except Exception as exc:
                raw_by_kind[kind] = [{"ok": False, "error": str(exc)}]
        for snapshot in snapshots:
            snapshot.assert_unchanged()

    for kind in ("surface_energy", "formation_binding", "vaspsol"):
        raw = raw_by_kind[kind]
        if not raw:
            missing.append(f"{kind}: no manifest-declared operands were resolved")
            continue
        for item in raw:
            public = _public_value(item)
            parser = {"module": _safe_text(item.get("parser_module")),
                      "callable": _safe_text(item.get("parser_callable")),
                      "version": _safe_text(parser_version),
                      "version_source": "vcstudio.__version__"}
            sources = [
                source_by_id[source_id]
                for source_id in item.get("source_ids") or []
                if source_id in source_by_id
            ]
            source_ids = [str(value) for value in item.get("source_ids") or []]
            source_complete = (
                bool(source_ids) and len(sources) == len(source_ids)
                and all(source.get("manifest_sha256") for source in sources)
            )
            parser_complete = all(parser.get(key) for key in (
                "module", "callable", "version", "version_source"))
            ok = item.get("ok") is True and source_complete and parser_complete
            if not ok:
                if item.get("ok") is True and not source_complete:
                    reason = "calculator source identities/hashes are incomplete"
                elif item.get("ok") is True and not parser_complete:
                    reason = "calculator parser identity/version is incomplete"
                else:
                    reason = item.get("error") or "calculator failed closed"
                blocking.append(f"{kind}: {_safe_text(reason)}")
            values = []
            for key, label, unit in (
                ("gamma_jm2", "Surface energy", "J/m²"),
                ("binding_energy_eV", "Binding energy", "eV"),
                ("formation_energy_eV", "Formation energy", "eV"),
                ("solvation_energy_eV", "Solvation energy", "eV"),
            ):
                if key in item:
                    value = _public_value(item.get(key))
                    values.append({"key": key, "label": label, "value": value,
                                   "display": _display(value, spec.precision), "unit": unit})
            rows.append({
                "kind": kind, "available": ok,
                "status": "available" if ok else "unavailable",
                "parser": parser,
                "source_ids": source_ids,
                "sources": sources,
                "summary": _safe_text(item.get("summary") or item.get("error")),
                "values": values, "result": public,
            })
    available = sum(row["available"] is True for row in rows)
    payload = {
        "schema": VIEW_SCHEMA, "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(), "spec_sha256": spec.semantic_sha256,
        "scientific_status": "verified" if available and not blocking else
                             "unverified" if available else "blocked",
        "capability_status": "available" if available else
                             "missing_prerequisite" if missing and not blocking else "unavailable",
        "available": bool(available), "rows": rows,
        "missing": list(dict.fromkeys(missing)),
        "blocking": list(dict.fromkeys(blocking)), "warnings": [],
        "next_action": ("Inspect calculator operands and source hashes." if available else
                        "Create or complete a manifest-bound calculator pair, then refresh."),
        "denominator": {
            "resolved_targets": len(targets), "calculator_kinds": 3,
            "attempted_results": len(rows), "available_results": available,
            "blocked_results": len(rows) - available, "visible_rows": len(rows),
        },
    }
    payload["data_fingerprint"] = _canonical_hash(payload)
    return payload


__all__ = [
    "MAX_EVIDENCE_FILES", "MAX_EVIDENCE_FILE_BYTES", "MAX_EVIDENCE_LINES",
    "MAX_EVIDENCE_TOTAL_BYTES", "MAX_XDATCAR_FRAMES",
    "SourceSnapshot", "SourceSnapshotBudget", "SourceSnapshotChanged", "VIEW_SCHEMA",
    "build_property_view", "build_task_analysis_view", "capture_source_snapshot",
    "resolve_project_targets", "source_identity", "value_provenance",
    "verify_neb_endpoint_record",
]
