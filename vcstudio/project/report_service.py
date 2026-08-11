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
import ntpath
import os
import posixpath
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PREVIEW_SCHEMA = "vcstudio.report-preview/v1"
PREVIEW_TOKEN_SCHEMA = "vcstudio.report-preview-token/v1"
REVISION_SCHEMA = "vcstudio.report-revision/v1"
HISTORY_SCHEMA = "vcstudio.report-history/v1"

_OPERATION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
_UNC_PATH = re.compile(r"(?<![:A-Za-z0-9])(?:\\\\|//)[^\\/\s]+[\\/][^\s]+")
_TILDE_PATH = re.compile(r"(?<![A-Za-z0-9_])~[\\/]")
_POSIX_PATH = re.compile(r"(?<![:A-Za-z0-9_])/(?!/)[^\s]+")
_FILE_URI = re.compile(r"(?i)(?<![A-Za-z0-9_])file:(?:/{0,3}|\\)")
_PUBLIC_URL = re.compile(r"(?i)\b(?:https?|s3)://[^\s]+")
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:"
    r"\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+\S+"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|https?://[^\s/:]+:[^\s/@]+@"
    r")"
)
_SENSITIVE_KEY_WORDS = frozenset({
    "password", "passwd", "secret", "token", "credential", "credentials",
    "auth", "authorization", "cookie", "cookies", "privatekey",
})
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


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
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


