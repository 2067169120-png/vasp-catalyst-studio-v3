"""Cross-project adsorption/free-energy comparison data model.

This module is deliberately independent from the GUI and renderers.  It turns
already validated project summaries into one immutable, JSON-safe snapshot
used by charts, DOCX/PDF reports and the conversational assistant.

Scientific guardrails:

* adsorption configurations are grouped by canonical adsorbate species;
* the lowest electronic adsorption energy is selected per species, with a
  configurable near-degeneracy deadband retained for audit;
* adsorption-energy differences are never reinterpreted as reaction steps;
* free-energy ladders are compared only when their ordered step labels and
  energy correction basis match;
* missing cross-project method evidence permits a diagnostic comparison but
  never a definitive numerical ranking.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter

SCHEMA = "vcstudio.project-comparison/v1"
SPECIES_ORDER = ("S8", "Li2S8", "Li2S6", "Li2S4", "Li2S2", "Li2S")
_SPECIES_RANK = {species: index for index, species in enumerate(SPECIES_ORDER)}
_SUBSCRIPT_TRANS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
# Match a complete Li-S formula token.  A prefix expression such as
# ``Li2S(?:8|6|4|2)?`` silently aliases Li2S3/Li2S10 to terminal Li2S, which
# corrupts the stable-configuration table and any downstream comparison.  The
# generic digit suffix preserves uncommon/path-specific intermediates while
# the alphanumeric boundaries avoid extracting S8 from an unrelated material
# name such as FeS8.
_FORMULA_RE = re.compile(
    r"(?<![A-Za-z0-9])(Li2S(?:\d+)?|LiS\d+|S\d+)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_OPAQUE_CONFIGURATION_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$"
)


def _finite(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def canonical_species(value) -> str:
    """Return a stable Li-S species key without trusting directory prefixes."""
    text = str(value or "").translate(_SUBSCRIPT_TRANS)
    match = _FORMULA_RE.search(text)
    if not match:
        return text.strip()
    raw = match.group(1).lower()
    mapping = {
        "s8": "S8",
        "li2s8": "Li2S8",
        "li2s6": "Li2S6",
        "li2s4": "Li2S4",
        "li2s2": "Li2S2",
        "li2s": "Li2S",
        "lis2": "LiS2",
    }
    if raw in mapping:
        return mapping[raw]
    if raw.startswith("li2s"):
        return "Li2S" + raw[4:]
    if raw.startswith("lis"):
        return "LiS" + raw[3:]
    return "S" + raw[1:]


def _species_sort_key(species: str) -> tuple[int, str]:
    return _SPECIES_RANK.get(species, len(_SPECIES_RANK)), species


def stable_species_rows(summary: dict, *, deadband_eV: float = 0.15) -> list[dict]:
    """Select the most stable configuration per species and retain co-minima."""
    groups: dict[str, list[dict]] = {}
    for raw in (summary or {}).get("rows") or []:
        value = _finite(raw.get("delta_e"))
        if value is None:
            continue
        species = canonical_species(raw.get("species") or raw.get("reference_species")
                                    or raw.get("name"))
        if not species:
            continue
        raw_identity = str(
            raw.get("configuration_id") or raw.get("job_id") or ""
        ).strip()
        configuration_id = (
            raw_identity
            if _OPAQUE_CONFIGURATION_ID_RE.fullmatch(raw_identity)
            else ""
        )
        row = {
            "species": species,
            "name": str(raw.get("name") or ""),
            # Historical consumers call this field ``job``.  It is an opaque
            # configuration identity only; filesystem locators are never
            # projected into the comparison snapshot.
            "job": configuration_id,
            "delta_e": value,
            "state": str(raw.get("state") or ""),
            "method_status": str((raw.get("method_check") or {}).get("status") or
                                 (summary.get("method_consistency") or {}).get("status") or
                                 "unverified"),
            "note": str(raw.get("note") or ""),
        }
        groups.setdefault(species, []).append(row)

    selected = []
    for species, rows in groups.items():
        ordered = sorted(rows, key=lambda row: (row["delta_e"], row["name"], row["job"]))
        best = ordered[0]
        co_minima = [
            {
                "name": row["name"],
                "job": row["job"],
                "delta_e": row["delta_e"],
                "dd_e": round(row["delta_e"] - best["delta_e"], 6),
            }
            for row in ordered[1:]
            if row["delta_e"] - best["delta_e"] <= deadband_eV + 1e-12
        ]
        selected.append({
            **best,
            "n_configurations": len(rows),
            "co_minima": co_minima,
            "near_degenerate": bool(co_minima),
            "ranking_deadband_eV": float(deadband_eV),
        })
    return sorted(selected, key=lambda row: _species_sort_key(row["species"]))


def _fed_view(fed: dict | None) -> dict | None:
    if not isinstance(fed, dict):
        return None
    steps = list(fed.get("steps") or [])
    if len(steps) < 2:
        return None
    labels, values = [], []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return None
        value = _finite(step.get("G"))
        if value is None:
            return None
        labels.append(str(step.get("label") or step.get("species") or f"step {index + 1}"))
        values.append(value)
    pds_index = fed.get("pds_index")
    try:
        pds_index = int(pds_index) if pds_index is not None else None
    except (TypeError, ValueError):
        pds_index = None
    if pds_index is not None and not (0 <= pds_index < len(values) - 1):
        pds_index = None
    return {
        "step_labels": labels,
        "G": values,
        "pds_index": pds_index,
        "pds_label": (f"{labels[pds_index]} -> {labels[pds_index + 1]}"
                      if pds_index is not None else None),
        "u_l": _finite(fed.get("u_l")),
        "delta_g_max": _finite(fed.get("delta_g_max")),
        "thermo_corrected": bool(fed.get("thermo_corrected")),
        "temperature_K": _finite(fed.get("temperature_K") or
                                 (fed.get("thermo") or {}).get("temperature_K")),
        "thermo_correction_fingerprint": str(
            fed.get("thermo_correction_fingerprint") or ""),
        "solvated": bool(fed.get("solvated") or fed.get("solvation_corrected")),
        "solvation_model": str(
            fed.get("solvation_model") or fed.get("solvent_model") or ""),
        "reference": str(fed.get("reference") or fed.get("electrode") or ""),
        "reaction_path_id": str(fed.get("reaction_path_id") or ""),
    }


def _method_signature(project: dict, summary: dict) -> str:
    """Return a *spin-neutral cross-project* fingerprint when explicitly supplied.

    Generic per-job fingerprints often include ``ISPIN``, ``MAGMOM`` and the
    element inventory.  Those fields can legitimately differ between a
    molecule, clean slab, adsorption structure, SAC and DAC, so hashing them
    together would create a false cross-project hard conflict.  Producers must
    therefore persist a dedicated comparison fingerprint after checking only
    the actual shared energy protocol (functional, dispersion/solvation,
    common-element PAWs, reference basis, etc.).
    """
    for source in (summary, project):
        value = (source.get("comparison_method_fingerprint")
                 if isinstance(source, dict) else None)
        if value:
            return str(value)
    return ""


def _method_evidence(project: dict, summary: dict) -> dict:
    for source in (summary, project):
        value = (source.get("comparison_method_evidence")
                 if isinstance(source, dict) else None)
        if isinstance(value, dict):
            return value
    return {}


def _display_names(items: list[dict]) -> list[str]:
    bases = [str(item.get("name") or "项目") for item in items]
    counts = Counter(bases)
    result = []
    for base, item in zip(bases, items):
        if counts[base] == 1:
            result.append(base)
            continue
        root = str(item.get("root") or item.get("path") or "")
        parent = os.path.basename(os.path.dirname(os.path.normpath(root))) or "项目"
        project_id = str(item.get("project_uuid") or "")
        suffix = project_id[:8] if project_id else parent
        result.append(f"{base} · {suffix}")
    return result


def build_comparison_snapshot(items: list[dict], *, preset_key: str | None = None,
                              deadband_eV: float = 0.15) -> dict:
    """Build a frozen multi-project comparison snapshot.

    Each input item may contain ``path``, ``project``, ``summary``, ``fed`` and
    ``fed_reason``.  Missing/moved projects remain visible as blocked records.
    """
    normalized, seen = [], set()
    for raw in items or []:
        path = os.path.normcase(os.path.normpath(str(raw.get("path") or "")))
        dedupe_key = path or f"missing:{len(normalized)}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        project = raw.get("project") if isinstance(raw.get("project"), dict) else None
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        normalized.append({
            "path": str(raw.get("path") or ""),
            "project": project,
            "summary": summary,
            "fed": raw.get("fed"),
            "fed_reason": str(raw.get("fed_reason") or ""),
            "name": str((project or {}).get("name") or raw.get("name") or "项目"),
            "root": str((project or {}).get("root") or ""),
            "project_uuid": str((project or {}).get("project_uuid") or ""),
        })

    display_names = _display_names(normalized)
    projects, blocking, warnings = [], [], []
    for item, display_name in zip(normalized, display_names):
        project = item["project"]
        if project is None:
            reason = "项目不存在或 project.yaml 已被移动"
            projects.append({
                "path": item["path"], "name": item["name"], "display_name": display_name,
                "status": "blocked", "block_reasons": [reason], "warnings": [],
                "species": [], "ladder": None, "method_status": "unverified",
            })
            blocking.append(f"{display_name}: {reason}")
            continue

        summary = item["summary"]
        reference_missing = (
            summary.get("reference_mode") == "none"
            or summary.get("has_ref") is False
        )
        invalid_reference_rows = [
            str(row.get("name") or "?")
            for row in summary.get("rows") or []
            if _finite(row.get("delta_e")) is not None
            and row.get("reference_valid") is False
        ]
        species = (
            [] if reference_missing or invalid_reference_rows
            else stable_species_rows(summary, deadband_eV=deadband_eV)
        )
        method = summary.get("method_consistency") or {}
        method_status = str(method.get("status") or "unverified")
        reasons = []
        item_warnings = list(method.get("warnings") or [])
        if reference_missing:
            reasons.append(
                "缺少有效吸附物参考态；E(slab+ads)-E(slab) 不能作为吸附能比较")
        elif invalid_reference_rows:
            reasons.append(
                "以下构型的吸附物参考态无效：" + "、".join(invalid_reference_rows))
        if method_status == "incompatible":
            reasons.append("项目内部能量操作数方法不兼容")
            reasons.extend(str(value) for value in (method.get("issues") or []))
        if not species:
            reasons.append("没有可用的物种级吸附能")
        ladder = _fed_view(item["fed"])
        if ladder is None and item["fed_reason"]:
            item_warnings.append("台阶图暂不可用: " + item["fed_reason"])
        signature = _method_signature(project, summary)
        method_evidence = _method_evidence(project, summary)
        projects.append({
            "path": item["path"],
            "project_uuid": item["project_uuid"],
            "name": item["name"],
            "display_name": display_name,
            "status": "ready" if not reasons else "blocked",
            "block_reasons": reasons,
            "warnings": list(dict.fromkeys(item_warnings)),
            "method_status": method_status,
            "method_signature": signature,
            "method_evidence": method_evidence,
            "species": species,
            "ladder": ladder,
        })
        blocking.extend(f"{display_name}: {reason}" for reason in reasons)

    ready = [project for project in projects if project["status"] == "ready"]
    ladder_ready = [project for project in ready if project.get("ladder")]
    signatures = {project["method_signature"] for project in ready
                  if project.get("method_signature")}
    missing_signatures = [project["display_name"] for project in ready
                          if not project.get("method_signature")]
    method_conflict = len(signatures) > 1
    if method_conflict:
        blocking.append("多个项目的显式方法指纹不一致")
    if missing_signatures:
        warnings.append("缺少跨项目方法指纹: " + "、".join(missing_signatures))
    method_evidence_conflict = False
    evidence_projects = [
        project for project in ready if project.get("method_evidence")
    ]
    if evidence_projects:
        missing_evidence = [
            project["display_name"] for project in ready
            if (project.get("method_evidence") or {}).get("status") != "verified"
        ]
        if missing_evidence:
            warnings.append(
                "跨项目实际方法证据未核验：" + "、".join(missing_evidence))
        for left_index, left in enumerate(evidence_projects):
            left_evidence = left.get("method_evidence") or {}
            if left_evidence.get("status") != "verified":
                continue
            for right in evidence_projects[left_index + 1:]:
                right_evidence = right.get("method_evidence") or {}
                if right_evidence.get("status") != "verified":
                    continue
                left_paw = left_evidence.get("potcar_ids") or {}
                right_paw = right_evidence.get("potcar_ids") or {}
                shared = sorted(set(left_paw) & set(right_paw))
                pair = f'{left["display_name"]} / {right["display_name"]}'
                if not shared:
                    warnings.append(f"{pair} 没有共同元素可核验 POTCAR 身份")
                    continue
                mismatched_paw = [
                    element for element in shared
                    if left_paw.get(element) != right_paw.get(element)
                ]
                if mismatched_paw:
                    method_evidence_conflict = True
                    blocking.append(
                        f'{pair} 的共同元素 POTCAR 不一致：'
                        + "、".join(mismatched_paw))
                left_u = left_evidence.get("u_by_element") or {}
                right_u = right_evidence.get("u_by_element") or {}
                missing_u = [
                    element for element in shared
                    if element not in left_u or element not in right_u
                ]
                if missing_u:
                    warnings.append(
                        f'{pair} 的共同元素 DFT+U 证据不完整：'
                        + "、".join(missing_u))
                differing_u = [
                    element for element in shared
                    if element in left_u and element in right_u
                    and left_u[element] != right_u[element]
                ]
                if differing_u:
                    method_evidence_conflict = True
                    blocking.append(
                        f'{pair} 的共同元素 DFT+U 不一致：'
                        + "、".join(differing_u))

    step_sets = {tuple(project["ladder"]["step_labels"]) for project in ladder_ready}
    basis_sets = {
        (project["ladder"]["thermo_corrected"], project["ladder"]["solvated"],
         project["ladder"]["reference"], project["ladder"]["temperature_K"],
         project["ladder"]["reaction_path_id"],
         project["ladder"]["thermo_correction_fingerprint"],
         project["ladder"]["solvation_model"])
        for project in ladder_ready
    }
    if len(step_sets) > 1:
        blocking.append("所选项目的反应步骤或顺序不一致，不能强行叠加台阶图")
    if len(basis_sets) > 1:
        blocking.append("所选项目混用了电子能/热校正/溶剂或参比口径")
    missing_ladders = [
        project["display_name"] for project in ready if not project.get("ladder")
    ]
    if missing_ladders:
        warnings.append("以下项目缺少可用自由能路径: " + "、".join(missing_ladders))
    if len(ladder_ready) < 2:
        warnings.append("至少需要 2 个具有同一路径自由能的项目才能生成叠加台阶图")

    individual_statuses = {project["method_status"] for project in ready}
    if blocking:
        gate_status = "incompatible"
    elif "unverified" in individual_statuses or missing_signatures or warnings:
        gate_status = "unverified"
    else:
        gate_status = "verified"
    can_plot = (
        len(ladder_ready) >= 2
        and len(step_sets) <= 1
        and len(basis_sets) <= 1
        and not method_conflict
        and not method_evidence_conflict
    )

    columns = sorted(
        {row["species"] for project in ready for row in project["species"]},
        key=_species_sort_key)
    matrix = []
    for project in ready:
        values = {row["species"]: row["delta_e"] for row in project["species"]}
        matrix.append([values.get(species) for species in columns])

    ladder_paths = []
    if can_plot:
        for project in ladder_ready:
            ladder = project["ladder"]
            ladder_paths.append({
                "name": project["display_name"],
                "G": list(ladder["G"]),
                "pds_index": ladder["pds_index"],
                "u_l": ladder["u_l"],
            })

    payload = {
        "schema": SCHEMA,
        "preset_key": str(preset_key or ""),
        "ranking_deadband_eV": float(deadband_eV),
        "selected_count": len(projects),
        "ready_count": len(ready),
        "ladder_ready_count": len(ladder_ready),
        "projects": projects,
        "comparison_gate": {
            "status": gate_status,
            "blocking": list(dict.fromkeys(blocking)),
            "warnings": list(dict.fromkeys(warnings)),
        },
        "can_plot": can_plot,
        "can_final_report": (
            len(ready) >= 2
            and gate_status == "verified"
            and can_plot
            and len(ladder_ready) == len(ready)
        ),
        "adsorption_matrix": {
            "rows": [project["display_name"] for project in ready],
            "cols": columns,
            "values": matrix,
        },
        "ladder": {
            "paths": ladder_paths,
            "step_labels": (list(ladder_ready[0]["ladder"]["step_labels"])
                            if can_plot else []),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    payload["data_fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return payload
