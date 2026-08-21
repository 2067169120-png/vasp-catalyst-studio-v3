"""Unified report-workbench orchestration.

This module deliberately owns the stateful parts which do not belong in the
renderer: opaque previews, preview-to-publish binding, monotonic revisions and
the local revision index.  Scientific data extraction remains an injected host
operation so the service can be used by the web, Tk and automation adapters
without importing a GUI module.

The public workbench projections never expose local paths.  Full paths remain
in the local history index because they are required to re-open and audit a
published bundle on the same workstation.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import ntpath
import os
import posixpath
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

from vcstudio.shared.credential_classifier import (
    contains_local_path,
    is_sensitive_key,
    looks_like_credential,
)

if TYPE_CHECKING:
    from vcstudio.project.report_contracts import ReportSpec


PREVIEW_SCHEMA = "vcstudio.report-preview/v1"
PREVIEW_TOKEN_SCHEMA = "vcstudio.report-preview-token/v1"
REVISION_SCHEMA = "vcstudio.report-revision/v1"
HISTORY_SCHEMA = "vcstudio.report-history/v1"
REVISION_JOURNAL_SCHEMA = "vcstudio.report-revision-journal/v1"
DESTINATION_RESERVATION_SCHEMA = "vcstudio.report-destination-reservation/v1"
DEFAULT_PREVIEW_LIMIT = 64
DEFAULT_PREVIEW_TTL_SECONDS = 30 * 60
_PREVIEW_CLEANUP_AUDIT_LIMIT = 64

_LOGGER = logging.getLogger(__name__)

_OPERATION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TILDE_PATH = re.compile(r"(?<![A-Za-z0-9_])~[\\/]")
_PATH_KEYS = frozenset({
    "path", "root", "dir", "directory", "locator", "source_job",
    "destination", "out_dir", "temp_root", "figure_dir", "figures_dir",
    "recovery_path", "rollback_path", "manifest_path",
})
_PUBLIC_PUBLISH_DROP_KEYS = frozenset({
    "assets", "figures", "out_dir", "temp_root", "figure_dir", "figures_dir",
    "destination", "recovery_record", "recovery_path", "rollback",
})
_BINDING_KEYS = (
    "project_id",
    "spec_sha256",
    "snapshot_sha256",
    "validation_sha256",
    "report_model_sha256",
    "base_revision",
    "base_manifest_sha256",
)
_JOURNAL_STATES = frozenset({
    "history_prepared",
    "marker_failed",
    "marker_ready",
    "complete",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json_file(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write the mutable history index through its dedicated test seam."""

    _atomic_json_file(path, value)


def _atomic_transaction_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write durable transaction records independently from the history index.

    A history-index failure must not prevent the revision journal from recording
    that the host marker already committed.  Keeping this as a separate seam is
    also important for recovery fault injection: failing ``history.json`` does
    not pretend the whole project volume disappeared.
    """

    _atomic_json_file(path, value)


@contextlib.contextmanager
def _exclusive_file_lock(path: Path, *, dir_fd: int | None = None):
    """Take a no-follow, cross-process byte lock on one regular file.

    ``dir_fd`` lets security-sensitive callers resolve the lock name relative
    to an already authenticated directory inode.  Windows callers instead
    open the path with ``FILE_FLAG_OPEN_REPARSE_POINT`` and without delete
    sharing so a pre-positioned junction/symlink is never followed and the
    containing directory cannot be renamed while the lock handle is live.
    """

    if dir_fd is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        if dir_fd is not None:  # pragma: no cover - Windows has no dir_fd support
            raise ValueError("directory-relative locks are unsupported on Windows")
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
        get_info.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        raw_handle = create_file(
            str(path), 0x80000000 | 0x40000000, 0x1 | 0x2, None, 4,
            0x80 | 0x00200000, None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if raw_handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "unable to open lock without following reparse points")
        info = _ByHandleFileInformation()
        try:
            if not get_info(raw_handle, ctypes.byref(info)):
                raise OSError(ctypes.get_last_error(), "unable to inspect lock handle")
            if info.dwFileAttributes & (0x400 | 0x10):
                raise ValueError("lock path must be a regular non-reparse file")
            descriptor = msvcrt.open_osfhandle(
                int(raw_handle), os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
            raw_handle = None
        finally:
            if raw_handle not in (None, invalid_handle):
                close_handle(raw_handle)
    else:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.fspath(path), flags, 0o600, dir_fd=dir_fd)
    try:
        opened = os.fstat(descriptor)
        named = (
            os.lstat(path) if dir_fd is None
            else os.stat(os.fspath(path), dir_fd=dir_fd, follow_symlinks=False)
        )
    except Exception:
        os.close(descriptor)
        raise
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (int(named.st_dev), int(named.st_ino))
        != (int(opened.st_dev), int(opened.st_ino))
    ):
        os.close(descriptor)
        raise ValueError("lock path changed during no-follow open")
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    windows_overlap = None
    windows_locked = False
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class _Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            lock_file = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
            lock_file.argtypes = (
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
            )
            lock_file.restype = wintypes.BOOL
            windows_overlap = _Overlapped()
            os_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
            if not lock_file(os_handle, 0x2, 0, 1, 0, ctypes.byref(windows_overlap)):
                raise OSError(ctypes.get_last_error(), "unable to acquire lock file")
            windows_locked = True
        else:  # pragma: no cover - exercised by the Linux CI matrix
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                if windows_locked:
                    import ctypes
                    import msvcrt
                    from ctypes import wintypes

                    unlock_file = ctypes.WinDLL("kernel32", use_last_error=True).UnlockFileEx
                    unlock_file.argtypes = (
                        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                        wintypes.DWORD, ctypes.c_void_p,
                    )
                    unlock_file.restype = wintypes.BOOL
                    os_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
                    unlock_file(os_handle, 0, 1, 0, ctypes.byref(windows_overlap))
            else:  # pragma: no cover - exercised by the Linux CI matrix
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _empty_history(project_id: str) -> dict[str, Any]:
    return {
        "schema": HISTORY_SCHEMA,
        "project_id": project_id,
        "generation": 0,
        "reports": {},
    }


def _validate_history(value: Any, project_id: str) -> dict[str, Any]:
    if value is None:
        return _empty_history(project_id)
    if not isinstance(value, Mapping):
        raise RuntimeError("report history must be a JSON object")
    payload = copy.deepcopy(dict(value))
    if payload.get("schema") != HISTORY_SCHEMA:
        raise RuntimeError("unsupported report history schema")
    if str(payload.get("project_id") or "") != project_id:
        raise RuntimeError("report history project binding mismatch")
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise RuntimeError("report history generation is invalid")
    reports = payload.get("reports")
    if not isinstance(reports, dict):
        raise RuntimeError("report history reports must be an object")
    for raw_report_id, raw_lineage in reports.items():
        report_id = str(raw_report_id or "")
        if not report_id or not _OPERATION_TOKEN.fullmatch(report_id):
            raise RuntimeError("report history contains an invalid report id")
        if not isinstance(raw_lineage, Mapping):
            raise RuntimeError("report history lineage must be an object")
        lineage = dict(raw_lineage)
        if str(lineage.get("report_id") or "") != report_id:
            raise RuntimeError("report history lineage binding mismatch")
        preset_id = str(lineage.get("preset_id") or "").strip()
        if not _OPERATION_TOKEN.fullmatch(preset_id):
            raise RuntimeError("report history lineage preset is invalid")
        latest_sequence = lineage.get("latest_sequence")
        if (isinstance(latest_sequence, bool)
                or not isinstance(latest_sequence, int)
                or latest_sequence < 0):
            raise RuntimeError("report history latest sequence is invalid")
        latest_manifest = lineage.get("latest_manifest_sha256")
        if latest_manifest is not None and not _HASH.fullmatch(str(latest_manifest)):
            raise RuntimeError("report history latest manifest hash is invalid")
        revisions = lineage.get("revisions")
        if not isinstance(revisions, list):
            raise RuntimeError("report history revisions must be an array")
        previous_sequence = 0
        previous_manifest = None
        for raw_entry in revisions:
            if not isinstance(raw_entry, Mapping):
                raise RuntimeError("report history revision must be an object")
            entry = dict(raw_entry)
            if entry.get("schema") != REVISION_SCHEMA:
                raise RuntimeError("report history revision schema is invalid")
            if str(entry.get("report_id") or "") != report_id:
                raise RuntimeError("report history revision binding mismatch")
            entry_preset = entry.get("preset_id")
            if (
                entry_preset is not None
                and str(entry_preset or "") != preset_id
            ):
                raise RuntimeError("report history revision preset binding mismatch")
            sequence = entry.get("sequence")
            if (isinstance(sequence, bool) or not isinstance(sequence, int)
                    or sequence != previous_sequence + 1):
                raise RuntimeError("report history revision sequence is invalid")
            expected_revision_id = f"{report_id}-r{sequence:04d}"
            if str(entry.get("revision_id") or "") != expected_revision_id:
                raise RuntimeError("report history revision id is invalid")
            parent = entry.get("parent_manifest_sha256")
            if parent != previous_manifest:
                raise RuntimeError("report history parent manifest binding is invalid")
            for field in (
                "spec_sha256", "snapshot_sha256", "validation_sha256",
                "report_model_sha256", "manifest_sha256",
            ):
                if not _HASH.fullmatch(str(entry.get(field) or "")):
                    raise RuntimeError(f"report history {field} is invalid")
            created_at = str(entry.get("created_at_utc") or "").strip()
            try:
                created = datetime.fromisoformat(created_at)
            except ValueError as exc:
                raise RuntimeError(
                    "report history revision timestamp is invalid"
                ) from exc
            offset = created.utcoffset() if created.tzinfo is not None else None
            if offset is None or offset.total_seconds() != 0:
                raise RuntimeError("report history revision timestamp is invalid")
            if not os.path.isabs(str(entry.get("manifest") or "")):
                raise RuntimeError("report history manifest path is invalid")
            files = entry.get("files")
            if not isinstance(files, Mapping) or not files:
                raise RuntimeError("report history files are invalid")
            if (
                any(str(key) not in {"html", "docx", "pdf"} for key in files)
                or any(not os.path.isabs(str(path or "")) for path in files.values())
            ):
                raise RuntimeError("report history file path is invalid")
            if not os.path.isabs(str(entry.get("model_file") or "")):
                raise RuntimeError("report history model path is invalid")
            contract_files = entry.get("contract_files")
            if (not isinstance(contract_files, Mapping)
                    or set(contract_files) != {"spec", "snapshot", "validation"}
                    or any(not os.path.isabs(str(path or ""))
                           for path in contract_files.values())):
                raise RuntimeError("report history contract paths are invalid")
            transaction_journal = entry.get("transaction_journal")
            if (
                transaction_journal is not None
                and not os.path.isabs(str(transaction_journal or ""))
            ):
                raise RuntimeError("report history transaction journal is invalid")
            artifact_status = entry.get("artifact_status")
            if artifact_status not in {"ready", "generated_unrecorded"}:
                raise RuntimeError("report history artifact status is invalid")
            if entry.get("scientific_status") not in {
                "final", "diagnostic", "draft"
            }:
                raise RuntimeError("report history scientific status is invalid")
            qualification = str(entry.get("scientific_qualification") or "")
            if not _OPERATION_TOKEN.fullmatch(qualification):
                raise RuntimeError("report history scientific qualification is invalid")
            error = entry.get("error")
            if error is not None and not isinstance(error, str):
                raise RuntimeError("report history revision error is invalid")
            if artifact_status == "ready" and error is not None:
                raise RuntimeError("ready report history revision cannot contain an error")
            previous_sequence = sequence
            previous_manifest = str(entry["manifest_sha256"])
        if latest_sequence != previous_sequence or latest_manifest != previous_manifest:
            raise RuntimeError("report history lineage head is inconsistent")
    return payload


def _read_history(path: Path, project_id: str) -> dict[str, Any]:
    if not path.is_file():
        return _empty_history(project_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a corrupt audit index must fail closed
        raise RuntimeError(f"cannot read report history: {exc}") from exc
    return _validate_history(value, project_id)


def _lineage_id(project_id: str, spec: Mapping[str, Any]) -> str:
    scope = spec.get("scope") if isinstance(spec.get("scope"), Mapping) else {}
    identity = {
        "project_id": project_id,
        "preset_id": str(spec.get("preset_id") or ""),
        "scope_kind": str(scope.get("kind") or "project"),
        "project_ids": list(scope.get("project_ids") or [project_id]),
    }
    return "report-" + _sha256_json(identity)[:24]


def _history_paths(project_root: os.PathLike[str] | str) -> tuple[Path, Path]:
    directory = Path(project_root).expanduser().resolve() / ".vcstudio" / "reports"
    return directory / "history.json", directory / ".history.lock"


def _revision_journal_path(
    project_root: os.PathLike[str] | str, revision_id: str
) -> Path:
    identifier = str(revision_id or "").strip()
    if not _OPERATION_TOKEN.fullmatch(identifier):
        raise RuntimeError("report revision journal id is invalid")
    directory = (
        Path(project_root).expanduser().resolve()
        / ".vcstudio"
        / "reports"
        / "transactions"
    )
    return directory / f"{identifier}.json"


def _normalized_key(value: Any) -> tuple[str, frozenset[str]]:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
    return normalized, frozenset(part for part in normalized.split("_") if part)


def _sensitive_public_key(value: Any) -> bool:
    return is_sensitive_key(value)


def _path_public_key(value: Any) -> bool:
    normalized, _ = _normalized_key(value)
    return bool(
        normalized in _PATH_KEYS
        or normalized.endswith(("_path", "_root", "_dir", "_directory", "_locator"))
    )


def _unsafe_public_string(value: str) -> bool:
    text = str(value or "")
    return bool(
        contains_local_path(text)
        or _TILDE_PATH.search(text)
        or looks_like_credential(text)
    )


def _public_value(value: Any) -> Any:
    """Return a recursive browser projection without locators or credentials."""

    if isinstance(value, Mapping):
        result = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if (
                _path_public_key(key)
                or _sensitive_public_key(key)
                or _unsafe_public_string(key)
            ):
                continue
            result[key] = _public_value(item)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_public_value(item) for item in value]
    if isinstance(value, os.PathLike):
        return "<local-path-redacted>"
    if isinstance(value, str):
        return "<sensitive-value-redacted>" if looks_like_credential(value) else (
            "<local-path-redacted>" if _unsafe_public_string(value) else value
        )
    return value


def _public_files(files: Any) -> dict[str, Any]:
    if not isinstance(files, Mapping):
        return {}
    result = {}
    for fmt, raw in files.items():
        path = str(raw or "")
        name = posixpath.basename(ntpath.basename(path))
        result[str(fmt)] = {
            "name": name,
            "available": bool(path and os.path.isfile(path)),
        }
    return result


def _public_status_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project marker-backed status while retaining private audit locators."""

    result = copy.deepcopy(dict(value))
    result["files"] = _public_files(result.get("files"))
    projected = _public_value(result)
    return dict(projected) if isinstance(projected, Mapping) else {}