@contextlib.contextmanager
def _exclusive_file_lock(path: Path):
    """Take one blocking, cross-process byte lock on ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised by the Linux CI matrix
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
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


def _normalized_key(value: Any) -> tuple[str, frozenset[str]]:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
    return normalized, frozenset(part for part in normalized.split("_") if part)


def _sensitive_public_key(value: Any) -> bool:
    normalized, words = _normalized_key(value)
    compact = normalized.replace("_", "")
    return bool(
        words & _SENSITIVE_KEY_WORDS
        or compact in {"apikey", "accesskey", "privatekey", "secretkey"}
        or compact.endswith((
            "password", "passwd", "secret", "token", "credential",
            "authorization", "cookie", "privatekey",
        ))
    )


def _path_public_key(value: Any) -> bool:
    normalized, _ = _normalized_key(value)
    return bool(
        normalized in _PATH_KEYS
        or normalized.endswith(("_path", "_root", "_dir", "_directory", "_locator"))
    )


def _unsafe_public_string(value: str) -> bool:
    text = str(value or "")
    inspected = _PUBLIC_URL.sub("", text)
    return bool(
        _DRIVE_PATH.search(inspected)
        or _UNC_PATH.search(inspected)
        or _TILDE_PATH.search(inspected)
        or _POSIX_PATH.search(inspected)
        or _FILE_URI.search(inspected)
        or _CREDENTIAL_VALUE.search(text)
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
        return "<sensitive-value-redacted>" if _CREDENTIAL_VALUE.search(value) else (
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
    report_id: str,
) -> bool:
    """Detect orphaned or foreign artifacts before an r0001 replacement."""

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
        lineage = (
            str(revision.get("report_id") or "")
            if isinstance(revision, Mapping) else ""
        )
        if lineage == report_id or str(manifest.get("report_id") or "") == report_id:
            return True
    return False


@dataclass
class _PreviewRecord:
    preview_id: str
    operation_id: str
    project_id: str
    project_path: str
    project_root: str
    report_id: str
    created_at_utc: str
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
        host: Any,
        *,
        preview_ttl_seconds: int = 30 * 60,
        clock=time.time,
        temp_root: os.PathLike[str] | str | None = None,
    ) -> None:
        self._host = host
        self._clock = clock
        self._ttl = max(60, int(preview_ttl_seconds))
        self._temp_root = None if temp_root is None else str(temp_root)
        if self._temp_root is not None:
            Path(self._temp_root).mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._previews: dict[str, _PreviewRecord] = {}

    def _project_context(self, path: str) -> dict[str, Any]:
        context = self._host._report_workbench_project_context(path)
        if not isinstance(context, Mapping):
            raise RuntimeError("report project context is unavailable")
        project_id = str(context.get("project_id") or "")
        root = str(context.get("project_root") or "")
        if not project_id or not root:
            raise RuntimeError("report project identity is incomplete")
        return dict(context)

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
                    "name": str(context.get("project_name") or ""),
                },
                "catalog": catalog,
                "report_spec": spec.to_dict(),
                "status": self._host.proj_report_status(path),
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
                "error": str(exc),
            }

    def _cleanup_expired(self) -> None:
        now = float(self._clock())
        expired: list[_PreviewRecord] = []
        with self._lock:
            for preview_id, record in list(self._previews.items()):
                if record.expires_at <= now:
                    expired.append(self._previews.pop(preview_id))
        for record in expired:
            shutil.rmtree(record.temp_root, ignore_errors=True)

    def _history_base(
        self, project_root: str, project_id: str, report_id: str
    ) -> tuple[int, str | None]:
        history_path, lock_path = _history_paths(project_root)
        # A first preview is observational: do not create .vcstudio/history or
        # even a lock file merely to discover that no revision exists yet.
        # A concurrent first publish is still caught by the publish-time CAS.
        if not history_path.is_file():
            return 0, None
        with _exclusive_file_lock(lock_path):
            history = _read_history(history_path, project_id)
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
            context["project_root"], context["project_id"], report_id
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
                project_path=path,
                project_root=context["project_root"],
                report_id=report_id,
                created_at_utc=_utc_now(),
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
            with self._lock:
                self._previews[preview_id] = record
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
                "gate_reason": str(build.get("gate_reason") or ""),
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
                "error": str(exc),
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
                "reason": str((record or {}).get("reason") or "")
                if isinstance(record, Mapping) else "能力状态不可用",
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
            current = self._host._report_workbench_current_state(
                path, record.build.get("report_spec"))
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
                    path, record.build.get("report_spec"))
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
                revision_stem = f"{base_stem}_r{sequence:04d}"
                if (
                    sequence == 1
                    and latest_manifest is None
                    and not lineage.get("revisions")
                    and _output_has_revision_collision(
                        out_dir,
                        revision_stem=revision_stem,
                        report_id=record.report_id,
                    )
                ):
                    conflict = self._orphaned_revision_conflict(record)
                    return _public_publish_result(conflict) if public else conflict
                rendered = self._host._report_workbench_render_build(
                    record.build,
                    out_dir,
                    stem=revision_stem,
                    revision=revision,
                )
                if rendered.get("ok") is False:
                    return self._publish_result(record, rendered, public=public)
                manifest = str(rendered.get("manifest") or "")
                if not manifest or not os.path.isfile(manifest):
                    raise RuntimeError("published report is missing its manifest")
                manifest_sha256 = _sha256_file(manifest)
                entry = {
                    **revision,
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
                    marker = self._host._report_workbench_persist_build(
                        record.build,
                        rendered,
                        revision=revision,
                    )
                except Exception as exc:  # noqa: BLE001 - retain unrecorded revision audit
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
                        recovery_state="marker_write_failed",
                        error_field="marker_error",
                    )
                    result["stale_input"] = (
                        exc.__class__.__name__ == "_ReportInputChanged"
                    )
                    if secondary_error is not None:
                        result["history_error"] = str(secondary_error)
                        result["recovery_state"] = (
                            "marker_and_history_error_update_failed"
                        )
                    return _public_publish_result(result) if public else result
                entry["artifact_status"] = "ready"
                rendered["marker"] = marker
                rendered["revision"] = revision
                history["generation"] += 1
                try:
                    _atomic_json(history_path, history)
                except Exception as exc:  # noqa: BLE001 - marker is already durable
                    return self._generated_unrecorded_result(
                        record,
                        rendered,
                        revision,
                        exc,
                        public=public,
                        marker=marker,
                        marker_recorded=True,
                        history_recorded=True,
                        history_ready_recorded=False,
                        recovery_state="marker_ready_history_finalize_failed",
                        error_field="history_error",
                    )
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
    def _orphaned_revision_conflict(record: _PreviewRecord) -> dict[str, Any]:
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
            "base_revision": 0,
            "current_revision": None,
            "current_manifest_sha256": None,
            "files": {},
            "error": (
                "目标目录已存在未被当前历史索引绑定的首版报告工件；"
                "为避免覆盖，需先恢复历史或选择新的输出目录"
            ),
        }

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
    ) -> dict[str, Any]:
        """Return a recoverable partial result without discarding bundle locators."""

        result = copy.deepcopy(dict(rendered))
        result.update({
            "ok": False,
            "artifact_status": "generated_unrecorded",
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
        if public:
            return _public_publish_result(result)
        return result

    def history(self, path: str) -> dict[str, Any]:
        try:
            context = self._project_context(str(path or "").strip())
            history_path, lock_path = _history_paths(context["project_root"])
            if history_path.is_file():
                with _exclusive_file_lock(lock_path):
                    history = _read_history(history_path, context["project_id"])
                    public_revisions = self._validated_public_history(history)
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
        self, history: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Project revisions only after local and host-authoritative verification."""

        revisions: list[dict[str, Any]] = []
        reports = history.get("reports")
        if not isinstance(reports, Mapping):
            raise RuntimeError("report history reports must be an object")
        validator = getattr(
            self._host, "_report_workbench_validate_history_entry", None
        )
        for report_id, lineage in reports.items():
            if not isinstance(lineage, Mapping):
                raise RuntimeError("report history lineage must be an object")
            for entry in lineage.get("revisions") or []:
                if not isinstance(entry, Mapping):
                    raise RuntimeError("report history revision must be an object")
                current = False
                audit_error: str | None = None
                try:
                    _validated_history_bundle(entry, lineage)
                    if not callable(validator):
                        raise RuntimeError(
                            "report history host validator is unavailable"
                        )
                    audit = validator(copy.deepcopy(dict(entry)))
                    if not isinstance(audit, Mapping):
                        raise RuntimeError(
                            "report history host validator returned an invalid result"
                        )
                    if audit.get("ok") is not True or audit.get("current") is not True:
                        raise RuntimeError(
                            str(audit.get("error") or "report history host audit failed")
                        )
                    for field in (
                        "scientific_status", "scientific_qualification"
                    ):
                        if str(audit.get(field) or "") != str(entry.get(field) or ""):
                            raise RuntimeError(
                                f"report history host {field} binding mismatch"
                            )
                    current = True
                except Exception as exc:  # noqa: BLE001 - one bad bundle is stale
                    audit_error = str(exc)
                original_status = str(entry.get("artifact_status") or "")
                status = original_status if current else "stale"
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
                    "manifest_sha256": entry.get("manifest_sha256"),
                    "files": _public_files(entry.get("files")),
                    "error": audit_error if audit_error is not None else entry.get("error"),
                }
                safe = _public_value(projected)
                revisions.append(dict(safe) if isinstance(safe, Mapping) else {})
        return revisions

    def close(self) -> None:
        with self._lock:
            records = list(self._previews.values())
            self._previews.clear()
        for record in records:
            shutil.rmtree(record.temp_root, ignore_errors=True)


__all__ = [
    "HISTORY_SCHEMA",
    "PREVIEW_SCHEMA",
    "PREVIEW_TOKEN_SCHEMA",
    "REVISION_SCHEMA",
    "ReportService",
]
