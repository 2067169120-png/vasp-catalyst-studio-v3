"""Authoritative, task-aware scientific input closure for managed jobs.

The closure is deliberately shared by generation, submission and strict
fingerprinting.  A filename being absent from the historical four-file list is
not evidence that VASP will not read it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


INPUT_CLOSURE_SCHEMA = "vcstudio.scientific-input-closure/v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_FRAME_RE = re.compile(r"^[0-9]{2}$")
_MAX_INPUT_FILES = 512
_MAX_RELATIVE_NAME = 160
_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_MAX_TOTAL_BYTES = 64 * _GIB
_MAX_DEFAULT_BYTES = 256 * _MIB
_MAX_LARGE_BYTES = 32 * _GIB
_MAX_POTCAR_BYTES = 512 * _MIB
_MAX_INCAR_BYTES = 4 * _MIB
_BASE_VASP_INPUTS = ("POSCAR", "INCAR", "KPOINTS", "POTCAR")
_RESERVED_INPUT_NAMES = {
    "job.yaml", "vcs_job.sh", ".vcstudio-job-operation.lock",
    ".vcstudio-submit-recovery.json", ".vcstudio-job-actions.json",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _InputResourceLimit(ValueError):
    pass


def _file_byte_limit(name: str) -> int:
    leaf = name.rsplit("/", 1)[-1]
    if leaf == "INCAR":
        return _MAX_INCAR_BYTES
    if leaf in {"CHGCAR", "WAVECAR", "ML_FF", "vdw_kernel.bindat", "vdw_kernel"}:
        return _MAX_LARGE_BYTES
    if leaf == "POTCAR":
        return _MAX_POTCAR_BYTES
    return _MAX_DEFAULT_BYTES


def _sha256_file(path: Path, *, byte_limit: int) -> tuple[str, int]:
    try:
        before = path.stat()
    except OSError as exc:
        raise _InputResourceLimit("file size unavailable") from exc
    initial_size = before.st_size
    if initial_size < 0 or initial_size > byte_limit:
        raise _InputResourceLimit(f"file exceeds {byte_limit} bytes")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if total > byte_limit:
                raise _InputResourceLimit(f"file exceeds {byte_limit} bytes")
            digest.update(chunk)
    try:
        after = path.stat()
    except OSError as exc:
        raise _InputResourceLimit("file metadata unavailable after hashing") from exc
    before_identity = (
        before.st_size, before.st_mtime_ns, getattr(before, "st_ino", None),
        getattr(before, "st_dev", None),
    )
    after_identity = (
        after.st_size, after.st_mtime_ns, getattr(after, "st_ino", None),
        getattr(after, "st_dev", None),
    )
    if total != initial_size or before_identity != after_identity:
        raise _InputResourceLimit("file changed while hashing")
    return digest.hexdigest(), total


def _safe_relative_name(value: Any, *, nested_neb: bool = False) -> str | None:
    name = str(value or "").strip().replace("\\", "/")
    if (not name or len(name) > _MAX_RELATIVE_NAME or name.startswith("/")
            or "\x00" in name):
        return None
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    if any(part in _RESERVED_INPUT_NAMES or part.startswith(".vcstudio-")
           for part in parts):
        return None
    if len(parts) == 1:
        return parts[0]
    if nested_neb and len(parts) == 2 and _FRAME_RE.fullmatch(parts[0]):
        return name
    return None


def _incar_values(root: Path) -> dict[str, Any]:
    try:
        from vcstudio.generate.incar_builder import parse_incar
        path = root / "INCAR"
        if path.stat().st_size > _MAX_INCAR_BYTES:
            return {}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_MAX_INCAR_BYTES + 1)
        if len(text.encode("utf-8", errors="replace")) > _MAX_INCAR_BYTES:
            return {}
        return dict(parse_incar(text) or {})
    except (OSError, TypeError, ValueError):
        return {}


def _integer(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().strip(".").upper() in {"T", "TRUE", "1", "YES"}


def _manifest_inputs(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = manifest.get("inputs")
    return value if isinstance(value, Mapping) else {}


def required_input_names(job_dir: str | os.PathLike[str],
                         manifest: Mapping[str, Any]) -> tuple[list[str], dict[str, str], list[str]]:
    """Return ordered relative files, per-file reasons and invalid declarations."""
    root = Path(job_dir).expanduser().resolve()
    inputs = _manifest_inputs(manifest)
    engine = str(inputs.get("engine") or "vasp").strip().lower() or "vasp"
    task = str(manifest.get("task_type") or "").strip().lower()
    reasons: dict[str, str] = {}
    invalid: list[str] = []
    neb_intermediates: list[int] = []

    def add(name: str, reason: str) -> None:
        if name not in reasons:
            reasons[name] = reason

    if engine != "vasp":
        declared = inputs.get("files")
        if not isinstance(declared, list) or not declared:
            return [], {}, ["inputs.files"]
        if len(declared) > _MAX_INPUT_FILES:
            return [], {}, [f"inputs.files exceeds {_MAX_INPUT_FILES} entries"]
        for raw in declared:
            name = _safe_relative_name(raw)
            if name is None:
                invalid.append(f"invalid input declaration:{raw}")
            else:
                add(name, f"{engine} inputs.files")
        return list(reasons), reasons, invalid

    incar = {str(key).upper(): value for key, value in _incar_values(root).items()}
    if task == "neb":
        for name in ("INCAR", "KPOINTS", "POTCAR"):
            add(name, "NEB shared root input")
        n_images = _integer(inputs.get("n_images"))
        if n_images is None or not 1 <= n_images <= 98:
            invalid.append("NEB inputs.n_images")
        else:
            incar_images = _integer(incar.get("IMAGES"))
            if incar_images is None or incar_images != n_images:
                invalid.append("NEB INCAR.IMAGES does not match inputs.n_images")
            neb_intermediates = list(range(1, n_images + 1))
            for index in range(n_images + 2):
                name = f"{index:02d}/POSCAR"
                add(name, "NEB ordered image structure")
    else:
        for name in _BASE_VASP_INPUTS:
            add(name, "managed VASP input")

    icharg = _integer(incar.get("ICHARG"))
    if task == "bands":
        add("CHGCAR", "bands requires fixed charge density (ICHARG=11)")
    elif icharg in {1, 11}:
        if task == "neb":
            for index in neb_intermediates:
                add(f"{index:02d}/CHGCAR", f"NEB image INCAR ICHARG={icharg}")
        else:
            add("CHGCAR", f"INCAR ICHARG={icharg}")
    istart = _integer(incar.get("ISTART"))
    if istart is not None and istart > 0:
        if task == "neb":
            for index in neb_intermediates:
                add(f"{index:02d}/WAVECAR", f"NEB image INCAR ISTART={istart}")
        else:
            add("WAVECAR", f"ISTART={istart}")
    if _truthy(incar.get("LUSE_VDW")):
        if (root / "vdw_kernel.bindat").is_file():
            add("vdw_kernel.bindat", "LUSE_VDW=true")
        elif (root / "vdw_kernel").is_file():
            add("vdw_kernel", "LUSE_VDW=true")
        else:
            add("vdw_kernel.bindat", "LUSE_VDW=true")
    if any(key in incar for key in ("SHAKEMAXITER", "SHAKETOL")) \
            or _truthy(inputs.get("uses_iconst")):
        add("ICONST", "SHAKE/declared constrained dynamics")
    ml_mode = str(incar.get("ML_MODE") or "").strip().lower()
    ml_start = _integer(incar.get("ML_ISTART"))
    if (ml_start is not None and ml_start > 0) or ml_mode in {"run", "select", "refit"}:
        add("ML_FF", f"ML_ISTART={ml_start} / ML_MODE={ml_mode or 'unset'}")
    if _truthy(inputs.get("uses_kpoints_opt")) or (root / "KPOINTS_OPT").is_file():
        add("KPOINTS_OPT", "declared/present VASP KPOINTS_OPT input")
    if task == "dimer":
        add("MODECAR", "Dimer initial mode")

    declared = inputs.get("required_files")
    if declared is not None:
        if not isinstance(declared, list):
            invalid.append("inputs.required_files")
        elif len(declared) > _MAX_INPUT_FILES:
            invalid.append(f"inputs.required_files exceeds {_MAX_INPUT_FILES} entries")
        else:
            for raw in declared:
                name = _safe_relative_name(raw, nested_neb=(task == "neb"))
                if name is None:
                    invalid.append(f"invalid required input:{raw}")
                else:
                    add(name, "manifest-declared scientific dependency")
    if len(reasons) > _MAX_INPUT_FILES:
        invalid.append(f"scientific input closure exceeds {_MAX_INPUT_FILES} files")
        reasons = dict(list(reasons.items())[:_MAX_INPUT_FILES])
    return list(reasons), reasons, invalid


def resolve_input_closure(job_dir: str | os.PathLike[str],
                          manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Hash the current required input closure, rejecting links and escapes."""
    root = Path(job_dir).expanduser().resolve()
    names, reasons, invalid = required_input_names(root, manifest)
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    missing: list[str] = list(invalid)
    resource_limits: list[str] = []
    total_bytes = 0
    for name in names:
        path = root.joinpath(*name.split("/"))
        try:
            resolved = path.resolve(strict=True)
            inside = os.path.normcase(os.path.commonpath((str(root), str(resolved)))) == \
                os.path.normcase(str(root))
        except (OSError, ValueError):
            inside = False
        relative_parts = Path(name).parts
        traversed = root
        has_link = False
        for part in relative_parts:
            traversed /= part
            if traversed.is_symlink():
                has_link = True
                break
        if not inside or has_link or not path.is_file():
            missing.append(name)
            continue
        try:
            digest, size = _sha256_file(path, byte_limit=_file_byte_limit(name))
            if total_bytes + size > _MAX_TOTAL_BYTES:
                raise _InputResourceLimit(
                    f"closure exceeds {_MAX_TOTAL_BYTES} bytes")
            hashes[name] = digest
            sizes[name] = size
            total_bytes += size
        except _InputResourceLimit as exc:
            missing.append(name)
            resource_limits.append(f"{name}:{exc}")
        except OSError:
            missing.append(name)
    payload = {
        "schema": INPUT_CLOSURE_SCHEMA,
        "engine": str((_manifest_inputs(manifest)).get("engine") or "vasp").lower(),
        "task_type": str(manifest.get("task_type") or "").lower(),
        "files": hashes,
        "sizes": sizes,
        "requirements": {name: reasons[name] for name in names},
        "missing": sorted(set(missing)),
        "resource_limits": sorted(set(resource_limits)),
        "total_bytes": total_bytes,
    }
    payload["digest"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    payload["status"] = "complete" if not payload["missing"] else "incomplete"
    return payload


def record_input_closure(job_dir: str | os.PathLike[str], manifest: dict[str, Any]) -> dict[str, Any]:
    """Bind the current closure into a manifest before its atomic save."""
    closure = resolve_input_closure(job_dir, manifest)
    inputs = manifest.setdefault("inputs", {})
    if not isinstance(inputs, dict):
        raise ValueError("manifest inputs must be a mapping")
    inputs["input_closure"] = {
        "schema": closure["schema"], "status": closure["status"],
        "digest": closure["digest"], "files": dict(closure["files"]),
        "sizes": dict(closure["sizes"]),
        "requirements": dict(closure["requirements"]),
        "missing": list(closure["missing"]),
        "resource_limits": list(closure["resource_limits"]),
        "total_bytes": closure["total_bytes"],
    }
    # Keep the established map as a compatibility projection, now containing
    # every actual dependency rather than only the historical quartet.
    inputs["sha256"] = dict(closure["files"])
    if "POTCAR" in closure["files"]:
        inputs["potcar_sha256"] = closure["files"]["POTCAR"]
    return closure


def recorded_closure(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = _manifest_inputs(manifest)
    value = inputs.get("input_closure")
    return value if isinstance(value, Mapping) else {}


def closure_record_matches(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    if recorded.get("schema") != INPUT_CLOSURE_SCHEMA:
        return False
    if not _HEX64_RE.fullmatch(str(recorded.get("digest") or "")):
        return False
    return (
        recorded.get("status") == current.get("status")
        and recorded.get("digest") == current.get("digest")
        and dict(recorded.get("files") or {}) == dict(current.get("files") or {})
        and dict(recorded.get("sizes") or {}) == dict(current.get("sizes") or {})
        and list(recorded.get("missing") or []) == list(current.get("missing") or [])
        and list(recorded.get("resource_limits") or [])
        == list(current.get("resource_limits") or [])
        and recorded.get("total_bytes") == current.get("total_bytes")
    )


__all__ = [
    "INPUT_CLOSURE_SCHEMA", "closure_record_matches", "record_input_closure",
    "recorded_closure", "required_input_names", "resolve_input_closure",
]
