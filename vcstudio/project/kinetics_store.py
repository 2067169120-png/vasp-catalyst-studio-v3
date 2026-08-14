"""Immutable project-local storage for externally imported kinetic results."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vcstudio.project import kinetics


RECEIPT_SCHEMA = "vcstudio.kinetics-result-receipt/v1"
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESULT_BYTES = 20 * 1024 * 1024


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


def _network_hash(network_source) -> str:
    audit = kinetics.audit_network(network_source)
    if audit["export_ready"] is not True or not audit.get("input_sha256"):
        raise KineticsStoreError("frozen network failed the kinetics input audit")
    return str(audit["input_sha256"])


def store_result(project_root, result: Mapping[str, Any], network_source, *,
                 expected_adapter: Mapping[str, Any]) -> dict[str, Any]:
    """Validate then store one immutable raw result and a mutable hash pointer."""
    raw = _canonical_bytes(result)
    if len(raw) > _MAX_RESULT_BYTES:
        raise KineticsStoreError("kinetics result exceeds the size limit")
    normalized = kinetics.import_result(
        result, network_source, expected_adapter=expected_adapter)
    result_sha256 = hashlib.sha256(raw).hexdigest()
    input_sha256 = _network_hash(network_source)
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
    _atomic_write(
        input_dir / "latest.json", _json_bytes({"result_sha256": result_sha256}),
        replace=True)
    return {
        "schema": RECEIPT_SCHEMA,
        "input_sha256": input_sha256,
        "result_sha256": result_sha256,
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


def load_result(project_root, network_source, *,
                expected_adapter: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reload the latest raw result and re-run schema/unit/hash normalization."""
    input_sha256 = _network_hash(network_source)
    root = _root(project_root)
    current = root
    for name in (".vcstudio", "kinetics", "results", input_sha256):
        current = _existing_child(current, name, root)
        if current is None:
            return None
    latest = current / "latest.json"
    if not latest.exists():
        return None
    pointer = _read_json(latest, maximum=4096)
    if (not isinstance(pointer, Mapping)
            or set(pointer) != {"result_sha256"}
            or not isinstance(pointer.get("result_sha256"), str)
            or not _HEX_RE.fullmatch(pointer["result_sha256"])):
        raise KineticsStoreError("kinetics latest pointer is invalid")
    result_sha256 = pointer["result_sha256"]
    result_dir = _existing_child(latest.parent, result_sha256, root)
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
            or receipt.get("input_sha256") != input_sha256
            or receipt.get("result_sha256") != result_sha256
            or receipt.get("normalized_sha256") != normalized_sha256
            or receipt.get("scientific_status") != "diagnostic"
            or receipt.get("eligible_final") is not False):
        raise KineticsStoreError("kinetics result receipt hash or status mismatch")
    return {
        "schema": RECEIPT_SCHEMA,
        "input_sha256": input_sha256,
        "result_sha256": result_sha256,
        "normalized": normalized,
    }


__all__ = [
    "KineticsStoreError", "RECEIPT_SCHEMA", "load_result", "store_result",
]