def _public_publish_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project an entire publish result, not only its conventional file fields."""

    result = copy.deepcopy(dict(value))
    result["files"] = _public_files(result.get("files"))
    result["contract_files"] = _public_files(result.get("contract_files"))
    for field in ("model_file", "manifest"):
        if result.get(field):
            path = str(result[field])
            result[field] = {
                "name": posixpath.basename(ntpath.basename(path)),
                "available": os.path.isfile(path),
            }
    result.pop("marker", None)
    for key in _PUBLIC_PUBLISH_DROP_KEYS:
        result.pop(key, None)
    projected = _public_value(result)
    return dict(projected) if isinstance(projected, Mapping) else {}


def _same_path(left: os.PathLike[str] | str, right: os.PathLike[str] | str) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right)))


def _canonical_project_paths(
    project_path: os.PathLike[str] | str,
    project_root: os.PathLike[str] | str,
) -> tuple[str, str]:
    """Return one physical project-instance identity without hashing it as science.

    Project UUIDs and portable scientific identifiers intentionally survive a
    copied workspace.  A preview, however, is an operation against one concrete
    instance and therefore freezes both its resolved root and project locator.
    Relative locators are resolved against a matching existing root when the
    host supplies one (the common ``project.yaml`` case).
    """

    raw_root = str(project_root or "").strip()
    raw_path = str(project_path or "").strip()
    if not raw_root or not raw_path:
        raise RuntimeError("report project instance identity is incomplete")
    root = Path(raw_root).expanduser().resolve()
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        direct = candidate.resolve()
        rooted = (root / candidate.name).resolve()
        resolved = direct if direct.exists() or len(candidate.parts) > 1 else rooted
    return str(resolved), str(root)


def _project_instance_sha256(
    project_path: os.PathLike[str] | str,
    project_root: os.PathLike[str] | str,
) -> str:
    canonical_path, canonical_root = _canonical_project_paths(
        project_path, project_root
    )
    return _sha256_json({
        "project_path": canonical_path,
        "project_root": canonical_root,
    })


def _manifest_member_path(
    manifest_path: Path,
    record: Any,
    *,
    digest_field: str = "sha256",
) -> Path:
    if not isinstance(record, Mapping):
        raise RuntimeError("report manifest file record is invalid")
    relative = str(record.get("path") or "")
    path_parts = tuple(part for part in re.split(r"[\\/]", relative) if part)
    if (
        not relative
        or os.path.isabs(relative)
        or ntpath.isabs(relative)
        or relative in {".", ".."}
        or any(part in {".", ".."} for part in path_parts)
        or ntpath.splitdrive(relative)[0]
    ):
        raise RuntimeError("report manifest contains an unsafe file path")
    base = manifest_path.parent.resolve()
    candidate = (base / relative).resolve()
    try:
        if os.path.commonpath((str(base), str(candidate))) != str(base):
            raise RuntimeError("report manifest file escapes its bundle")
    except ValueError as exc:
        raise RuntimeError("report manifest file escapes its bundle") from exc
    expected = str(record.get(digest_field) or "").lower()
    size = record.get("size")
    if not _HASH.fullmatch(expected):
        raise RuntimeError("report manifest file hash is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RuntimeError("report manifest file size is invalid")
    if not candidate.is_file():
        raise RuntimeError("report manifest member is missing")
    if candidate.stat().st_size != size or _sha256_file(candidate) != expected:
        raise RuntimeError("report manifest member hash mismatch")
    return candidate


def _validated_history_bundle(
    entry: Mapping[str, Any], lineage: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify every portable bundle member before history may claim it is current."""

    manifest_path = Path(str(entry.get("manifest") or ""))
    expected_manifest = str(entry.get("manifest_sha256") or "").lower()
    if (
        not manifest_path.is_absolute()
        or not manifest_path.is_file()
        or not _HASH.fullmatch(expected_manifest)
        or _sha256_file(manifest_path) != expected_manifest
    ):
        raise RuntimeError("report manifest is missing or changed")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt evidence must fail closed
        raise RuntimeError("report manifest cannot be read") from exc
    if not isinstance(manifest, Mapping):
        raise RuntimeError("report manifest must be an object")
    manifest = dict(manifest)
    if manifest.get("schema") != "vcstudio.paper-report.bundle/v2":
        raise RuntimeError("report manifest schema is invalid")
    if manifest.get("artifact_status") != "complete":
        raise RuntimeError("report manifest artifact status is invalid")
    revision = manifest.get("revision")
    if not isinstance(revision, Mapping):
        raise RuntimeError("report manifest revision is missing")
    for field in (
        "schema", "report_id", "revision_id", "sequence",
        "parent_manifest_sha256", "spec_sha256", "snapshot_sha256",
        "validation_sha256", "report_model_sha256", "created_at_utc",
    ):
        if revision.get(field) != entry.get(field):
            raise RuntimeError("report manifest revision binding mismatch")
    if str(manifest.get("report_id") or "") != str(entry.get("report_id") or ""):
        raise RuntimeError("report manifest lineage binding mismatch")
    if str(lineage.get("report_id") or "") != str(entry.get("report_id") or ""):
        raise RuntimeError("report history lineage binding mismatch")
    if str(manifest.get("preset_id") or "") != str(lineage.get("preset_id") or ""):
        raise RuntimeError("report manifest preset binding mismatch")
    if str(manifest.get("report_model_sha256") or "").lower() != str(
        entry.get("report_model_sha256") or ""
    ).lower():
        raise RuntimeError("report manifest model binding mismatch")
    if str(manifest.get("report_kind") or "") != str(
        entry.get("scientific_status") or ""
    ):
        raise RuntimeError("report manifest kind binding mismatch")
    for field in ("scientific_status", "scientific_qualification"):
        if str(manifest.get(field) or "") != str(entry.get(field) or ""):
            raise RuntimeError(f"report manifest {field} binding mismatch")

    manifest_files = manifest.get("files")
    entry_files = entry.get("files")
    if not isinstance(manifest_files, Mapping) or not manifest_files:
        raise RuntimeError("report manifest formats are missing")
    if not isinstance(entry_files, Mapping) or set(entry_files) != set(manifest_files):
        raise RuntimeError("report history format binding mismatch")
    formats = manifest.get("formats")
    if (
        not isinstance(formats, list)
        or any(
            not isinstance(item, str) or item not in {"html", "docx", "pdf"}
            for item in formats
        )
        or len(formats) != len(set(formats))
        or set(formats) != set(manifest_files)
    ):
        raise RuntimeError("report manifest format declaration is invalid")
    for fmt, record in manifest_files.items():
        candidate = _manifest_member_path(manifest_path, record)
        if not _same_path(candidate, str(entry_files.get(fmt) or "")):
            raise RuntimeError("report history format path mismatch")

    model_record = manifest.get("model_file")
    model_path = _manifest_member_path(manifest_path, model_record)
    if str(manifest.get("model_sha256") or "").lower() != str(
        model_record.get("sha256") if isinstance(model_record, Mapping) else ""
    ).lower():
        raise RuntimeError("report manifest frozen model hash mismatch")
    if not _same_path(model_path, str(entry.get("model_file") or "")):
        raise RuntimeError("report history model path mismatch")

    contracts = manifest.get("contracts")
    entry_contracts = entry.get("contract_files")
    required_contracts = {"spec", "snapshot", "validation"}
    if (
        not isinstance(contracts, Mapping)
        or set(contracts) != required_contracts
        or not isinstance(entry_contracts, Mapping)
        or set(entry_contracts) != required_contracts
    ):
        raise RuntimeError("report manifest contract sidecars are incomplete")
    for key in sorted(required_contracts):
        semantic_digest = str(contracts[key].get("sha256") or "").lower()
        if semantic_digest != str(entry.get(f"{key}_sha256") or "").lower():
            raise RuntimeError("report history contract semantic hash mismatch")
        candidate = _manifest_member_path(
            manifest_path, contracts[key], digest_field="file_sha256")
        if not _same_path(candidate, str(entry_contracts.get(key) or "")):
            raise RuntimeError("report history contract path mismatch")

    assets = manifest.get("assets") or []
    if not isinstance(assets, list):
        raise RuntimeError("report manifest assets are invalid")
    for record in assets:
        _manifest_member_path(manifest_path, record)
    return manifest


