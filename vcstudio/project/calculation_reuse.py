"""Strict scientific fingerprints and explainable calculation reuse.

This module deliberately separates three things that are easy to conflate:

* a versioned, complete scientific input identity;
* a bounded advisory index rebuilt from authoritative job manifests/files; and
* an explicit, durable user decision to reference a verified prior result.

The method-recipe subsystem remains the authority for recipe semantics.  This
module only consumes its canonical ``semantic_sha256``.  A legacy manifest
without that binding is ``incomplete`` and can never produce an exact-match or
reuse-eligible conclusion.
"""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any, Callable, Iterable, Mapping

import yaml

from vcstudio.generate.method_recipe import (
    METHOD_RECIPE_AUTHORITY,
    METHOD_RECIPE_SCHEMA,
)
from vcstudio.project.reuse_verification import verify_outputs
from vcstudio.shared import manifest as manifest_mod
from vcstudio.shared.execution_environment import (
    validate_execution_environment,
)
from vcstudio.shared.scientific_inputs import (
    INPUT_CLOSURE_SCHEMA,
    closure_record_matches,
    recorded_closure,
    required_input_names,
    resolve_input_closure,
)


FINGERPRINT_SCHEMA = "vcstudio.scientific-fingerprint/v1"
INDEX_SCHEMA = "vcstudio.calculation-reuse-index/v1"
ADVISORY_SCHEMA = "vcstudio.calculation-reuse-advisory/v1"
REUSE_DECISION_SCHEMA = "vcstudio.calculation-reuse-decision/v1"
FORCE_DECISION_SCHEMA = "vcstudio.force-recalculation-decision/v1"
PROVENANCE_SCHEMA = "vcstudio.job-provenance/v1"
AUTHORITATIVE_LOOKUP_SCHEMA = "vcstudio.authoritative-reuse-lookup/v1"
INDEX_LIMIT = 512
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_NODES = 100_000
MAX_CANONICAL_TEXT_BYTES = 32 * 1024 * 1024
MAX_POTCAR_BYTES = 512 * 1024 * 1024
MAX_INPUT_FILE_BYTES = 128 * 1024 * 1024 * 1024
MAX_AUTHORITATIVE_SCAN = 100_000
MAX_AUTHORITATIVE_MATCHES = 128
MAX_AUTHORITATIVE_VERIFICATIONS = 4096
MAX_ADVISORY_TARGETS = 32
MAX_ADVISORY_NEAR = 32

_INPUT_FILES = ("POSCAR", "INCAR", "KPOINTS", "POTCAR")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REUSE_DIAGNOSIS_FIELDS = (
    "failure_class", "evidence", "restartable", "exit_code", "clean_exit",
    "normal_termination", "task_converged", "ionic_converged",
    "electronic_converged", "max_steps_hit", "failed", "engine", "parser",
    "parser_version",
)

_CONTROL_GROUPS = {
    "constraints": (
        "IBRION", "NSW", "ISIF", "I_CONSTRAINED_M", "LAMBDA", "SHAKEMAXITER",
    ),
    "charge": ("NELECT",),
    "spin": (
        "ISPIN", "MAGMOM", "NUPDOWN", "LSORBIT", "LNONCOLLINEAR", "SAXIS",
    ),
    "hubbard_u": ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ", "LMAXMIX"),
    "dispersion": (
        "IVDW", "LUSE_VDW", "VDW_RADIUS", "VDW_S6", "VDW_S8", "VDW_SR", "VDW_A1",
        "VDW_A2", "BPARAM", "CPARAM", "ZAB_VDW",
    ),
    "solvent": (
        "LSOL", "EB_K", "TAU", "LAMBDA_D_K", "NC_K", "LRHOB", "LION", "C_MOLAR",
        "R_B", "R_CAV", "R_DIEL", "DIELECTRIC_CONST",
    ),
}


class ScientificFingerprintError(RuntimeError):
    """A strict fingerprint or reuse transaction failed closed."""


class ReuseConflictError(ScientificFingerprintError):
    """An idempotency key was reused for different semantic input."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _json_digest(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _canonical_number(value: str) -> str:
    raw = str(value).strip()
    try:
        number = Decimal(raw.replace("D", "E").replace("d", "e"))
    except InvalidOperation:
        return raw.upper()
    if not number.is_finite():
        raise ValueError("non-finite scientific input")
    if number.is_zero():
        return "0"
    rendered = format(number.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_token(value: str) -> str:
    raw = str(value).strip()
    logical = raw.strip(".").upper()
    if logical in {"T", "TRUE"}:
        return "T"
    if logical in {"F", "FALSE"}:
        return "F"
    return _canonical_number(raw)


def canonical_incar(text: str) -> dict[str, str]:
    """Canonicalise the complete effective INCAR mapping (last key wins)."""
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].split("!", 1)[0]
        for raw_part in line.split(";"):
            if "=" not in raw_part:
                continue
            key, raw_value = raw_part.split("=", 1)
            key = key.strip().upper()
            if not key:
                continue
            tokens = raw_value.replace(",", " , ").split()
            values[key] = " ".join(_canonical_token(token) for token in tokens)
    return {key: values[key] for key in sorted(values)}


def canonical_kpoints(text: str) -> str:
    """Canonicalise every effective KPOINTS line while ignoring its comment line."""
    lines = str(text or "").splitlines()
    if len(lines) < 3:
        raise ValueError("KPOINTS is incomplete")
    records = []
    for raw_line in lines[1:]:
        effective = raw_line.split("!", 1)[0].split("#", 1)[0].strip()
        if effective:
            records.append(" ".join(_canonical_token(token) for token in effective.split()))
    if len(records) < 2:
        raise ValueError("KPOINTS is incomplete")
    return "\n".join(records)


def canonical_poscar(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a formatting-stable complete POSCAR identity and safe summary.

    Atom order, selective-dynamics flags and any velocity or predictor-corrector
    tail remain part of the identity.  Direct and Cartesian coordinate modes
    intentionally converge on the same physical fractional representation.
    """
    from vcstudio.engines.calcspec import parse_structure

    raw_lines = str(text or "").splitlines()
    if len(raw_lines) < 8:
        raise ValueError("POSCAR is incomplete")
    lines = [line.split("!", 1)[0].split("#", 1)[0].strip() for line in raw_lines[1:]]
    lines = [line for line in lines if line]
    if len(lines) < 7:
        raise ValueError("POSCAR is incomplete")

    species_tokens = lines[4].split()
    vasp4 = all(re.fullmatch(r"[+-]?\d+", token) for token in species_tokens)
    if vasp4:
        raise ValueError("VASP4 POSCAR lacks explicit element identity")
    species = [token[0].upper() + token[1:].lower() for token in species_tokens]
    try:
        counts = [int(token) for token in lines[5].split()]
    except ValueError as exc:
        raise ValueError("POSCAR atom counts are invalid") from exc
    if len(species) != len(counts) or not counts or any(count <= 0 for count in counts):
        raise ValueError("POSCAR species/counts are inconsistent")

    cursor = 6
    selective = lines[cursor][:1].upper() == "S"
    if selective:
        cursor += 1
    if cursor >= len(lines):
        raise ValueError("POSCAR coordinate mode is missing")
    coordinate_mode = lines[cursor][:1].upper()
    if coordinate_mode not in {"D", "C", "K"}:
        raise ValueError("POSCAR coordinate mode is invalid")
    cursor += 1
    natoms = sum(counts)
    if len(lines) < cursor + natoms:
        raise ValueError("POSCAR coordinates are incomplete")

    parsed = parse_structure(text)
    elements = list(parsed.get("elements") or [])
    cell = list(parsed.get("cell") or [])
    fractional = list(parsed.get("frac") or [])
    sd_flags = parsed.get("sd")
    if len(elements) != natoms or len(cell) != 3 or len(fractional) != natoms:
        raise ValueError("POSCAR structure parser returned incomplete data")

    def canonical_float(value: Any, *, periodic=False) -> str:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("POSCAR contains non-finite coordinates")
        if periodic:
            number %= 1.0
            if abs(number - 1.0) < 1e-12 or abs(number) < 1e-12:
                number = 0.0
        if abs(number) < 1e-12:
            number = 0.0
        return _canonical_number(format(number, ".12g"))

    canonical_cell = [[canonical_float(value) for value in row] for row in cell]
    coordinates = []
    constrained = 0
    for index, row in enumerate(fractional):
        flags: list[str] = []
        if sd_flags is not None:
            raw_flags = str(sd_flags[index] if index < len(sd_flags) else "").split()
            flags = [_canonical_token(token) for token in raw_flags[:3]]
            if len(flags) != 3 or any(flag not in {"T", "F"} for flag in flags):
                raise ValueError("POSCAR selective-dynamics flag is invalid")
            if flags != ["T", "T", "T"]:
                constrained += 1
        coordinates.append({
            "element": str(elements[index]),
            "fractional": [canonical_float(value, periodic=True) for value in row],
            "flags": flags,
        })

    tail = [
        " ".join(_canonical_token(token) for token in line.split())
        for line in lines[cursor + natoms:]
    ]
    canonical = {
        "normalization": "cell-angstrom+fractional-mod1-12significant/v1",
        "cell_angstrom": canonical_cell, "species": species, "counts": counts,
        "selective_dynamics": sd_flags is not None,
        "coordinates": coordinates, "tail": tail,
    }
    summary = {
        "formula": "".join(
            element + (str(count) if count != 1 else "")
            for element, count in zip(species, counts)
        ),
        "atom_count": natoms,
        "selective_dynamics": sd_flags is not None,
        "constrained_atoms": constrained,
        "coordinate_mode": "fractional-canonical",
    }
    return canonical, summary


