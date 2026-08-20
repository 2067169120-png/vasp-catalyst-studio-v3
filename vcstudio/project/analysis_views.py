"""Server-owned Phase D analysis projections.

These helpers consume already parsed project summaries plus a canonical
``AnalysisSpec``.  They never read browser-supplied scientific values and they
never impute missing results.  Raw numbers remain available for audit while a
separate display string applies the requested precision.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from vcstudio.project.analysis_registry import AnalysisSpec
from vcstudio.project import comparison
from vcstudio.project.analysis_scientific import (
    build_aimd_analysis_view,
    build_convergence_analysis_view,
    build_neb_analysis_view,
)


VIEW_SCHEMA = "vcstudio.analysis-view/v1"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _display(value: float | None, precision: int) -> str:
    return "—" if value is None else f"{value:.{precision}f}"


def _safe_public_text(value: Any, *, limit: int = 4000) -> str:
    """Redact local paths and credentials from server-owned display text."""
    text = str(value or "")
    if re.search(
            r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
            r"|\bBearer\s+\S+|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
            r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+)",
            text):
        return "<redacted>"
    remote_urls = []

    def preserve_remote_url(match):
        remote_urls.append(match.group(0))
        return f"<analysis-remote-url-{len(remote_urls) - 1}>"

    text = re.sub(
        r"(?i)\b(?:https?|s3)://[^\s,;，；]+", preserve_remote_url, text)
    text = re.sub(
        r"(?i)\bfile:(?://+|\\+)[^\s,;，；]+", "<local-path>", text)
    text = re.sub(r"(?i)\b[A-Z]:[\\/][^\s,;，；]+", "<local-path>", text)
    text = re.sub(
        r"(?<![:A-Za-z0-9])(?:\\\\|//)[^\\/\s,;，；]+"
        r"[\\/][^\s,;，；]+", "<local-path>", text)
    text = re.sub(
        r"(?<![#/A-Za-z0-9_])/(?!/)[^\s,;，；]+", "<local-path>", text)
    for index, url in enumerate(remote_urls):
        text = text.replace(f"<analysis-remote-url-{index}>", url)
    return text[:max(0, int(limit))]


def _messages(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [_safe_public_text(item) for item in values]


def _method_status(row: Mapping[str, Any], summary: Mapping[str, Any]) -> str:
    check = row.get("method_check")
    if isinstance(check, Mapping) and check.get("status"):
        return str(check.get("status")).lower()
    method = summary.get("method_consistency")
    if isinstance(method, Mapping) and method.get("status"):
        return str(method.get("status")).lower()
    return "unverified"


def _method_record(row: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    check = row.get("method_check")
    check = check if isinstance(check, Mapping) else {}
    return {
        "configuration_id": str(
            row.get("configuration_id") or row.get("job_id")
            or row.get("name") or ""),
        "species": comparison.canonical_species(
            row.get("species") or row.get("reference_species") or row.get("name")),
        "status": _method_status(row, summary),
        "issues": [str(value) for value in check.get("issues") or []],
        "warnings": [str(value) for value in check.get("warnings") or []],
        "advisories": [str(value) for value in check.get("advisories") or []],
    }


def _sort_adsorption_rows(rows: list[dict[str, Any]], spec: AnalysisSpec) -> None:
    direction = -1 if spec.sort_direction == "desc" else 1

    def key(record):
        if spec.sort_key == "energy":
            value = record.get("delta_e_eV")
            return (value is None, direction * (value or 0.0), record.get("name") or "")
        value = {
            "species": record.get("species"),
            "name": record.get("name"),
            "state": record.get("state"),
        }.get(spec.sort_key, record.get("species"))
        text = str(value or "")
        if direction < 0:
            # Stable and deterministic reverse ordering without locale state.
            return tuple(-ord(char) for char in text)
        return (text,)

    rows.sort(key=key)


def build_adsorption_view(summary: Mapping[str, Any], spec: AnalysisSpec) -> dict[str, Any]:
    """Project one adsorption summary into all/stable workbench rows."""
    if spec.analysis_id != "adsorption-energy":
        raise ValueError("adsorption view requires analysis_id=adsorption-energy")
    source_rows = [dict(row or {}) for row in (summary or {}).get("rows") or []]
    groups: dict[str, list[tuple[dict[str, Any], float]]] = {}
    missing = []
    method_matrix = []
    for row in source_rows:
        species = comparison.canonical_species(
            row.get("species") or row.get("reference_species") or row.get("name"))
        value = _finite(row.get("delta_e"))
        method = _method_status(row, summary)
        reference_valid = row.get("reference_valid") is not False
        scientifically_available = (
            value is not None and reference_valid and method != "incompatible")
        method_matrix.append(_method_record(row, summary))
        if scientifically_available:
            groups.setdefault(species, []).append((row, value))
        else:
            missing.append({
                "configuration_id": str(
                    row.get("configuration_id") or row.get("job_id")
                    or row.get("name") or ""),
                "name": str(row.get("name") or ""),
                "species": species,
                "state": str(row.get("state") or "UNKNOWN"),
                "method_status": method,
                "reference_valid": reference_valid,
                "reason": str(row.get("note") or (
                    "方法证据不兼容" if method == "incompatible" else
                    "参考态无效" if not reference_valid else "缺少可用吸附能")),
            })

    all_rows = []
    for species, entries in groups.items():
        ordered = sorted(entries, key=lambda item: (
            item[1], str(item[0].get("name") or ""),
            str(item[0].get("configuration_id") or item[0].get("job_id") or "")))
        best_value = ordered[0][1]
        for index, (row, value) in enumerate(ordered):
            delta = value - best_value
            all_rows.append({
                "configuration_id": str(
                    row.get("configuration_id") or row.get("job_id")
                    or row.get("name") or ""),
                "name": str(row.get("name") or ""),
                "species": species,
                "state": str(row.get("state") or "UNKNOWN"),
                "delta_e_eV": value,
                "delta_e_display": _display(value, spec.precision),
                "relative_to_minimum_eV": delta,
                "relative_to_minimum_display": _display(delta, spec.precision),
                "is_minimum": index == 0,
                "near_degenerate": delta <= spec.near_degenerate_eV + 1e-12,
                "method_status": _method_status(row, summary),
                "note": str(row.get("note") or ""),
            })

    if spec.data_mode == "all":
        rows = list(all_rows)
        if spec.missing_policy == "show_missing":
            rows.extend({
                **record,
                "delta_e_eV": None,
                "delta_e_display": "—",
                "relative_to_minimum_eV": None,
                "relative_to_minimum_display": "—",
                "is_minimum": False,
                "near_degenerate": False,
                "note": record["reason"],
            } for record in missing)
    else:
        rows = []
        for species in sorted(groups):
            candidates = sorted(groups[species], key=lambda item: (
                item[1], str(item[0].get("name") or "")))
            row, value = candidates[0]
            co_minima = []
            for candidate, candidate_value in candidates[1:]:
                delta = candidate_value - value
                if delta <= spec.near_degenerate_eV + 1e-12:
                    co_minima.append({
                        "configuration_id": str(
                            candidate.get("configuration_id")
                            or candidate.get("job_id") or candidate.get("name") or ""),
                        "name": str(candidate.get("name") or ""),
                        "delta_e_eV": candidate_value,
                        "delta_e_display": _display(candidate_value, spec.precision),
                        "relative_to_minimum_eV": delta,
                        "relative_to_minimum_display": _display(delta, spec.precision),
                    })
            rows.append({
                "configuration_id": str(
                    row.get("configuration_id") or row.get("job_id")
                    or row.get("name") or ""),
                "name": str(row.get("name") or ""),
                "species": species,
                "state": str(row.get("state") or "UNKNOWN"),
                "delta_e_eV": value,
                "delta_e_display": _display(value, spec.precision),
                "relative_to_minimum_eV": 0.0,
                "relative_to_minimum_display": _display(0.0, spec.precision),
                "is_minimum": True,
                "near_degenerate": bool(co_minima),
                "co_minima": co_minima,
                "n_configurations": len(candidates),
                "method_status": _method_status(row, summary),
                "note": str(row.get("note") or ""),
            })
    _sort_adsorption_rows(rows, spec)
    aggregate = summary.get("method_consistency")
    aggregate = aggregate if isinstance(aggregate, Mapping) else {}
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": str(aggregate.get("status") or "unverified"),
        "rows": rows,
        "missing": missing,
        "method_matrix": method_matrix,
        "denominator": {
            "input_configurations": len(source_rows),
            "numeric_configurations": len(all_rows),
            "missing_configurations": len(missing),
            "species_with_numeric_results": len(groups),
            "visible_rows": len(rows),
            "near_degenerate_groups": sum(
                1 for row in rows if row.get("near_degenerate")),
        },
        "warnings": [str(value) for value in aggregate.get("warnings") or []],
        "blocking": [str(value) for value in aggregate.get("issues") or []],
    }
    payload["data_fingerprint"] = _canonical_hash({
        "analysis_id": payload["analysis_id"],
        "spec_sha256": payload["spec_sha256"],
        "scientific_status": payload["scientific_status"],
        "rows": payload["rows"],
        "missing": payload["missing"],
        "method_matrix": payload["method_matrix"],
    })
    return payload


def build_free_energy_view(frozen: Mapping[str, Any],
                           spec: AnalysisSpec) -> dict[str, Any]:
    """Project one frozen ``Api._proj_fed`` result without partial ladders.

    ``frozen`` is never a browser payload.  It may be the detached result
    returned by ``Api._proj_fed`` directly, or a server-created envelope whose
    ``result`` member contains that value.  The envelope's ``missing`` member
    carries the exact server-side reason when no result exists, and its
    ``method_consistency`` member is used only as a fallback when a path result
    could not carry its own molecule/adsorbate method audit.
    """
    if spec.analysis_id != "free-energy-path":
        raise ValueError(
            "free-energy view requires analysis_id=free-energy-path")
    if not isinstance(frozen, Mapping):
        raise TypeError("free-energy frozen output must be an object")

    source = copy.deepcopy(dict(frozen))
    enveloped = "result" in source
    raw_result = source.get("result") if enveloped else source
    result = dict(raw_result) if isinstance(raw_result, Mapping) else None
    missing = _messages(source.get("missing")) if enveloped else []
    blocking = _messages(source.get("blocking")) if enveloped else []
    warnings = _messages(source.get("warnings")) if enveloped else []
    if raw_result is None:
        if not missing:
            missing.append("自由能路径结果不可用")
    elif result is None:
        missing.append("自由能路径结果不是对象")

    raw_steps = result.get("steps") if result is not None else None
    input_steps = len(raw_steps) if isinstance(raw_steps, (list, tuple)) else 0
    projected_steps = []
    numeric_steps = 0
    invalid_step_indexes = set()
    if result is not None:
        if not isinstance(raw_steps, (list, tuple)):
            missing.append("steps 缺失或不是数组")
        elif len(raw_steps) < 2:
            missing.append("steps 至少需要两个有序台阶")
        for index, raw_step in enumerate(raw_steps or []):
            if not isinstance(raw_step, Mapping):
                invalid_step_indexes.add(index)
                missing.append(f"steps[{index}]不是对象")
                continue
            label = _safe_public_text(raw_step.get("label"))
            value = _finite(raw_step.get("G"))
            if value is not None:
                numeric_steps += 1
            if not label:
                invalid_step_indexes.add(index)
                missing.append(f"steps[{index}].label 缺失")
            if value is None:
                invalid_step_indexes.add(index)
                missing.append(f"steps[{index}].G 缺少有限数值")
            if label and value is not None:
                projected_steps.append({
                    "step_index": index,
                    "label": label,
                    "sub_label": _safe_public_text(raw_step.get("sub_label")),
                    "G": value,
                    "G_display": _display(value, spec.precision),
                })

    pds_index = None
    u_l = None
    mu_li = None
    thermo_corrected = False
    temperature = None
    thermo_fingerprint = ""
    reference = ""
    reaction_path_id = ""
    if result is not None:
        raw_pds = result.get("pds_index")
        if isinstance(raw_pds, bool) or not isinstance(raw_pds, int):
            missing.append("pds_index 缺失或不是整数")
        elif not 0 <= raw_pds < max(input_steps - 1, 0):
            missing.append("pds_index 超出台阶转换范围")
        else:
            pds_index = raw_pds

        u_l = _finite(result.get("u_l"))
        if u_l is None:
            missing.append("u_l 缺少有限数值")
        mu_li = _finite(result.get("mu_li"))
        if mu_li is None:
            missing.append("mu_li 缺少有限数值")

        raw_thermo = result.get("thermo_corrected")
        if not isinstance(raw_thermo, bool):
            missing.append("thermo_corrected 缺失或不是布尔值")
        else:
            thermo_corrected = raw_thermo
        raw_temperature = result.get("temperature_K")
        if raw_temperature is not None:
            temperature = _finite(raw_temperature)
            if temperature is None:
                missing.append("temperature_K 不是有限数值")
        thermo_fingerprint = _safe_public_text(
            result.get("thermo_correction_fingerprint"))
        if thermo_corrected and temperature is None:
            missing.append("热校正路径缺少有限 temperature_K")
        if thermo_corrected and not thermo_fingerprint:
            missing.append("热校正路径缺少 thermo_correction_fingerprint")
        reference = _safe_public_text(
            result.get("reference") or result.get("electrode"))
        reaction_path_id = _safe_public_text(result.get("reaction_path_id"))
        warnings.extend(_messages(result.get("warnings")))

    path_method = result.get("method_consistency") if result is not None else None
    method_source = (path_method if isinstance(path_method, Mapping) else
                     source.get("method_consistency"))
    method_source = (dict(method_source)
                     if isinstance(method_source, Mapping) else {})
    declared_method_status = str(method_source.get("status") or "").strip().lower()
    method_status = (declared_method_status
                     if declared_method_status in {
                         "verified", "unverified", "incompatible"}
                     else "unverified")
    method_errors = [
        *_messages(method_source.get("errors")),
        *_messages(method_source.get("issues")),
    ]
    method_warnings = _messages(method_source.get("warnings"))
    method = {
        "status": method_status,
        "verified": method_status == "verified" and not method_errors,
        "managed": method_source.get("managed") is True,
        "errors": method_errors,
        "warnings": method_warnings,
    }
    if method_status == "incompatible" or method_errors:
        blocking.extend(method_errors or ["自由能路径方法证据不兼容"])

    # Any missing required scalar invalidates the whole ladder.  Valid-looking
    # neighbours remain counted in the denominator but are not exposed as a
    # partial scientific path.
    blocking.extend(missing)
    available = bool(result is not None and not blocking)
    steps = projected_steps if available else []
    pds = ({
        "index": pds_index,
        "from_label": steps[pds_index]["label"],
        "to_label": steps[pds_index + 1]["label"],
    } if available and pds_index is not None else {
        "index": None, "from_label": "", "to_label": "",
    })
    denominator = {
        "requested_paths": 1,
        "available_paths": 1 if available else 0,
        "input_steps": input_steps,
        "numeric_steps": numeric_steps,
        "missing_steps": len(invalid_step_indexes),
        "input_transitions": max(input_steps - 1, 0),
        "valid_transitions": max(len(steps) - 1, 0),
        "visible_rows": len(steps),
    }
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": "blocked" if blocking else method_status,
        "available": available,
        "steps": steps,
        "rows": copy.deepcopy(steps),
        "pds_index": pds_index if available else None,
        "pds": pds,
        "u_l": u_l if available else None,
        "u_l_display": _display(u_l if available else None, spec.precision),
        "mu_li": mu_li if available else None,
        "mu_li_display": _display(mu_li if available else None, spec.precision),
        "thermo": {
            "corrected": thermo_corrected if available else False,
            "status": (
                "corrected" if available and thermo_corrected else
                "electronic_energy_only" if available else "unavailable"),
            "temperature_K": temperature if available else None,
            "temperature_display": _display(
                temperature if available else None, spec.precision),
            "correction_fingerprint": thermo_fingerprint if available else "",
        },
        "method_status": method_status,
        "method": method,
        "reference": reference if available else "",
        "reaction_path_id": reaction_path_id if available else "",
        "missing": missing,
        "blocking": blocking,
        "warnings": warnings,
        "reason": blocking[0] if blocking else "",
        "denominator": denominator,
    }
    payload["data_fingerprint"] = _canonical_hash({
        "analysis_id": payload["analysis_id"],
        "spec_sha256": payload["spec_sha256"],
        "scientific_status": payload["scientific_status"],
        "available": payload["available"],
        "steps": payload["steps"],
        "pds": payload["pds"],
        "u_l": payload["u_l"],
        "mu_li": payload["mu_li"],
        "thermo": payload["thermo"],
        "method": payload["method"],
        "reference": payload["reference"],
        "reaction_path_id": payload["reaction_path_id"],
        "missing": payload["missing"],
        "blocking": payload["blocking"],
        "warnings": payload["warnings"],
        "denominator": payload["denominator"],
    })
    return payload


def _method_projection(project: Mapping[str, Any]) -> dict[str, Any]:
    evidence = project.get("method_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    protocol = evidence.get("protocol")
    protocol = protocol if isinstance(protocol, Mapping) else {}
    potcars = evidence.get("potcar_ids")
    potcars = potcars if isinstance(potcars, Mapping) else {}
    u_values = evidence.get("u_by_element")
    u_values = u_values if isinstance(u_values, Mapping) else {}
    return {
        "project_id": str(project.get("project_id") or ""),
        "name": str(project.get("display_name") or project.get("name") or ""),
        "status": str(evidence.get("status") or project.get("method_status")
                      or "unverified"),
        "fingerprint": str(evidence.get("fingerprint") or ""),
        "functional": protocol.get("functional"),
        "dispersion": protocol.get("dispersion"),
        "encut_eV": _finite(protocol.get("encut_eV")),
        "kpoints_scheme": protocol.get("kpoints_scheme"),
        "reference_mode": protocol.get("reference_mode"),
        "energy_quantity": protocol.get("energy_quantity"),
        "potcar_ids": {str(key): str(value) for key, value in potcars.items()},
        "u_by_element": copy.deepcopy(dict(u_values)),
        "missing": [str(value) for value in evidence.get("missing") or []],
    }


def _sensitivity(matrix: Mapping[str, Any], deadbands: Sequence[float]) -> dict[str, Any]:
    project_ids = list(matrix.get("project_ids") or [])
    species = list(matrix.get("cols") or [])
    values = list(matrix.get("values") or [])
    points = []
    membership: dict[str, dict[str, int]] = {
        item: {project_id: 0 for project_id in project_ids} for item in species}
    for deadband in deadbands:
        tied_sets = []
        for column, item in enumerate(species):
            observed = [
                (project_ids[index], _finite(row[column]))
                for index, row in enumerate(values)
                if index < len(project_ids) and column < len(row)
            ]
            observed = [(project_id, value) for project_id, value in observed
                        if value is not None]
            if not observed:
                tied = []
                minimum = None
            else:
                minimum = min(value for _project_id, value in observed)
                tied = [project_id for project_id, value in observed
                        if value - minimum <= deadband + 1e-12]
                for project_id in tied:
                    membership[item][project_id] += 1
            tied_sets.append({
                "species": item,
                "lowest_energy_eV": minimum,
                "within_deadband_project_ids": tied,
                "observed_denominator": len(observed),
            })
        points.append({"deadband_eV": deadband, "lowest_energy_sets": tied_sets})
    denominator = len(deadbands)
    robustness = [
        {
            "species": item,
            "project_id": project_id,
            "membership_count": count,
            "tested_deadbands": denominator,
            "membership_fraction": count / denominator if denominator else None,
        }
        for item in species
        for project_id, count in membership[item].items()
        if count
    ]
    return {
        "interpretation": (
            "The sets identify projects within each electronic-energy deadband; "
            "they are not an activity ranking or kinetic conclusion."),
        "points": points,
        "membership": robustness,
    }


def build_comparison_view(items: Sequence[Mapping[str, Any]],
                          spec: AnalysisSpec) -> dict[str, Any]:
    """Build a path-free comparison projection with baseline and sensitivity."""
    if spec.analysis_id != "multi-project-comparison":
        raise ValueError(
            "comparison view requires analysis_id=multi-project-comparison")
    by_id = {}
    ordered_items = []
    for index, raw in enumerate(items or []):
        project_id = str(raw.get("project_id") or "")
        if not project_id or project_id in by_id:
            continue
        item = copy.deepcopy(dict(raw))
        # comparison.py needs a stable internal dedupe locator; an opaque ID is
        # sufficient and no local path survives this module's public projection.
        item["path"] = f"opaque:{project_id}"
        item["project_id"] = project_id
        by_id[project_id] = item
        ordered_items.append(item)
    selected = [by_id[project_id] for project_id in spec.comparison_project_ids
                if project_id in by_id]
    missing_projects = [project_id for project_id in spec.comparison_project_ids
                        if project_id not in by_id]
    if missing_projects:
        raise ValueError(
            "comparison project identity is unavailable: " + ", ".join(missing_projects))
    if len(selected) < 2:
        raise ValueError("comparison view requires at least two server-resolved projects")
    snapshot = comparison.build_comparison_snapshot(
        selected, deadband_eV=spec.near_degenerate_eV)
    projects = []
    for raw, source in zip(snapshot.get("projects") or [], selected):
        record = copy.deepcopy(dict(raw))
        record.pop("path", None)
        record["project_id"] = source["project_id"]
        record.pop("method_evidence", None)
        projects.append(record)
    ready = [record for record in projects if record.get("status") == "ready"]
    matrix = copy.deepcopy(snapshot.get("adsorption_matrix") or {})
    matrix["project_ids"] = [record["project_id"] for record in ready]
    cols = list(matrix.get("cols") or [])
    values = list(matrix.get("values") or [])
    if spec.missing_policy == "complete_cases":
        keep = [index for index, _species in enumerate(cols)
                if all(index < len(row) and _finite(row[index]) is not None
                       for row in values)]
        cols = [cols[index] for index in keep]
        values = [[row[index] for index in keep] for row in values]
    matrix["cols"] = cols
    matrix["values"] = values
    matrix["display_values"] = [
        [_display(_finite(value), spec.precision) for value in row]
        for row in values
    ]
    baseline = None
    if spec.baseline_project_id:
        try:
            baseline_index = matrix["project_ids"].index(spec.baseline_project_id)
        except ValueError as exc:
            raise ValueError("baseline project is not ready for comparison") from exc
        baseline_values = values[baseline_index]
        baseline = {
            "project_id": spec.baseline_project_id,
            "deltas": [
                {
                    "project_id": project_id,
                    "values_eV": [
                        (None if _finite(value) is None or _finite(base) is None
                         else _finite(value) - _finite(base))
                        for value, base in zip(row, baseline_values)
                    ],
                }
                for project_id, row in zip(matrix["project_ids"], values)
            ],
        }
        for record in baseline["deltas"]:
            record["display_values"] = [
                _display(_finite(value), spec.precision)
                for value in record["values_eV"]
            ]
    sensitivity = _sensitivity(matrix, spec.sensitivity_deadbands_eV)
    source_method = {
        source["project_id"]: source for source in selected
    }
    method_matrix = []
    for project in projects:
        source = source_method[project["project_id"]]
        enriched = dict(project)
        enriched["method_evidence"] = (
            (source.get("summary") or {}).get("comparison_method_evidence")
            or (source.get("project") or {}).get("comparison_method_evidence")
            or {})
        method_matrix.append(_method_projection(enriched))
    total_cells = len(matrix["project_ids"]) * len(cols)
    present_cells = sum(
        1 for row in values for value in row if _finite(value) is not None)
    gate = copy.deepcopy(snapshot.get("comparison_gate") or {})
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": str(gate.get("status") or "unverified"),
        "can_final_report": bool(snapshot.get("can_final_report")),
        "projects": projects,
        "comparison_gate": gate,
        "matrix": matrix,
        "baseline": baseline,
        "method_matrix": method_matrix,
        "sensitivity": sensitivity,
        "ladder": copy.deepcopy(snapshot.get("ladder") or {}),
        "denominator": {
            "selected_projects": len(projects),
            "ready_projects": len(ready),
            "blocked_projects": len(projects) - len(ready),
            "species_columns": len(cols),
            "possible_numeric_cells": total_cells,
            "observed_numeric_cells": present_cells,
            "missing_numeric_cells": total_cells - present_cells,
        },
    }
    payload["data_fingerprint"] = _canonical_hash({
        "spec_sha256": payload["spec_sha256"],
        "scientific_status": payload["scientific_status"],
        "projects": payload["projects"],
        "comparison_gate": payload["comparison_gate"],
        "matrix": payload["matrix"],
        "baseline": payload["baseline"],
        "method_matrix": payload["method_matrix"],
        "sensitivity": payload["sensitivity"],
        "ladder": payload["ladder"],
    })
    return payload


def build_kinetic_view(
        network: Mapping[str, Any], audit: Mapping[str, Any],
        normalized_result: Mapping[str, Any] | None,
        tool: Mapping[str, Any], spec: AnalysisSpec, *,
        configured_tool: Mapping[str, Any] | None = None,
        tool_identity_matches: bool | None = None) -> dict[str, Any]:
    """Project one server-validated microkinetic result for display only."""
    if spec.analysis_id != "kinetic-dashboard":
        raise ValueError(
            "kinetic dashboard requires analysis_id=kinetic-dashboard")
    if not isinstance(network, Mapping) or not isinstance(audit, Mapping):
        raise TypeError("kinetic dashboard requires frozen network and audit objects")
    if normalized_result is not None and not isinstance(normalized_result, Mapping):
        raise TypeError("normalized kinetic result must be an object or None")
    precision = int(spec.precision)

    def display(value: Any) -> str:
        number = _finite(value)
        return "—" if number is None else f"{number:.{precision}f}"

    def metric(records: Any, identifier: str, *, extras: Sequence[str] = ()) -> list[dict]:
        projected = []
        for raw in records if isinstance(records, Sequence) else []:
            if not isinstance(raw, Mapping):
                continue
            record = {
                identifier: _safe_public_text(raw.get(identifier)),
                "value": _finite(raw.get("value")),
                "display": display(raw.get("value")),
            }
            for key in extras:
                record[key] = _safe_public_text(raw.get(key))
            projected.append(record)
        return projected

    points = []
    result_points = ((normalized_result or {}).get("points") or [])
    for index, raw in enumerate(result_points):
        if not isinstance(raw, Mapping):
            continue
        conditions = raw.get("conditions") or {}
        temperature = _finite(conditions.get("temperature"))
        pressure = _finite(conditions.get("pressure"))
        potential = _finite(conditions.get("potential"))
        condition_parts = [
            f"T={display(temperature)} K", f"p={display(pressure)} bar",
        ]
        if potential is not None:
            condition_parts.append(f"U={display(potential)} V")
        convergence = raw.get("convergence") or {}
        converged = convergence.get("converged") is True
        residual = _finite(convergence.get("residual"))
        points.append({
            "point_index": index,
            "condition_display": " · ".join(condition_parts),
            "conditions": {
                "temperature": temperature,
                "temperature_display": display(temperature),
                "pressure": pressure,
                "pressure_display": display(pressure),
                "potential": potential,
                "potential_display": display(potential),
            },
            "tof": metric(raw.get("tof"), "species_id"),
            "coverage": metric(
                raw.get("coverage"), "species_id", extras=("site_type",)),
            "selectivity": metric(raw.get("selectivity"), "species_id"),
            "drc": metric(
                raw.get("drc"), "step_id", extras=("target_species_id",)),
            "dsc": metric(
                raw.get("dsc"), "step_id", extras=("target_species_id",)),
            "reaction_order": metric(
                raw.get("reaction_order"), "species_id",
                extras=("target_species_id",)),
            "apparent_activation_energy": metric(
                raw.get("apparent_activation_energy"), "species_id"),
            "free_energy_diagram": metric(
                raw.get("free_energy_diagram"), "state_id"),
            "completeness": copy.deepcopy(raw.get("completeness") or {}),
            "convergence": {
                "status": "converged" if converged else "unconverged",
                "converged": converged,
                "residual": residual,
                "residual_display": (
                    "—" if residual is None else f"{residual:.{precision}e}"),
                "iterations": (
                    int(convergence["iterations"])
                    if isinstance(convergence.get("iterations"), int)
                    and not isinstance(convergence.get("iterations"), bool) else None),
                "solver": _safe_public_text(convergence.get("solver")),
            },
        })

    sensitivity = (normalized_result or {}).get("sensitivity") or {}
    sensitivity_analyses = []
    for raw in sensitivity.get("analyses") or []:
        if not isinstance(raw, Mapping):
            continue
        value = _finite(raw.get("max_relative_change"))
        sensitivity_analyses.append({
            "kind": _safe_public_text(raw.get("kind")),
            "max_relative_change": value,
            "max_relative_change_display": display(value),
        })

    issues = audit.get("issues") or []
    blocking = [
        _safe_public_text(item.get("message")) for item in issues
        if isinstance(item, Mapping) and item.get("severity") == "error"
    ]
    warnings = [
        _safe_public_text(item.get("message")) for item in issues
        if isinstance(item, Mapping) and item.get("severity") == "warning"
    ]
    warnings.extend(
        _safe_public_text(item) for item in sensitivity.get("warnings") or [])
    configured = configured_tool if isinstance(configured_tool, Mapping) else tool
    tool_available = (
        configured.get("available") is True and tool_identity_matches is not False)
    result_available = normalized_result is not None
    result_ready = bool(result_available and normalized_result.get("available") is True)
    reason_codes = list((normalized_result or {}).get("reason_codes") or [])
    if not tool_available:
        reason_codes.append("CATMAP_UNAVAILABLE")
    if not result_available:
        reason_codes.append("RESULT_NOT_IMPORTED")
    reason_codes = list(dict.fromkeys(
        _safe_public_text(value) for value in reason_codes if value))
    if reason_codes:
        blocking.extend(reason_codes)
    assumptions = network.get("assumptions") or {}
    methodology = network.get("methodology") or {}
    limitations = {
        "mean_field": assumptions.get("mean_field") is True,
        "steady_state": assumptions.get("steady_state") is True,
        "uniform_sites": assumptions.get("site_uniformity") == "uniform",
        "lateral_interactions": _safe_public_text(
            assumptions.get("lateral_interactions")),
        "mechanism_completeness": _safe_public_text(
            assumptions.get("mechanism_completeness")),
        "mechanism_completeness_is_asserted_not_proven": True,
        "browser_solves": False,
        "diagnostic_only": True,
        "may_enter_accepted_or_final": False,
    }
    denominator = {
        "condition_points": len(points),
        "converged_points": sum(
            1 for point in points if point["convergence"]["converged"]),
        "elementary_steps": int(
            (audit.get("denominator") or {}).get("elementary_steps") or 0),
        "species": int((audit.get("denominator") or {}).get("species") or 0),
        "visible_rows": len(points),
    }
    fingerprint_payload = {
        "input_sha256": audit.get("input_sha256"),
        "spec_sha256": spec.semantic_sha256,
        "tool_sha256": tool.get("sha256"),
        "configured_tool_sha256": configured.get("sha256"),
        "tool_identity_matches": tool_identity_matches,
        "result": normalized_result,
    }
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": "diagnostic" if result_available else "unavailable",
        # The audit/export workbench remains open even when the external solver
        # or numerical result is unavailable.
        "capability_status": "available",
        "available": bool(tool_available and result_ready),
        "solver_status": "available" if tool_available else "unavailable",
        "input_audit_status": (
            "machine_pass" if audit.get("machine_pass") is True else "failed"),
        "result_status": (
            "available" if result_ready else
            "diagnostic_unavailable" if result_available else "not_imported"),
        "reason_codes": reason_codes,
        "units": {
            str(key): _safe_public_text(value)
            for key, value in ((normalized_result or {}).get("units") or {}).items()
        },
        "points": points,
        "rows": points,
        "audit": {
            "schema": _safe_public_text(audit.get("schema")),
            "input_sha256": _safe_public_text(audit.get("input_sha256")),
            "source_projection_sha256": _safe_public_text(
                audit.get("source_projection_sha256")),
            "machine_pass": audit.get("machine_pass") is True,
            "export_ready": audit.get("export_ready") is True,
            "error_count": int(audit.get("error_count") or 0),
            "warning_count": int(audit.get("warning_count") or 0),
            "issues": [
                {
                    "severity": _safe_public_text(item.get("severity")),
                    "code": _safe_public_text(item.get("code")),
                    "field": _safe_public_text(item.get("path")),
                    "message": _safe_public_text(item.get("message")),
                }
                for item in issues if isinstance(item, Mapping)
            ],
        },
        "adapter": {
            "available": tool.get("available") is True,
            "name": _safe_public_text(tool.get("name")),
            "version": _safe_public_text(tool.get("version")),
            "sha256": _safe_public_text(tool.get("sha256")),
            "size": (int(tool["size"])
                     if isinstance(tool.get("size"), int) else None),
            "bundled": False,
            "auto_install": False,
            "executes_in_app": False,
        },
        "configured_adapter": {
            "available": configured.get("available") is True,
            "name": _safe_public_text(configured.get("name")),
            "version": _safe_public_text(configured.get("version")),
            "sha256": _safe_public_text(configured.get("sha256")),
            "size": (int(configured["size"])
                     if isinstance(configured.get("size"), int) else None),
            "matches_confirmed_export": tool_identity_matches,
        },
        "method_matrix": [{
            "name": _safe_public_text(methodology.get("method_id")),
            "status": _safe_public_text(methodology.get("compatibility_status")),
            "energy_basis": _safe_public_text(methodology.get("energy_basis")),
            "thermochemistry": _safe_public_text(methodology.get("thermochemistry")),
            "solvation": _safe_public_text(methodology.get("solvation")),
            "potential_model": _safe_public_text(methodology.get("potential_model")),
        }],
        "kinetic_sensitivity": {
            "status": _safe_public_text(sensitivity.get("status")) or "unavailable",
            "analyses": sensitivity_analyses,
            "warnings": [
                _safe_public_text(item) for item in sensitivity.get("warnings") or []],
        },
        "blocking": list(dict.fromkeys(value for value in blocking if value)),
        "warnings": list(dict.fromkeys(value for value in warnings if value)),
        "limitations": limitations,
        "report_limitation": {
            "result_kind": "diagnostic",
            "may_enter_accepted_or_final": False,
            "human_review_not_replaced": True,
        },
        "denominator": denominator,
        "data_fingerprint": _canonical_hash(fingerprint_payload),
    }
    return payload


__all__ = [
    "VIEW_SCHEMA", "build_adsorption_view", "build_aimd_analysis_view",
    "build_comparison_view", "build_convergence_analysis_view",
    "build_free_energy_view", "build_kinetic_view", "build_neb_analysis_view",
]