def _output_has_revision_collision(
    out_dir: os.PathLike[str] | str,
    *,
    revision_stem: str,
    revision_id: str,
) -> bool:
    """Detect an orphaned or foreign claim on one exact revision destination."""

    destination = Path(str(out_dir or "")).expanduser()
    if not destination.is_dir():
        return False
    safe_stem = (
        revision_stem
        if revision_stem == ntpath.basename(revision_stem)
        and revision_stem == posixpath.basename(revision_stem)
        else ""
    )
    if safe_stem:
        prefix = f"{safe_stem}."
        if any(child.is_file() and child.name.startswith(prefix)
               for child in destination.iterdir()):
            return True
    for manifest_path in destination.glob("*.manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable candidate is not attributable
            continue
        if not isinstance(manifest, Mapping):
            continue
        revision = manifest.get("revision")
        manifest_revision_id = (
            str(revision.get("revision_id") or "")
            if isinstance(revision, Mapping) else ""
        )
        if manifest_revision_id == revision_id:
            return True
    return False


def _destination_paths(
    out_dir: os.PathLike[str] | str,
) -> tuple[Path, Path]:
    raw = str(out_dir or "").strip()
    if not raw:
        raise ValueError("report output directory is required")
    destination = Path(raw).expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise NotADirectoryError(f"report output is not a directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    return destination, destination / ".vcstudio-report-destination.lock"


def _destination_reservation_path(destination: Path, revision_stem: str) -> Path:
    safe_stem = str(revision_stem or "")
    if (
        not safe_stem
        or safe_stem != ntpath.basename(safe_stem)
        or safe_stem != posixpath.basename(safe_stem)
        or "\x00" in safe_stem
    ):
        raise ValueError("report revision stem must be a safe file name")
    key = _sha256_json({"revision_stem": safe_stem})[:24]
    return destination / f".vcstudio-report-reservation-{key}.json"


def _safe_revision_stem(value: Any, sequence: int) -> str:
    """Mirror the host filename contract while preserving the revision suffix."""

    suffix = f"_r{sequence:04d}"
    base = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "report")
    ).strip(" ._")
    base = (base or "report")[:96 - len(suffix)].rstrip(" ._") or "report"
    return f"{base}{suffix}"


