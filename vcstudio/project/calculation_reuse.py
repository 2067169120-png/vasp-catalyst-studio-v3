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
import time
from typing import Any, Callable, Iterable, Mapping

from vcstudio.shared import manifest as manifest_mod


FINGERPRINT_SCHEMA = "vcstudio.scientific-fingerprint/v1"
INDEX_SCHEMA = "vcstudio.calculation-reuse-index/v1"
ADVISORY_SCHEMA = "vcstudio.calculation-reuse-advisory/v1"
REUSE_DECISION_SCHEMA = "vcstudio.calculation-reuse-decision/v1"
PROVENANCE_SCHEMA = "vcstudio.job-provenance/v1"
INDEX_LIMIT = 512

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
    missing = []
    if not digest:
        missing.append("method_recipe.semantic_sha256")
    elif not _HEX64_RE.fullmatch(digest):
        missing.append("method_recipe.semantic_sha256_invalid")
    if not schema:
        missing.append("method_recipe.schema")
    return {"schema": schema or None, "semantic_sha256": digest or None}, missing, not digest


def _environment_binding(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    results = manifest.get("results") if isinstance(manifest.get("results"), Mapping) else {}
    environment = inputs.get("execution_environment")
    if not isinstance(environment, Mapping):
        environment = inputs.get("environment")
    if not isinstance(environment, Mapping):
        environment = results.get("execution_environment")
    if not isinstance(environment, Mapping):
        environment = {}
    vasp_version = str(
        environment.get("vasp_version") or inputs.get("vasp_version") or ""
    ).strip()
    build_identity = str(
        environment.get("build_identity")
        or environment.get("vasp_build_sha256")
        or environment.get("compiler_identity")
        or inputs.get("vasp_build_identity")
        or ""
    ).strip()
    creator = str(manifest.get("created_by") or "").strip()
    value = {
        "vasp_version": vasp_version or None,
        "build_identity": build_identity or None,
        "vcstudio_creator": creator or None,
    }
    missing = []
    if not vasp_version:
        missing.append("execution_environment.vasp_version")
    if not build_identity:
        missing.append("execution_environment.build_identity")
    if not creator:
        missing.append("created_by")
    return value, missing


def _manifest_project_identity(manifest: Mapping[str, Any]) -> str | None:
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    value = str(
        manifest.get("project_uuid") or inputs.get("project_uuid")
        or inputs.get("remote_namespace") or ""
    ).strip()
    return value or None


def _recorded_input_hash(manifest: Mapping[str, Any], name: str) -> str:
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    hashes = inputs.get("sha256") if isinstance(inputs.get("sha256"), Mapping) else {}
    value = str(hashes.get(name) or "").strip().lower()
    if name == "POTCAR" and not value:
        value = str(inputs.get("potcar_sha256") or "").strip().lower()
    return value


def _titel_identities(text: str) -> list[str]:
    identities = []
    for line in str(text or "").splitlines():
        if "TITEL" in line.upper():
            identities.append(_sha256_bytes(line.strip().encode("utf-8")))
    return identities


def _load_authoritative_snapshot(job_dir: str | os.PathLike[str]) -> dict[str, Any]:
    root = Path(job_dir).expanduser().resolve()
    manifest_path = root / manifest_mod.MANIFEST_NAME
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = manifest_mod.load_manifest(root)
    except OSError as exc:
        raise ScientificFingerprintError("authoritative job.yaml is unavailable") from exc
    if not isinstance(manifest, dict):
        raise ScientificFingerprintError("authoritative job.yaml is invalid")
    files: dict[str, bytes] = {}
    for name in _INPUT_FILES:
        try:
            files[name] = (root / name).read_bytes()
        except OSError:
            continue
    return {
        "root": root, "manifest": manifest, "manifest_bytes": manifest_bytes,
        "manifest_sha256": _sha256_bytes(manifest_bytes), "files": files,
        "file_sha256": {name: _sha256_bytes(payload) for name, payload in files.items()},
    }


def build_scientific_fingerprint(job_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Build one fail-closed strict fingerprint from current authoritative bytes."""
    try:
        snapshot = _load_authoritative_snapshot(job_dir)
    except ScientificFingerprintError as exc:
        return {
            "schema": FINGERPRINT_SCHEMA, "status": "incomplete", "digest": None,
            "legacy_recipe": True, "missing": ["job.yaml"],
            "recipe_status": "explicit_legacy",
            "integrity_issues": [str(exc)], "components": {},
            "manifest_sha256": None, "project_identity": None,
        }
    manifest = snapshot["manifest"]
    files = snapshot["files"]
    missing: list[str] = []
    integrity: list[str] = []
    components: dict[str, Any] = {}

    for name in _INPUT_FILES:
        if name not in files:
            missing.append(name)
            continue
        expected = _recorded_input_hash(manifest, name)
        if not expected:
            missing.append(f"inputs.sha256.{name}")
        elif not _HEX64_RE.fullmatch(expected):
            integrity.append(f"inputs.sha256.{name} is invalid")
        elif expected != snapshot["file_sha256"][name]:
            integrity.append(f"{name} no longer matches job.yaml")

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

    if "POSCAR" in files:
        try:
            structure, summary = canonical_poscar(files["POSCAR"].decode("utf-8"))
            components["structure"] = {
                "sha256": _json_digest(structure), "summary": summary,
            }
        except (UnicodeError, ValueError) as exc:
            missing.append("POSCAR.canonical")
            integrity.append(str(exc))

    if "POTCAR" in files:
        try:
            potcar_text = files["POTCAR"].decode("utf-8", errors="replace")
            titel_hashes = _titel_identities(potcar_text)
            if not titel_hashes:
                missing.append("POTCAR.TITEL")
            components["potcar"] = {
                "content_sha256": snapshot["file_sha256"]["POTCAR"],
                "titel_sha256": titel_hashes,
            }
        except (UnicodeError, ValueError) as exc:
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
        "task_type": str(manifest.get("task_type") or "").strip().lower() or None,
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
    }
    return {
        "schema": FINGERPRINT_SCHEMA, "status": status,
        "digest": _json_digest(semantic) if status == "complete" else None,
        "legacy_recipe": legacy_recipe, "missing": missing,
        "recipe_status": ("explicit_legacy" if legacy_recipe else "canonical_recipe"),
        "integrity_issues": integrity, "components": components,
        "manifest_sha256": snapshot["manifest_sha256"],
        "project_identity": _manifest_project_identity(manifest),
    }


def _convergence_evidence(manifest: Mapping[str, Any]) -> bool:
    results = manifest.get("results") if isinstance(manifest.get("results"), Mapping) else {}
    diagnosis = results.get("diagnosis") if isinstance(results.get("diagnosis"), Mapping) else {}
    failure_class = str(diagnosis.get("failure_class") or "").strip().upper()
    return bool(
        failure_class == "CONVERGED"
        or diagnosis.get("task_converged") is True
        or results.get("converged") is True
    )


def _result_hashes(job_dir: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    results = manifest.get("results") if isinstance(manifest.get("results"), Mapping) else {}
    declared = results.get("fetched_sha256")
    if not isinstance(declared, Mapping):
        declared = results.get("output_sha256")
    if not isinstance(declared, Mapping) or not declared:
        return {}, ["results.fetched_sha256"]
    verified: dict[str, str] = {}
    issues = []
    for raw_name, raw_digest in sorted(declared.items(), key=lambda item: str(item[0])):
        name = str(raw_name)
        digest = str(raw_digest or "").strip().lower()
        if (not name or Path(name).name != name or name.startswith(".")
                or name in {*_INPUT_FILES, manifest_mod.MANIFEST_NAME}
                or not _HEX64_RE.fullmatch(digest)):
            issues.append(f"invalid result hash declaration:{name or '?'}")
            continue
        path = job_dir / name
        try:
            current = manifest_mod.sha256_file(path)
        except OSError:
            issues.append(f"result file unavailable:{name}")
            continue
        if current != digest:
            issues.append(f"result file changed:{name}")
            continue
        verified[name] = current
    if not verified:
        issues.append("no result file remains verifiable")
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    if str(inputs.get("engine") or "vasp").strip().lower() == "vasp":
        if "OSZICAR" not in verified:
            issues.append("OSZICAR is not hash-bound and verifiable")
        if not {"OUTCAR", "vasprun.xml"}.intersection(verified):
            issues.append("OUTCAR or vasprun.xml is not hash-bound and verifiable")
    return verified, issues


def source_verification(job_dir: str | os.PathLike[str],
                        fingerprint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Verify source completeness, convergence and still-present result bytes."""
    root = Path(job_dir).expanduser().resolve()
    manifest = manifest_mod.load_manifest(root)
    fp = dict(fingerprint or build_scientific_fingerprint(root))
    if not isinstance(manifest, dict):
        return {"status": "incomplete", "reusable": False,
                "issues": ["job.yaml is unavailable"], "result_files": {}}
    state = str(manifest.get("state") or "").strip().upper()
    if state in {"FAILED", "NEEDS_HUMAN"}:
        return {"status": "failed", "reusable": False, "issues": [], "result_files": {}}
    if state == "UNCONVERGED":
        return {"status": "unconverged", "reusable": False,
                "issues": [], "result_files": {}}
    issues = []
    if fp.get("status") != "complete":
        issues.extend(fp.get("missing") or [])
        issues.extend(fp.get("integrity_issues") or [])
    if state != "DONE":
        issues.append(f"source state is {state or 'unknown'}, not DONE")
    if not _convergence_evidence(manifest):
        issues.append("convergence evidence is missing")
    verified_energy = None
    try:
        from vcstudio.project.energy_gate import validate_done_energy
        verified_energy = validate_done_energy(
            root, "reuse source", manifest_mod, require_oszicar=True)[0]
    except ValueError as exc:
        issues.append(str(exc))
    result_files, result_issues = _result_hashes(root, manifest)
    issues.extend(result_issues)
    status = "verified" if not issues else "incomplete"
    return {
        "status": status, "reusable": status == "verified", "issues": sorted(set(issues)),
        "result_files": result_files,
        "result_bundle_sha256": _json_digest(result_files) if result_files else None,
        "energy_e0_eV": verified_energy if status == "verified" else None,
    }


def _component_summary(component: Mapping[str, Any] | None) -> Any:
    component = component if isinstance(component, Mapping) else {}
    if "summary" in component:
        return deepcopy(component.get("summary"))
    values = component.get("values")
    if isinstance(values, Mapping):
        if set(values) <= {"vasp_version", "build_identity", "vcstudio_creator"}:
            return deepcopy(dict(values))
        if "method_recipe" in values:
            return {
                "engine": values.get("engine"), "task_type": values.get("task_type"),
                "calc_type": values.get("calc_type"),
                "method_recipe": deepcopy(values.get("method_recipe")),
            }
        if "field_count" not in component:
            return deepcopy(dict(values))
    if "field_count" in component:
        return {"field_count": component.get("field_count")}
    if "line_count" in component:
        return {"line_count": component.get("line_count")}
    if "content_sha256" in component:
        return {"content_sha256": component.get("content_sha256"),
                "titel_sha256": deepcopy(component.get("titel_sha256") or [])}
    return None


def public_fingerprint(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """Return a path-free, secret-free display projection."""
    fields = []
    for name in (
        "structure", "incar", "kpoints", "potcar", "task", "environment",
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
        "structure", "incar", "kpoints", "potcar", "task", "environment",
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
    job_dir: str
    manifest: dict[str, Any]
    fingerprint: dict[str, Any]
    verification: dict[str, Any]


class CalculationReuseIndex:
    """Bounded, rebuild-only local index; never a source of scientific truth."""

    def __init__(self, *, limit: int = INDEX_LIMIT):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 4096:
            raise ValueError("index limit must be between 1 and 4096")
        self.limit = limit
        self.records: list[_IndexRecord] = []
        self.total_entries = 0
        self.truncated = False

    def rebuild(self, entries: Iterable[tuple[str, Mapping[str, Any] | None]], *,
                job_id: Callable[[str, Mapping[str, Any]], str],
                project_id: Callable[[str, Mapping[str, Any]], str | None] | None = None
                ) -> "CalculationReuseIndex":
        source = list(entries)
        self.total_entries = len(source)
        self.truncated = len(source) > self.limit
        self.records = []
        for directory, listed_manifest in source[-self.limit:]:
            manifest = manifest_mod.load_manifest(directory)
            if not isinstance(manifest, dict):
                # A ledger copy is not promoted to fact when job.yaml disappeared
                # or changed between listing and rebuild.
                continue
            if isinstance(listed_manifest, Mapping) and dict(listed_manifest) != manifest:
                # Rebuild from the current authoritative bytes, not the stale row.
                listed_manifest = manifest
            fingerprint = build_scientific_fingerprint(directory)
            verification = source_verification(directory, fingerprint)
            identifier = job_id(directory, manifest)
            if not _ID_RE.fullmatch(identifier):
                continue
            self.records.append(_IndexRecord(
                job_id=identifier,
                project_id=(project_id(directory, manifest) if project_id else None),
                job_dir=str(Path(directory).resolve()), manifest=manifest,
                fingerprint=fingerprint, verification=verification,
            ))
        return self

    def advisory(self, target_job_ids: Iterable[str], *, near_limit: int = 5) -> dict[str, Any]:
        requested = list(target_job_ids)
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
                    "cross_project": bool(
                        target.project_id and candidate.project_id
                        and target.project_id != candidate.project_id),
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


def record_reuse_reference(target_dir: str | os.PathLike[str],
                           source_dir: str | os.PathLike[str], *,
                           decision_id: str, reason: str,
                           target_project_id: str | None = None,
                           source_project_id: str | None = None,
                           before_commit: Callable[[], None] | None = None,
                           after_materialize: Callable[[str, int], None] | None = None,
                           ) -> dict[str, Any]:
    """Explicitly reference one exact verified result with TOCTOU revalidation."""
    key = _decision_key(decision_id)
    reason = str(reason or "").strip()
    if len(reason) > 1000:
        raise ValueError("reuse reason is too long")
    with _operation_locks((target_dir, source_dir), "引用既有结果"):
        target_before = _load_authoritative_snapshot(target_dir)
        source_before = _load_authoritative_snapshot(source_dir)
        target_manifest = target_before["manifest"]
        source_manifest = source_before["manifest"]
        target_id = _job_identity(target_manifest)
        source_id = _job_identity(source_manifest)
        if target_id == source_id:
            raise ScientificFingerprintError("a job cannot reuse itself")
        target_fp = build_scientific_fingerprint(target_dir)
        source_fp = build_scientific_fingerprint(source_dir)
        request = {
            "action": "reference_existing_result", "target_job_id": target_id,
            "source_job_id": source_id, "target_fingerprint": target_fp.get("digest"),
            "source_fingerprint": source_fp.get("digest"), "reason": reason,
        }
        request_sha256 = _json_digest(request)
        if (target_fp.get("status") != "complete" or source_fp.get("status") != "complete"
                or target_fp.get("digest") != source_fp.get("digest")):
            raise ScientificFingerprintError("strict complete fingerprints do not match")
        verification = source_verification(source_dir, source_fp)
        if not verification.get("reusable"):
            raise ScientificFingerprintError("source result is not complete, converged and verifiable")
        replay = _decision_replay(target_manifest, key, request_sha256)
        source_current = source_before
        verification_after = verification
        if replay is not None:
            if (replay.get("source_manifest_sha256") != source_current["manifest_sha256"]
                    or replay.get("source_result_bundle_sha256")
                    != verification_after.get("result_bundle_sha256")
                    or replay.get("scientific_fingerprint") != target_fp.get("digest")
                    or replay.get("source_fingerprint") != source_fp.get("digest")):
                raise ScientificFingerprintError(
                    "previous reuse decision is stale because its source changed")
            if replay.get("status") == "succeeded":
                target_verification = source_verification(target_dir, target_fp)
                if (not target_verification.get("reusable")
                        or target_verification.get("result_bundle_sha256")
                        != replay.get("source_result_bundle_sha256")):
                    raise ScientificFingerprintError(
                        "materialised reuse result no longer verifies")
                return {"ok": True, "replayed": True, "decision": replay,
                        "target_job_id": target_id, "source_job_id": source_id,
                        "state": target_manifest.get("state"),
                        "accepted_inherited": False, "final_inherited": False}
            if replay.get("status") != "prepared":
                raise ScientificFingerprintError("reuse decision has an unknown recovery status")
            if str(target_manifest.get("state") or "").upper() != "CREATED":
                raise ScientificFingerprintError("prepared reuse decision has invalid target state")
            updated = deepcopy(target_manifest)
            decision = next(
                item for item in updated.get("reuse_decisions") or []
                if isinstance(item, dict) and item.get("decision_id") == key)
        else:
            if str(target_manifest.get("state") or "").upper() != "CREATED":
                raise ScientificFingerprintError(
                    "only an unsubmitted CREATED job can reference a result")
            if target_manifest.get("cluster") or target_manifest.get("scheduler_job_id"):
                raise ScientificFingerprintError("target job is already bound to a remote submission")
            if before_commit is not None:
                before_commit()
            # Re-read every authority after the user choice and immediately
            # before writing the recoverable prepared record.
            target_after = _load_authoritative_snapshot(target_dir)
            source_after = _load_authoritative_snapshot(source_dir)
            target_fp_after = build_scientific_fingerprint(target_dir)
            source_fp_after = build_scientific_fingerprint(source_dir)
            verification_after = source_verification(source_dir, source_fp_after)
            if (target_after["manifest_sha256"] != target_before["manifest_sha256"]
                    or source_after["manifest_sha256"] != source_before["manifest_sha256"]
                    or target_fp_after.get("digest") != target_fp.get("digest")
                    or source_fp_after.get("digest") != source_fp.get("digest")
                    or verification_after.get("result_bundle_sha256")
                    != verification.get("result_bundle_sha256")
                    or not verification_after.get("reusable")):
                raise ScientificFingerprintError("source or target changed during reuse validation")
            source_current = source_after
            now = _now()
            cross_project = bool(
                target_project_id and source_project_id
                and target_project_id != source_project_id)
            decision = {
                "schema": REUSE_DECISION_SCHEMA, "decision_id": key,
                "request_sha256": request_sha256, "action": "reference_existing_result",
                "status": "prepared", "decided_at": now,
                "actor": "manual-local-user", "reason": reason,
                "target_job_id": target_id, "source_job_id": source_id,
                "target_project_id": target_project_id,
                "source_project_id": source_project_id,
                "cross_project": cross_project,
                "scientific_fingerprint": target_fp["digest"],
                "source_fingerprint": source_fp["digest"],
                "source_manifest_sha256": source_after["manifest_sha256"],
                "source_result_bundle_sha256": verification_after["result_bundle_sha256"],
                "source_result_files": deepcopy(verification_after["result_files"]),
                "source_verification_status": "verified",
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
                    "project_id": target_project_id,
                    "scientific_fingerprint": target_fp["digest"], "created_at": now,
                },
                {
                    "id": source_id, "kind": "source_calculation",
                    "project_id": source_project_id,
                    "scientific_fingerprint": source_fp["digest"],
                    "verification": "verified",
                },
            ])
            links.append({
                "id": key, "type": "reuses", "from": target_id, "to": source_id,
                "decision_id": key, "cross_project": cross_project,
            })
        source_results = source_current["manifest"].get("results") \
            if isinstance(source_current["manifest"].get("results"), Mapping) else {}
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
        }
        source_diagnosis = source_results.get("diagnosis")
        if not isinstance(source_diagnosis, Mapping):
            raise ScientificFingerprintError("source convergence diagnosis is unavailable")
        results["diagnosis"] = {
            field: deepcopy(source_diagnosis[field])
            for field in _REUSE_DIAGNOSIS_FIELDS if field in source_diagnosis
        }
        results["diagnosis"]["evidence_source"] = "verified_reuse_reference"
        results["fetched_sha256"] = deepcopy(verification_after["result_files"])
        results["fetched"] = sorted(verification_after["result_files"])
        results["fetched_missing"] = []
        results["fetch_contract"] = {
            "schema": 1, "mode": "verified_reuse_reference", "state": "DONE",
            "source_job_id": source_id, "decision_id": key,
        }
        if replay is None:
            updated.setdefault("attempts", []).append({
                "n": len(updated.get("attempts") or []) + 1, "at": now,
                "result": "reuse_prepared", "decision_id": key, "remote_effect": False,
            })
            # Durable two-phase boundary: a restart sees status=prepared and
            # reconciles already-copied files instead of inventing a new node.
            manifest_mod.save_manifest(target_dir, updated)
        # Materialise only hash-bound result bytes into the new provenance node.
        # This gives existing deterministic analysis gates local files to
        # re-check without pretending the target ran a second calculation.
        target_root = Path(target_dir).resolve()
        source_root = Path(source_dir).resolve()
        recovery_suffix = _sha256_bytes(key.encode("utf-8"))[:12]
        for materialized_count, (name, digest) in enumerate(
                sorted(verification_after["result_files"].items()), start=1):
            destination = target_root / name
            if destination.exists():
                if manifest_mod.sha256_file(destination) == digest:
                    continue
                raise ScientificFingerprintError(
                    f"prepared reuse destination has conflicting bytes:{name}")
            temporary = target_root / f".{name}.{recovery_suffix}.reuse-tmp"
            with open(source_root / name, "rb") as source_handle, \
                    open(temporary, "wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if manifest_mod.sha256_file(temporary) != digest:
                raise ScientificFingerprintError(f"copied result hash mismatch:{name}")
            os.replace(temporary, destination)
            if after_materialize is not None:
                after_materialize(name, materialized_count)
        # A non-cooperating writer can ignore the operation lock.  Re-read the
        # complete source authority after materialisation so a file changed
        # after its own copy cannot be blessed by the earlier verification.
        source_final = _load_authoritative_snapshot(source_dir)
        source_fp_final = build_scientific_fingerprint(source_dir)
        verification_final = source_verification(source_dir, source_fp_final)
        if (source_final["manifest_sha256"] != decision["source_manifest_sha256"]
                or source_fp_final.get("digest") != decision["source_fingerprint"]
                or not verification_final.get("reusable")
                or verification_final.get("result_bundle_sha256")
                != decision["source_result_bundle_sha256"]):
            raise ScientificFingerprintError(
                "source changed while verified result bytes were materialised")
        decision["status"] = "succeeded"
        decision["completed_at"] = _now()
        reuse_attempt = next((
            item for item in reversed(updated.get("attempts") or [])
            if isinstance(item, dict) and item.get("decision_id") == key
        ), None)
        if reuse_attempt is None:
            raise ScientificFingerprintError("prepared reuse attempt record is missing")
        reuse_attempt["result"] = "reused"
        manifest_mod.set_state(updated, "DONE", note=f"reused result decision={key}")
        manifest_mod.save_manifest(target_dir, updated)
        persisted = manifest_mod.load_manifest(target_dir)
        if not isinstance(persisted, dict):
            raise ScientificFingerprintError("reuse decision could not be re-read")
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
    with _operation_locks((job_dir,), "记录强制重算理由"):
        manifest = manifest_mod.load_manifest(job_dir)
        if not isinstance(manifest, dict):
            raise ScientificFingerprintError("authoritative job.yaml is unavailable")
        if str(manifest.get("state") or "").upper() != "CREATED":
            raise ScientificFingerprintError("only an unsubmitted CREATED job can be recalculated")
        fingerprint = build_scientific_fingerprint(job_dir)
        request = {
            "action": "force_recalculate", "job_id": _job_identity(manifest),
            "fingerprint": fingerprint.get("digest"), "reason": reason,
        }
        request_sha256 = _json_digest(request)
        replay = _decision_replay(manifest, key, request_sha256)
        if replay is not None:
            return {"ok": True, "replayed": True, "decision": replay}
        decision = {
            "schema": REUSE_DECISION_SCHEMA, "decision_id": key,
            "request_sha256": request_sha256, "action": "force_recalculate",
            "decided_at": _now(), "actor": "manual-local-user", "reason": reason,
            "target_job_id": request["job_id"],
            "scientific_fingerprint": fingerprint.get("digest"),
            "fingerprint_status": fingerprint.get("status"),
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
    for decision in reversed(manifest.get("reuse_decisions") or []):
        if (isinstance(decision, Mapping)
                and decision.get("action") == "force_recalculate"
                and decision.get("scientific_fingerprint") == digest):
            return True
    return False


__all__ = [
    "ADVISORY_SCHEMA", "CalculationReuseIndex", "FINGERPRINT_SCHEMA", "INDEX_LIMIT",
    "PROVENANCE_SCHEMA", "REUSE_DECISION_SCHEMA", "ReuseConflictError",
    "ScientificFingerprintError", "build_scientific_fingerprint", "canonical_incar",
    "canonical_kpoints", "canonical_poscar", "estimate_saved_core_hours",
    "fingerprint_differences", "has_current_force_recalculation", "public_fingerprint",
    "record_force_recalculation", "record_reuse_reference", "source_verification",
]
