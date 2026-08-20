"""Explainable, fail-closed VASP method recipes.

This module is deliberately project independent.  It turns an explicit user draft (optionally
seeded from :mod:`vcstudio.project.lab_policies`) into a versioned, provenance-rich recipe, compares
that recipe with an existing INCAR, and holds a short-lived preview capability for the later write
step.  It never submits a job and it never promotes scientific state.

``incar_builder`` remains the INCAR parser/serializer and legacy completion layer.  Recipe policy,
confirmation, token consumption, and provenance live here instead of being added to that module.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from vcstudio.generate import task_catalog
from vcstudio.generate.incar_builder import (
    dipole_correction_keys,
    incar_dict_to_str,
    parse_incar,
    vaspsol_keys,
)
from vcstudio.generate.kpoints import kpoints_str, recommend_kpoints
from vcstudio.generate.poscar import parse_poscar_species, read_cell_vectors
from vcstudio.generate.potcar import potcar_provenance
from vcstudio.project import lab_policies, method_policy


RECIPE_SCHEMA = "vcstudio.method-recipe/v1"
DRAFT_SCHEMA = "vcstudio.method-recipe-draft/v1"
PREVIEW_SCHEMA = "vcstudio.method-recipe-preview/v1"
RECORD_SCHEMA = "vcstudio.method-recipe-record/v2"
CONFIRMATION_SCHEMA = "vcstudio.method-recipe-confirmation/v1"
SIDECAR_NAME = "method-recipe.json"
RECIPE_VERSION = 1

SOURCES = frozenset({"default", "policy", "project", "user"})
RISKS = frozenset({"low", "medium", "high", "blocking"})
SYSTEM_TYPES = ("molecule", "slab", "bulk")
SUPPORTED_XC = {"PBE": {"potcar_family": "PAW_PBE", "incar": "PE"}}
DISPERSION = {"none": None, "d3-zero": 11, "d3-bj": 12}
PRECISION = {"normal": "Normal", "accurate": "Accurate"}
SUPPORTED_LDAUTYPES = frozenset({2})

# These task profiles are intentionally finite.  Complex builders (NEB, AIMD, adsorption
# projects, convergence series, and so on) keep using their dedicated workflows rather than being
# reduced to one deceptively complete INCAR.
_TASK_PROFILES: dict[str, dict[str, Any]] = {
    "relax": {"IBRION": 2, "NSW": 200, "ISIF": 2},
    "cellopt": {"IBRION": 2, "NSW": 200, "ISIF": 3},
    "static": {"IBRION": -1, "NSW": 0},
    "dos_pdos": {"IBRION": -1, "NSW": 0, "LORBIT": 11, "NEDOS": 2001},
    "bader": {"IBRION": -1, "NSW": 0, "LAECHG": True, "LCHARG": True},
    "elf": {"IBRION": -1, "NSW": 0, "LELF": True},
    "workfunction": {"IBRION": -1, "NSW": 0, "LVTOT": True, "LVHAR": True},
    "freq": {"IBRION": 5, "NSW": 1, "POTIM": 0.015},
    "vaspsol": {"IBRION": -1, "NSW": 0},
}
_TASK_SYSTEMS: dict[str, frozenset[str]] = {
    "relax": frozenset({"molecule", "slab", "bulk"}),
    "cellopt": frozenset({"bulk"}),
    "static": frozenset({"molecule", "slab", "bulk"}),
    "dos_pdos": frozenset({"molecule", "slab", "bulk"}),
    "bader": frozenset({"molecule", "slab", "bulk"}),
    "elf": frozenset({"molecule", "slab", "bulk"}),
    "workfunction": frozenset({"slab"}),
    "freq": frozenset({"molecule", "slab", "bulk"}),
    "vaspsol": frozenset({"molecule", "slab"}),
}
_IONIC_TASKS = frozenset({"relax", "cellopt", "freq"})
_CONVERGENCE_TASKS = ("conv_encut", "conv_kmesh", "conv_vacuum", "conv_thickness")

_DRAFT_FIELDS = frozenset({
    "schema", "system_type", "task", "xc", "dispersion", "precision", "ediff",
    "ediffg", "spin_mode", "magmom", "hubbard_mode", "hubbard_u", "dipole_mode",
    "solvent_mode", "solvent_dielectric", "encut_mode", "encut_value",
    "encut_multiplier", "kpoints_mode", "kpoints_grid", "include_convergence_dry_run",
})
_PREVIEW_REQUEST_FIELDS = frozenset({
    "poscar_path", "incar_path", "out_dir", "lib_root", "draft", "policy_id",
    "project_id", "client_intent_id",
})
_CONFIRM_REQUEST_FIELDS = frozenset({
    "token", "preview_sha256", "target_id", "confirmed", "idempotency_key",
    "resolutions", "client_intent_id",
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SECRETISH = re.compile(
    r"(?i)(?:password|passwd|secret|api.?key|private.?key|credential|bearer\s+)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:gh[opusr]_[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}|bearer\s+|password|passwd|secret|api.?key|private.?key)"
)
_ABSOLUTE_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|^/[^\s]+|^\\\\|file://)")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Existing INCAR keys are classified exhaustively: a key is either already owned by the recipe,
# proven method-neutral under this contract, a known disabled-only toggle, a known method tag that
# must be absent, or unknown-and-denied.  There is no permissive fallback for scientific tags.
_METHOD_NEUTRAL_PASSTHROUGH = frozenset({
    "SYSTEM", "NWRITE", "LWAVE", "LCHARG", "LVTOT", "LVHAR", "LELF", "LAECHG",
    "LORBIT", "NEDOS", "EMIN", "EMAX", "KPAR", "NCORE", "NPAR", "LPLANE", "NSIM",
    "LSCALU", "LASYNC",
})
_METHOD_FALSE_COMPATIBLE = frozenset({
    "LNONCOLLINEAR", "LSORBIT", "LUSE_VDW", "LSPIRAL",
})
_METHOD_MUST_BE_ABSENT = frozenset({
    # XC / hybrid / vdW-DF combinations unsupported by the bounded PBE recipe.
    "AEXX", "HFSCREEN", "HFALPHA", "ALDAC", "AGGAC", "AGGAX", "PARAM1", "PARAM2",
    "ZAB_VDW", "BPARAM", "CPARAM", "VDW_S6", "VDW_S8", "VDW_A1", "VDW_A2",
    "VDW_SR", "VDW_RADIUS", "VDW_SCALING", "VDW_D", "VDW_CNRADIUS",
    # Charge, constrained occupations, non-collinear/SOC/spin-constraint controls.
    "NELECT", "NUPDOWN", "FERWE", "FERDO", "SAXIS", "QSPIRAL", "M_CONSTR",
    "I_CONSTRAINED_M", "LAMBDA", "RWIGS",
    # Additional VASPsol controls are not represented by the recipe DTO.
    "TAU", "LAMBDA_D_K", "NC_K", "SIGMA_K", "C_MOLAR", "R_SOLV",
})


class MethodRecipeError(ValueError):
    """A safe, user-facing recipe validation error."""


class MethodRecipeTokenError(MethodRecipeError):
    """The preview capability is missing, stale, tampered with, or already consumed."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def semantic_sha256(value: Any) -> str:
    """Hash JSON-compatible semantic content with stable ordering."""
    return hashlib.sha256(_canonical(value)).hexdigest()


def confirmation_binding_sha256(value: Any) -> str:
    """Validate and hash the path-free authority frozen by the first write attempt."""
    if not isinstance(value, Mapping):
        raise MethodRecipeError("confirmation binding must be an object")
    binding = dict(value)
    required = {
        "schema", "token_sha256", "preview_sha256", "recipe_semantic_sha256",
        "resolutions", "expected_source_hashes", "expected_target_hashes", "target_id",
        "project_id", "client_intent_id", "idempotency_key",
    }
    source_fields = {"POSCAR", "INCAR", "POTCAR", "POTCAR_EVIDENCE"}
    target_fields = {"INCAR", "POSCAR", "KPOINTS", "POTCAR"}
    source_hashes = binding.get("expected_source_hashes")
    target_hashes = binding.get("expected_target_hashes")
    resolutions = binding.get("resolutions")
    identifiers = ("client_intent_id", "idempotency_key")
    if (set(binding) != required or binding.get("schema") != CONFIRMATION_SCHEMA
            or any(not _HEX64.fullmatch(str(binding.get(key) or "")) for key in (
                "token_sha256", "preview_sha256", "recipe_semantic_sha256", "target_id"))
            or not isinstance(source_hashes, Mapping) or set(source_hashes) != source_fields
            or not isinstance(target_hashes, Mapping) or set(target_hashes) != target_fields
            or any(not _HEX64.fullmatch(str(target_hashes.get(key) or ""))
                   for key in target_fields)
            or any(not _HEX64.fullmatch(str(source_hashes.get(key) or ""))
                   for key in source_fields - {"INCAR"})
            or (source_hashes.get("INCAR") is not None
                and not _HEX64.fullmatch(str(source_hashes.get("INCAR"))))
            or not isinstance(resolutions, Mapping)
            or any(not isinstance(key, str) or value not in {"existing", "recipe"}
                   for key, value in resolutions.items())
            or any(not _SAFE_ID.fullmatch(str(binding.get(key) or ""))
                   or _SECRETISH.search(str(binding.get(key)))
                   or _SECRET_VALUE.search(str(binding.get(key))) for key in identifiers)
            or (binding.get("project_id") is not None
                and (not _SAFE_ID.fullmatch(str(binding.get("project_id")))
                     or _SECRETISH.search(str(binding.get("project_id")))
                     or _SECRET_VALUE.search(str(binding.get("project_id")))))):
        raise MethodRecipeError("confirmation binding is invalid")
    return semantic_sha256(binding)