def _destination_content_sha256(destination: Path) -> str:
    """Hash user-visible destination bytes, excluding persistent lock/CAS files."""

    records: list[dict[str, Any]] = []
    for path in sorted(destination.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        name = path.name
        if (
            name == ".vcstudio-report-destination.lock"
            or name.startswith(".vcstudio-report-reservation-")
            or (name.startswith(".") and name.endswith(".publish.lock"))
        ):
            continue
        records.append({
            "path": path.relative_to(destination).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        })
    return _sha256_json(records)


def _reservation_binding(
    project_id: str,
    project_instance_sha256: str,
    revision_stem: str,
    revision: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "project_instance_sha256": project_instance_sha256,
        "report_id": str(revision.get("report_id") or ""),
        "revision_id": str(revision.get("revision_id") or ""),
        "sequence": revision.get("sequence"),
        "revision_stem": revision_stem,
        "parent_manifest_sha256": revision.get("parent_manifest_sha256"),
        "spec_sha256": revision.get("spec_sha256"),
        "snapshot_sha256": revision.get("snapshot_sha256"),
        "validation_sha256": revision.get("validation_sha256"),
        "report_model_sha256": revision.get("report_model_sha256"),
    }


def _reclaimable_destination_reservation(
    reservation_path: Path,
    destination: Path,
    expected_binding: Mapping[str, Any],
) -> bool:
    try:
        value = json.loads(reservation_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            return False
        payload = dict(value)
        if payload.get("schema") != DESTINATION_RESERVATION_SCHEMA:
            return False
        if payload.get("state") not in {"reserved", "render_failed_clean"}:
            return False
        if any(payload.get(key) != expected
               for key, expected in expected_binding.items()):
            return False
        baseline = str(payload.get("baseline_sha256") or "")
        if not _HASH.fullmatch(baseline):
            return False
        if _destination_content_sha256(destination) != baseline:
            return False
        reservation_path.unlink()
        return True
    except Exception:  # noqa: BLE001 - malformed/locked claims fail closed
        return False


def _reserve_revision_destination(
    destination: Path,
    *,
    revision_stem: str,
    project_id: str,
    project_instance_sha256: str,
    revision: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]] | None:
    """CAS one immutable destination stem while its directory lock is held."""

    revision_id = str(revision.get("revision_id") or "")
    reservation_path = _destination_reservation_path(destination, revision_stem)
    expected_binding = _reservation_binding(
        project_id, project_instance_sha256, revision_stem, revision
    )
    if _output_has_revision_collision(
        destination,
        revision_stem=revision_stem,
        revision_id=revision_id,
    ):
        return None
    if reservation_path.exists() and not _reclaimable_destination_reservation(
        reservation_path,
        destination,
        expected_binding,
    ):
        return None
    if _output_has_revision_collision(
        destination,
        revision_stem=revision_stem,
        revision_id=revision_id,
    ):
        return None
    payload = {
        "schema": DESTINATION_RESERVATION_SCHEMA,
        "state": "reserved",
        **expected_binding,
        "created_at_utc": _utc_now(),
        "baseline_sha256": _destination_content_sha256(destination),
        "manifest_sha256": None,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(reservation_path, flags, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical_bytes(payload) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return reservation_path, payload


def _fail_destination_reservation(
    reservation_path: Path,
    payload: Mapping[str, Any],
    destination: Path,
    *,
    error: Exception | str,
) -> None:
    failed = copy.deepcopy(dict(payload))
    current = _destination_content_sha256(destination)
    clean = current == str(payload.get("baseline_sha256") or "")
    failed.update({
        "state": "render_failed_clean" if clean else "render_failed_partial",
        "updated_at_utc": _utc_now(),
        "error": str(error),
        "destination_sha256": current,
    })
    _atomic_transaction_json(reservation_path, failed)


def _finish_destination_reservation(
    reservation_path: Path,
    payload: Mapping[str, Any],
    *,
    manifest_sha256: str,
) -> None:
    committed = copy.deepcopy(dict(payload))
    committed.update({
        "state": "bundle_committed",
        "manifest_sha256": manifest_sha256,
        "updated_at_utc": _utc_now(),
    })
    _atomic_transaction_json(reservation_path, committed)


def _write_revision_journal(
    project_root: os.PathLike[str] | str,
    project_id: str,
    entry: Mapping[str, Any],
    *,
    state: str,
    marker_recorded: bool,
    history_ready_recorded: bool,
    error: str | None = None,
) -> Path:
    if state not in _JOURNAL_STATES:
        raise RuntimeError("report revision journal state is invalid")
    revision_id = str(entry.get("revision_id") or "")
    path = _revision_journal_path(project_root, revision_id)
    intent = copy.deepcopy(dict(entry))
    intent["artifact_status"] = "ready"
    intent["error"] = None
    payload = {
        "schema": REVISION_JOURNAL_SCHEMA,
        "project_id": project_id,
        "report_id": str(entry.get("report_id") or ""),
        "revision_id": revision_id,
        "sequence": entry.get("sequence"),
        "manifest_sha256": str(entry.get("manifest_sha256") or ""),
        "state": state,
        "marker_recorded": marker_recorded,
        "history_ready_recorded": history_ready_recorded,
        "history_entry": intent,
        "intent_sha256": _sha256_json(intent),
        "updated_at_utc": _utc_now(),
        "error": error,
    }
    _atomic_transaction_json(path, payload)
    return path


def _read_revision_journal(
    project_root: os.PathLike[str] | str,
    project_id: str,
    entry: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    declared = str(entry.get("transaction_journal") or "")
    if not declared:
        return None, None
    try:
        expected = _revision_journal_path(
            project_root, str(entry.get("revision_id") or "")
        )
        if not os.path.isabs(declared) or not _same_path(declared, expected):
            raise RuntimeError("report revision journal path is invalid")
        if not expected.is_file():
            raise RuntimeError("report revision journal is missing")
        value = json.loads(expected.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise RuntimeError("report revision journal must be an object")
        payload = dict(value)
        if payload.get("schema") != REVISION_JOURNAL_SCHEMA:
            raise RuntimeError("report revision journal schema is invalid")
        bindings = {
            "project_id": project_id,
            "report_id": str(entry.get("report_id") or ""),
            "revision_id": str(entry.get("revision_id") or ""),
            "sequence": entry.get("sequence"),
            "manifest_sha256": str(entry.get("manifest_sha256") or ""),
        }
        if any(payload.get(key) != expected_value
               for key, expected_value in bindings.items()):
            raise RuntimeError("report revision journal binding mismatch")
        if payload.get("state") not in _JOURNAL_STATES:
            raise RuntimeError("report revision journal state is invalid")
        if not isinstance(payload.get("marker_recorded"), bool):
            raise RuntimeError("report revision journal marker state is invalid")
        if not isinstance(payload.get("history_ready_recorded"), bool):
            raise RuntimeError("report revision journal history state is invalid")
        expected_flags = {
            "history_prepared": (False, False),
            "marker_failed": (False, False),
            "marker_ready": (True, False),
            "complete": (True, True),
        }[str(payload["state"])]
        if (
            payload["marker_recorded"],
            payload["history_ready_recorded"],
        ) != expected_flags:
            raise RuntimeError("report revision journal phase flags are inconsistent")
        journal_error = payload.get("error")
        if journal_error is not None and not isinstance(journal_error, str):
            raise RuntimeError("report revision journal error is invalid")
        intent = payload.get("history_entry")
        if not isinstance(intent, Mapping):
            raise RuntimeError("report revision journal intent is missing")
        intent = copy.deepcopy(dict(intent))
        if (
            intent.get("artifact_status") != "ready"
            or intent.get("error") is not None
            or payload.get("intent_sha256") != _sha256_json(intent)
        ):
            raise RuntimeError("report revision journal intent is invalid")
        current_intent = copy.deepcopy(dict(entry))
        current_intent["artifact_status"] = "ready"
        current_intent["error"] = None
        if _sha256_json(current_intent) != payload["intent_sha256"]:
            raise RuntimeError("report revision journal intent binding mismatch")
        payload["history_entry"] = intent
        return payload, None
    except Exception as exc:  # noqa: BLE001 - callers reconcile with the marker
        return None, str(exc)


def _marker_matches_revision(status: Any, entry: Mapping[str, Any]) -> bool:
    if not isinstance(status, Mapping):
        return False
    if (
        status.get("ok") is not True
        or status.get("artifact_current") is not True
        or status.get("artifact_status") != "ready"
    ):
        return False
    revision = status.get("revision")
    if not isinstance(revision, Mapping):
        return False
    for field in (
        "schema",
        "report_id",
        "revision_id",
        "sequence",
        "parent_manifest_sha256",
        "spec_sha256",
        "snapshot_sha256",
        "validation_sha256",
        "report_model_sha256",
        "created_at_utc",
    ):
        if revision.get(field) != entry.get(field):
            return False
    return True


@runtime_checkable
class ReportServiceHost(Protocol):
    """Narrow host boundary required by the renderer-independent service."""

    def _report_workbench_project_context(self, path: str) -> Mapping[str, Any]: ...

    def proj_report_capabilities(self) -> Mapping[str, Any]: ...

    def _proj_report_status_for_path(self, path: str) -> Mapping[str, Any]: ...

    def _normalize_report_formats(self, formats: Any) -> tuple[str, ...]: ...

    def _report_workbench_build(
        self, path: str, spec: ReportSpec, temp_root: str
    ) -> dict[str, Any]: ...

    def _report_workbench_render_preview(
        self, model: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def _report_workbench_current_state(
        self, path: str, report_spec: Any = None
    ) -> Mapping[str, Any]: ...

    def _report_workbench_render_build(
        self,
        build: Mapping[str, Any],
        out_dir: str,
        *,
        stem: str | None,
        revision: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def _report_workbench_persist_build(
        self,
        build: Mapping[str, Any],
        rendered: Mapping[str, Any],
        *,
        revision: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def _report_workbench_validate_history_entry(
        self, entry: dict[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass
class _PreviewRecord:
    preview_id: str
    operation_id: str
    project_id: str
    project_path: str
    project_root: str
    report_id: str
    created_at_utc: str
    created_at: float
    expires_at: float
    base_revision: int
    base_manifest_sha256: str | None
    build: dict[str, Any]
    token: dict[str, Any]
    temp_root: str


class ReportService:
    """One orchestration seam for workbench, legacy GUI and automation reports."""

    def __init__(
        self,
        host: ReportServiceHost,
        *,
        preview_ttl_seconds: float = DEFAULT_PREVIEW_TTL_SECONDS,
        preview_limit: int = DEFAULT_PREVIEW_LIMIT,
        clock=time.time,
        temp_root: os.PathLike[str] | str | None = None,
    ) -> None:
        self._host: ReportServiceHost = host
        self._clock = clock
        self._ttl = float(preview_ttl_seconds)
        if self._ttl <= 0:
            raise ValueError("preview_ttl_seconds must be positive")
        self._preview_limit = int(preview_limit)
        if self._preview_limit <= 0:
            raise ValueError("preview_limit must be positive")
        self._temp_root = None if temp_root is None else str(temp_root)
        if self._temp_root is not None:
            Path(self._temp_root).mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._previews: dict[str, _PreviewRecord] = {}
        self._preview_cleanup_audit: list[dict[str, Any]] = []

    def _project_context(self, path: str) -> dict[str, Any]:
        context = self._host._report_workbench_project_context(path)
        if not isinstance(context, Mapping):
            raise RuntimeError("report project context is unavailable")
        project_id = str(context.get("project_id") or "")
        root = str(context.get("project_root") or "")
        project_path = str(context.get("project_path") or path or "")
        if not project_id or not root or not project_path:
            raise RuntimeError("report project identity is incomplete")
        canonical_path, canonical_root = _canonical_project_paths(project_path, root)
        normalized = dict(context)
        normalized.update({
            "project_id": project_id,
            "project_path": canonical_path,
            "project_root": canonical_root,
        })
        return normalized

    def catalog(self, capabilities: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from vcstudio.project.report_presets import report_workbench_catalog

        if capabilities is None:
            capabilities = self._host.proj_report_capabilities()
        return report_workbench_catalog(capabilities=capabilities)

    def bootstrap(self, path: str, preset_id: str | None = None) -> dict[str, Any]:
        try:
            context = self._project_context(str(path or "").strip())
            capabilities = self._host.proj_report_capabilities()
            catalog = self.catalog(capabilities)
            from vcstudio.project.report_presets import (
                get_report_preset,
                normalize_report_request,
            )

            request: dict[str, Any] = {}
            if preset_id:
                request["preset_id"] = preset_id
            preset = get_report_preset(preset_id or "scientific-review")
            available_formats = [
                fmt for fmt in preset["formats"]
                if isinstance((catalog.get("formats") or {}).get(fmt), Mapping)
                and catalog["formats"][fmt].get("available") is True
            ]
            if available_formats:
                request["formats"] = available_formats
            spec = normalize_report_request(
                request,
                project_id=context["project_id"],
                capabilities=capabilities,
            )
            history = self.history(path)
            return {
                "schema": "vcstudio.report-workbench-bootstrap/v1",
                "ok": True,
                "project_id": context["project_id"],
                "project": {
                    "id": context["project_id"],
                    "name": _public_value(str(context.get("project_name") or "")),
                },
                "catalog": catalog,
                "report_spec": spec.to_dict(),
                "status": _public_value(self.status(path)),
                "history": history,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - JSON-safe public boundary
            return {
                "schema": "vcstudio.report-workbench-bootstrap/v1",
                "ok": False,
                "project_id": None,
                "project": None,
                "catalog": self.catalog(),
                "report_spec": None,
                "status": None,
                "history": None,
                "error": _public_value(str(exc)),
            }

    def _cleanup_preview_record(self, record: _PreviewRecord, *, reason: str) -> None:
        try:
            shutil.rmtree(record.temp_root)
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort, but audited
            event = {
                "preview_id": record.preview_id,
                "reason": reason,
                "error_type": type(exc).__name__,
            }
            with self._lock:
                self._preview_cleanup_audit.append(event)
                del self._preview_cleanup_audit[:-_PREVIEW_CLEANUP_AUDIT_LIMIT]
            _LOGGER.warning(
                "report preview cleanup failed preview_id=%s reason=%s error_type=%s",
                record.preview_id,
                reason,
                type(exc).__name__,
            )

    def _cleanup_preview_records(
        self, records: list[tuple[_PreviewRecord, str]]
    ) -> None:
        for record, reason in records:
            self._cleanup_preview_record(record, reason=reason)

    def _expired_previews_locked(self, now: float) -> list[tuple[_PreviewRecord, str]]:
        expired: list[tuple[_PreviewRecord, str]] = []
        for preview_id, record in list(self._previews.items()):
            if record.expires_at <= now:
                expired.append((self._previews.pop(preview_id), "expired"))
        return expired

    def _cleanup_expired(self) -> None:
        with self._lock:
            expired = self._expired_previews_locked(float(self._clock()))
        self._cleanup_preview_records(expired)

    def _register_preview(self, record: _PreviewRecord) -> None:
        """Register one preview under the shared bounded store policy."""

        cleanup: list[tuple[_PreviewRecord, str]] = []
        with self._lock:
            cleanup.extend(self._expired_previews_locked(float(self._clock())))
            replaced = self._previews.pop(record.preview_id, None)
            if replaced is not None and replaced is not record:
                cleanup.append((replaced, "replaced"))
            while len(self._previews) >= self._preview_limit:
                oldest_id = min(
                    self._previews,
                    key=lambda item: (
                        self._previews[item].created_at,
                        self._previews[item].preview_id,
                    ),
                )
                cleanup.append((self._previews.pop(oldest_id), "capacity_eviction"))
            self._previews[record.preview_id] = record
        self._cleanup_preview_records(cleanup)

    def _discard_preview(self, record: _PreviewRecord, *, reason: str) -> None:
        removed = None
        with self._lock:
            if self._previews.get(record.preview_id) is record:
                removed = self._previews.pop(record.preview_id)
        if removed is not None:
            self._cleanup_preview_record(removed, reason=reason)

    def _preview_cleanup_events(self) -> tuple[dict[str, Any], ...]:
        """Return a path-free internal audit snapshot for cleanup failures."""

        with self._lock:
            return tuple(copy.deepcopy(self._preview_cleanup_audit))

    def _audit_history_entry(
        self,
        entry: Mapping[str, Any],
        lineage: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Prove a history entry against its bundle and host science contract."""

        _validated_history_bundle(entry, lineage)
        validator = getattr(
            self._host, "_report_workbench_validate_history_entry", None
        )
        if not callable(validator):
            raise RuntimeError("report history host validator is unavailable")
        audit = validator(copy.deepcopy(dict(entry)))
        if not isinstance(audit, Mapping):
            raise RuntimeError(
                "report history host validator returned an invalid result"
            )
        if audit.get("ok") is not True or audit.get("current") is not True:
            raise RuntimeError(
                str(audit.get("error") or "report history host audit failed")
            )
        for field in ("scientific_status", "scientific_qualification"):
            if str(audit.get(field) or "") != str(entry.get(field) or ""):
                raise RuntimeError(f"report history host {field} binding mismatch")
        return audit

    def _reconcile_history_locked(
        self,
        context: Mapping[str, Any],
        history_path: Path,
        history: dict[str, Any],
        marker_status: Any,
    ) -> dict[str, Any]:
        """Finalize durable journal intents while holding the project history lock.

        The journal supplies the immutable intended ``ready`` entry.  Recovery
        still requires both a byte-for-byte valid report bundle and the current
        host marker for that exact revision.  A forged journal, stale marker, or
        missing member therefore remains fail-closed as ``generated_unrecorded``.
        """

        project_root = str(context["project_root"])
        project_id = str(context["project_id"])
        recovered: list[tuple[dict[str, Any], dict[str, Any]]] = []
        history_recovered = 0
        for lineage in history["reports"].values():
            if not isinstance(lineage, dict):
                continue
            revisions = lineage.get("revisions")
            if not isinstance(revisions, list):
                continue
            for index, current_entry in enumerate(revisions):
                if not isinstance(current_entry, Mapping):
                    continue
                journal, _journal_error = _read_revision_journal(
                    project_root, project_id, current_entry
                )
                if journal is None or journal.get("state") == "marker_failed":
                    continue
                if (
                    current_entry.get("artifact_status") == "ready"
                    and journal.get("state") == "complete"
                ):
                    continue
                intent = journal.get("history_entry")
                if not isinstance(intent, Mapping):
                    continue
                intended_entry = copy.deepcopy(dict(intent))
                if not _marker_matches_revision(marker_status, intended_entry):
                    continue
                try:
                    self._audit_history_entry(intended_entry, lineage)
                except Exception:  # noqa: BLE001 - corrupt recovery evidence stays stale
                    continue
                if current_entry.get("artifact_status") != "ready":
                    revisions[index] = intended_entry
                    recovered.append((lineage, intended_entry))
                    history_recovered += 1
                elif journal.get("state") != "complete":
                    recovered.append((lineage, intended_entry))

        if history_recovered:
            history["generation"] += history_recovered
            _atomic_json(history_path, history)

        for _lineage, entry in recovered:
            with contextlib.suppress(Exception):
                _write_revision_journal(
                    project_root,
                    project_id,
                    entry,
                    state="complete",
                    marker_recorded=True,
                    history_ready_recorded=True,
                )
        return history

    def _history_base(
        self,
        project_root: str,
        project_id: str,
        report_id: str,
        *,
        project_path: str | None = None,
    ) -> tuple[int, str | None]:
        history_path, lock_path = _history_paths(project_root)
        # A first preview is observational: do not create .vcstudio/history or
        # even a lock file merely to discover that no revision exists yet.
        # A concurrent first publish is still caught by the publish-time CAS.
        if not history_path.is_file():
            return 0, None
        with _exclusive_file_lock(lock_path):
            history = _read_history(history_path, project_id)
            if project_path:
                marker_status = self._host._proj_report_status_for_path(project_path)
                history = self._reconcile_history_locked(
                    {
                        "project_id": project_id,
                        "project_root": project_root,
                        "project_path": project_path,
                    },
                    history_path,
                    history,
                    marker_status,
                )
            lineage = history["reports"].get(report_id) or {}
            sequence = lineage.get("latest_sequence", 0)
            manifest_sha256 = lineage.get("latest_manifest_sha256")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise RuntimeError("report history latest sequence is invalid")
            if manifest_sha256 is not None and not _HASH.fullmatch(str(manifest_sha256)):
                raise RuntimeError("report history latest manifest hash is invalid")
            return sequence, manifest_sha256

    @staticmethod
    def _operation_id(value: Any) -> str:
        candidate = str(value or "").strip()
        return candidate if _OPERATION_TOKEN.fullmatch(candidate) else uuid.uuid4().hex

    def _make_preview(
        self,
        path: str,
        request: Mapping[str, Any] | None,
        *,
        render_html: bool,
    ) -> tuple[_PreviewRecord, dict[str, Any] | None]:
        self._cleanup_expired()
        outer = dict(request or {})
        operation_id = self._operation_id(outer.pop("operation_id", None))
        spec_request = outer.pop("spec", None)
        if spec_request is None:
            spec_request = outer
        elif outer:
            raise ValueError(
                "workbench preview accepts only operation_id and spec in the outer request"
            )
        if not isinstance(spec_request, Mapping):
            raise TypeError("workbench report spec request must be an object")
        context = self._project_context(path)
        capabilities = self._host.proj_report_capabilities()
        from vcstudio.project.report_presets import normalize_report_request

        spec = normalize_report_request(
            spec_request,
            project_id=context["project_id"],
            capabilities=capabilities,
        )
        report_id = _lineage_id(context["project_id"], spec.to_dict())
        base_revision, base_manifest = self._history_base(
            context["project_root"],
            context["project_id"],
            report_id,
            project_path=context["project_path"],
        )
        temp_root = tempfile.mkdtemp(
            prefix="vcstudio-report-preview-", dir=self._temp_root
        )
        try:
            build = self._host._report_workbench_build(path, spec, temp_root)
            refs = build.get("contracts", {}).get("contract_refs") or {}
            spec_ref = refs.get("spec") if isinstance(refs, Mapping) else {}
            snapshot_ref = refs.get("snapshot") if isinstance(refs, Mapping) else {}
            validation_ref = refs.get("validation") if isinstance(refs, Mapping) else {}
            bindings = {
                "project_id": context["project_id"],
                "spec_sha256": str((spec_ref or {}).get("sha256") or ""),
                "snapshot_sha256": str((snapshot_ref or {}).get("sha256") or ""),
                "validation_sha256": str((validation_ref or {}).get("sha256") or ""),
                "report_model_sha256": str(build.get("report_model_sha256") or ""),
                "base_revision": base_revision,
                "base_manifest_sha256": base_manifest,
            }
            for key in ("spec_sha256", "snapshot_sha256", "validation_sha256",
                        "report_model_sha256"):
                if not _HASH.fullmatch(bindings[key]):
                    raise RuntimeError(f"report preview is missing a valid {key}")
            preview_id = uuid.uuid4().hex + uuid.uuid4().hex
            token = {
                "schema": PREVIEW_TOKEN_SCHEMA,
                "preview_id": preview_id,
                **bindings,
            }
            now = float(self._clock())
            record = _PreviewRecord(
                preview_id=preview_id,
                operation_id=operation_id,
                project_id=context["project_id"],
                project_path=context["project_path"],
                project_root=context["project_root"],
                report_id=report_id,
                created_at_utc=_utc_now(),
                created_at=now,
                expires_at=now + self._ttl,
                base_revision=base_revision,
                base_manifest_sha256=base_manifest,
                build=build,
                token=token,
                temp_root=temp_root,
            )
            preview_render = None
            if render_html:
                preview_render = self._host._report_workbench_render_preview(build["model"])
                if not isinstance(preview_render, Mapping):
                    raise RuntimeError("report preview renderer returned an invalid result")
            self._register_preview(record)
            return record, None if preview_render is None else dict(preview_render)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

    def preview(self, path: str, request: Mapping[str, Any] | None) -> dict[str, Any]:
        operation_id = self._operation_id(
            request.get("operation_id") if isinstance(request, Mapping) else None
        )
        try:
            prepared = dict(request or {})
            prepared["operation_id"] = operation_id
            record, rendered = self._make_preview(
                str(path or "").strip(), prepared, render_html=True
            )
            build = record.build
            contracts = build["contracts"]
            validation = contracts.get("validation") or {}
            checks = validation.get("checks") if isinstance(validation, Mapping) else []
            blocking = [
                _public_value(item) for item in checks or []
                if isinstance(item, Mapping)
                and item.get("status") not in {"pass", "not_applicable"}
                and (item.get("required") is True or item.get("severity") == "blocking")
            ]
            warnings = [
                _public_value(item) for item in checks or []
                if isinstance(item, Mapping)
                and item.get("status") == "warn"
                and item.get("severity") != "blocking"
            ]
            return {
                "schema": PREVIEW_SCHEMA,
                "ok": True,
                "operation_id": record.operation_id,
                "preview_id": record.preview_id,
                "project_id": record.project_id,
                "created_at_utc": record.created_at_utc,
                "expires_at_utc": datetime.fromtimestamp(
                    record.expires_at, timezone.utc
                ).replace(microsecond=0).isoformat(),
                "artifact_status": "preview",
                "scientific_status": build["report_kind"],
                "scientific_qualification": contracts["scientific_qualification"],
                "publication_gate_status": (
                    "eligible" if build["eligible_final"] else "blocked"
                ),
                "desired_report_kind": (
                    "final" if build["eligible_final"] else "diagnostic"
                ),
                "gate_reason": _public_value(str(build.get("gate_reason") or "")),
                "report_spec": _public_value(contracts["report_spec"]),
                "public_snapshot": _public_value(contracts["report_snapshot"]),
                "validation": _public_value(validation),
                "contract_refs": _public_value(contracts["contract_refs"]),
                "report_model_sha256": build["report_model_sha256"],
                "preview_token": dict(record.token),
                "base_revision": record.base_revision,
                "html": str((rendered or {}).get("html") or ""),
                "blocking": blocking,
                "warnings": warnings,
                "format_status": self._preview_format_status(build),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - JSON-safe public boundary
            return {
                "schema": PREVIEW_SCHEMA,
                "ok": False,
                "operation_id": operation_id,
                "preview_id": None,
                "project_id": None,
                "artifact_status": "failed",
                "scientific_status": None,
                "scientific_qualification": None,
                "publication_gate_status": "unknown",
                "desired_report_kind": None,
                "html": "",
                "blocking": [],
                "warnings": [],
                "format_status": {},
                "error": _public_value(str(exc)),
            }

    def legacy_publish(
        self,
        path: str,
        out_dir: str,
        *,
        formats: Any = None,
        final: bool = True,
        stem: str | None = None,
        record_artifact: bool = True,
        requested_kind: str | None = None,
    ) -> dict[str, Any]:
        """Adapt legacy arguments once, then use the same frozen build/publish seam."""

        try:
            normalized_formats = self._host._normalize_report_formats(formats)
            context = self._project_context(str(path or "").strip())
            requested = (
                ("final" if bool(final) else "diagnostic")
                if requested_kind is None
                else str(requested_kind or "").strip().lower()
            )
            requested_formats = list(normalized_formats)
            request = {
                "preset_id": "scientific-review",
                "requested_kind": requested,
                "formats": requested_formats,
                "scope": {
                    "kind": "project",
                    "project_ids": [context["project_id"]],
                    "job_ids": [],
                    "species": [],
                    "configuration_ids": [],
                    "stable_only": True,
                    "include_failed": True,
                },
            }
            record, _ = self._make_preview(
                str(path or "").strip(), request, render_html=False
            )
            result = self.publish(
                path,
                out_dir,
                record.preview_id,
                record.token,
                stem=stem,
                public=False,
                record_artifact=bool(record_artifact),
            )
            result.setdefault("requested_kind", requested)
            return result
        except Exception as exc:  # noqa: BLE001 - compatibility JSON boundary
            return {
                "ok": False,
                "kind": None,
                "artifact_status": "failed",
                "scientific_status": None,
                "scientific_qualification": None,
                "publication_gate_status": "unknown",
                "desired_report_kind": None,
                "gate_reason": "",
                "marker": None,
                "files": {},
                "requested_kind": (
                    str(requested_kind or "").strip().lower() or None
                ),
                "error": str(exc),
            }

    def _preview_format_status(self, build: Mapping[str, Any]) -> dict[str, Any]:
        spec = (build.get("contracts") or {}).get("report_spec") or {}
        requested = set(spec.get("formats") or [])
        capabilities = self._host.proj_report_capabilities().get("formats") or {}
        result = {}
        for fmt in ("html", "docx", "pdf"):
            record = capabilities.get(fmt) if isinstance(capabilities, Mapping) else {}
            available = isinstance(record, Mapping) and record.get("available") is True
            result[fmt] = {
                "state": "not_requested" if fmt not in requested else (
                    "ready_to_generate" if available else "unavailable"
                ),
                "available": available,
                "reason": _public_value(
                    str((record or {}).get("reason") or "")
                    if isinstance(record, Mapping) else "能力状态不可用"
                ),
            }
        for sidecar in ("model", "validation", "manifest"):
            result[sidecar] = {
                "state": "ready_to_generate",
                "available": True,
                "reason": "",
            }
        return result

    @staticmethod
    def _assert_expected(record: _PreviewRecord, expected: Any) -> None:
        if not isinstance(expected, Mapping):
            raise ValueError("publish requires the complete preview binding token")
        supplied = dict(expected)
        if supplied.get("schema") not in {None, PREVIEW_TOKEN_SCHEMA}:
            raise ValueError("unsupported preview token schema")
        if str(supplied.get("preview_id") or "") != record.preview_id:
            raise ValueError("preview token id does not match the selected preview")
        for key in _BINDING_KEYS:
            if key not in supplied:
                raise ValueError(f"preview token is missing {key}")
            if supplied.get(key) != record.token.get(key):
                raise ValueError(f"preview token binding mismatch: {key}")

    def _current_record(self, preview_id: str) -> _PreviewRecord:
        self._cleanup_expired()
        with self._lock:
            record = self._previews.get(str(preview_id or "").strip())
        if record is None:
            raise ValueError("preview is missing or expired; refresh the preview")
        return record

    def publish(
        self,
        path: str,
        out_dir: str,
        preview_id: str,
        expected: Mapping[str, Any] | None,
        *,
        stem: str | None = None,
        public: bool = True,
        record_artifact: bool = True,
    ) -> dict[str, Any]:
        record: _PreviewRecord | None = None
        try:
            record = self._current_record(preview_id)
            self._assert_expected(record, expected)
            context = self._project_context(str(path or "").strip())
            if context["project_id"] != record.project_id:
                raise ValueError("preview belongs to a different project")
            frozen_path, frozen_root = _canonical_project_paths(
                record.project_path, record.project_root
            )
            if (
                not _same_path(context["project_root"], frozen_root)
                or not _same_path(context["project_path"], frozen_path)
            ):
                raise ValueError(
                    "preview belongs to a different physical project instance"
                )
            current = self._host._report_workbench_current_state(
                context["project_path"], record.build.get("report_spec"))
            if current.get("project_id") != record.project_id:
                raise RuntimeError("project identity changed after preview")
            if current.get("scientific_fingerprint") != record.build.get(
                "scientific_fingerprint"
            ):
                stale = self._stale_preview(
                    record, "项目科学输入已在预览后变化，请刷新预览"
                )
                return _public_publish_result(stale) if public else stale
            if not record_artifact:
                rendered = self._host._report_workbench_render_build(
                    record.build,
                    out_dir,
                    stem=stem,
                    revision={},
                )
                return self._publish_result(record, rendered, public=public)

            history_path, lock_path = _history_paths(record.project_root)
            with _exclusive_file_lock(lock_path):
                history = _read_history(history_path, record.project_id)
                marker_status = self._host._proj_report_status_for_path(
                    context["project_path"]
                )
                history = self._reconcile_history_locked(
                    context, history_path, history, marker_status
                )
                lineage = history["reports"].get(record.report_id) or {
                    "report_id": record.report_id,
                    "preset_id": record.build["contracts"].get("preset_id"),
                    "latest_sequence": 0,
                    "latest_manifest_sha256": None,
                    "revisions": [],
                }
                latest_sequence = lineage.get("latest_sequence", 0)
                latest_manifest = lineage.get("latest_manifest_sha256")
                if (
                    latest_sequence != record.base_revision
                    or latest_manifest != record.base_manifest_sha256
                ):
                    conflict = self._revision_conflict(
                        record, latest_sequence, latest_manifest
                    )
                    return _public_publish_result(conflict) if public else conflict
                current = self._host._report_workbench_current_state(
                    context["project_path"], record.build.get("report_spec"))
                if current.get("scientific_fingerprint") != record.build.get(
                    "scientific_fingerprint"
                ):
                    stale = self._stale_preview(
                        record, "项目科学输入已在发布锁等待期间变化，请刷新预览"
                    )
                    return _public_publish_result(stale) if public else stale
                sequence = latest_sequence + 1
                revision_id = f"{record.report_id}-r{sequence:04d}"
                revision = {
                    "schema": REVISION_SCHEMA,
                    "report_id": record.report_id,
                    "revision_id": revision_id,
                    "sequence": sequence,
                    "parent_manifest_sha256": latest_manifest,
                    "spec_sha256": record.token["spec_sha256"],
                    "snapshot_sha256": record.token["snapshot_sha256"],
                    "validation_sha256": record.token["validation_sha256"],
                    "report_model_sha256": record.token["report_model_sha256"],
                    "created_at_utc": _utc_now(),
                }
                base_stem = stem or record.build.get("default_stem") or "report"
                revision_stem = _safe_revision_stem(base_stem, sequence)
                destination, destination_lock = _destination_paths(out_dir)
                with _exclusive_file_lock(destination_lock):
                    reservation = _reserve_revision_destination(
                        destination,
                        revision_stem=revision_stem,
                        project_id=record.project_id,
                        project_instance_sha256=_project_instance_sha256(
                            frozen_path, frozen_root
                        ),
                        revision=revision,
                    )
                    if reservation is None:
                        conflict = self._destination_revision_conflict(
                            record, sequence
                        )
                        return (
                            _public_publish_result(conflict) if public else conflict
                        )
                    reservation_path, reservation_payload = reservation
                    try:
                        rendered = self._host._report_workbench_render_build(
                            record.build,
                            str(destination),
                            stem=revision_stem,
                            revision=revision,
                        )
                    except Exception as exc:
                        with contextlib.suppress(Exception):
                            _fail_destination_reservation(
                                reservation_path,
                                reservation_payload,
                                destination,
                                error=exc,
                            )
                        raise
                    if rendered.get("ok") is False:
                        with contextlib.suppress(Exception):
                            _fail_destination_reservation(
                                reservation_path,
                                reservation_payload,
                                destination,
                                error=str(rendered.get("error") or "render failed"),
                            )
                        return self._publish_result(record, rendered, public=public)
                    manifest = str(rendered.get("manifest") or "")
                    if not manifest or not os.path.isfile(manifest):
                        raise RuntimeError("published report is missing its manifest")
                    manifest_sha256 = _sha256_file(manifest)
                    try:
                        _finish_destination_reservation(
                            reservation_path,
                            reservation_payload,
                            manifest_sha256=manifest_sha256,
                        )
                    except Exception as exc:  # noqa: BLE001 - the O_EXCL claim remains
                        rendered["destination_reservation_warning"] = str(exc)
                entry = {
                    **revision,
                    "preset_id": str(
                        record.build["contracts"].get("preset_id") or ""
                    ),
                    "manifest_sha256": manifest_sha256,
                    "manifest": os.path.abspath(manifest),
                    "artifact_status": "generated_unrecorded",
                    "scientific_status": record.build["report_kind"],
                    "scientific_qualification": record.build["contracts"][
                        "scientific_qualification"
                    ],
                    "files": {
                        str(key): os.path.abspath(str(value))
                        for key, value in (rendered.get("files") or {}).items()
                        if str(key) in {"html", "docx", "pdf"} and value
                    },
                    "model_file": os.path.abspath(str(rendered.get("model_file") or "")),
                    "contract_files": {
                        str(key): os.path.abspath(str(value))
                        for key, value in (rendered.get("contract_files") or {}).items()
                        if value
                    },
                    "transaction_journal": os.path.abspath(str(
                        _revision_journal_path(record.project_root, revision_id)
                    )),
                    "error": None,
                }
                revisions = list(lineage.get("revisions") or [])
                revisions.append(entry)
                lineage.update({
                    "latest_sequence": sequence,
                    "latest_manifest_sha256": manifest_sha256,
                    "revisions": revisions,
                })
                history["reports"][record.report_id] = lineage
                history["generation"] += 1
                try:
                    _atomic_json(history_path, history)
                except Exception as exc:  # noqa: BLE001 - keep rendered evidence
                    return self._generated_unrecorded_result(
                        record,
                        rendered,
                        revision,
                        exc,
                        public=public,
                        marker=None,
                        marker_recorded=False,
                        history_recorded=False,
                        history_ready_recorded=False,
                        recovery_state="history_entry_write_failed",
                        error_field="history_error",
                    )
                try:
                    _write_revision_journal(
                        record.project_root,
                        record.project_id,
                        entry,
                        state="history_prepared",
                        marker_recorded=False,
                        history_ready_recorded=False,
                    )
                except Exception as exc:  # noqa: BLE001 - do not commit a marker blindly
                    entry["error"] = str(exc)
                    history["generation"] += 1
                    secondary_error = None
                    try:
                        _atomic_json(history_path, history)
                    except Exception as history_exc:  # noqa: BLE001
                        secondary_error = history_exc
                    result = self._generated_unrecorded_result(
                        record,
                        rendered,
                        revision,
                        exc,
                        public=False,
                        marker=None,
                        marker_recorded=False,
                        history_recorded=True,
                        history_ready_recorded=False,
                        recovery_state="revision_journal_write_failed",
                        error_field="journal_error",
                    )
                    result["journal_recorded"] = False
                    if secondary_error is not None:
                        result["history_error"] = str(secondary_error)
                    return _public_publish_result(result) if public else result
                try:
                    marker = self._host._report_workbench_persist_build(
                        record.build,
                        rendered,
                        revision=revision,
                    )
                except Exception as exc:  # noqa: BLE001 - retain unrecorded revision audit
                    entry["error"] = str(exc)
                    history["generation"] += 1
                    secondary_error = None
                    journal_error = None
                    try:
                        _write_revision_journal(
                            record.project_root,
                            record.project_id,
                            entry,
                            state="marker_failed",
                            marker_recorded=False,
                            history_ready_recorded=False,
                            error=str(exc),
                        )
                    except Exception as journal_exc:  # noqa: BLE001
                        journal_error = journal_exc
                    try:
                        _atomic_json(history_path, history)
                    except Exception as history_exc:  # noqa: BLE001
                        secondary_error = history_exc
                    result = self._generated_unrecorded_result(
                        record,
                        rendered,
                        revision,
                        exc,
                        public=False,
                        marker=None,
                        marker_recorded=False,
                        history_recorded=True,
                        history_ready_recorded=False,
                        recovery_state="marker_write_failed",
                        error_field="marker_error",
                    )
                    result["stale_input"] = (
                        exc.__class__.__name__ == "_ReportInputChanged"
                    )
                    result["journal_recorded"] = journal_error is None
                    if journal_error is not None:
                        result["journal_error"] = str(journal_error)
                    if secondary_error is not None:
                        result["history_error"] = str(secondary_error)
                        result["recovery_state"] = (
                            "marker_and_history_error_update_failed"
                        )
                    return _public_publish_result(result) if public else result
                journal_error = None
                try:
                    _write_revision_journal(
                        record.project_root,
                        record.project_id,
                        entry,
                        state="marker_ready",
                        marker_recorded=True,
                        history_ready_recorded=False,
                    )
                except Exception as exc:  # noqa: BLE001 - marker remains authoritative
                    journal_error = exc
                entry["artifact_status"] = "ready"
                rendered["marker"] = marker
                rendered["revision"] = revision
                history["generation"] += 1
                try:
                    _atomic_json(history_path, history)
                except Exception as exc:  # noqa: BLE001 - marker is already durable
                    with contextlib.suppress(Exception):
                        _write_revision_journal(
                            record.project_root,
                            record.project_id,
                            entry,
                            state="marker_ready",
                            marker_recorded=True,
                            history_ready_recorded=False,
                            error=str(exc),
                        )
                    result = self._generated_unrecorded_result(
                        record,
                        rendered,
                        revision,
                        exc,
                        public=False,
                        marker=marker,
                        marker_recorded=True,
                        history_recorded=True,
                        history_ready_recorded=False,
                        recovery_state="marker_ready_history_finalize_failed",
                        error_field="history_error",
                        artifact_status="ready",
                    )
                    result["journal_recorded"] = journal_error is None
                    result["recovery_required"] = True
                    if journal_error is not None:
                        result["journal_error"] = str(journal_error)
                    return _public_publish_result(result) if public else result
                try:
                    _write_revision_journal(
                        record.project_root,
                        record.project_id,
                        entry,
                        state="complete",
                        marker_recorded=True,
                        history_ready_recorded=True,
                    )
                    rendered["journal_recorded"] = True
                except Exception as exc:  # noqa: BLE001 - history and marker agree
                    rendered["journal_recorded"] = False
                    rendered["journal_warning"] = str(exc)
                rendered.setdefault("artifact_status", "complete")
                return self._publish_result(record, rendered, public=public)
        except Exception as exc:  # noqa: BLE001 - JSON-safe public boundary
            failure = {
                "schema": "vcstudio.report-publish/v1",
                "ok": False,
                "preview_id": str(preview_id or "") or None,
                "project_id": record.project_id if record is not None else None,
                "kind": None,
                "artifact_status": "failed",
                "scientific_status": None,
                "scientific_qualification": None,
                "publication_gate_status": (
                    "eligible" if record and record.build["eligible_final"]
                    else "blocked" if record else "unknown"
                ),
                "desired_report_kind": (
                    "final" if record and record.build["eligible_final"]
                    else "diagnostic" if record else None
                ),
                "gate_reason": (
                    str(record.build.get("gate_reason") or "") if record else ""
                ),
                "requested_kind": (
                    record.build.get("requested_kind") if record else None
                ),
                "marker": None,
                "files": {},
                "error": str(exc),
            }
            return _public_publish_result(failure) if public else failure

    @staticmethod
    def _stale_preview(record: _PreviewRecord, reason: str) -> dict[str, Any]:
        return {
            "schema": "vcstudio.report-publish/v1",
            "ok": False,
            "preview_id": record.preview_id,
            "project_id": record.project_id,
            "artifact_status": "stale_preview",
            "scientific_status": record.build["report_kind"],
            "scientific_qualification": record.build["contracts"][
                "scientific_qualification"
            ],
            "publication_gate_status": (
                "eligible" if record.build["eligible_final"] else "blocked"
            ),
            "desired_report_kind": (
                "final" if record.build["eligible_final"] else "diagnostic"
            ),
            "stale_preview": True,
            "files": {},
            "error": reason,
        }

    @staticmethod
    def _revision_conflict(
        record: _PreviewRecord, current_sequence: int, current_manifest: str | None
    ) -> dict[str, Any]:
        return {
            "schema": "vcstudio.report-publish/v1",
            "ok": False,
            "preview_id": record.preview_id,
            "project_id": record.project_id,
            "artifact_status": "revision_conflict",
            "scientific_status": record.build["report_kind"],
            "scientific_qualification": record.build["contracts"][
                "scientific_qualification"
            ],
            "publication_gate_status": (
                "eligible" if record.build["eligible_final"] else "blocked"
            ),
            "desired_report_kind": (
                "final" if record.build["eligible_final"] else "diagnostic"
            ),
            "revision_conflict": True,
            "base_revision": record.base_revision,
            "current_revision": current_sequence,
            "current_manifest_sha256": current_manifest,
            "files": {},
            "error": "报告版本已由另一发布更新，请刷新预览后重试",
        }

    @staticmethod
    def _destination_revision_conflict(
        record: _PreviewRecord, sequence: int
    ) -> dict[str, Any]:
        return {
            "schema": "vcstudio.report-publish/v1",
            "ok": False,
            "preview_id": record.preview_id,
            "project_id": record.project_id,
            "artifact_status": "revision_conflict",
            "scientific_status": record.build["report_kind"],
            "scientific_qualification": record.build["contracts"][
                "scientific_qualification"
            ],
            "publication_gate_status": (
                "eligible" if record.build["eligible_final"] else "blocked"
            ),
            "desired_report_kind": (
                "final" if record.build["eligible_final"] else "diagnostic"
            ),
            "revision_conflict": True,
            "destination_conflict": True,
            "base_revision": record.base_revision,
            "current_revision": None if sequence == 1 else record.base_revision,
            "reserved_revision": sequence,
            "current_manifest_sha256": None,
            "files": {},
            "error": (
                "the target revision stem is already reserved or contains report "
                "artifacts; recover the existing revision or choose another "
                "output directory/stem"
            ),
        }

    @staticmethod
    def _orphaned_revision_conflict(record: _PreviewRecord) -> dict[str, Any]:
        """Compatibility name for the historical first-revision collision."""

        return ReportService._destination_revision_conflict(record, 1)

    def _generated_unrecorded_result(
        self,
        record: _PreviewRecord,
        rendered: Mapping[str, Any],
        revision: Mapping[str, Any],
        error: Exception,
        *,
        public: bool,
        marker: Any,
        marker_recorded: bool,
        history_recorded: bool,
        history_ready_recorded: bool,
        recovery_state: str,
        error_field: str,
        artifact_status: str = "generated_unrecorded",
    ) -> dict[str, Any]:
        """Return a recoverable partial result without discarding bundle locators."""

        result = copy.deepcopy(dict(rendered))
        result.update({
            "ok": False,
            "artifact_status": artifact_status,
            "partial_success": True,
            "revision": copy.deepcopy(dict(revision)),
            "marker": marker,
            "marker_recorded": marker_recorded,
            "history_recorded": history_recorded,
            "history_ready_recorded": history_ready_recorded,
            "recovery_state": recovery_state,
            error_field: str(error),
            "error": str(error),
        })
        return self._publish_result(record, result, public=public)

    def _publish_result(
        self, record: _PreviewRecord, rendered: Mapping[str, Any], *, public: bool
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(rendered))
        result.update({
            "schema": "vcstudio.report-publish/v1",
            "preview_id": record.preview_id,
            "project_id": record.project_id,
            "scientific_status": record.build["report_kind"],
            "scientific_qualification": record.build["contracts"][
                "scientific_qualification"
            ],
            "publication_gate_status": (
                "eligible" if record.build["eligible_final"] else "blocked"
            ),
            "desired_report_kind": (
                "final" if record.build["eligible_final"] else "diagnostic"
            ),
            "gate_reason": str(record.build.get("gate_reason") or ""),
        })
        if result.get("ok") is True:
            self._discard_preview(record, reason="published")
        if public:
            return _public_publish_result(result)
        return result

    def status(self, path: str) -> dict[str, Any]:
        """Return marker status after reconciling any durable revision intent."""

        try:
            context = self._project_context(str(path or "").strip())
            history_path, lock_path = _history_paths(context["project_root"])
            if history_path.is_file():
                with _exclusive_file_lock(lock_path):
                    history = _read_history(history_path, context["project_id"])
                    marker_status = self._host._proj_report_status_for_path(
                        context["project_path"]
                    )
                    self._reconcile_history_locked(
                        context, history_path, history, marker_status
                    )
                    # Keep the status read within the same lock as recovery so a
                    # concurrent publisher cannot interleave a newer projection.
                    result = self._host._proj_report_status_for_path(
                        context["project_path"]
                    )
            else:
                result = self._host._proj_report_status_for_path(
                    context["project_path"]
                )
            if not isinstance(result, Mapping):
                raise RuntimeError("report status host returned an invalid result")
            return _public_status_result(result)
        except Exception as exc:  # noqa: BLE001 - JSON-safe public adapter seam
            return {
                "schema": "vcstudio.report-status/v1",
                "ok": False,
                "artifact_status": "missing",
                "artifact_current": False,
                "has_marker": False,
                "scientific_status": None,
                "scientific_qualification": None,
                "scientific_stale": False,
                "eligible_final": False,
                "publication_gate_status": "unknown",
                "desired_report_kind": None,
                "report_reason": "",
                "files": {},
                "error": _public_value(str(exc)),
            }

    def history(self, path: str) -> dict[str, Any]:
        try:
            context = self._project_context(str(path or "").strip())
            history_path, lock_path = _history_paths(context["project_root"])
            if history_path.is_file():
                with _exclusive_file_lock(lock_path):
                    history = _read_history(history_path, context["project_id"])
                    try:
                        marker_status = self._host._proj_report_status_for_path(
                            context["project_path"]
                        )
                    except Exception:  # noqa: BLE001 - audit still fails closed
                        marker_status = None
                    history = self._reconcile_history_locked(
                        context, history_path, history, marker_status
                    )
                    public_revisions = self._validated_public_history(
                        history,
                        project_root=context["project_root"],
                        marker_status=marker_status,
                    )
            else:
                history = _empty_history(context["project_id"])
                public_revisions = []
            public_revisions.sort(
                key=lambda item: (str(item.get("report_id")), int(item.get("sequence") or 0)),
                reverse=True,
            )
            return {
                "schema": HISTORY_SCHEMA,
                "ok": True,
                "project_id": context["project_id"],
                "generation": history["generation"],
                "revisions": public_revisions,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - JSON-safe public boundary
            return {
                "schema": HISTORY_SCHEMA,
                "ok": False,
                "project_id": None,
                "generation": None,
                "revisions": [],
                "error": _public_value(str(exc)),
            }

    def _validated_public_history(
        self,
        history: Mapping[str, Any],
        *,
        project_root: str,
        marker_status: Any = None,
    ) -> list[dict[str, Any]]:
        """Project revisions only after local and host-authoritative verification."""

        revisions: list[dict[str, Any]] = []
        reports = history.get("reports")
        if not isinstance(reports, Mapping):
            raise RuntimeError("report history reports must be an object")
        for report_id, lineage in reports.items():
            if not isinstance(lineage, Mapping):
                raise RuntimeError("report history lineage must be an object")
            for entry in lineage.get("revisions") or []:
                if not isinstance(entry, Mapping):
                    raise RuntimeError("report history revision must be an object")
                current = False
                audit_error: str | None = None
                try:
                    self._audit_history_entry(entry, lineage)
                    current = True
                except Exception as exc:  # noqa: BLE001 - one bad bundle is stale
                    audit_error = str(exc)
                original_status = str(entry.get("artifact_status") or "")
                _journal, journal_error = _read_revision_journal(
                    project_root,
                    str(history.get("project_id") or ""),
                    entry,
                )
                marker_committed = _marker_matches_revision(marker_status, entry)
                status = "stale" if not current else original_status
                recovery_state = None
                if original_status == "generated_unrecorded" and marker_committed:
                    recovery_state = "history_recovery_required"
                visible_error = audit_error
                if visible_error is None:
                    if original_status == "generated_unrecorded":
                        visible_error = entry.get("error") or journal_error
                projected = {
                    "report_id": report_id,
                    "revision_id": entry.get("revision_id"),
                    "sequence": entry.get("sequence"),
                    "preset_id": lineage.get("preset_id"),
                    "created_at_utc": entry.get("created_at_utc"),
                    "scientific_status": entry.get("scientific_status"),
                    "scientific_qualification": entry.get(
                        "scientific_qualification"
                    ),
                    "artifact_status": status,
                    "current": current,
                    "recovery_state": recovery_state,
                    "manifest_sha256": entry.get("manifest_sha256"),
                    "files": _public_files(entry.get("files")),
                    "error": visible_error,
                }
                safe = _public_value(projected)
                revisions.append(dict(safe) if isinstance(safe, Mapping) else {})
        return revisions

    def close(self) -> None:
        with self._lock:
            records = list(self._previews.values())
            self._previews.clear()
        self._cleanup_preview_records([(record, "service_closed") for record in records])


__all__ = [
    "DESTINATION_RESERVATION_SCHEMA",
    "DEFAULT_PREVIEW_LIMIT",
    "DEFAULT_PREVIEW_TTL_SECONDS",
    "HISTORY_SCHEMA",
    "PREVIEW_SCHEMA",
    "PREVIEW_TOKEN_SCHEMA",
    "REVISION_SCHEMA",
    "REVISION_JOURNAL_SCHEMA",
    "ReportService",
    "ReportServiceHost",
]
