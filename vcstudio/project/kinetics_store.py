"""Immutable kinetic-result uploads plus audited CAS selection."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vcstudio.project import kinetics


RECEIPT_SCHEMA = "vcstudio.kinetics-result-receipt/v2"
SELECTION_SCHEMA = "vcstudio.kinetics-result-selection/v1"
SELECTION_EVENT_SCHEMA = "vcstudio.kinetics-result-selection-event/v1"
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESULT_BYTES = 20 * 1024 * 1024
_SELECTION_LOCK = threading.RLock()


class KineticsStoreError(ValueError):
    """A project-local result receipt is missing, unsafe, or tampered."""


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(is_junction) and is_junction())


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n").encode("utf-8")


def _root(project_root) -> Path:
    path = Path(project_root)
    if not path.is_absolute():
        path = path.resolve()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise KineticsStoreError("project root does not exist") from exc
    if not resolved.is_dir():
        raise KineticsStoreError("project root must be a directory")
    return resolved


def _ensure_directory(path: Path, root: Path) -> Path:
    if path.exists() and _is_link(path):
        raise KineticsStoreError("kinetics result directory must not be a symlink")
    path.mkdir(parents=False, exist_ok=True)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise KineticsStoreError("kinetics result directory escaped project root") from exc
    if not resolved.is_dir():
        raise KineticsStoreError("kinetics result directory is not a directory")
    return resolved


def _results_root(project_root: Path) -> Path:
    current = project_root
    for name in (".vcstudio", "kinetics", "results"):
        current = _ensure_directory(current / name, project_root)
    return current


def _atomic_write(path: Path, payload: bytes, *, replace: bool) -> None:
    if path.exists() and _is_link(path):
        raise KineticsStoreError(f"result file {path.name} must not be a symlink")
    if path.exists() and not replace:
        if path.is_file() and path.read_bytes() == payload:
            return
        raise KineticsStoreError(f"immutable result file {path.name} already differs")
    fd, temporary = tempfile.mkstemp(prefix=".vcs-kinetics-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _advisory_lock(path: Path):
    if path.exists() and _is_link(path):
        raise KineticsStoreError("result selection lock must not be a symlink")
    path.touch(exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
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


def _network_hash(network_source) -> str:
    audit = kinetics.audit_network(network_source)
    if audit["export_ready"] is not True or not audit.get("input_sha256"):
        raise KineticsStoreError("frozen network failed the kinetics input audit")
    return str(audit["input_sha256"])


def store_result(project_root, result: Mapping[str, Any], network_source, *,
                 expected_adapter: Mapping[str, Any],
                 confirmed_export_sha256: str) -> dict[str, Any]:
    """Validate and upload one immutable result without selecting it."""
    raw = _canonical_bytes(result)
    if len(raw) > _MAX_RESULT_BYTES:
        raise KineticsStoreError("kinetics result exceeds the size limit")
    normalized = kinetics.import_result(
        result, network_source, expected_adapter=expected_adapter)
    result_sha256 = hashlib.sha256(raw).hexdigest()
    input_sha256 = _network_hash(network_source)
    if (not isinstance(confirmed_export_sha256, str)
            or not _HEX_RE.fullmatch(confirmed_export_sha256)):
        raise KineticsStoreError("confirmed export hash must be SHA-256")
    root = _root(project_root)
    results = _results_root(root)
    input_dir = _ensure_directory(results / input_sha256, root)
    result_dir = _ensure_directory(input_dir / result_sha256, root)
    normalized_bytes = _canonical_bytes(normalized)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "input_sha256": input_sha256,
        "result_sha256": result_sha256,
        "normalized_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
        "confirmed_export_sha256": confirmed_export_sha256,
        "adapter": {
            "id": normalized["adapter"]["id"],
            "version": normalized["adapter"]["version"],
            "tool_sha256": normalized["adapter"]["tool_sha256"],
        },
        "scientific_status": "diagnostic",
        "eligible_final": False,
    }
    _atomic_write(result_dir / "result.json", _json_bytes(result), replace=False)
    _atomic_write(result_dir / "normalized.json", _json_bytes(normalized), replace=False)
    _atomic_write(result_dir / "receipt.json", _json_bytes(receipt), replace=False)
    return {
        "schema": RECEIPT_SCHEMA,
        "input_sha256": input_sha256,
        "result_sha256": result_sha256,
        "confirmed_export_sha256": confirmed_export_sha256,
        "selected": False,
        "normalized": normalized,
    }


def _read_json(path: Path, *, maximum: int = _MAX_RESULT_BYTES) -> Any:
    if _is_link(path) or not path.is_file():
        raise KineticsStoreError(f"kinetics result file {path.name} is unavailable")
    if path.stat().st_size > maximum:
        raise KineticsStoreError(f"kinetics result file {path.name} exceeds the size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KineticsStoreError(f"kinetics result file {path.name} is invalid") from exc


def _existing_child(parent: Path, name: str, root: Path) -> Path | None:
    candidate = parent / name
    if not candidate.exists():
        return None
    if _is_link(candidate) or not candidate.is_dir():
        raise KineticsStoreError("kinetics result directory chain is unsafe")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise KineticsStoreError(
            "kinetics result directory escaped project root") from exc
    return resolved


def _input_directory(project_root, input_sha256: str, *,
                     create: bool) -> tuple[Path, Path | None]:
    root = _root(project_root)
    if create:
        results = _results_root(root)
        return root, _ensure_directory(results / input_sha256, root)
    current = root
    for name in (".vcstudio", "kinetics", "results", input_sha256):
        current = _existing_child(current, name, root)
        if current is None:
            return root, None
    return root, current


def _selection_default(input_sha256: str) -> dict[str, Any]:
    return {
        "schema": SELECTION_SCHEMA, "input_sha256": input_sha256,
        "revision": 0, "latest_result_sha256": None,
        "confirmed_export_sha256": None, "selection_event_sha256": None,
    }


def _read_selection(input_dir: Path, input_sha256: str) -> dict[str, Any]:
    latest = input_dir / "latest.json"
    if not latest.exists():
        return _selection_default(input_sha256)
    pointer = _read_json(latest, maximum=8192)
    if (not isinstance(pointer, Mapping)
            or set(pointer) != {
                "schema", "input_sha256", "revision", "latest_result_sha256",
                "confirmed_export_sha256", "selection_event_sha256"}
            or pointer.get("schema") != SELECTION_SCHEMA
            or pointer.get("input_sha256") != input_sha256
            or isinstance(pointer.get("revision"), bool)
            or not isinstance(pointer.get("revision"), int)
            or pointer["revision"] < 1
            or any(not isinstance(pointer.get(key), str)
                   or not _HEX_RE.fullmatch(pointer[key]) for key in (
                       "latest_result_sha256", "confirmed_export_sha256",
                       "selection_event_sha256"))):
        raise KineticsStoreError("kinetics latest pointer is invalid")
    return dict(pointer)


def selection_snapshot(project_root, network_source) -> dict[str, Any]:
    """Return the current result-selection CAS identity without writing."""
    input_sha256 = _network_hash(network_source)
    _root_path, input_dir = _input_directory(
        project_root, input_sha256, create=False)
    if input_dir is None:
        return _selection_default(input_sha256)
    return _read_selection(input_dir, input_sha256)


def _load_uploaded(input_dir: Path, root: Path, result_sha256: str,
                   network_source, expected_adapter: Mapping[str, Any], *,
                   expected_export_sha256: str) -> dict[str, Any]:
    if not isinstance(result_sha256, str) or not _HEX_RE.fullmatch(result_sha256):
        raise KineticsStoreError("kinetics result hash is invalid")
    result_dir = _existing_child(input_dir, result_sha256, root)
    if result_dir is None:
        raise KineticsStoreError("kinetics result directory is unavailable")
    raw_path = result_dir / "result.json"
    raw = _read_json(raw_path)
    canonical_hash = hashlib.sha256(_canonical_bytes(raw)).hexdigest()
    if canonical_hash != result_sha256:
        raise KineticsStoreError("kinetics raw result hash mismatch")
    normalized = kinetics.import_result(
        raw, network_source, expected_adapter=expected_adapter)
    receipt = _read_json(result_dir / "receipt.json", maximum=65536)
    normalized_sha256 = hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
    if (not isinstance(receipt, Mapping) or receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("input_sha256") != _network_hash(network_source)
            or receipt.get("result_sha256") != result_sha256
            or receipt.get("normalized_sha256") != normalized_sha256
            or receipt.get("confirmed_export_sha256") != expected_export_sha256
            or receipt.get("scientific_status") != "diagnostic"
            or receipt.get("eligible_final") is not False):
        raise KineticsStoreError("kinetics result receipt hash or status mismatch")
    return {
        "schema": RECEIPT_SCHEMA,
        "input_sha256": receipt["input_sha256"],
        "result_sha256": result_sha256,
        "confirmed_export_sha256": expected_export_sha256,
        "normalized": normalized,
    }


def select_result(project_root, result_sha256: str, network_source, *,
                  confirmed_export_sha256: str,
                  expected_latest_sha256: str | None,
                  expected_revision: int, confirmed: bool,
                  expected_adapter: Mapping[str, Any]) -> dict[str, Any]:
    """Select an uploaded result with revision+hash CAS and immutable audit."""
    if confirmed is not True:
        raise KineticsStoreError("explicit confirmation is required for result selection")
    if (not isinstance(confirmed_export_sha256, str)
            or not _HEX_RE.fullmatch(confirmed_export_sha256)):
        raise KineticsStoreError("confirmed export hash must be SHA-256")
    if (expected_latest_sha256 is not None
            and (not isinstance(expected_latest_sha256, str)
                 or not _HEX_RE.fullmatch(expected_latest_sha256))):
        raise KineticsStoreError("expected latest result hash is invalid")
    if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision < 0):
        raise KineticsStoreError("expected result revision must be non-negative")
    input_sha256 = _network_hash(network_source)
    root, input_dir = _input_directory(project_root, input_sha256, create=False)
    if input_dir is None:
        raise KineticsStoreError("kinetics result directory is unavailable")
    uploaded = _load_uploaded(
        input_dir, root, result_sha256, network_source, expected_adapter,
        expected_export_sha256=confirmed_export_sha256)
    with _SELECTION_LOCK, _advisory_lock(input_dir / ".selection.lock"):
        current = _read_selection(input_dir, input_sha256)
        if (current["revision"] != expected_revision
                or current["latest_result_sha256"] != expected_latest_sha256):
            raise KineticsStoreError("kinetics result selection conflict")
        revision = current["revision"] + 1
        event = {
            "schema": SELECTION_EVENT_SCHEMA,
            "input_sha256": input_sha256,
            "confirmed_export_sha256": confirmed_export_sha256,
            "previous_result_sha256": current["latest_result_sha256"],
            "selected_result_sha256": result_sha256,
            "previous_revision": current["revision"],
            "revision": revision,
            "selected_at": datetime.now(timezone.utc).isoformat(),
            "source": "explicit_local_selection",
            "scientific_status": "diagnostic", "eligible_final": False,
        }
        event_sha256 = hashlib.sha256(_canonical_bytes(event)).hexdigest()
        history = _ensure_directory(input_dir / "selection-history", root)
        event_name = f"{revision:08d}-{event_sha256}.json"
        _atomic_write(history / event_name, _json_bytes(event), replace=False)
        pointer = {
            "schema": SELECTION_SCHEMA, "input_sha256": input_sha256,
            "revision": revision, "latest_result_sha256": result_sha256,
            "confirmed_export_sha256": confirmed_export_sha256,
            "selection_event_sha256": event_sha256,
        }
        _atomic_write(input_dir / "latest.json", _json_bytes(pointer), replace=True)
    return {
        **uploaded, "selected": True, "selection_revision": revision,
        "latest_result_sha256": result_sha256,
        "selection_event_sha256": event_sha256,
    }


def load_result(project_root, network_source, *,
                expected_adapter: Mapping[str, Any],
                confirmed_export_sha256: str) -> dict[str, Any] | None:
    """Reload only the CAS-selected result and verify its selection event."""
    input_sha256 = _network_hash(network_source)
    root, input_dir = _input_directory(project_root, input_sha256, create=False)
    if input_dir is None:
        return None
    pointer = _read_selection(input_dir, input_sha256)
    if pointer["revision"] == 0:
        return None
    if pointer["confirmed_export_sha256"] != confirmed_export_sha256:
        raise KineticsStoreError("selected result belongs to a different export")
    history = _existing_child(input_dir, "selection-history", root)
    if history is None:
        raise KineticsStoreError("kinetics selection history is unavailable")
    matches = list(history.glob(
        f"{pointer['revision']:08d}-{pointer['selection_event_sha256']}.json"))
    if len(matches) != 1:
        raise KineticsStoreError("kinetics selection history is unavailable")
    event = _read_json(matches[0], maximum=65536)
    if (not isinstance(event, Mapping)
            or event.get("schema") != SELECTION_EVENT_SCHEMA
            or hashlib.sha256(_canonical_bytes(event)).hexdigest()
            != pointer["selection_event_sha256"]
            or event.get("input_sha256") != input_sha256
            or event.get("confirmed_export_sha256") != confirmed_export_sha256
            or event.get("selected_result_sha256")
            != pointer["latest_result_sha256"]
            or event.get("revision") != pointer["revision"]
            or event.get("scientific_status") != "diagnostic"
            or event.get("eligible_final") is not False):
        raise KineticsStoreError("kinetics selection history is invalid")
    return _load_uploaded(
        input_dir, root, pointer["latest_result_sha256"], network_source,
        expected_adapter, expected_export_sha256=confirmed_export_sha256)


__all__ = [
    "KineticsStoreError", "RECEIPT_SCHEMA", "SELECTION_EVENT_SCHEMA",
    "SELECTION_SCHEMA", "load_result", "select_result", "selection_snapshot",
    "store_result",
]