def _recipe_binding(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], list[str], bool]:
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    recipe = inputs.get("method_recipe")
    if not isinstance(recipe, Mapping):
        recipe = {}
    # This is the only authoritative integration contract.  Legacy aliases do
    # not acquire authority merely because they contain hash-shaped text.
    digest = str(recipe.get("semantic_sha256") or "").strip().lower()
    schema = str(recipe.get("schema") or "").strip()
    authority = str(recipe.get("authority") or "").strip()
    missing = []
    if not digest:
        missing.append("method_recipe.semantic_sha256")
    elif not _HEX64_RE.fullmatch(digest):
        missing.append("method_recipe.semantic_sha256_invalid")
    if schema != METHOD_RECIPE_SCHEMA:
        missing.append("method_recipe.schema_authority")
    if authority != METHOD_RECIPE_AUTHORITY:
        missing.append("method_recipe.authority")
    return {
        "schema": schema or None,
        "authority": authority or None,
        "semantic_sha256": digest or None,
    }, missing, not digest


def _environment_binding(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    environment = inputs.get("execution_environment")
    if not isinstance(environment, Mapping):
        return {
            "schema": None, "authority": None, "engine": None,
            "vasp_version": None, "build_identity": None, "evidence": None,
        }, ["execution_environment.authoritative_binding"]
    try:
        return validate_execution_environment(environment), []
    except ValueError as exc:
        return {
            "schema": environment.get("schema"),
            "authority": environment.get("authority"),
            "engine": environment.get("engine"),
            "vasp_version": environment.get("vasp_version"),
            "build_identity": environment.get("build_identity"),
            "evidence": deepcopy(environment.get("evidence")),
        }, [f"execution_environment.invalid:{exc}"]


def _normalise_project_identity(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if re.fullmatch(r"[0-9a-f]{32}", lowered):
        return f"project-{lowered}"
    if re.fullmatch(r"project-[0-9a-f]{32}", lowered):
        return lowered
    remote = re.search(r"-([0-9a-f]{32})$", lowered)
    if remote:
        return f"project-{remote.group(1)}"
    return raw


def _manifest_project_identity(manifest: Mapping[str, Any]) -> str | None:
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    return _normalise_project_identity(
        manifest.get("project_uuid") or inputs.get("project_uuid")
        or inputs.get("remote_namespace") or ""
    )


def _bounded_yaml_nodes(value: Any) -> None:
    pending = [value]
    seen: set[int] = set()
    count = 0
    while pending:
        item = pending.pop()
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            count += len(item) + 1
            if count > MAX_MANIFEST_NODES:
                raise ScientificFingerprintError("authoritative job.yaml exceeds node limit")
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            else:
                pending.extend(item)


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > limit:
            raise ScientificFingerprintError(f"{label} exceeds byte limit")
        with path.open("rb") as handle:
            payload = handle.read(limit + 1)
    except OSError as exc:
        raise ScientificFingerprintError(f"{label} is unavailable") from exc
    if len(payload) > limit:
        raise ScientificFingerprintError(f"{label} exceeds byte limit")
    return payload


def _potcar_titel_identities(path: Path) -> list[str]:
    identities: list[str] = []
    try:
        if path.stat().st_size > MAX_POTCAR_BYTES:
            raise ScientificFingerprintError("POTCAR exceeds byte limit")
        with path.open("rb") as handle:
            for raw_line in handle:
                if len(raw_line) > 1024 * 1024:
                    raise ScientificFingerprintError("POTCAR line exceeds byte limit")
                if b"TITEL" in raw_line.upper():
                    identities.append(_sha256_bytes(raw_line.strip()))
                if len(identities) > 256:
                    raise ScientificFingerprintError("POTCAR TITEL count exceeds limit")
    except OSError as exc:
        raise ScientificFingerprintError("POTCAR is unavailable") from exc
    return identities


def _is_reparse_stat(value: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(value.st_mode)
        or (
            os.name == "nt"
            and int(getattr(value, "st_file_attributes", 0)) & 0x400
        )
    )


def _canonical_job_root(job_dir: str | os.PathLike[str]) -> Path:
    """Resolve a real directory while refusing symlink/reparse aliases."""
    raw = Path(os.path.abspath(os.fspath(Path(job_dir).expanduser())))
    current = Path(raw.anchor)
    try:
        for part in raw.parts[1:]:
            current /= part
            if _is_reparse_stat(os.lstat(current)):
                raise ScientificFingerprintError(
                    "job root aliases through a symlink or reparse point"
                )
        resolved = raw.resolve(strict=True)
        root_stat = os.stat(resolved, follow_symlinks=False)
    except ScientificFingerprintError:
        raise
    except OSError as exc:
        raise ScientificFingerprintError("job root is unavailable") from exc
    if _is_reparse_stat(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ScientificFingerprintError("job root is not a real directory")
    return resolved


def _open_windows_root_handle(
    root: Path, *, share_delete: bool
) -> tuple[int, tuple[str, int, int]]:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    share_mode = 0x1 | 0x2 | (0x4 if share_delete else 0)
    handle = create_file(
        str(root), 0, share_mode, None, 3,
        0x02000000 | 0x00200000, None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ScientificFingerprintError("job root identity is unavailable")
    information = _ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        close_handle(handle)
        raise ScientificFingerprintError("job root identity is unavailable")
    if int(information.file_attributes) & 0x400:
        close_handle(handle)
        raise ScientificFingerprintError(
            "job root aliases through a symlink or reparse point"
        )
    file_id = (
        (int(information.file_index_high) << 32)
        | int(information.file_index_low)
    )
    identity = "windows", int(information.volume_serial_number), file_id
    return int(handle), identity


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _windows_root_identity(root: Path) -> tuple[str, int, int]:
    handle, identity = _open_windows_root_handle(root, share_delete=True)
    _close_windows_handle(handle)
    return identity


def _root_identity(root: Path) -> tuple[str, int, int]:
    if os.name == "nt":
        return _windows_root_identity(root)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise ScientificFingerprintError("job root identity is unavailable") from exc
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode):
            raise ScientificFingerprintError("job root is not a real directory")
        return "posix", int(value.st_dev), int(value.st_ino)
    finally:
        os.close(descriptor)


@dataclass
class _RootBinding:
    root: Path
    identity: tuple[str, int, int]
    directory_fd: int | None = None
    windows_handle: int | None = None

    def close(self) -> None:
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None
        if self.windows_handle is not None:
            _close_windows_handle(self.windows_handle)
            self.windows_handle = None


def _bind_root(root: Path) -> _RootBinding:
    canonical = _canonical_job_root(root)
    if os.name == "nt":
        handle, identity = _open_windows_root_handle(
            canonical, share_delete=False
        )
        return _RootBinding(
            root=canonical, identity=identity, windows_handle=handle
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(canonical, flags)
        value = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ScientificFingerprintError("job root identity is unavailable") from exc
    return _RootBinding(
        root=canonical,
        identity=("posix", int(value.st_dev), int(value.st_ino)),
        directory_fd=descriptor,
    )


def _assert_root_binding(binding: _RootBinding) -> None:
    try:
        current = _root_identity(binding.root)
    except ScientificFingerprintError as exc:
        raise ReuseConflictError("job root identity changed during reuse") from exc
    if current != binding.identity:
        raise ReuseConflictError("job root identity changed during reuse")


def _load_bound_snapshot(binding: _RootBinding) -> dict[str, Any]:
    _assert_root_binding(binding)
    snapshot = _load_authoritative_snapshot(binding.root)
    _assert_root_binding(binding)
    return snapshot


def _load_authoritative_snapshot(job_dir: str | os.PathLike[str]) -> dict[str, Any]:
    root = _canonical_job_root(job_dir)
    manifest_path = root / manifest_mod.MANIFEST_NAME
    try:
        manifest_bytes = _read_bounded(manifest_path, MAX_MANIFEST_BYTES, "authoritative job.yaml")
        manifest = yaml.safe_load(manifest_bytes.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ScientificFingerprintError("authoritative job.yaml is invalid") from exc
    if not isinstance(manifest, dict):
        raise ScientificFingerprintError("authoritative job.yaml is invalid")
    _bounded_yaml_nodes(manifest)
    names, _, invalid = required_input_names(root, manifest)
    if invalid:
        input_size_issues = list(invalid)
    else:
        input_size_issues = []
    for name in names:
        path = root.joinpath(*name.split("/"))
        try:
            limit = MAX_POTCAR_BYTES if name == "POTCAR" else MAX_INPUT_FILE_BYTES
            if path.stat().st_size > limit:
                input_size_issues.append(f"input exceeds byte limit:{name}")
        except OSError:
            continue
    current_closure = resolve_input_closure(root, manifest)
    record = recorded_closure(manifest)
    files: dict[str, bytes] = {}
    task = str(manifest.get("task_type") or "").strip().lower()
    text_names = ["INCAR", "KPOINTS"]
    text_names.extend(
        sorted(name for name in current_closure.get("files", {}) if name.endswith("/POSCAR"))
        if task == "neb" else ["POSCAR"]
    )
    for name in text_names:
        try:
            files[name] = _read_bounded(
                root.joinpath(*name.split("/")), MAX_CANONICAL_TEXT_BYTES, name
            )
        except ScientificFingerprintError:
            continue
    return {
        "root": root, "manifest": manifest, "manifest_bytes": manifest_bytes,
        "manifest_sha256": _sha256_bytes(manifest_bytes), "files": files,
        "file_sha256": {name: _sha256_bytes(payload) for name, payload in files.items()},
        "current_closure": current_closure, "recorded_closure": record,
        "closure_matches": closure_record_matches(record, current_closure),
        "input_size_issues": input_size_issues,
    }


def _fingerprint_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    manifest = snapshot["manifest"]
    files = snapshot["files"]
    missing: list[str] = []
    integrity: list[str] = list(snapshot.get("input_size_issues") or [])
    components: dict[str, Any] = {}
    current_closure = snapshot.get("current_closure") or {}
    record = snapshot.get("recorded_closure") or {}
    if record.get("schema") != INPUT_CLOSURE_SCHEMA:
        missing.append("inputs.input_closure.authority")
    if current_closure.get("status") != "complete":
        missing.extend(f"input_closure:{name}" for name in current_closure.get("missing") or [])
    if record.get("status") != "complete":
        missing.append("inputs.input_closure.complete")
    if not snapshot.get("closure_matches"):
        integrity.append("current scientific input closure no longer matches job.yaml")
    closure_files = current_closure.get("files") if isinstance(
        current_closure.get("files"), Mapping
    ) else {}
    closure_semantic = {
        "required_files": sorted(closure_files),
        "content_identities": {
            name: digest for name, digest in sorted(closure_files.items())
            if name not in {"INCAR", "KPOINTS", "POSCAR"}
            and not name.endswith("/POSCAR")
        },
    }
    components["input_closure"] = {
        "sha256": _json_digest(closure_semantic),
        "file_count": len(closure_files),
        "files": sorted(closure_files),
    }

    incar: dict[str, str] = {}
    if "INCAR" in files:
        try:
            incar = canonical_incar(files["INCAR"].decode("utf-8"))
            if not incar:
                raise ValueError("INCAR has no effective assignments")
            components["incar"] = {
                "sha256": _json_digest(incar), "field_count": len(incar), "values": incar,
            }
        except (UnicodeError, ValueError) as exc:
            missing.append("INCAR.canonical")
            integrity.append(str(exc))

    if "KPOINTS" in files:
        try:
            kpoints = canonical_kpoints(files["KPOINTS"].decode("utf-8"))
            components["kpoints"] = {
                "sha256": _sha256_bytes(kpoints.encode("utf-8")),
                "line_count": len(kpoints.splitlines()),
            }
        except (UnicodeError, ValueError) as exc:
            missing.append("KPOINTS.canonical")
            integrity.append(str(exc))

    task_type = str(manifest.get("task_type") or "").strip().lower()
    structure_names = (
        sorted(name for name in closure_files if name.endswith("/POSCAR"))
        if task_type == "neb" else ["POSCAR"]
    )
    structures: list[dict[str, Any]] = []
    structure_summaries: list[dict[str, Any]] = []
    for name in structure_names:
        if name not in files:
            missing.append(f"{name}.canonical")
            continue
        try:
            structure, summary = canonical_poscar(files[name].decode("utf-8"))
            structures.append({"name": name, "structure": structure})
            structure_summaries.append({"name": name, **summary})
        except (UnicodeError, ValueError) as exc:
            missing.append(f"{name}.canonical")
            integrity.append(str(exc))
    if structures:
        components["structure"] = {
            "sha256": _json_digest(structures),
            "summary": (
                {"neb_images": structure_summaries, "image_count": len(structures)}
                if task_type == "neb" else structure_summaries[0]
            ),
        }

    potcar_digest = str(closure_files.get("POTCAR") or "").strip().lower()
    if not _HEX64_RE.fullmatch(potcar_digest):
        missing.append("input_closure.POTCAR")
    else:
        try:
            titel_hashes = _potcar_titel_identities(Path(snapshot["root"]) / "POTCAR")
            if not titel_hashes:
                missing.append("POTCAR.TITEL")
            components["potcar"] = {
                "content_sha256": potcar_digest, "titel_sha256": titel_hashes,
            }
        except ScientificFingerprintError as exc:
            missing.append("POTCAR.identity")
            integrity.append(str(exc))

    recipe, recipe_missing, legacy_recipe = _recipe_binding(manifest)
    environment, environment_missing = _environment_binding(manifest)
    missing.extend(recipe_missing)
    missing.extend(environment_missing)
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    manifest_identity = str(
        manifest.get("job_uuid") or manifest.get("job_id") or "").strip()
    if not _ID_RE.fullmatch(manifest_identity):
        missing.append("job_identity")
    task = {
        "engine": str(inputs.get("engine") or "vasp").strip().lower() or None,
        "task_type": task_type or None,
        "calc_type": str(manifest.get("calc_type") or "").strip().lower() or None,
        "method_recipe": recipe,
    }
    for field in ("engine", "task_type", "calc_type"):
        if not task[field]:
            missing.append(f"task.{field}")
    components["task"] = {"sha256": _json_digest(task), "values": task}
    components["environment"] = {
        "sha256": _json_digest(environment), "values": environment,
    }
    controls = {
        name: {key: incar[key] for key in keys if key in incar}
        for name, keys in _CONTROL_GROUPS.items()
    }
    structure_summary = (components.get("structure") or {}).get("summary") or {}
    if task_type == "neb":
        images = structure_summary.get("neb_images") or []
        controls["constraints"]["selective_dynamics"] = any(
            bool(item.get("selective_dynamics")) for item in images
        )
        controls["constraints"]["constrained_atoms"] = sum(
            int(item.get("constrained_atoms") or 0) for item in images
        )
    else:
        controls["constraints"]["selective_dynamics"] = bool(
            structure_summary.get("selective_dynamics"))
        controls["constraints"]["constrained_atoms"] = int(
            structure_summary.get("constrained_atoms") or 0)
    components["scientific_controls"] = {
        "sha256": _json_digest(controls), "values": controls,
    }

    missing = sorted(set(missing))
    integrity = sorted(set(integrity))
    status = "complete" if not missing and not integrity else "incomplete"
    semantic = {
        "schema": FINGERPRINT_SCHEMA,
        "structure_sha256": (components.get("structure") or {}).get("sha256"),
        "incar_sha256": (components.get("incar") or {}).get("sha256"),
        "kpoints_sha256": (components.get("kpoints") or {}).get("sha256"),
        "potcar": components.get("potcar"),
        "task": task,
        "environment": environment,
        "scientific_controls": controls,
        "input_closure_sha256": components["input_closure"]["sha256"],
    }
    return {
        "schema": FINGERPRINT_SCHEMA, "status": status,
        "digest": _json_digest(semantic) if status == "complete" else None,
        "legacy_recipe": legacy_recipe, "missing": missing,
        "recipe_status": ("explicit_legacy" if legacy_recipe else "canonical_recipe"),
        "integrity_issues": integrity, "components": components,
        "manifest_sha256": snapshot["manifest_sha256"],
        "project_identity": _manifest_project_identity(manifest),
        "input_closure_digest": current_closure.get("digest"),
    }


def build_scientific_fingerprint(job_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Build one fail-closed strict fingerprint from one manifest snapshot."""
    try:
        snapshot = _load_authoritative_snapshot(job_dir)
        return _fingerprint_from_snapshot(snapshot)
    except ScientificFingerprintError as exc:
        return {
            "schema": FINGERPRINT_SCHEMA, "status": "incomplete", "digest": None,
            "legacy_recipe": True, "missing": ["job.yaml"],
            "recipe_status": "explicit_legacy",
            "integrity_issues": [str(exc)], "components": {},
            "manifest_sha256": None, "project_identity": None,
            "input_closure_digest": None,
        }


def _fingerprint_snapshot_binding(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """Return the strict CAS fields tying a fingerprint to one input snapshot."""
    document = dict(fingerprint)
    return {
        "fingerprint_schema": document.get("schema"),
        "fingerprint_status": document.get("status"),
        "fingerprint_digest": document.get("digest"),
        "fingerprint_document_sha256": _json_digest(document),
        "input_closure_digest": document.get("input_closure_digest"),
        "manifest_sha256": document.get("manifest_sha256"),
    }


def _provided_fingerprint_matches(
    provided: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> bool:
    if provided is None:
        return True
    try:
        return (
            _fingerprint_snapshot_binding(provided)
            == _fingerprint_snapshot_binding(current)
            and _canonical_json(dict(provided)) == _canonical_json(dict(current))
        )
    except (TypeError, ValueError):
        return False


def _source_verification_from_snapshot(
    snapshot: Mapping[str, Any], fingerprint: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    # A caller-provided/indexed fingerprint is only an expected CAS value.  It
    # is never promoted to current evidence: rebuild from this exact manifest
    # and input snapshot on every verification.
    current_fingerprint = _fingerprint_from_snapshot(snapshot)
    fingerprint_matches = _provided_fingerprint_matches(
        fingerprint, current_fingerprint
    )
    root = Path(snapshot["root"])
    manifest = snapshot["manifest"]
    state = str(manifest.get("state") or "").strip().upper()
    bindings = _fingerprint_snapshot_binding(current_fingerprint)
    if state == "FAILED":
        return {
            "status": "failed", "reusable": False, "issues": [],
            "result_files": {}, "result_bundle_sha256": None,
            "bindings": {
                **bindings, "result_bundle_sha256": None,
                "result_parser_sha256": None,
            },
        }
    if state == "UNCONVERGED":
        return {
            "status": "unconverged", "reusable": False,
            "issues": [], "result_files": {}, "result_bundle_sha256": None,
            "bindings": {
                **bindings, "result_bundle_sha256": None,
                "result_parser_sha256": None,
            },
        }
    issues = []
    if current_fingerprint.get("status") != "complete":
        issues.extend(current_fingerprint.get("missing") or [])
        issues.extend(current_fingerprint.get("integrity_issues") or [])
    if not fingerprint_matches:
        issues.append(
            "provided scientific fingerprint does not match current authoritative snapshot"
        )
    if state != "DONE":
        issues.append(f"source state is {state or 'unknown'}, not DONE")
    parsed = verify_outputs(root, manifest)
    issues.extend(parsed.get("issues") or [])
    result_files = parsed.get("result_files") or {}
    result_bundle_sha256 = _json_digest(result_files) if result_files else None
    parser = deepcopy(parsed.get("parser"))
    parser_sha256 = _json_digest(parser) if isinstance(parser, Mapping) else None
    status = parsed.get("contradiction_status") or (
        "verified" if not issues else "incomplete"
    )
    return {
        "status": status, "reusable": status == "verified", "issues": sorted(set(issues)),
        "result_files": result_files,
        "result_bundle_sha256": result_bundle_sha256,
        "bindings": {
            **bindings, "result_bundle_sha256": result_bundle_sha256,
            "result_parser_sha256": parser_sha256,
        },
        "energy_e0_eV": parsed.get("energy_e0_eV") if status == "verified" else None,
        "parser": parser,
        "neb_energies_eV": deepcopy(parsed.get("neb_energies_eV")),
    }


def source_verification(job_dir: str | os.PathLike[str],
                        fingerprint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Verify convergence from the same hash-bound authoritative snapshot."""
    try:
        snapshot = _load_authoritative_snapshot(job_dir)
    except ScientificFingerprintError as exc:
        return {"status": "incomplete", "reusable": False,
                "issues": [str(exc)], "result_files": {}}
    return _source_verification_from_snapshot(snapshot, fingerprint)


def _component_summary(component: Mapping[str, Any] | None) -> Any:
    component = component if isinstance(component, Mapping) else {}
    if "summary" in component:
        return deepcopy(component.get("summary"))
    values = component.get("values")
    if isinstance(values, Mapping):
        if "vasp_version" in values or "build_identity" in values:
            evidence = values.get("evidence") if isinstance(values.get("evidence"), Mapping) else {}
            return {
                "vasp_version": values.get("vasp_version"),
                "build_identity_sha256": _sha256_bytes(
                    str(values.get("build_identity") or "").encode("utf-8")
                ) if values.get("build_identity") else None,
                "evidence_kind": evidence.get("kind"),
                "evidence_sha256": evidence.get("sha256"),
            }
        if "method_recipe" in values:
            return {
                "engine": values.get("engine"), "task_type": values.get("task_type"),
                "calc_type": values.get("calc_type"),
                "method_recipe": deepcopy(values.get("method_recipe")),
            }
        if "field_count" not in component:
            groups = []
            total_fields = 0
            for group, group_value in list(sorted(values.items()))[:32]:
                if isinstance(group_value, Mapping):
                    names = sorted(str(name) for name in group_value)[:64]
                    total_fields += len(group_value)
                    groups.append({
                        "group": str(group)[:64], "field_count": len(group_value),
                        "fields": names, "truncated": len(group_value) > len(names),
                    })
                else:
                    total_fields += 1
                    groups.append({"group": str(group)[:64], "field_count": 1})
            return {
                "group_count": len(values), "field_count": total_fields,
                "groups": groups, "truncated": len(values) > len(groups),
            }
    if "field_count" in component:
        return {"field_count": component.get("field_count")}
    if "line_count" in component:
        return {"line_count": component.get("line_count")}
    if "file_count" in component:
        return {"file_count": component.get("file_count")}
    if "content_sha256" in component:
        return {"content_sha256": component.get("content_sha256"),
                "titel_sha256": deepcopy(component.get("titel_sha256") or [])}
    return None


def public_fingerprint(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """Return a path-free, secret-free display projection."""
    fields = []
    for name in (
        "structure", "incar", "kpoints", "potcar", "input_closure", "task", "environment",
        "scientific_controls",
    ):
        component = fingerprint.get("components", {}).get(name, {})
        digest = component.get("sha256") or component.get("content_sha256")
        fields.append({"field": name, "digest": digest,
                       "summary": _component_summary(component)})
    return {
        "schema": fingerprint.get("schema"), "status": fingerprint.get("status"),
        "digest": fingerprint.get("digest"),
        "legacy_recipe": bool(fingerprint.get("legacy_recipe")),
        "recipe_status": fingerprint.get("recipe_status"),
        "missing": list(fingerprint.get("missing") or []),
        "integrity_issues": list(fingerprint.get("integrity_issues") or []),
        "fields": fields,
    }


def fingerprint_differences(target: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict]:
    """Return component-level differences only; never an equivalence claim."""
    differences = []
    names = (
        "structure", "incar", "kpoints", "potcar", "input_closure", "task", "environment",
        "scientific_controls",
    )
    target_components = target.get("components") or {}
    source_components = source.get("components") or {}
    for name in names:
        left = target_components.get(name) or {}
        right = source_components.get(name) or {}
        left_digest = left.get("sha256") or left.get("content_sha256")
        right_digest = right.get("sha256") or right.get("content_sha256")
        if left_digest != right_digest:
            differences.append({
                "field": name, "target_digest": left_digest,
                "source_digest": right_digest,
                "target": _component_summary(left), "source": _component_summary(right),
            })
    for field in ("status", "missing", "integrity_issues"):
        if target.get(field) != source.get(field):
            differences.append({
                "field": field, "target": deepcopy(target.get(field)),
                "source": deepcopy(source.get(field)),
            })
    return differences


def estimate_saved_core_hours(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Estimate saved resources from source evidence without inventing precision."""
    results = manifest.get("results") if isinstance(manifest.get("results"), Mapping) else {}
    usage = results.get("resource_usage") if isinstance(results.get("resource_usage"), Mapping) else {}
    actual = usage.get("core_hours")
    if isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual):
        return {"status": "measured", "core_hours": round(float(actual), 6)}
    attempts = [item for item in (manifest.get("attempts") or []) if isinstance(item, Mapping)]
    attempt = attempts[-1] if attempts else {}
    cores = attempt.get("cores")
    walltime = str(attempt.get("walltime") or "").strip()
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", walltime)
    if (isinstance(cores, int) and not isinstance(cores, bool) and cores > 0 and match
            and int(match.group(2)) < 60 and int(match.group(3)) < 60):
        seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        return {"status": "requested_upper_bound",
                "core_hours": round(cores * seconds / 3600.0, 6)}
    return {"status": "unknown", "core_hours": None}


@dataclass(frozen=True)
class _IndexRecord:
    job_id: str
    project_id: str | None
    manifest_project_id: str | None
    project_identity_status: str
    job_dir: str
    manifest: dict[str, Any]
    fingerprint: dict[str, Any]
    verification: dict[str, Any]


def _project_identity_binding(
    manifest: Mapping[str, Any], registry_identity: Any
) -> tuple[str | None, str | None, str]:
    manifest_id = _manifest_project_identity(manifest)
    registry_id = _normalise_project_identity(registry_identity)
    if manifest_id and registry_id:
        return registry_id, manifest_id, "verified" if manifest_id == registry_id else "conflict"
    return registry_id, manifest_id, "unknown"


def _project_relation(left: _IndexRecord, right: _IndexRecord) -> str:
    if (
        left.project_identity_status != "verified"
        or right.project_identity_status != "verified"
        or not left.project_id
        or not right.project_id
    ):
        return "unknown"
    return "same" if left.project_id == right.project_id else "different"


class CalculationReuseIndex:
    """Bounded, rebuild-only local index; never a source of scientific truth."""

    def __init__(self, *, limit: int = INDEX_LIMIT):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 4096:
            raise ValueError("index limit must be between 1 and 4096")
        self.limit = limit
        self.records: list[_IndexRecord] = []
        self.total_entries = 0
        self.truncated = False
        self.resource_limit_reached = False

    def rebuild(self, entries: Iterable[tuple[str, Mapping[str, Any] | None]], *,
                job_id: Callable[[str, Mapping[str, Any]], str],
                project_id: Callable[[str, Mapping[str, Any]], str | None] | None = None
                ) -> "CalculationReuseIndex":
        source: deque[tuple[str, Mapping[str, Any] | None]] = deque(maxlen=self.limit)
        self.total_entries = 0
        self.resource_limit_reached = False
        for entry in entries:
            if self.total_entries >= MAX_AUTHORITATIVE_SCAN:
                self.resource_limit_reached = True
                break
            source.append(entry)
            self.total_entries += 1
        self.truncated = self.total_entries > self.limit or self.resource_limit_reached
        self.records = []
        for directory, listed_manifest in source:
            try:
                snapshot = _load_authoritative_snapshot(directory)
            except ScientificFingerprintError:
                # A ledger copy is not promoted to fact when job.yaml disappeared
                # or became invalid between listing and rebuild.
                continue
            manifest = snapshot["manifest"]
            if isinstance(listed_manifest, Mapping) and dict(listed_manifest) != manifest:
                # Rebuild from the current authoritative bytes, not the stale row.
                listed_manifest = manifest
            fingerprint = _fingerprint_from_snapshot(snapshot)
            verification = _source_verification_from_snapshot(snapshot, fingerprint)
            identifier = job_id(directory, manifest)
            if not _ID_RE.fullmatch(identifier):
                continue
            registry_id = project_id(directory, manifest) if project_id else None
            bound_id, manifest_id, identity_status = _project_identity_binding(
                manifest, registry_id
            )
            self.records.append(_IndexRecord(
                job_id=identifier,
                project_id=bound_id, manifest_project_id=manifest_id,
                project_identity_status=identity_status,
                job_dir=str(snapshot["root"]), manifest=manifest,
                fingerprint=fingerprint, verification=verification,
            ))
        return self

    def advisory(self, target_job_ids: Iterable[str], *, near_limit: int = 5) -> dict[str, Any]:
        requested = list(target_job_ids)
        if len(requested) > MAX_ADVISORY_TARGETS:
            raise ValueError(f"at most {MAX_ADVISORY_TARGETS} advisory targets are allowed")
        if not isinstance(near_limit, int) or not 0 <= near_limit <= MAX_ADVISORY_NEAR:
            raise ValueError(f"near_limit must be 0..{MAX_ADVISORY_NEAR}")
        by_id: dict[str, _IndexRecord] = {}
        duplicates = set()
        for record in self.records:
            if record.job_id in by_id:
                duplicates.add(record.job_id)
            by_id[record.job_id] = record
        if duplicates:
            raise ScientificFingerprintError("index contains duplicate opaque job IDs")
        missing = sorted(set(requested) - set(by_id))
        if missing:
            raise ScientificFingerprintError("one or more target job IDs are not authoritative")
        targets = []
        for target_id in requested:
            target = by_id[target_id]
            exact = []
            near = []
            statuses = {"failed": [], "unconverged": [], "incomplete": []}
            for candidate in self.records:
                if candidate.job_id == target.job_id:
                    continue
                candidate_status = candidate.verification.get("status") or "incomplete"
                same = bool(
                    target.fingerprint.get("status") == "complete"
                    and candidate.fingerprint.get("status") == "complete"
                    and target.fingerprint.get("digest")
                    and target.fingerprint.get("digest") == candidate.fingerprint.get("digest")
                )
                common = {
                    "source_job_id": candidate.job_id,
                    "source_project_id": candidate.project_id,
                    "project_relation": _project_relation(target, candidate),
                    "cross_project": (
                        _project_relation(target, candidate) == "different"
                        if _project_relation(target, candidate) != "unknown" else None
                    ),
                    "verification": {
                        "status": candidate_status,
                        "reusable": bool(candidate.verification.get("reusable")),
                        "issues": list(candidate.verification.get("issues") or []),
                        "result_bundle_sha256": candidate.verification.get(
                            "result_bundle_sha256"),
                    },
                    "saved_estimate": estimate_saved_core_hours(candidate.manifest),
                    "fingerprint": public_fingerprint(candidate.fingerprint),
                }
                if same:
                    exact.append(common)
                    if candidate_status in statuses:
                        statuses[candidate_status].append(candidate.job_id)
                    continue
                differences = fingerprint_differences(target.fingerprint, candidate.fingerprint)
                # A near match needs at least one major component in common.
                major = {"structure", "incar", "kpoints", "potcar", "task"}
                differing_major = {item["field"] for item in differences}.intersection(major)
                if len(differing_major) < len(major):
                    near.append({**common, "differences": differences,
                                 "not_equivalent": True})
                if candidate_status in statuses:
                    statuses[candidate_status].append(candidate.job_id)
            near.sort(key=lambda item: (len(item["differences"]), item["source_job_id"]))
            exact.sort(key=lambda item: item["source_job_id"])
            targets.append({
                "target_job_id": target.job_id, "target_project_id": target.project_id,
                "project_identity_status": target.project_identity_status,
                "fingerprint": public_fingerprint(target.fingerprint),
                "exact_matches": exact, "near_matches": near[:near_limit],
                "source_statuses": statuses,
                "requires_explicit_choice": any(
                    item["verification"]["reusable"] for item in exact),
                "default_action": "recalculate",
            })
        return {
            "schema": ADVISORY_SCHEMA, "ok": True, "advisory_only": True,
            "automatic_reuse": False, "equivalence_claim": False,
            "authorizes_submission": False, "requires_user_confirmation": True,
            "targets": targets,
            "index": {
                "schema": INDEX_SCHEMA, "capacity": self.limit,
                "indexed": len(self.records), "observed": self.total_entries,
                "truncated": self.truncated, "rebuildable": True,
                "authoritative": False,
            },
        }


def authoritative_reuse_lookup(
    entries: Iterable[tuple[str, Mapping[str, Any] | None]],
    target_job_ids: Iterable[str], *,
    job_id: Callable[[str, Mapping[str, Any]], str],
    project_id: Callable[[str, Mapping[str, Any]], str | None] | None = None,
) -> dict[str, Any]:
    """Full authoritative digest lookup/CAS, independent of the bounded advisory.

    ``complete=False`` means absence has no meaning and callers must fail closed.
    Exact-match presence remains reported even if its display list is truncated.
    """
    requested = list(target_job_ids)
    errors: list[str] = []
    if not requested or len(requested) > MAX_ADVISORY_TARGETS or len(set(requested)) != len(requested):
        return {
            "schema": AUTHORITATIVE_LOOKUP_SCHEMA, "authoritative": True,
            "complete": False, "authorizes_submission": False,
            "errors": ["target job IDs are empty, duplicated, or over limit"], "targets": [],
        }
    records: list[_IndexRecord] = []
    seen_ids: set[str] = set()
    complete = True
    for offset, (directory, _listed_manifest) in enumerate(entries):
        if offset >= MAX_AUTHORITATIVE_SCAN:
            complete = False
            errors.append("authoritative ledger scan limit reached")
            break
        try:
            snapshot = _load_authoritative_snapshot(directory)
            manifest = snapshot["manifest"]
            identifier = str(job_id(directory, manifest) or "")
            if not _ID_RE.fullmatch(identifier):
                raise ScientificFingerprintError("invalid authoritative job ID")
            if identifier in seen_ids:
                raise ScientificFingerprintError(f"duplicate authoritative job ID:{identifier}")
            seen_ids.add(identifier)
            registry_id = project_id(directory, manifest) if project_id else None
            bound_id, manifest_id, identity_status = _project_identity_binding(
                manifest, registry_id
            )
            records.append(_IndexRecord(
                job_id=identifier, project_id=bound_id,
                manifest_project_id=manifest_id,
                project_identity_status=identity_status,
                job_dir=str(snapshot["root"]), manifest=manifest,
                fingerprint=_fingerprint_from_snapshot(snapshot), verification={},
            ))
        except Exception as exc:  # authoritative scan failure must not prove absence
            complete = False
            errors.append(str(exc))
    by_id = {record.job_id: record for record in records}
    missing_targets = [identifier for identifier in requested if identifier not in by_id]
    if missing_targets:
        complete = False
        errors.append("one or more targets are absent from authoritative ledger")
    digest_buckets: dict[str, list[_IndexRecord]] = {}
    for record in records:
        digest = record.fingerprint.get("digest")
        if record.fingerprint.get("status") == "complete" and isinstance(digest, str):
            digest_buckets.setdefault(digest, []).append(record)
    targets = []
    verification_cache: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for target_id in requested:
        target = by_id.get(target_id)
        if target is None:
            continue
        matches = []
        match_count = 0
        for candidate in digest_buckets.get(str(target.fingerprint.get("digest")), []):
            if candidate.job_id == target_id:
                continue
            if candidate.job_id not in verification_cache:
                if len(verification_cache) >= MAX_AUTHORITATIVE_VERIFICATIONS:
                    complete = False
                    errors.append("authoritative source verification limit reached")
                    break
                try:
                    current_snapshot = _load_authoritative_snapshot(candidate.job_dir)
                    current_fingerprint = _fingerprint_from_snapshot(current_snapshot)
                    verification = _source_verification_from_snapshot(
                        current_snapshot, candidate.fingerprint
                    )
                except ScientificFingerprintError as exc:
                    complete = False
                    errors.append(str(exc))
                    continue
                verification_cache[candidate.job_id] = (
                    current_fingerprint, verification
                )
            current_fingerprint, verification = verification_cache[candidate.job_id]
            if not _provided_fingerprint_matches(
                candidate.fingerprint, current_fingerprint
            ):
                complete = False
                errors.append(
                    f"authoritative source changed during lookup:{candidate.job_id}"
                )
                continue
            relation = _project_relation(target, candidate)
            if not verification.get("reusable"):
                continue
            match_count += 1
            if len(matches) < MAX_AUTHORITATIVE_MATCHES:
                matches.append({
                    "source_job_id": candidate.job_id,
                    "source_project_id": candidate.project_id,
                    "project_relation": relation,
                    "cross_project": (
                        relation == "different" if relation != "unknown" else None
                    ),
                    "verification": verification,
                    "fingerprint": public_fingerprint(current_fingerprint),
                    "cas": {
                        "fingerprint": current_fingerprint.get("digest"),
                        "fingerprint_document_sha256": verification.get(
                            "bindings", {}
                        ).get("fingerprint_document_sha256"),
                        "input_closure_digest": current_fingerprint.get(
                            "input_closure_digest"
                        ),
                        "manifest_sha256": current_fingerprint.get("manifest_sha256"),
                        "result_bundle_sha256": verification.get("result_bundle_sha256"),
                    },
                })
        matches.sort(key=lambda item: item["source_job_id"])
        targets.append({
            "target_job_id": target_id,
            "fingerprint": public_fingerprint(target.fingerprint),
            "project_identity_status": target.project_identity_status,
            "exact_matches": matches, "match_count": match_count,
            "matches_truncated": match_count > len(matches),
            "requires_explicit_choice": match_count > 0,
            "absence_authoritative": False,
        })
    for identifier in requested:
        target = by_id.get(identifier)
        if target is None:
            continue
        try:
            current_snapshot = _load_authoritative_snapshot(target.job_dir)
            current_fingerprint = _fingerprint_from_snapshot(current_snapshot)
        except ScientificFingerprintError as exc:
            complete = False
            errors.append(str(exc))
            continue
        if not _provided_fingerprint_matches(target.fingerprint, current_fingerprint):
            complete = False
            errors.append(f"authoritative target changed during lookup:{identifier}")
    for target in targets:
        target["absence_authoritative"] = bool(
            complete
            and target["fingerprint"].get("status") == "complete"
            and target["match_count"] == 0
        )
    return {
        "schema": AUTHORITATIVE_LOOKUP_SCHEMA, "authoritative": True,
        "complete": complete, "authorizes_submission": False,
        "errors": sorted(set(errors))[:128], "targets": targets,
        "scanned": len(records), "scan_limit": MAX_AUTHORITATIVE_SCAN,
    }


def _decision_key(value: str) -> str:
    key = str(value or "").strip()
    if not _ID_RE.fullmatch(key):
        raise ValueError("decision_id must be an opaque 1-128 character identifier")
    return key


def _job_identity(manifest: Mapping[str, Any]) -> str:
    value = str(manifest.get("job_uuid") or manifest.get("job_id") or "").strip()
    if not _ID_RE.fullmatch(value):
        raise ScientificFingerprintError("job manifest lacks a stable opaque identity")
    return value


def _decision_replay(manifest: Mapping[str, Any], decision_id: str,
                     expected_request_sha256: str) -> dict[str, Any] | None:
    for decision in manifest.get("reuse_decisions") or []:
        if not isinstance(decision, Mapping) or decision.get("decision_id") != decision_id:
            continue
        if decision.get("request_sha256") != expected_request_sha256:
            raise ReuseConflictError("decision_id already binds different reuse input")
        return deepcopy(dict(decision))
    return None


def _operation_locks(paths: Iterable[str | os.PathLike[str]], action: str):
    from vcstudio.cluster.submitter import job_operation

    stack = ExitStack()
    ordered = sorted({os.path.normcase(os.path.realpath(os.path.abspath(str(path))))
                      for path in paths})
    for path in ordered:
        stack.enter_context(job_operation(path, action))
    return stack


def _reuse_journal_path(target_dir: str | os.PathLike[str], decision_id: str) -> Path:
    suffix = _sha256_bytes(decision_id.encode("utf-8"))[:20]
    return Path(target_dir).resolve() / f".reuse-journal-{suffix}.json"


def _write_reuse_journal(path: Path, value: Mapping[str, Any]) -> None:
    payload = (_canonical_json(dict(value)) + "\n").encode("utf-8")
    if len(payload) > 1024 * 1024:
        raise ScientificFingerprintError("reuse journal exceeds byte limit")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            parent_fd = os.open(str(path.parent), flags)
        except OSError:
            pass
        else:
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            finally:
                os.close(parent_fd)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _load_reuse_journal(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = _read_bounded(path, 1024 * 1024, "reuse journal")
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReuseConflictError("reuse journal is invalid") from exc
    if not isinstance(value, dict):
        raise ReuseConflictError("reuse journal is invalid")
    return value


def _manifest_payload(manifest: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(
        dict(manifest), allow_unicode=True, sort_keys=False
    ).encode("utf-8")


def _write_bound_bytes(binding: _RootBinding, name: str, payload: bytes) -> None:
    """Atomically write a root-level file without resolving the root path."""
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise ScientificFingerprintError("invalid root-relative write target")
    if binding.directory_fd is None:
        _assert_root_binding(binding)
        target = binding.root / name
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        _assert_root_binding(binding)
        return
    root_fd = binding.directory_fd
    temporary_name = f".{name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=root_fd)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd
        )
        os.fsync(root_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass


def _parse_reuse_journal(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReuseConflictError("reuse journal is invalid") from exc
    if not isinstance(value, dict):
        raise ReuseConflictError("reuse journal is invalid")
    return value


def _load_bound_reuse_journal(
    binding: _RootBinding, name: str
) -> dict[str, Any] | None:
    if binding.directory_fd is None:
        _assert_root_binding(binding)
        value = _load_reuse_journal(binding.root / name)
        _assert_root_binding(binding)
        return value
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=binding.directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReuseConflictError("reuse journal is unavailable") from exc
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_size > 1024 * 1024:
            raise ReuseConflictError("reuse journal is invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(1024 * 1024 + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > 1024 * 1024:
        raise ReuseConflictError("reuse journal is invalid")
    return _parse_reuse_journal(payload)


def _write_bound_reuse_journal(
    binding: _RootBinding, name: str, value: Mapping[str, Any]
) -> None:
    if binding.directory_fd is None:
        _assert_root_binding(binding)
        _write_reuse_journal(binding.root / name, value)
        _assert_root_binding(binding)
        return
    payload = (_canonical_json(dict(value)) + "\n").encode("utf-8")
    if len(payload) > 1024 * 1024:
        raise ScientificFingerprintError("reuse journal exceeds byte limit")
    _write_bound_bytes(binding, name, payload)


def _save_bound_manifest(binding: _RootBinding, manifest: Mapping[str, Any]) -> str:
    payload = _manifest_payload(manifest)
    if binding.directory_fd is None:
        _assert_root_binding(binding)
        manifest_mod.save_manifest(binding.root, dict(manifest))
        _assert_root_binding(binding)
        return _sha256_bytes(payload)
    _write_bound_bytes(binding, manifest_mod.MANIFEST_NAME, payload)
    return _sha256_bytes(payload)


def _open_bound_parent(
    binding: _RootBinding, name: str, *, create: bool
) -> tuple[int, str]:
    if binding.directory_fd is None:
        raise ScientificFingerprintError("fd-anchored path is unavailable")
    parts = name.split("/")
    if (
        not parts
        or any(part in {"", ".", ".."} or "\\" in part for part in parts)
    ):
        raise ScientificFingerprintError("invalid result path")
    descriptor = os.dup(binding.directory_fd)
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _materialize_bound_result(
    source: _RootBinding, target: _RootBinding, name: str, expected_sha256: str
) -> None:
    """Copy one result via held directory fds, never via a replaceable root path."""
    source_parent, source_name = _open_bound_parent(source, name, create=False)
    target_parent, target_name = _open_bound_parent(target, name, create=True)
    temporary_name = (
        f".{target_name}.{os.getpid()}.{os.urandom(8).hex()}.reuse-tmp"
    )
    source_descriptor: int | None = None
    target_descriptor: int | None = None
    try:
        read_flags = os.O_RDONLY
        read_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            existing = os.open(target_name, read_flags, dir_fd=target_parent)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            try:
                if not stat.S_ISREG(os.fstat(existing).st_mode):
                    raise ScientificFingerprintError(
                        f"prepared reuse destination is not regular:{name}"
                    )
                if _sha256_descriptor(existing) == expected_sha256:
                    return
            finally:
                os.close(existing)
            raise ScientificFingerprintError(
                f"prepared reuse destination has conflicting bytes:{name}"
            )
        source_descriptor = os.open(source_name, read_flags, dir_fd=source_parent)
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ScientificFingerprintError(f"source result is not regular:{name}")
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        write_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        target_descriptor = os.open(
            temporary_name, write_flags, 0o600, dir_fd=target_parent
        )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                view = view[written:]
        os.fsync(target_descriptor)
        source_after = os.fstat(source_descriptor)
        if (
            (source_stat.st_dev, source_stat.st_ino, source_stat.st_size,
             source_stat.st_mtime_ns)
            != (source_after.st_dev, source_after.st_ino, source_after.st_size,
                source_after.st_mtime_ns)
            or digest.hexdigest() != expected_sha256
        ):
            raise ScientificFingerprintError(f"copied result hash mismatch:{name}")
        os.close(target_descriptor)
        target_descriptor = None
        os.replace(
            temporary_name, target_name,
            src_dir_fd=target_parent, dst_dir_fd=target_parent,
        )
        os.fsync(target_parent)
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if target_descriptor is not None:
            os.close(target_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=target_parent)
        except FileNotFoundError:
            pass
        os.close(source_parent)
        os.close(target_parent)


def record_reuse_reference(target_dir: str | os.PathLike[str],
                           source_dir: str | os.PathLike[str], *,
                           decision_id: str, reason: str,
                           target_project_id: str | None = None,
                           source_project_id: str | None = None,
                           before_commit: Callable[[], None] | None = None,
                           after_materialize: Callable[[str, int], None] | None = None,
                           ) -> dict[str, Any]:
    """Reference one verified source through a durable prepared/CAS boundary."""
    key = _decision_key(decision_id)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reuse reason is required")
    if len(reason) > 1000:
        raise ValueError("reuse reason is too long")
    target_entry = _canonical_job_root(target_dir)
    source_entry = _canonical_job_root(source_dir)
    with _operation_locks(
        (target_entry, source_entry), "reference existing result"
    ) as operation_stack:
        # Bind only after both canonical operation locks are held, then never
        # follow the caller's original alias again during this transaction.
        target_binding = _bind_root(target_entry)
        operation_stack.callback(target_binding.close)
        source_binding = _bind_root(source_entry)
        operation_stack.callback(source_binding.close)
        target_root = target_binding.root
        source_root = source_binding.root
        target_before = _load_bound_snapshot(target_binding)
        source_before = _load_bound_snapshot(source_binding)
        target_manifest = target_before["manifest"]
        source_manifest = source_before["manifest"]
        target_id = _job_identity(target_manifest)
        source_id = _job_identity(source_manifest)
        if target_id == source_id:
            raise ScientificFingerprintError("a job cannot reuse itself")
        target_fp = _fingerprint_from_snapshot(target_before)
        source_fp = _fingerprint_from_snapshot(source_before)
        target_registry, target_manifest_project, target_identity_status = \
            _project_identity_binding(target_manifest, target_project_id)
        source_registry, source_manifest_project, source_identity_status = \
            _project_identity_binding(source_manifest, source_project_id)
        if target_identity_status != "verified" or source_identity_status != "verified":
            raise ScientificFingerprintError(
                "project identity is unknown or conflicts with authoritative manifest"
            )
        project_relation = (
            "same" if target_registry == source_registry else "different"
        )
        request = {
            "action": "reference_existing_result", "target_job_id": target_id,
            "source_job_id": source_id, "target_fingerprint": target_fp.get("digest"),
            "source_fingerprint": source_fp.get("digest"), "reason": reason,
            "target_project_id": target_registry, "source_project_id": source_registry,
        }
        request_sha256 = _json_digest(request)
        if (target_fp.get("status") != "complete" or source_fp.get("status") != "complete"
                or target_fp.get("digest") != source_fp.get("digest")):
            raise ScientificFingerprintError("strict complete fingerprints do not match")
        verification = _source_verification_from_snapshot(source_before, source_fp)
        if not verification.get("reusable"):
            raise ScientificFingerprintError("source result is not complete, converged and verifiable")
        replay = _decision_replay(target_manifest, key, request_sha256)
        journal_path = _reuse_journal_path(target_root, key)
        journal = _load_bound_reuse_journal(target_binding, journal_path.name)
        if replay is None and journal is not None:
            if journal.get("request_sha256") != request_sha256:
                raise ReuseConflictError("decision journal already binds different input")
            if (
                journal.get("status") != "preparing"
                or journal.get("target_base_manifest_sha256")
                != target_before["manifest_sha256"]
            ):
                raise ReuseConflictError("orphaned reuse journal conflicts with target generation")
        if replay is not None:
            if (not journal or journal.get("request_sha256") != request_sha256
                    or replay.get("source_manifest_sha256") != source_before["manifest_sha256"]
                    or replay.get("source_result_bundle_sha256")
                    != verification.get("result_bundle_sha256")
                    or replay.get("source_verification_bindings")
                    != verification.get("bindings")
                    or journal.get("source_verification_bindings")
                    != verification.get("bindings")
                    or replay.get("scientific_fingerprint") != target_fp.get("digest")
                    or replay.get("source_fingerprint") != source_fp.get("digest")):
                raise ReuseConflictError("prepared reuse generation/source CAS changed")
            if replay.get("status") == "succeeded":
                if journal.get("status") == "succeeded":
                    if journal.get("done_manifest_sha256") != target_before["manifest_sha256"]:
                        raise ReuseConflictError("completed reuse target generation changed")
                elif journal.get("status") not in {"preparing", "prepared"}:
                    raise ReuseConflictError("completed reuse journal state is invalid")
                target_verification = _source_verification_from_snapshot(
                    target_before, target_fp
                )
                if (not target_verification.get("reusable")
                        or target_verification.get("result_bundle_sha256")
                        != replay.get("source_result_bundle_sha256")):
                    raise ScientificFingerprintError(
                        "materialised reuse result no longer verifies")
                # Recover the narrow crash window after the authoritative DONE
                # manifest commit and before the journal's succeeded marker.
                if journal.get("status") != "succeeded":
                    journal["status"] = "succeeded"
                    journal["done_manifest_sha256"] = target_before["manifest_sha256"]
                    _write_bound_reuse_journal(
                        target_binding, journal_path.name, journal
                    )
                return {"ok": True, "replayed": True, "decision": replay,
                        "target_job_id": target_id, "source_job_id": source_id,
                        "state": target_manifest.get("state"),
                        "accepted_inherited": False, "final_inherited": False}
            if replay.get("status") != "prepared":
                raise ScientificFingerprintError("reuse decision has an unknown recovery status")
            if journal.get("prepared_manifest_sha256") != target_before["manifest_sha256"]:
                raise ReuseConflictError("prepared reuse target generation changed")
            if str(target_manifest.get("state") or "").upper() != "CREATED":
                raise ScientificFingerprintError("prepared reuse decision has invalid target state")
            updated = deepcopy(target_manifest)
            decision = next(
                item for item in updated.get("reuse_decisions") or []
                if isinstance(item, dict) and item.get("decision_id") == key
            )
        else:
            if str(target_manifest.get("state") or "").upper() != "CREATED":
                raise ScientificFingerprintError(
                    "only an unsubmitted CREATED job can reference a result")
            if target_manifest.get("cluster") or target_manifest.get("scheduler_job_id"):
                raise ScientificFingerprintError("target job is already bound to a remote submission")
            if before_commit is not None:
                before_commit()
            target_after = _load_bound_snapshot(target_binding)
            source_after = _load_bound_snapshot(source_binding)
            target_fp_after = _fingerprint_from_snapshot(target_after)
            source_fp_after = _fingerprint_from_snapshot(source_after)
            verification_after = _source_verification_from_snapshot(
                source_after, source_fp_after
            )
            if (target_after["manifest_sha256"] != target_before["manifest_sha256"]
                    or source_after["manifest_sha256"] != source_before["manifest_sha256"]
                    or target_fp_after.get("digest") != target_fp.get("digest")
                    or source_fp_after.get("digest") != source_fp.get("digest")
                    or verification_after.get("bindings")
                    != verification.get("bindings")
                    or not verification_after.get("reusable")):
                raise ScientificFingerprintError("source or target changed during reuse validation")
            now = _now()
            decision = {
                "schema": REUSE_DECISION_SCHEMA, "decision_id": key,
                "request_sha256": request_sha256, "action": "reference_existing_result",
                "status": "prepared", "decided_at": now,
                "actor": "manual-local-user", "reason": reason,
                "target_job_id": target_id, "source_job_id": source_id,
                "target_project_id": target_registry,
                "source_project_id": source_registry,
                "target_manifest_project_id": target_manifest_project,
                "source_manifest_project_id": source_manifest_project,
                "project_relation": project_relation,
                "cross_project": project_relation == "different",
                "scientific_fingerprint": target_fp["digest"],
                "source_fingerprint": source_fp["digest"],
                "source_manifest_sha256": source_after["manifest_sha256"],
                "source_result_bundle_sha256": verification_after["result_bundle_sha256"],
                "source_result_files": deepcopy(verification_after["result_files"]),
                "source_verification_bindings": deepcopy(
                    verification_after["bindings"]
                ),
                "source_verification_status": "verified",
                "source_parser": deepcopy(verification_after.get("parser")),
                "target_base_manifest_sha256": target_after["manifest_sha256"],
                "accepted_inherited": False, "final_inherited": False,
                "remote_effect": False,
            }
            updated = deepcopy(target_manifest)
            updated.setdefault("reuse_decisions", []).append(decision)
            provenance = updated.setdefault("provenance", {})
            if not isinstance(provenance, dict):
                raise ScientificFingerprintError("target provenance section is invalid")
            provenance.setdefault("schema", PROVENANCE_SCHEMA)
            nodes = provenance.setdefault("nodes", [])
            links = provenance.setdefault("links", [])
            if not isinstance(nodes, list) or not isinstance(links, list):
                raise ScientificFingerprintError("target provenance graph is invalid")
            nodes.extend([
                {
                    "id": target_id, "kind": "calculation_reference",
                    "project_id": target_registry,
                    "scientific_fingerprint": target_fp["digest"], "created_at": now,
                },
                {
                    "id": source_id, "kind": "source_calculation",
                    "project_id": source_registry,
                    "scientific_fingerprint": source_fp["digest"],
                    "verification": "verified",
                },
            ])
            links.append({
                "id": key, "type": "reuses", "from": target_id, "to": source_id,
                "decision_id": key, "project_relation": project_relation,
                "cross_project": project_relation == "different",
            })
            source_results = source_after["manifest"].get("results") \
                if isinstance(source_after["manifest"].get("results"), Mapping) else {}
            results = updated.setdefault("results", {})
            if not isinstance(results, dict):
                raise ScientificFingerprintError("target results section is invalid")
            energy = verification_after.get("energy_e0_eV")
            if (isinstance(energy, (int, float)) and not isinstance(energy, bool)
                    and math.isfinite(float(energy))):
                results["energy_e0_eV"] = float(energy)
            results["reuse_reference"] = {
                "decision_id": key, "source_job_id": source_id,
                "scientific_fingerprint": source_fp["digest"],
                "result_bundle_sha256": verification_after["result_bundle_sha256"],
                "verification_status": "verified",
                "parser": deepcopy(verification_after.get("parser")),
            }
            source_diagnosis = source_results.get("diagnosis")
            results["diagnosis"] = {
                field: deepcopy(source_diagnosis[field])
                for field in _REUSE_DIAGNOSIS_FIELDS
                if isinstance(source_diagnosis, Mapping) and field in source_diagnosis
            }
            results["diagnosis"]["evidence_source"] = "verified_reuse_reference"
            results["fetched_sha256"] = deepcopy(verification_after["result_files"])
            results["fetched"] = sorted(verification_after["result_files"])
            results["fetched_missing"] = []
            results["fetch_contract"] = {
                "schema": 1, "mode": "verified_reuse_reference", "state": "DONE",
                "source_job_id": source_id, "decision_id": key,
            }
            updated.setdefault("attempts", []).append({
                "n": len(updated.get("attempts") or []) + 1, "at": now,
                "result": "reuse_prepared", "decision_id": key,
                "remote_effect": False,
            })
            prepared_payload = _manifest_payload(updated)
            prepared_sha256 = _sha256_bytes(prepared_payload)
            journal = {
                "schema": "vcstudio.reuse-materialization-journal/v1",
                "decision_id": key, "request_sha256": request_sha256,
                "target_base_manifest_sha256": target_after["manifest_sha256"],
                "prepared_manifest_sha256": prepared_sha256,
                "target_fingerprint": target_fp["digest"],
                "source_manifest_sha256": source_after["manifest_sha256"],
                "source_result_files": deepcopy(verification_after["result_files"]),
                "source_result_bundle_sha256": verification_after["result_bundle_sha256"],
                "source_verification_bindings": deepcopy(
                    verification_after["bindings"]
                ),
                "status": "preparing", "decided_at": now,
            }
            _write_bound_reuse_journal(target_binding, journal_path.name, journal)
            _save_bound_manifest(target_binding, updated)
            persisted_prepared = _load_bound_snapshot(target_binding)
            if persisted_prepared["manifest_sha256"] != prepared_sha256:
                raise ReuseConflictError("prepared target manifest generation mismatch")
            journal["status"] = "prepared"
            _write_bound_reuse_journal(target_binding, journal_path.name, journal)

        verification_after = {
            "result_files": deepcopy(decision["source_result_files"]),
            "result_bundle_sha256": decision["source_result_bundle_sha256"],
        }
        recovery_suffix = _sha256_bytes(key.encode("utf-8"))[:12]
        for materialized_count, (name, digest) in enumerate(
                sorted(verification_after["result_files"].items()), start=1):
            _assert_root_binding(target_binding)
            _assert_root_binding(source_binding)
            if target_binding.directory_fd is not None:
                _materialize_bound_result(
                    source_binding, target_binding, name, digest
                )
                if after_materialize is not None:
                    after_materialize(name, materialized_count)
                _assert_root_binding(target_binding)
                _assert_root_binding(source_binding)
                continue
            destination = target_root.joinpath(*name.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_root_binding(target_binding)
            if destination.exists():
                if manifest_mod.sha256_file(destination) == digest:
                    _assert_root_binding(target_binding)
                    continue
                raise ScientificFingerprintError(
                    f"prepared reuse destination has conflicting bytes:{name}")
            temporary = destination.with_name(
                f".{destination.name}.{recovery_suffix}.reuse-tmp"
            )
            with open(source_root.joinpath(*name.split("/")), "rb") as source_handle, \
                    open(temporary, "wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            _assert_root_binding(target_binding)
            _assert_root_binding(source_binding)
            if manifest_mod.sha256_file(temporary) != digest:
                raise ScientificFingerprintError(f"copied result hash mismatch:{name}")
            _assert_root_binding(target_binding)
            os.replace(temporary, destination)
            _assert_root_binding(target_binding)
            if after_materialize is not None:
                after_materialize(name, materialized_count)
            _assert_root_binding(target_binding)
            _assert_root_binding(source_binding)
        source_final = _load_bound_snapshot(source_binding)
        source_fp_final = _fingerprint_from_snapshot(source_final)
        verification_final = _source_verification_from_snapshot(
            source_final, source_fp_final
        )
        if (source_final["manifest_sha256"] != decision["source_manifest_sha256"]
                or source_fp_final.get("digest") != decision["source_fingerprint"]
                or not verification_final.get("reusable")
                or verification_final.get("bindings")
                != decision["source_verification_bindings"]):
            raise ScientificFingerprintError(
                "source changed while verified result bytes were materialised")
        target_final = _load_bound_snapshot(target_binding)
        if target_final["manifest_sha256"] != journal.get("prepared_manifest_sha256"):
            raise ReuseConflictError("target manifest drifted after prepared materialization")
        target_fp_final = _fingerprint_from_snapshot(target_final)
        if (
            target_fp_final.get("status") != "complete"
            or target_fp_final.get("digest") != decision["scientific_fingerprint"]
        ):
            raise ReuseConflictError("target fingerprint drifted after prepared materialization")
        target_outputs = verify_outputs(target_root, target_final["manifest"])
        if (
            target_outputs.get("issues")
            or _json_digest(target_outputs.get("result_files") or {})
            != decision["source_result_bundle_sha256"]
            or dict(target_outputs.get("result_files") or {})
            != dict(decision["source_result_files"])
        ):
            raise ReuseConflictError("target materialized result hashes failed revalidation")
        decision["status"] = "succeeded"
        decision["completed_at"] = _now()
        reuse_attempt = next((
            item for item in reversed(updated.get("attempts") or [])
            if isinstance(item, dict) and item.get("decision_id") == key
        ), None)
        if reuse_attempt is None:
            raise ScientificFingerprintError("prepared reuse attempt record is missing")
        rollback_manifest = deepcopy(target_final["manifest"])
        rollback_journal = deepcopy(journal)
        rollback_journal["status"] = "prepared"
        rollback_journal.pop("done_manifest_sha256", None)
        reuse_attempt["result"] = "reused"
        manifest_mod.set_state(updated, "DONE", note=f"reused result decision={key}")
        try:
            _assert_root_binding(target_binding)
            done_manifest_sha256 = _save_bound_manifest(target_binding, updated)
            journal["status"] = "succeeded"
            journal["done_manifest_sha256"] = done_manifest_sha256
            _write_bound_reuse_journal(target_binding, journal_path.name, journal)
            persisted_snapshot = _load_bound_snapshot(target_binding)
            if persisted_snapshot["manifest_sha256"] != done_manifest_sha256:
                raise ReuseConflictError(
                    "DONE target manifest generation mismatch"
                )
        except ReuseConflictError:
            # POSIX permits renaming an open directory.  Anchored writes never
            # touch replacement B, and this compensating write keeps detached
            # entity A at the durable prepared recovery point.
            _save_bound_manifest(target_binding, rollback_manifest)
            _write_bound_reuse_journal(
                target_binding, journal_path.name, rollback_journal
            )
            raise
        persisted = persisted_snapshot["manifest"]
        stored = _decision_replay(persisted, key, request_sha256)
        if stored is None:
            raise ScientificFingerprintError("reuse decision was not persisted")
        return {
            "ok": True, "replayed": False, "target_job_id": target_id,
            "source_job_id": source_id, "decision": stored,
            "state": persisted.get("state"), "accepted_inherited": False,
            "final_inherited": False,
        }


def record_force_recalculation(job_dir: str | os.PathLike[str], *, decision_id: str,
                               reason: str) -> dict[str, Any]:
    """Durably retain an explicit force-recalculation choice, idempotently."""
    key = _decision_key(decision_id)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("force recalculation requires a reason")
    if len(reason) > 1000:
        raise ValueError("force recalculation reason is too long")
    with _operation_locks((job_dir,), "record force recalculation"):
        snapshot = _load_authoritative_snapshot(job_dir)
        manifest = snapshot["manifest"]
        if str(manifest.get("state") or "").upper() != "CREATED":
            raise ScientificFingerprintError("only an unsubmitted CREATED job can be recalculated")
        fingerprint = _fingerprint_from_snapshot(snapshot)
        request = {
            "action": "force_recalculate", "job_id": _job_identity(manifest),
            "fingerprint_schema": fingerprint.get("schema"),
            "fingerprint_status": fingerprint.get("status"),
            "fingerprint": fingerprint.get("digest"),
            "input_closure_digest": fingerprint.get("input_closure_digest"),
            "reason": reason,
        }
        request_sha256 = _json_digest(request)
        replay = _decision_replay(manifest, key, request_sha256)
        if replay is not None:
            return {"ok": True, "replayed": True, "decision": replay}
        decision = {
            "schema": FORCE_DECISION_SCHEMA, "decision_id": key,
            "request_sha256": request_sha256, "action": "force_recalculate",
            "status": "succeeded",
            "decided_at": _now(), "actor": "manual-local-user", "reason": reason,
            "target_job_id": request["job_id"],
            "scientific_fingerprint": fingerprint.get("digest"),
            "fingerprint_schema": fingerprint.get("schema"),
            "fingerprint_status": fingerprint.get("status"),
            "input_closure_digest": fingerprint.get("input_closure_digest"),
            "target_manifest_generation_sha256": snapshot["manifest_sha256"],
            "remote_effect": False,
        }
        updated = deepcopy(manifest)
        updated.setdefault("reuse_decisions", []).append(decision)
        manifest_mod.save_manifest(job_dir, updated)
        return {"ok": True, "replayed": False, "decision": decision}


def has_current_force_recalculation(manifest: Mapping[str, Any],
                                    fingerprint: Mapping[str, Any]) -> bool:
    """Return whether a retained force decision binds the current target input."""
    digest = fingerprint.get("digest")
    closure = fingerprint.get("input_closure_digest")
    for decision in reversed(manifest.get("reuse_decisions") or []):
        if (isinstance(decision, Mapping)
                and decision.get("schema") == FORCE_DECISION_SCHEMA
                and decision.get("action") == "force_recalculate"
                and decision.get("status") == "succeeded"
                and decision.get("target_job_id") == _job_identity(manifest)
                and decision.get("fingerprint_schema") == FINGERPRINT_SCHEMA
                and decision.get("scientific_fingerprint") == digest
                and decision.get("input_closure_digest") == closure):
            return True
    return False


__all__ = [
    "ADVISORY_SCHEMA", "AUTHORITATIVE_LOOKUP_SCHEMA", "CalculationReuseIndex",
    "FINGERPRINT_SCHEMA", "FORCE_DECISION_SCHEMA", "INDEX_LIMIT", "PROVENANCE_SCHEMA",
    "REUSE_DECISION_SCHEMA", "ReuseConflictError", "ScientificFingerprintError",
    "authoritative_reuse_lookup", "build_scientific_fingerprint", "canonical_incar",
    "canonical_kpoints", "canonical_poscar", "estimate_saved_core_hours",
    "fingerprint_differences", "has_current_force_recalculation", "public_fingerprint",
    "record_force_recalculation", "record_reuse_reference", "source_verification",
]