def _strict_object(value: Any, allowed: frozenset[str], *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MethodRecipeError(f"{field} must be an object")
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        # Never echo unknown values: they may contain a path or credential.
        raise MethodRecipeError(f"{field} contains unknown fields")
    return dict(value)


def _safe_identifier(value: Any, *, field: str, optional: bool = False) -> str | None:
    text = str(value or "").strip()
    if optional and not text:
        return None
    if (not _SAFE_ID.fullmatch(text) or _SECRETISH.search(text)
            or _SECRET_VALUE.search(text)):
        raise MethodRecipeError(f"{field} must be a safe opaque identifier")
    return text


def _finite(value: Any, *, field: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MethodRecipeError(f"{field} must be numeric") from exc
    if isinstance(value, bool) or not math.isfinite(number) or not low <= number <= high:
        raise MethodRecipeError(f"{field} is outside the supported range")
    return number


def _read_text(path_value: Any, *, field: str, optional: bool = False) -> tuple[str, str] | None:
    text = str(path_value or "").strip()
    if optional and not text:
        return None
    if not text:
        raise MethodRecipeError(f"{field} is required")
    path = Path(text)
    try:
        if not path.is_file():
            raise MethodRecipeError(f"{field} must name a readable file")
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode("utf-8")
    except MethodRecipeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise MethodRecipeError(f"{field} could not be read") from exc
    return raw, hashlib.sha256(raw_bytes).hexdigest()


def _reason(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


def _entry(*, value: Any, source: str, reason_zh: str, reason_en: str, risk: str,
           evidence: list[dict[str, Any]], user_override: bool) -> dict[str, Any]:
    if source not in SOURCES:
        raise MethodRecipeError("internal recipe source is invalid")
    if risk not in RISKS:
        raise MethodRecipeError("internal recipe risk is invalid")
    base = {
        "value": copy.deepcopy(value),
        "source": source,
        "reason": _reason(reason_zh, reason_en),
        "risk": risk,
        "evidence": copy.deepcopy(evidence),
        "user_override": bool(user_override),
    }
    return {**base, "semantic_sha256": semantic_sha256(base)}


def _default_draft() -> dict[str, Any]:
    return {
        "schema": DRAFT_SCHEMA,
        "system_type": "slab",
        "task": "relax",
        "xc": "PBE",
        # The policy language only says "declare and match"; it does not select one physical
        # correction.  The user must explicitly choose none or a supported IVDW mode.
        "dispersion": "unresolved",
        "precision": "accurate",
        "ediff": 1e-5,
        "ediffg": 0.02,
        "spin_mode": "unresolved",
        "magmom": "",
        "hubbard_mode": "unresolved",
        "hubbard_u": {},
        "dipole_mode": "unresolved",
        "solvent_mode": "off",
        "solvent_dielectric": 78.4,
        "encut_mode": "enmax_multiplier",
        "encut_value": None,
        "encut_multiplier": 1.30,
        "kpoints_mode": "recommended",
        "kpoints_grid": None,
        "include_convergence_dry_run": False,
    }


def _task_evidence(task: str) -> dict[str, Any]:
    item = task_catalog.get_task(task)
    public = {
        "key": item["key"], "name_zh": item["name_zh"], "name_en": item["name_en"],
        "category": item["category"], "builder_ref": item["builder_ref"],
    }
    return {
        "kind": "task_catalog", "id": task,
        "semantic_sha256": semantic_sha256(public),
    }


def _policy_seed(policy_id: str | None, system_type: str) -> tuple[dict[str, Any], dict | None]:
    if not policy_id:
        return {}, None
    try:
        resolved = lab_policies.resolve(policy_id, applicability=system_type)
    except lab_policies.LabPolicyError as exc:
        raise MethodRecipeError("laboratory policy is unknown or not applicable") from exc
    method = resolved["method"]
    seed = {
        "xc": method.get("functional"),
        "ediff": method.get("energy_tolerance_eV"),
        "ediffg": method.get("force_tolerance_eV_A"),
        "encut_mode": "enmax_multiplier",
        "encut_multiplier": method.get("encut_enmax_multiplier"),
        "kpoints_mode": "recommended",
    }
    evidence = {
        "kind": "lab_policy", "id": resolved["policy_id"],
        "version": resolved["policy_version"],
        "semantic_sha256": resolved["semantic_sha256"],
        "recommendation_only": True,
    }
    return seed, evidence


def _normalize_hubbard(value: Any, elements: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise MethodRecipeError("hubbard_u must be an object keyed by element")
    if set(value) != set(elements):
        raise MethodRecipeError("manual DFT+U must explicitly map every POSCAR species")
    result: dict[str, dict[str, Any]] = {}
    for element in elements:
        item = value[element]
        if not isinstance(item, Mapping) or set(item) != {"l", "u", "j"}:
            raise MethodRecipeError("each DFT+U species requires exactly l, u, and j")
        try:
            orbital_number = float(item["l"])
        except (TypeError, ValueError) as exc:
            raise MethodRecipeError("DFT+U orbital l must be an integer") from exc
        if (isinstance(item["l"], bool) or not math.isfinite(orbital_number)
                or not orbital_number.is_integer()):
            raise MethodRecipeError("DFT+U orbital l must be an integer")
        orbital = int(orbital_number)
        if orbital not in {-1, 0, 1, 2, 3}:
            raise MethodRecipeError("DFT+U orbital l is unsupported")
        u_value = _finite(item["u"], field="DFT+U U", low=0.0, high=20.0)
        j_value = _finite(item["j"], field="DFT+U J", low=0.0, high=20.0)
        result[element] = {"l": orbital, "u": u_value, "j": j_value}
    return result


def _magmom_count(text: str) -> int:
    count = 0
    for token in text.split():
        if "*" in token:
            left, right = token.split("*", 1)
            try:
                repeat = int(left)
                moment = float(right)
            except ValueError as exc:
                raise MethodRecipeError("MAGMOM must use numeric VASP tokens") from exc
            if repeat <= 0 or not math.isfinite(moment):
                raise MethodRecipeError("MAGMOM repetition counts must be positive")
            count += repeat
        else:
            try:
                moment = float(token)
            except ValueError as exc:
                raise MethodRecipeError("MAGMOM must use numeric VASP tokens") from exc
            if not math.isfinite(moment):
                raise MethodRecipeError("MAGMOM values must be finite")
            count += 1
    return count


def _normalize_grid(value: Any) -> list[int]:
    if isinstance(value, str):
        parts: list[Any] = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise MethodRecipeError("kpoints_grid must contain three integers")
    if len(parts) != 3:
        raise MethodRecipeError("kpoints_grid must contain three integers")
    out = []
    for part in parts:
        try:
            numeric = float(part)
        except (TypeError, ValueError) as exc:
            raise MethodRecipeError("kpoints_grid must contain three integers") from exc
        if (isinstance(part, bool) or not math.isfinite(numeric)
                or not numeric.is_integer()):
            raise MethodRecipeError("kpoints_grid must contain three integers")
        number = int(numeric)
        if not 1 <= number <= 99:
            raise MethodRecipeError("kpoints_grid values must be between 1 and 99")
        out.append(number)
    return out


def _normalize_draft(value: Any, *, elements: list[str], counts: list[int],
                     policy_id: str | None = None,
                     project_defaults: Mapping[str, Any] | None = None,
                     ) -> tuple[dict[str, Any], dict[str, str], dict | None]:
    raw = _strict_object(value, _DRAFT_FIELDS, field="draft")
    if raw.get("schema") != DRAFT_SCHEMA:
        raise MethodRecipeError("unsupported method recipe draft schema")

    base = _default_draft()
    origins = {key: "default" for key in base if key != "schema"}
    requested_system = str(raw.get("system_type") or "").strip().lower()
    if requested_system not in SYSTEM_TYPES:
        raise MethodRecipeError("system_type is unsupported")
    policy_seed, policy_evidence = _policy_seed(policy_id, requested_system)
    for key, item in policy_seed.items():
        if item is not None:
            base[key] = item
            origins[key] = "policy"

    if project_defaults is not None:
        project = _strict_object(project_defaults, _DRAFT_FIELDS - {"schema"},
                                 field="project_defaults")
        for key, item in project.items():
            base[key] = copy.deepcopy(item)
            origins[key] = "project"

    # The complete draft is required so a browser cannot omit a difficult field and silently gain
    # a default.  The only exception is the schema, already checked above.
    missing = sorted((_DRAFT_FIELDS - {"schema"}) - set(raw))
    if missing:
        raise MethodRecipeError("draft is incomplete")
    for key in _DRAFT_FIELDS - {"schema"}:
        if raw[key] != base[key]:
            origins[key] = "user"
        base[key] = copy.deepcopy(raw[key])

    draft = base
    draft["schema"] = DRAFT_SCHEMA
    draft["system_type"] = str(draft["system_type"] or "").strip().lower()
    if draft["system_type"] not in SYSTEM_TYPES:
        raise MethodRecipeError("system_type is unsupported")
    draft["task"] = str(draft["task"] or "").strip().lower()
    try:
        task_catalog.get_task(draft["task"])
    except KeyError as exc:
        raise MethodRecipeError("task is not present in the task catalog") from exc
    if draft["task"] not in _TASK_PROFILES:
        raise MethodRecipeError("this task requires its dedicated workflow, not a single recipe")
    if draft["system_type"] not in _TASK_SYSTEMS[draft["task"]]:
        raise MethodRecipeError("task is incompatible with the selected system_type")

    draft["xc"] = str(draft["xc"] or "").strip().upper()
    if draft["xc"] not in SUPPORTED_XC:
        raise MethodRecipeError("XC to pseudopotential mapping is unknown; only PBE is mapped")
    draft["dispersion"] = str(draft["dispersion"] or "").strip().lower()
    if draft["dispersion"] == "unresolved":
        raise MethodRecipeError("dispersion must be explicitly selected; policy text is not a value")
    if draft["dispersion"] not in DISPERSION:
        raise MethodRecipeError("dispersion mode is unsupported")
    draft["precision"] = str(draft["precision"] or "").strip().lower()
    if draft["precision"] not in PRECISION:
        raise MethodRecipeError("precision is unsupported")
    draft["ediff"] = _finite(draft["ediff"], field="EDIFF", low=1e-9, high=1e-2)
    draft["ediffg"] = _finite(draft["ediffg"], field="EDIFFG", low=1e-5, high=1.0)

    draft["spin_mode"] = str(draft["spin_mode"] or "").strip().lower()
    if draft["spin_mode"] == "unresolved":
        raise MethodRecipeError("magnetic state must be explicitly selected")
    if draft["spin_mode"] not in {"nonspin", "collinear"}:
        raise MethodRecipeError("spin_mode is unsupported")
    draft["magmom"] = str(draft["magmom"] or "").strip()
    if draft["spin_mode"] == "collinear":
        if not counts or sum(counts) <= 0:
            raise MethodRecipeError("POSCAR atom counts are required to validate MAGMOM")
        if not draft["magmom"] or _magmom_count(draft["magmom"]) != sum(counts):
            raise MethodRecipeError("MAGMOM must explicitly cover every POSCAR atom")
    elif draft["magmom"]:
        raise MethodRecipeError("MAGMOM is not allowed when spin_mode is nonspin")

    draft["hubbard_mode"] = str(draft["hubbard_mode"] or "").strip().lower()
    if draft["hubbard_mode"] == "unresolved":
        raise MethodRecipeError("DFT+U must be explicitly disabled or fully mapped")
    if draft["hubbard_mode"] not in {"off", "manual"}:
        raise MethodRecipeError("hubbard_mode is unsupported")
    if draft["hubbard_mode"] == "manual":
        draft["hubbard_u"] = _normalize_hubbard(draft["hubbard_u"], elements)
    elif draft["hubbard_u"] not in ({}, None):
        raise MethodRecipeError("hubbard_u values are not allowed when DFT+U is off")
    else:
        draft["hubbard_u"] = {}

    draft["dipole_mode"] = str(draft["dipole_mode"] or "").strip().lower()
    if draft["dipole_mode"] == "unresolved":
        raise MethodRecipeError("dipole correction must be explicitly selected")
    allowed_dipole = {"off", "slab-z"} if draft["system_type"] == "slab" else {"off"}
    if draft["dipole_mode"] not in allowed_dipole:
        raise MethodRecipeError("dipole mode is incompatible with the selected system")

    draft["solvent_mode"] = str(draft["solvent_mode"] or "").strip().lower()
    if draft["solvent_mode"] not in {"off", "vaspsol"}:
        raise MethodRecipeError("solvent_mode is unsupported")
    draft["solvent_dielectric"] = _finite(
        draft["solvent_dielectric"], field="solvent dielectric", low=1.0, high=1000.0)
    if draft["task"] == "vaspsol" and draft["solvent_mode"] != "vaspsol":
        raise MethodRecipeError("the vaspsol task requires explicit VASPsol selection")

    draft["encut_mode"] = str(draft["encut_mode"] or "").strip().lower()
    if draft["encut_mode"] not in {"enmax_multiplier", "explicit"}:
        raise MethodRecipeError("encut_mode is unsupported")
    draft["encut_multiplier"] = _finite(
        draft["encut_multiplier"], field="ENCUT multiplier", low=1.0, high=3.0)
    if draft["encut_mode"] == "explicit":
        draft["encut_value"] = int(math.ceil(_finite(
            draft["encut_value"], field="ENCUT", low=100.0, high=5000.0)))
    elif draft["encut_value"] is not None:
        raise MethodRecipeError("encut_value is only allowed in explicit mode")

    draft["kpoints_mode"] = str(draft["kpoints_mode"] or "").strip().lower()
    if draft["kpoints_mode"] not in {"recommended", "explicit"}:
        raise MethodRecipeError("kpoints_mode is unsupported")
    if draft["kpoints_mode"] == "explicit":
        draft["kpoints_grid"] = _normalize_grid(draft["kpoints_grid"])
    elif draft["kpoints_grid"] is not None:
        raise MethodRecipeError("kpoints_grid is only allowed in explicit mode")
    if not isinstance(draft["include_convergence_dry_run"], bool):
        raise MethodRecipeError("include_convergence_dry_run must be boolean")
    return draft, origins, policy_evidence


def suggested_draft(system_type: Any, task: Any, policy_id: Any = None) -> dict[str, Any]:
    """Return a complete editable draft without performing scientific calculations or file IO."""
    system = str(system_type or "").strip().lower()
    selected_task = str(task or "").strip().lower()
    if system not in SYSTEM_TYPES:
        raise MethodRecipeError("system_type is unsupported")
    try:
        task_item = task_catalog.get_task(selected_task)
    except KeyError as exc:
        raise MethodRecipeError("task is not present in the task catalog") from exc
    if selected_task not in _TASK_PROFILES:
        raise MethodRecipeError("this task requires its dedicated workflow, not a single recipe")
    if system not in _TASK_SYSTEMS[selected_task]:
        raise MethodRecipeError("task is incompatible with the selected system_type")
    policy = _safe_identifier(policy_id, field="policy_id", optional=True)
    seed, evidence = _policy_seed(policy, system)
    draft = _default_draft()
    draft.update({key: value for key, value in seed.items() if value is not None})
    draft["system_type"] = system
    draft["task"] = selected_task
    return {
        "schema": DRAFT_SCHEMA,
        "draft": draft,
        "task": {
            "key": selected_task,
            "name_zh": task_item["name_zh"],
            "name_en": task_item["name_en"],
        },
        "policy_evidence": evidence,
        "requires_explicit": ["dispersion", "spin_mode", "hubbard_mode", "dipole_mode"],
        "recommendation_only": True,
        "authorizes_submission": False,
    }


def catalog() -> dict[str, Any]:
    tasks = []
    for key in _TASK_PROFILES:
        item = task_catalog.get_task(key)
        tasks.append({
            "key": key, "name_zh": item["name_zh"], "name_en": item["name_en"],
            "category": item["category"], "category_en": item["category_en"],
            "systems": sorted(_TASK_SYSTEMS[key]),
        })
    return {
        "schema": RECIPE_SCHEMA,
        "version": RECIPE_VERSION,
        "systems": list(SYSTEM_TYPES),
        "tasks": tasks,
        "xc": sorted(SUPPORTED_XC),
        "dispersion": list(DISPERSION),
        "precision": list(PRECISION),
        "lab_policies": lab_policies.catalog(),
        "recommendation_only": True,
        "authorizes_submission": False,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_text_sha256(value: str) -> str:
    """Hash bytes produced by the existing text-mode job builder on this platform."""
    rendered = value if os.linesep == "\n" else value.replace("\n", os.linesep)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _potcar_evidence(elements: list[str], lib_root: str,
                     xc: str) -> tuple[list[dict], str, str]:
    try:
        provenance = potcar_provenance(elements, lib_root)
    except Exception as exc:  # POTCAR errors may contain local absolute paths; replace them.
        raise MethodRecipeError("pseudopotential library is incomplete or unreadable") from exc
    expected = SUPPORTED_XC[xc]["potcar_family"]
    evidence = []
    rendered_chunks = []
    for item in provenance:
        title = str(item.get("titel") or "").strip()
        if not title.upper().startswith(expected.upper()):
            raise MethodRecipeError("XC to installed pseudopotential family mapping is unverified")
        if (_SECRET_VALUE.search(title) or _ABSOLUTE_PATH.search(title)
                or len(title) > 256):
            raise MethodRecipeError("pseudopotential identity metadata is unsafe")
        variant = str(item["variant"])
        try:
            content_hash = _file_sha256(Path(lib_root) / variant / "POTCAR")
            rendered_chunks.append(
                (Path(lib_root) / variant / "POTCAR").read_text(
                    encoding="utf-8", errors="replace"))
        except OSError as exc:
            raise MethodRecipeError("pseudopotential library is incomplete or unreadable") from exc
        evidence.append({
            "kind": "potcar", "element": item["element"], "variant": variant,
            "titel": title, "enmax_eV": float(item["enmax"]),
            "content_sha256": content_hash,
        })
    rendered_sha256 = _native_text_sha256("".join(rendered_chunks))
    return evidence, semantic_sha256(evidence), rendered_sha256


def _origin_entry(field: str, value: Any, *, origins: dict[str, str], reason_zh: str,
                  reason_en: str, risk: str, common_evidence: list[dict],
                  extra_evidence: list[dict] | None = None) -> dict[str, Any]:
    source = origins.get(field, "default")
    evidence = list(common_evidence)
    if extra_evidence:
        evidence.extend(extra_evidence)
    if source == "user":
        evidence.append({"kind": "explicit_user_draft", "field": field})
    return _entry(
        value=value, source=source, reason_zh=reason_zh, reason_en=reason_en, risk=risk,
        evidence=evidence, user_override=(source == "user"),
    )


def _build_recipe(draft: dict[str, Any], origins: dict[str, str], *, poscar_text: str,
                  poscar_sha256: str, elements: list[str], counts: list[int],
                  lib_root: str, policy_evidence: dict | None,
                  ) -> tuple[dict, list[int], str, str]:
    cell = read_cell_vectors(poscar_text)
    potcar_evidence, potcar_fingerprint, potcar_sha256 = _potcar_evidence(
        elements, lib_root, draft["xc"])
    task_ev = _task_evidence(draft["task"])
    common = [task_ev, {"kind": "poscar", "sha256": poscar_sha256}]
    if policy_evidence:
        common.append(policy_evidence)

    max_enmax = max(item["enmax_eV"] for item in potcar_evidence)
    if draft["encut_mode"] == "explicit":
        encut = int(draft["encut_value"])
        if encut + 1e-9 < max_enmax:
            raise MethodRecipeError("explicit ENCUT is below the selected POTCAR ENMAX")
        encut_reason = ("使用用户明确给出的 ENCUT，并核对不低于所选 POTCAR ENMAX。",
                        "Use the explicit ENCUT after verifying it is not below POTCAR ENMAX.")
        encut_risk = "high"
    else:
        encut = int(math.ceil(max_enmax * draft["encut_multiplier"]))
        encut_reason = ("由所选 POTCAR 的最大 ENMAX 乘显式倍率得到；仍须做 ENCUT 收敛测试。",
                        "Derived from maximum POTCAR ENMAX and the explicit multiplier; an ENCUT "
                        "convergence test is still required.")
        encut_risk = "high"

    if draft["kpoints_mode"] == "explicit":
        kpoints = list(draft["kpoints_grid"])
        kp_reason = ("使用用户明确给出的 Gamma-centered 网格；仍需记录收敛证据。",
                     "Use the explicit Gamma-centered mesh; convergence evidence is still needed.")
    else:
        kpoints = recommend_kpoints(cell, draft["system_type"])
        kp_reason = ("后端按倒格矢间距启发式生成起始网格；这不是 k 点已收敛的证据。",
                     "Backend reciprocal-spacing heuristic for a starting mesh; this is not "
                     "evidence of k-point convergence.")

    dimensions = {
        "system": _origin_entry(
            "system_type", draft["system_type"], origins=origins,
            reason_zh="体系边界决定 KPOINTS 与可用偶极模式。",
            reason_en="System boundary determines KPOINTS and allowed dipole modes.",
            risk="high", common_evidence=common),
        "task": _origin_entry(
            "task", draft["task"], origins=origins,
            reason_zh="任务键来自现有 task_catalog；复杂任务继续走专用 builder。",
            reason_en="Task key comes from task_catalog; complex tasks retain dedicated builders.",
            risk="medium", common_evidence=common),
        "xc": _origin_entry(
            "xc", {"functional": draft["xc"],
                   "potcar_family": SUPPORTED_XC[draft["xc"]]["potcar_family"]},
            origins=origins, reason_zh="泛函只在已验证的赝势族映射内生成。",
            reason_en="Generate XC only within a verified pseudopotential-family mapping.",
            risk="high", common_evidence=common, extra_evidence=potcar_evidence),
        "dispersion": _origin_entry(
            "dispersion", draft["dispersion"], origins=origins,
            reason_zh="色散模型由用户明确选择，策略文本不会被猜成 IVDW 数值。",
            reason_en="Dispersion is explicit; policy prose is never guessed into an IVDW value.",
            risk="high", common_evidence=common),
        "precision": _origin_entry(
            "precision", draft["precision"], origins=origins,
            reason_zh="PREC 控制 FFT 网格与投影精度，是起始精度设置。",
            reason_en="PREC controls FFT grids and projection precision as a starting setting.",
            risk="medium", common_evidence=common),
        "electronic_convergence": _origin_entry(
            "ediff", {"ediff_eV": draft["ediff"], "algo": "Normal",
                      "ismear": 0, "sigma_eV": 0.05}, origins=origins,
            reason_zh="显式记录电子收敛阈值；通用展宽仍需按体系验证。",
            reason_en="Record the electronic threshold explicitly; generic smearing still needs "
                      "system-specific validation.", risk="high", common_evidence=common),
        "ionic_convergence": _origin_entry(
            "ediffg", ({"force_eV_A": draft["ediffg"]} if draft["task"] in _IONIC_TASKS
                       else {"not_applicable": True}), origins=origins,
            reason_zh="有离子步时使用负 EDIFFG 表示力阈值；静态任务不写该键。",
            reason_en="Use negative EDIFFG as a force threshold for ionic tasks; omit it for static "
                      "tasks.", risk="medium", common_evidence=common),
        "spin": _origin_entry(
            "spin_mode", {"mode": draft["spin_mode"], "magmom": draft["magmom"] or None},
            origins=origins, reason_zh="磁态必须由用户明确选择；向导不推断基态自旋。",
            reason_en="The user must select the magnetic state; the wizard does not infer a ground "
                      "state.", risk="high", common_evidence=common),
        "hubbard_u": _origin_entry(
            "hubbard_mode", {"mode": draft["hubbard_mode"], "by_element": draft["hubbard_u"]},
            origins=origins, reason_zh="+U 必须明确关闭或按全部物种完整映射；未知值拒绝。",
            reason_en="DFT+U must be explicitly off or completely mapped for every species; unknown "
                      "values are rejected.", risk="high", common_evidence=common),
        "dipole": _origin_entry(
            "dipole_mode", draft["dipole_mode"], origins=origins,
            reason_zh="偶极修正由用户按几何对称性明确选择。",
            reason_en="Dipole correction is explicitly selected based on geometric symmetry.",
            risk="high", common_evidence=common),
        "solvent": _origin_entry(
            "solvent_mode", {"mode": draft["solvent_mode"],
                             "dielectric": (draft["solvent_dielectric"]
                                            if draft["solvent_mode"] == "vaspsol" else None)},
            origins=origins, reason_zh="溶剂模式显式记录；VASPsol 仍要求补丁版可执行文件。",
            reason_en="Solvent mode is explicit; VASPsol still requires a patched executable.",
            risk=("high" if draft["solvent_mode"] == "vaspsol" else "medium"),
            common_evidence=common),
        "encut": _origin_entry(
            "encut_value" if draft["encut_mode"] == "explicit" else "encut_multiplier",
            {"mode": draft["encut_mode"], "value_eV": encut,
             "enmax_multiplier": (draft["encut_multiplier"]
                                  if draft["encut_mode"] == "enmax_multiplier" else None)},
            origins=origins, reason_zh=encut_reason[0], reason_en=encut_reason[1],
            risk=encut_risk, common_evidence=common, extra_evidence=potcar_evidence),
        "kpoints": _origin_entry(
            "kpoints_grid" if draft["kpoints_mode"] == "explicit" else "kpoints_mode",
            {"mode": draft["kpoints_mode"], "scheme": "Gamma", "grid": kpoints,
             "shift": [0, 0, 0]}, origins=origins,
            reason_zh=kp_reason[0], reason_en=kp_reason[1], risk="high",
            common_evidence=common),
    }

    final_values: OrderedDict[str, Any] = OrderedDict()
    final_values["GGA"] = SUPPORTED_XC[draft["xc"]]["incar"]
    # PBE requires no METAGGA tag.  ``None`` is a removal proposal, not the literal string
    # ``METAGGA = None`` (which is not a portable VASP value).
    final_values["METAGGA"] = None
    final_values["LHFCALC"] = False
    final_values["PREC"] = PRECISION[draft["precision"]]
    final_values["ENCUT"] = encut
    final_values["EDIFF"] = draft["ediff"]
    final_values["ALGO"] = "Normal"
    final_values["ISMEAR"] = 0
    final_values["SIGMA"] = 0.05
    final_values.update(_TASK_PROFILES[draft["task"]])
    if draft["task"] in _IONIC_TASKS:
        final_values["EDIFFG"] = -abs(draft["ediffg"])
    final_values["ISPIN"] = 2 if draft["spin_mode"] == "collinear" else 1
    final_values["MAGMOM"] = draft["magmom"] if draft["spin_mode"] == "collinear" else None
    final_values["IVDW"] = DISPERSION[draft["dispersion"]]

    if draft["hubbard_mode"] == "off":
        final_values["LDAU"] = False
        for key in ("LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ"):
            final_values[key] = None
    else:
        final_values["LDAU"] = True
        final_values["LDAUTYPE"] = 2
        final_values["LDAUL"] = " ".join(str(draft["hubbard_u"][el]["l"]) for el in elements)
        final_values["LDAUU"] = " ".join(f'{draft["hubbard_u"][el]["u"]:g}' for el in elements)
        final_values["LDAUJ"] = " ".join(f'{draft["hubbard_u"][el]["j"]:g}' for el in elements)
        # Reuse method_policy's order-aware effective-U mapping as the execution gate.
        u_plan = {
            "ldau": True, "ldautype": 2, "ldaul": final_values["LDAUL"],
            "ldauu": final_values["LDAUU"], "ldauj": final_values["LDAUJ"],
            "element_orders": [elements],
        }
        if method_policy.effective_u_by_element(u_plan) is None:
            raise MethodRecipeError("DFT+U mapping is incomplete or ambiguous")

    if draft["dipole_mode"] == "slab-z":
        final_values.update(dipole_correction_keys(poscar_text, "slab"))
    else:
        final_values["LDIPOL"] = False
        final_values["IDIPOL"] = None
        final_values["DIPOL"] = None
    if draft["solvent_mode"] == "vaspsol":
        final_values.update(vaspsol_keys(True, eb_k=draft["solvent_dielectric"]))
    else:
        # Do not emit VASPsol-only tags for standard VASP.  ``None`` removes an existing tag after
        # explicit conflict review and otherwise stays absent.
        final_values["LSOL"] = None
        final_values["EB_K"] = None

    group_for_key: dict[str, str] = {
        "GGA": "xc", "METAGGA": "xc", "LHFCALC": "xc", "PREC": "precision",
        "ENCUT": "encut", "EDIFF": "electronic_convergence", "ALGO": "electronic_convergence",
        "ISMEAR": "electronic_convergence", "SIGMA": "electronic_convergence",
        "EDIFFG": "ionic_convergence", "ISPIN": "spin", "MAGMOM": "spin", "IVDW": "dispersion",
        "LDAU": "hubbard_u", "LDAUTYPE": "hubbard_u", "LDAUL": "hubbard_u",
        "LDAUU": "hubbard_u", "LDAUJ": "hubbard_u", "LDIPOL": "dipole",
        "IDIPOL": "dipole", "DIPOL": "dipole", "LSOL": "solvent", "EB_K": "solvent",
    }
    for key in _TASK_PROFILES[draft["task"]]:
        group_for_key[key] = "task"

    final_incar: dict[str, dict[str, Any]] = {}
    for key, value in final_values.items():
        dimension = dimensions[group_for_key[key]]
        base = {
            "value": copy.deepcopy(value), "source": dimension["source"],
            "reason": copy.deepcopy(dimension["reason"]), "risk": dimension["risk"],
            "evidence": copy.deepcopy(dimension["evidence"]),
            "user_override": dimension["user_override"],
        }
        final_incar[key] = {**base, "semantic_sha256": semantic_sha256(base)}

    kp_dimension = dimensions["kpoints"]
    kp_base = {
        "value": {"scheme": "Gamma", "grid": kpoints, "shift": [0, 0, 0]},
        "source": kp_dimension["source"], "reason": copy.deepcopy(kp_dimension["reason"]),
        "risk": kp_dimension["risk"], "evidence": copy.deepcopy(kp_dimension["evidence"]),
        "user_override": kp_dimension["user_override"],
    }
    final_kpoints = {"GRID": {**kp_base, "semantic_sha256": semantic_sha256(kp_base)}}

    dry_run = None
    if draft["include_convergence_dry_run"]:
        for key in _CONVERGENCE_TASKS:
            task_catalog.get_task(key)  # execution-time assertion that catalog semantics still exist
        denser = [min(99, item + 2 if item > 1 else 1) for item in kpoints]
        dry_run = {
            "schema": "vcstudio.method-recipe-convergence-dry-run/v1",
            "scientifically_converged": False,
            "authorizes_submission": False,
            "warning_zh": "仅为待运行的收敛候选；没有任何一点被计算或判定收敛。",
            "warning_en": "Candidates for future convergence runs only; no point has been computed "
                          "or judged converged.",
            "series": [
                {"task": "conv_encut", "values_eV": sorted(set([
                    int(math.ceil(max_enmax * factor)) for factor in (1.10, 1.20, 1.30, 1.40)
                ]))},
                {"task": "conv_kmesh", "gamma_grids": [kpoints, denser]},
                {"task": "conv_vacuum", "candidate_A": [12, 15, 18, 21],
                 "requires_new_structures": True},
                {"task": "conv_thickness", "candidate_layers": [4, 5, 6, 7],
                 "requires_new_structures": True},
            ],
        }
        dry_run["semantic_sha256"] = semantic_sha256(dry_run)

    semantic = {
        "schema": RECIPE_SCHEMA, "version": RECIPE_VERSION,
        "dimensions": dimensions,
        "final": {"INCAR": final_incar, "KPOINTS": final_kpoints},
        "potcar_fingerprint": potcar_fingerprint,
        "potcar_sha256": potcar_sha256,
        "convergence_dry_run": dry_run,
        "scientific_status": "candidate",
        "scientifically_validated": False,
        "authorizes_submission": False,
    }
    recipe = {**semantic, "semantic_sha256": semantic_sha256(semantic)}
    return recipe, kpoints, potcar_fingerprint, potcar_sha256


def _vasp_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        def logical(value: Any) -> bool | None:
            if isinstance(value, bool):
                return value
            text = str(value).strip().upper().strip(".")
            return True if text in {"T", "TRUE"} else False if text in {"F", "FALSE"} else None
        return logical(left) is not None and logical(left) == logical(right)
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return " ".join(str(left).split()).upper() == " ".join(str(right).split()).upper()
    return math.isfinite(left_number) and math.isfinite(right_number) and math.isclose(
        left_number, right_number, rel_tol=1e-12, abs_tol=1e-12)


def semantic_diff(existing: Mapping[str, Any], recipe: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compare recipe-managed keys only; unrelated existing values are never exposed."""
    current = {str(key).upper(): value for key, value in existing.items()}
    out = []
    for key, entry in recipe["final"]["INCAR"].items():
        proposed = entry["value"]
        if key not in current:
            status = "unchanged_absent" if proposed is None else "add"
            existing_value = None
            requires = False
        else:
            existing_value = current[key]
            if proposed is not None and _vasp_equal(existing_value, proposed):
                status, requires = "same", False
            else:
                status, requires = ("remove_conflict" if proposed is None else "conflict"), True
        out.append({
            "key": key, "status": status, "existing": existing_value,
            "proposed": proposed, "default_action": "existing" if requires else "recipe",
            "requires_resolution": requires,
            "risk": entry["risk"], "reason": copy.deepcopy(entry["reason"]),
        })
    return out


def _public_diff_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[redacted]"
    text = str(value)
    if len(text) > 512 or _SECRET_VALUE.search(text) or _ABSOLUTE_PATH.search(text.strip()):
        return "[redacted]"
    return text


def _target_snapshot(path_value: Any) -> tuple[Path, str, dict[str, str]]:
    raw = str(path_value or "").strip()
    if not raw:
        raise MethodRecipeError("out_dir is required")
    try:
        target = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise MethodRecipeError("out_dir is invalid") from exc
    # Directory publication uses one no-replace rename.  Even an empty directory, broken link, or
    # junction at the requested name is an existing authority and must never be merged/overwritten.
    if os.path.lexists(str(target)):
        raise MethodRecipeError("out_dir must name a target that does not already exist")
    semantic = {
        # The canonical path is hashed into server-side authority and is never returned.
        "canonical_path": os.path.normcase(str(target)), "exists": False, "managed": {},
    }
    return target, semantic_sha256(semantic), {}


def _logical(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper().strip(".")
    if text in {"T", "TRUE"}:
        return True
    if text in {"F", "FALSE"}:
        return False
    return None


def _existing_tag_entry(value: Any, *, key: str, classification: str,
                        compatible: bool) -> dict[str, Any]:
    evidence = [{
        "kind": "method_tag_policy", "key": key, "classification": classification,
        "policy_version": 1,
    }]
    if compatible:
        return _entry(
            value=value, source="user",
            reason_zh="现有标签经明确策略判定不改变本 recipe 声明的方法，可保留并纳入哈希。",
            reason_en="The explicit tag policy found this existing value compatible with the "
                      "declared recipe; it is retained and hashed.",
            risk="low", evidence=evidence, user_override=True)
    return _entry(
        value=None, source="user",
        reason_zh="现有标签会改变或无法证明不改变声明方法，必须显式移除；不能选择保留。",
        reason_en="This existing tag changes, or cannot be proven not to change, the declared "
                  "method. It must be explicitly removed and cannot be retained.",
        risk="blocking", evidence=evidence, user_override=True)


def _apply_existing_method_tag_policy(existing: Mapping[str, Any], recipe: dict) -> dict:
    """Make every existing INCAR key part of the hashed allow/deny contract."""
    governed = copy.deepcopy(recipe)
    final = governed["final"]["INCAR"]
    for raw_key, value in existing.items():
        key = str(raw_key).upper()
        if key in final:
            continue
        safe_value = _public_diff_value(value) != "[redacted]"
        if key in _METHOD_NEUTRAL_PASSTHROUGH and safe_value:
            final[key] = _existing_tag_entry(
                value, key=key, classification="method_neutral_passthrough", compatible=True)
        elif key in _METHOD_FALSE_COMPATIBLE and _logical(value) is False and safe_value:
            final[key] = _existing_tag_entry(
                value, key=key, classification="disabled_only", compatible=True)
        elif key in _METHOD_FALSE_COMPATIBLE:
            final[key] = _existing_tag_entry(
                value, key=key, classification="disabled_only", compatible=False)
        elif key in _METHOD_MUST_BE_ABSENT:
            final[key] = _existing_tag_entry(
                value, key=key, classification="must_be_absent", compatible=False)
        else:
            # Unknown is deny-by-default.  This is intentionally stricter than VASP accepting a
            # tag: acceptance by the executable is not evidence of compatibility with this recipe.
            final[key] = _existing_tag_entry(
                value, key=key, classification="unknown_method_significant", compatible=False)
    semantic = {key: value for key, value in governed.items() if key != "semantic_sha256"}
    governed["semantic_sha256"] = semantic_sha256(semantic)
    return governed


def _validate_merged_incar(merged: Mapping[str, Any], recipe: Mapping[str, Any], *,
                           elements: list[str], counts: list[int]) -> None:
    """Reject existing-key choices that would contradict a fail-closed recipe dimension."""
    values = {str(key).upper(): value for key, value in merged.items()}
    for key in recipe["final"]["INCAR"]:
        if key not in values:
            continue
        text = str(values[key])
        if (_SECRET_VALUE.search(text) or _ABSOLUTE_PATH.search(text.strip())
                or len(text) > 4096):
            raise MethodRecipeError("final recipe-managed INCAR value is unsafe")
    for key, entry in recipe["final"]["INCAR"].items():
        policy = next((item for item in entry.get("evidence", [])
                       if item.get("kind") == "method_tag_policy"), None)
        if not policy:
            continue
        classification = policy.get("classification")
        if classification in {"must_be_absent", "unknown_method_significant"} and key in values:
            raise MethodRecipeError("final INCAR retains a denied method-significant tag")
        if (classification == "disabled_only" and key in values
                and _logical(values[key]) is not False):
            raise MethodRecipeError("final INCAR enables an incompatible method-significant tag")
    xc = recipe["dimensions"]["xc"]["value"]["functional"]
    if xc != "PBE" or not _vasp_equal(values.get("GGA"), "PE"):
        raise MethodRecipeError("final XC to pseudopotential mapping is not PBE/PAW_PBE")
    if "METAGGA" in values:
        raise MethodRecipeError("final INCAR still enables or declares an unsupported METAGGA")
    if _logical(values.get("LHFCALC", False)) is not False:
        raise MethodRecipeError("final INCAR still enables an unsupported hybrid functional")

    dispersion = recipe["dimensions"]["dispersion"]["value"]
    expected_ivdw = DISPERSION[dispersion]
    if expected_ivdw is None:
        if "IVDW" in values:
            raise MethodRecipeError("final INCAR conflicts with the explicit no-dispersion choice")
    elif not _vasp_equal(values.get("IVDW"), expected_ivdw):
        raise MethodRecipeError("final INCAR does not match the selected dispersion model")

    task = recipe["dimensions"]["task"]["value"]
    for key, expected in _TASK_PROFILES[task].items():
        if key not in values or not _vasp_equal(values[key], expected):
            raise MethodRecipeError("final INCAR conflicts with the selected task profile")

    spin = recipe["dimensions"]["spin"]["value"]
    expected_ispin = 2 if spin["mode"] == "collinear" else 1
    if not _vasp_equal(values.get("ISPIN"), expected_ispin):
        raise MethodRecipeError("final INCAR conflicts with the selected magnetic state")
    if expected_ispin == 2:
        magmom = str(values.get("MAGMOM") or "").strip()
        if not magmom or not counts or _magmom_count(magmom) != sum(counts):
            raise MethodRecipeError("final MAGMOM does not cover every POSCAR atom")
    elif "MAGMOM" in values:
        raise MethodRecipeError("final INCAR retains MAGMOM for the explicit nonspin state")

    hubbard = recipe["dimensions"]["hubbard_u"]["value"]
    if hubbard["mode"] == "off":
        if _logical(values.get("LDAU", False)) is not False:
            raise MethodRecipeError("final INCAR conflicts with the explicit DFT+U off choice")
        if any(key in values for key in ("LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ")):
            raise MethodRecipeError("final INCAR retains inactive or unsupported DFT+U parameters")
    else:
        try:
            u_type = int(float(values.get("LDAUTYPE")))
        except (TypeError, ValueError) as exc:
            raise MethodRecipeError("final LDAUTYPE is invalid") from exc
        if u_type not in SUPPORTED_LDAUTYPES:
            supported = ", ".join(str(item) for item in sorted(SUPPORTED_LDAUTYPES))
            raise MethodRecipeError(
                f"final LDAUTYPE is unsupported; allowed values: {supported}")
        plan = {
            "ldau": values.get("LDAU"), "ldautype": values.get("LDAUTYPE"),
            "ldaul": values.get("LDAUL"), "ldauu": values.get("LDAUU"),
            "ldauj": values.get("LDAUJ"), "element_orders": [elements],
        }
        if method_policy.effective_u_by_element(plan) is None:
            raise MethodRecipeError("final DFT+U mapping is incomplete or ambiguous")

    dipole = recipe["dimensions"]["dipole"]["value"]
    if dipole == "off" and _logical(values.get("LDIPOL", False)) is not False:
        raise MethodRecipeError("final INCAR conflicts with the explicit dipole-off choice")
    if dipole == "off" and any(key in values for key in ("IDIPOL", "DIPOL")):
        raise MethodRecipeError("final INCAR retains inactive dipole parameters")
    if dipole == "slab-z":
        if _logical(values.get("LDIPOL")) is not True or not _vasp_equal(
                values.get("IDIPOL"), 3):
            raise MethodRecipeError("final INCAR does not match slab-z dipole correction")

    solvent = recipe["dimensions"]["solvent"]["value"]["mode"]
    if solvent == "off" and any(key in values for key in ("LSOL", "EB_K")):
        raise MethodRecipeError("final INCAR still contains inactive VASPsol parameters")
    if solvent == "vaspsol" and _logical(values.get("LSOL")) is not True:
        raise MethodRecipeError("final INCAR does not match the selected VASPsol mode")

    encut = values.get("ENCUT")
    try:
        numeric_encut = float(encut)
    except (TypeError, ValueError) as exc:
        raise MethodRecipeError("final ENCUT is not numeric") from exc
    enmax = max(
        float(item["enmax_eV"])
        for item in recipe["dimensions"]["xc"]["evidence"]
        if item.get("kind") == "potcar"
    )
    if not math.isfinite(numeric_encut) or numeric_encut < enmax:
        raise MethodRecipeError("final ENCUT is below the selected POTCAR ENMAX")


def _merge_incar(existing: Mapping[str, Any], recipe: dict, diff: list[dict],
                 resolutions: Mapping[str, str], *, elements: list[str],
                 counts: list[int]) -> tuple[OrderedDict, dict]:
    merged = OrderedDict((str(key).upper(), value) for key, value in existing.items())
    final_recipe = copy.deepcopy(recipe)
    by_key = {item["key"]: item for item in diff}
    required = {key for key, item in by_key.items() if item["requires_resolution"]}
    if set(resolutions) != required:
        raise MethodRecipeError("every INCAR conflict requires one explicit resolution")
    for key, decision in resolutions.items():
        if decision not in {"existing", "recipe"}:
            raise MethodRecipeError("conflict resolution must be existing or recipe")
        if key not in required:
            raise MethodRecipeError("conflict resolution contains an unknown key")

    for key, entry in final_recipe["final"]["INCAR"].items():
        item = by_key[key]
        decision = resolutions.get(key, "recipe")
        if item["requires_resolution"] and decision == "existing":
            value = merged[key]
            base = {
                "value": value, "source": "user",
                "reason": _reason(
                    "用户在逐项冲突确认中保留了已有 INCAR 值。",
                    "The user kept the existing INCAR value in the per-key conflict review."),
                "risk": entry["risk"],
                "evidence": list(entry["evidence"]) + [{
                    "kind": "explicit_conflict_resolution", "decision": "existing", "key": key,
                }],
                "user_override": True,
            }
            try:
                entry_hash = semantic_sha256(base)
            except (TypeError, ValueError) as exc:
                raise MethodRecipeError(
                    "existing INCAR value is not safe recipe metadata") from exc
            final_recipe["final"]["INCAR"][key] = {
                **base, "semantic_sha256": entry_hash,
            }
            continue
        value = entry["value"]
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    _validate_merged_incar(merged, final_recipe, elements=elements, counts=counts)
    semantic = {key: value for key, value in final_recipe.items() if key != "semantic_sha256"}
    final_recipe["semantic_sha256"] = semantic_sha256(semantic)
    return merged, final_recipe


def _dry_public_preview(recipe: dict, diff: list[dict], *, token: str, target_id: str,
                        expires_at: float, client_intent_id: str) -> dict[str, Any]:
    conflicts = [item["key"] for item in diff if item["requires_resolution"]]
    return {
        "schema": PREVIEW_SCHEMA,
        "ok": True,
        "token": token,
        "preview_sha256": recipe["semantic_sha256"],
        "target_id": target_id,
        "client_intent_id": client_intent_id,
        "expires_at_unix": int(expires_at),
        "recipe": copy.deepcopy(recipe),
        "diff": [
            {**copy.deepcopy(item), "existing": _public_diff_value(item.get("existing"))}
            for item in diff
        ],
        "conflicts": conflicts,
        "default_overwrites_existing_keys": False,
        "requires_explicit_confirmation": True,
        "authorizes_submission": False,
        "scientifically_validated": False,
        "error": None,
    }


class MethodRecipeService:
    """In-memory preview capability store with expiry, single use, and idempotent replay."""

    def __init__(self, *, clock: Callable[[], float] | None = None, ttl_seconds: int = 600,
                 max_previews: int = 128):
        if not 30 <= int(ttl_seconds) <= 3600:
            raise ValueError("ttl_seconds must be between 30 and 3600")
        self._clock = clock or time.time
        self._ttl = int(ttl_seconds)
        self._max = int(max_previews)
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}

    def _prune(self, now: float) -> None:
        expired = [token for token, record in self._records.items()
                   if record["expires_at"] < now and not record.get("used")]
        for token in expired:
            self._records.pop(token, None)
        if len(self._records) > self._max:
            ordered = sorted(self._records, key=lambda item: self._records[item]["created_at"])
            for token in ordered[:len(self._records) - self._max]:
                self._records.pop(token, None)

    def preview(self, request: Any, *, project_defaults: Mapping[str, Any] | None = None) -> dict:
        prepared = _strict_object(request, _PREVIEW_REQUEST_FIELDS, field="preview request")
        poscar_loaded = _read_text(prepared.get("poscar_path"), field="POSCAR")
        assert poscar_loaded is not None
        poscar_text, poscar_hash = poscar_loaded
        elements, counts = parse_poscar_species(poscar_text)
        if not elements or len(elements) != len(counts) or any(count <= 0 for count in counts):
            raise MethodRecipeError("POSCAR must use a valid VASP5 species/count header")

        incar_loaded = _read_text(prepared.get("incar_path"), field="existing INCAR", optional=True)
        if incar_loaded:
            incar_text, incar_hash = incar_loaded
            existing = parse_incar(incar_text)
        else:
            incar_text, incar_hash, existing = "", None, OrderedDict()
        lib_root = str(prepared.get("lib_root") or "").strip()
        if not lib_root:
            raise MethodRecipeError("pseudopotential library is required")
        policy_id = _safe_identifier(prepared.get("policy_id"), field="policy_id", optional=True)
        project_id = _safe_identifier(prepared.get("project_id"), field="project_id", optional=True)
        client_intent_id = _safe_identifier(
            prepared.get("client_intent_id"), field="client_intent_id")
        draft, origins, policy_evidence = _normalize_draft(
            prepared.get("draft"), elements=elements, counts=counts, policy_id=policy_id,
            project_defaults=project_defaults)
        recipe, kpoints, potcar_fingerprint, potcar_sha256 = _build_recipe(
            draft, origins, poscar_text=poscar_text, poscar_sha256=poscar_hash,
            elements=elements, counts=counts, lib_root=lib_root,
            policy_evidence=policy_evidence)
        recipe = _apply_existing_method_tag_policy(existing, recipe)
        diff = semantic_diff(existing, recipe)
        target, target_id, _managed = _target_snapshot(prepared.get("out_dir"))
        now = float(self._clock())
        token = secrets.token_urlsafe(32)
        record = {
            "created_at": now, "expires_at": now + self._ttl,
            "target": target, "target_id": target_id, "project_id": project_id,
            "client_intent_id": client_intent_id,
            "poscar_path": Path(str(prepared["poscar_path"])), "poscar_hash": poscar_hash,
            "incar_path": (Path(str(prepared["incar_path"])) if prepared.get("incar_path") else None),
            "incar_hash": incar_hash, "existing": existing, "lib_root": lib_root,
            "recipe": recipe, "diff": diff, "kpoints": kpoints,
            "potcar_fingerprint": potcar_fingerprint, "used": False,
            "potcar_sha256": potcar_sha256,
            "elements": list(elements), "counts": list(counts),
        }
        with self._lock:
            self._prune(now)
            self._records[token] = record
        return _dry_public_preview(
            recipe, diff, token=token, target_id=target_id, expires_at=record["expires_at"],
            client_intent_id=client_intent_id)

    def _revalidate(self, record: dict[str, Any], *, allow_transaction_target: bool = False) -> None:
        try:
            if _file_sha256(record["poscar_path"]) != record["poscar_hash"]:
                raise MethodRecipeTokenError("POSCAR changed after preview; request a new preview")
            incar_path = record["incar_path"]
            if incar_path is not None:
                if _file_sha256(incar_path) != record["incar_hash"]:
                    raise MethodRecipeTokenError(
                        "existing INCAR changed after preview; request a new preview")
            if not allow_transaction_target:
                _target, target_id, _managed = _target_snapshot(record["target"])
                if target_id != record["target_id"]:
                    raise MethodRecipeTokenError("target identity changed after preview")
            elements = [item["element"] for item in record["recipe"]["dimensions"]["xc"]
                        ["evidence"] if item.get("kind") == "potcar"]
            _evidence, fingerprint, rendered_sha256 = _potcar_evidence(
                elements, record["lib_root"],
                record["recipe"]["dimensions"]["xc"]["value"]["functional"])
            if fingerprint != record["potcar_fingerprint"]:
                raise MethodRecipeTokenError(
                    "pseudopotential evidence changed after preview; request a new preview")
            if rendered_sha256 != record["potcar_sha256"]:
                raise MethodRecipeTokenError(
                    "pseudopotential content changed after preview; request a new preview")
        except MethodRecipeTokenError:
            raise
        except (OSError, MethodRecipeError) as exc:
            raise MethodRecipeTokenError("preview evidence is no longer available") from exc

    def confirm(self, request: Any, writer: Callable[[dict[str, Any]], dict[str, Any]]) -> dict:
        prepared = _strict_object(request, _CONFIRM_REQUEST_FIELDS, field="confirm request")
        token = str(prepared.get("token") or "").strip()
        preview_hash = str(prepared.get("preview_sha256") or "").strip().lower()
        target_id = str(prepared.get("target_id") or "").strip().lower()
        idempotency_key = _safe_identifier(
            prepared.get("idempotency_key"), field="idempotency_key")
        client_intent_id = _safe_identifier(
            prepared.get("client_intent_id"), field="client_intent_id")
        if not token or not _HEX64.fullmatch(preview_hash) or not _HEX64.fullmatch(target_id):
            raise MethodRecipeTokenError("preview binding is invalid")
        if prepared.get("confirmed") is not True:
            raise MethodRecipeError("confirmed=true is required before writing inputs")
        resolutions = prepared.get("resolutions")
        if not isinstance(resolutions, Mapping):
            raise MethodRecipeError("resolutions must be an object")
        resolutions = {str(key).upper(): str(value).strip().lower()
                       for key, value in resolutions.items()}
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            record = self._records.get(token)
            if record is None:
                raise MethodRecipeTokenError("preview token is unknown or expired")
            used = bool(record.get("used"))
            if not used and record["expires_at"] < now:
                self._records.pop(token, None)
                raise MethodRecipeTokenError("preview token has expired")
            if preview_hash != record["recipe"]["semantic_sha256"]:
                raise MethodRecipeTokenError("preview hash does not match the token")
            if target_id != record["target_id"]:
                raise MethodRecipeTokenError("target identity does not match the token")
            if client_intent_id != record["client_intent_id"]:
                raise MethodRecipeTokenError("client intent does not match the token")
            if not used:
                self._revalidate(
                    record, allow_transaction_target=bool(record.get("write_attempted")))
            merged, final_recipe = _merge_incar(
                record["existing"], record["recipe"], record["diff"], resolutions,
                elements=record["elements"], counts=record["counts"])
            final_incar = incar_dict_to_str(merged)
            expected_hashes = {
                "INCAR": _native_text_sha256(final_incar),
                "POSCAR": record["poscar_hash"],
                "KPOINTS": _native_text_sha256(kpoints_str(record["kpoints"])),
                "POTCAR": record["potcar_sha256"],
            }
            confirmation_binding = {
                "schema": CONFIRMATION_SCHEMA,
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "preview_sha256": preview_hash,
                "recipe_semantic_sha256": final_recipe["semantic_sha256"],
                "resolutions": dict(resolutions),
                "expected_source_hashes": {
                    "POSCAR": record["poscar_hash"],
                    "INCAR": record["incar_hash"],
                    "POTCAR": record["potcar_sha256"],
                    "POTCAR_EVIDENCE": record["potcar_fingerprint"],
                },
                "expected_target_hashes": dict(expected_hashes),
                "target_id": record["target_id"],
                "project_id": record["project_id"],
                "client_intent_id": client_intent_id,
                "idempotency_key": idempotency_key,
            }
            fingerprint = confirmation_binding_sha256(confirmation_binding)
            frozen_binding = record.get("confirmation_binding")
            frozen_fingerprint = record.get("confirm_fingerprint")
            if frozen_binding is not None:
                if frozen_binding != confirmation_binding or frozen_fingerprint != fingerprint:
                    message = ("preview token has already been used" if used else
                               "confirmation intent differs from the first write attempt")
                    raise MethodRecipeTokenError(message)
            elif used:
                raise MethodRecipeTokenError("used preview lacks a confirmation binding")
            if used:
                return copy.deepcopy(record["result"])

            plan = {
                "target": record["target"], "target_id": record["target_id"],
                "project_id": record["project_id"], "poscar_path": record["poscar_path"],
                "lib_root": record["lib_root"], "final_incar": final_incar,
                "kpoints": list(record["kpoints"]), "recipe": final_recipe,
                "preview_sha256": preview_hash, "resolutions": dict(resolutions),
                "calc_type": final_recipe["dimensions"]["system"]["value"],
                "task": final_recipe["dimensions"]["task"]["value"],
                "confirmed_at_unix": int(now),
                "expected_hashes": expected_hashes,
                "confirmation_binding": copy.deepcopy(confirmation_binding),
                "confirmation_sha256": fingerprint,
                # The publisher calls this again only after acquiring the stable target OS lock.
                "revalidate": lambda: self._revalidate(
                    record, allow_transaction_target=bool(record.get("write_attempted"))),
            }
            # The desktop bridge is synchronous.  Keeping the lock across the write closes the
            # two-click race and gives this in-memory capability exactly-once behavior.
            if frozen_binding is None:
                record["confirmation_binding"] = copy.deepcopy(confirmation_binding)
                record["confirm_fingerprint"] = fingerprint
            # Freeze before invoking any writer code, including code that raises or never returns a
            # structured result.  Every later retry must reproduce the same semantic authority.
            record["write_attempted"] = True
            result = writer(plan)
            if not isinstance(result, dict):
                raise MethodRecipeError("recipe writer returned an invalid result")
            if result.get("ok") is not True:
                # A failed recoverable publication never burns the one-use capability.  The same
                # bound intent may retry after rollback/recovery, or the user may request a new
                # preview.  Only a fully published and registered bundle consumes the token.
                return copy.deepcopy(result)
            record["used"] = True
            record["result"] = copy.deepcopy(result)
            return copy.deepcopy(result)


def write_sidecar(job_dir: str | os.PathLike, record: Mapping[str, Any]) -> tuple[Path, str]:
    """Atomically write the license-safe recipe record (never POTCAR contents or local paths)."""
    target = Path(job_dir) / SIDECAR_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{SIDECAR_NAME}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return target, _file_sha256(target)


def sidecar_record(plan: Mapping[str, Any], *, incar_sha256: str) -> dict[str, Any]:
    """Build the immutable audit record referenced from ``job.yaml``."""
    recipe = copy.deepcopy(plan["recipe"])
    return {
        "schema": RECORD_SCHEMA,
        "version": RECIPE_VERSION,
        "recipe": recipe,
        "preview_sha256": plan["preview_sha256"],
        "recipe_semantic_sha256": recipe["semantic_sha256"],
        "target_id": plan["target_id"],
        "project_id": plan.get("project_id"),
        "conflict_resolutions": dict(plan["resolutions"]),
        "confirmation_binding": copy.deepcopy(plan["confirmation_binding"]),
        "confirmation_sha256": plan["confirmation_sha256"],
        "incar_sha256": incar_sha256,
        "confirmed_at_unix": int(plan["confirmed_at_unix"]),
        "scientific_status": "candidate",
        "scientifically_validated": False,
        "authorizes_submission": False,
    }


def manifest_reference(record: Mapping[str, Any], *, sidecar_sha256: str) -> dict[str, Any]:
    """Create the compact recipe lineage embedded in the existing job manifest chain."""
    final_incar = record["recipe"]["final"]["INCAR"]
    sources: dict[str, int] = {source: 0 for source in sorted(SOURCES)}
    overrides = []
    for key, item in final_incar.items():
        sources[item["source"]] += 1
        if item["user_override"]:
            overrides.append(key)
    return {
        "schema": RECORD_SCHEMA,
        "version": RECIPE_VERSION,
        "sidecar": SIDECAR_NAME,
        "sidecar_sha256": sidecar_sha256,
        "recipe_semantic_sha256": record["recipe_semantic_sha256"],
        "preview_sha256": record["preview_sha256"],
        "incar_sha256": record["incar_sha256"],
        "sources": sources,
        "user_overrides": sorted(overrides),
        "conflict_resolutions": dict(record["conflict_resolutions"]),
        "confirmation_sha256": record["confirmation_sha256"],
        "scientific_status": "candidate",
        "scientifically_validated": False,
        "authorizes_submission": False,
    }


__all__ = [
    "DRAFT_SCHEMA", "MethodRecipeError", "MethodRecipeService", "MethodRecipeTokenError",
    "PREVIEW_SCHEMA", "RECIPE_SCHEMA", "RECIPE_VERSION", "RECORD_SCHEMA", "SIDECAR_NAME",
    "CONFIRMATION_SCHEMA", "catalog", "confirmation_binding_sha256", "manifest_reference",
    "semantic_diff", "semantic_sha256", "sidecar_record", "suggested_draft", "write_sidecar",
]
