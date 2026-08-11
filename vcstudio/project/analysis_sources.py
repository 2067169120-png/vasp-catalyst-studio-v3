"""Server-owned source resolution and stable Analysis Workbench projections.

This module is deliberately ignorant of browser selections.  A source can enter
an analysis view only when it is a project member or a ledger job whose manifest
forms a descendant chain from a member.  Public projections contain opaque job
identities and content hashes, never local filesystem locators.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from vcstudio.project.analysis_registry import AnalysisSpec


VIEW_SCHEMA = "vcstudio.analysis-view/v1"
_PATH_RE = re.compile(r"(?i)(?:[A-Z]:[\\/]|(?:^|\s)/(?:[^/\s]+/)+|file:/{1,3}|\\\\)")
_SECRET_RE = re.compile(
    r"(?i)(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{8,}"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"
)
_FORBIDDEN_KEYS = frozenset({
    "path", "dir", "directory", "job_dir", "project_path", "root",
    "source_job", "peer_dir", "vacuum_dir", "solvent_dir", "report_file",
    "figure", "files", "password", "passwd", "secret", "token", "api_key",
})


def _path_key(value: Any) -> str:
    try:
        return os.path.normcase(os.path.realpath(os.fspath(value)))
    except (TypeError, ValueError, OSError):
        return ""


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "").replace("\x00", "").strip()[:limit]
    if _PATH_RE.search(text) or _SECRET_RE.search(text):
        return "<redacted>"
    return text


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _public_value(value: Any) -> Any:
    """Project parser output without locator/credential-shaped fields."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _public_value(item)
            for key, item in value.items()
            if str(key).lower() not in _FORBIDDEN_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    return _safe_text(value)


def _member_paths(project: Mapping[str, Any]) -> list[str]:
    members = project.get("members") or {}
    if not isinstance(members, Mapping):
        return []
    values: list[Any] = [members.get("clean_slab"), members.get("gas_ref")]
    values.extend(members.get("configs") or [])
    for refs in (members.get("molecules"), project.get("species_ref_jobs")):
        if isinstance(refs, Mapping):
            values.extend(refs.values())
        elif isinstance(refs, (list, tuple)):
            values.extend(refs)
    result: list[str] = []
    for value in values:
        key = _path_key(value)
        if value and key and key not in {_path_key(item) for item in result}:
            result.append(str(value))
    return result


def _manifest_parent(manifest: Mapping[str, Any]) -> str | None:
    inputs = manifest.get("inputs") or {}
    candidates = [manifest.get("parent_job")]
    if isinstance(inputs, Mapping):
        candidates.extend((inputs.get("parent_job"), inputs.get("source_job")))
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def resolve_project_targets(
    project: Mapping[str, Any],
    ledger_entries: Iterable[tuple[Any, Any]],
    *,
    manifest_loader: Callable[[str], Mapping[str, Any] | None],
    opaque_id: Callable[[str, Mapping[str, Any]], str],
) -> list[dict[str, Any]]:
    """Resolve project members and manifest-linked descendants, fail closed.

    Merely living below a project directory is not authority.  Descendants must
    be present in the server ledger and bind to a previously accepted source via
    ``parent_job`` (top-level or in ``inputs``).
    """
    members = _member_paths(project)
    member_keys = {_path_key(path) for path in members}
    ledger: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for raw_path, raw_manifest in ledger_entries:
        key = _path_key(raw_path)
        if not key or not isinstance(raw_manifest, Mapping):
            continue
        ledger[key] = (str(raw_path), dict(raw_manifest))

    accepted: dict[str, tuple[str, Mapping[str, Any], str]] = {}
    for path in members:
        key = _path_key(path)
        manifest = ledger.get(key, (path, None))[1]
        if not isinstance(manifest, Mapping):
            manifest = manifest_loader(path)
        if isinstance(manifest, Mapping):
            accepted[key] = (str(path), dict(manifest), "member")

    changed = True
    while changed:
        changed = False
        for key, (path, manifest) in ledger.items():
            if key in accepted:
                continue
            parent = _path_key(_manifest_parent(manifest))
            if parent and parent in accepted:
                accepted[key] = (path, manifest, "descendant")
                changed = True

    targets = []
    for key, (path, manifest, relation) in sorted(
            accepted.items(), key=lambda item: (str(item[1][1].get("task_type") or ""),
                                                str(item[1][1].get("job_uuid") or
                                                    item[1][1].get("job_id") or item[0]))):
        identifier = str(opaque_id(path, manifest) or "").strip()
        if not identifier:
            continue
        targets.append({
            "path": path,
            "path_key": key,
            "source_id": identifier,
            "relation": relation,
            "task_type": str(manifest.get("task_type") or "").strip().lower(),
            "state": str(manifest.get("state") or "UNKNOWN").strip().upper(),
            "manifest": dict(manifest),
            "member": key in member_keys,
        })
    return targets


def source_identity(
    target: Mapping[str, Any],
    evidence_names: Sequence[Any],
) -> tuple[dict[str, Any], list[str]]:
    root = Path(str(target["path"]))
    missing: list[str] = []
    files = []
    names: list[str] = ["job.yaml"]
    for evidence in evidence_names:
        if isinstance(evidence, (list, tuple)):
            chosen = next((str(name) for name in evidence
                           if (root / str(name)).is_file()), None)
            if chosen is None:
                missing.append(" or ".join(str(name) for name in evidence))
            elif chosen not in names:
                names.append(chosen)
        else:
            name = str(evidence)
            if name not in names:
                names.append(name)
    for name in names:
        candidate = root / name
        if not candidate.is_file():
            missing.append(name)
            continue
        try:
            files.append({
                "name": name,
                "sha256": _file_sha256(candidate),
                "size_bytes": candidate.stat().st_size,
            })
        except OSError:
            missing.append(name)
    return {
        "source_id": str(target["source_id"]),
        "relation": str(target.get("relation") or ""),
        "task_type": str(target.get("task_type") or ""),
        "manifest_state": str(target.get("state") or "UNKNOWN"),
        "manifest_sha256": next(
            (item["sha256"] for item in files if item["name"] == "job.yaml"), None),
        "files": files,
    }, missing


_ANALYSIS_TASKS = {
    "electronic-structure": ("dos_pdos", "bands", "workfunction"),
    "charge-wavefunction": ("bader", "chgdiff", "elf"),
}
_EVIDENCE = {
    "dos_pdos": ("vasprun.xml",),
    "bands": (("EIGENVAL", "vasprun.xml"),),
    "workfunction": ("LOCPOT", "OUTCAR"),
    "bader": ("ACF.dat",),
    "chgdiff": ("CHGDIFF.vasp",),
    "elf": ("ELFCAR",),
}


def _display(value: Any, precision: int) -> str:
    number = _finite(value)
    if number is not None:
        return f"{number:.{precision}f}"
    if value is None:
        return "—"
    return _safe_text(value)


def _value_rows(kind: str, result: Mapping[str, Any], precision: int) -> list[dict[str, Any]]:
    source = result.get("result") or {}
    if not isinstance(source, Mapping):
        source = {}
    fields = {
        "dos_pdos": (
            ("efermi_ev", "E_F", "eV"), ("n_energy_points", "Energy points", ""),
            ("n_ions", "Ions", ""), ("spin_polarized", "Spin polarized", ""),
        ),
        "bands": (),
        "workfunction": (
            ("phi", "Work function", "eV"),
            ("vacuum_level", "Vacuum level", "eV"),
        ),
        "bader": (("n_atoms", "Atoms", ""), ("sum_delta_q", "ΣΔq", "e")),
        "chgdiff": (
            ("n_points", "Profile points", ""),
            ("rho_min_e_a3", "ρ minimum", "e/Å³"),
            ("rho_max_e_a3", "ρ maximum", "e/Å³"),
        ),
        "elf": (
            ("grid_shape", "Grid shape", ""),
            ("natoms", "Atoms", ""),
            ("minimum", "ELF minimum", ""),
            ("maximum", "ELF maximum", ""),
            ("mean", "ELF mean", ""),
            ("std", "ELF standard deviation", ""),
        ),
    }.get(kind, ())
    if kind == "bands":
        gap = source.get("gap") or {}
        if isinstance(gap, Mapping):
            fields = (("value", "Band gap", "eV"), ("direct", "Direct gap", ""),
                      ("metal", "Metal", ""))
            source = gap
    rows = []
    for key, label, unit in fields:
        value = _public_value(source.get(key))
        rows.append({
            "key": key, "label": label, "value": value,
            "display": _display(value, precision), "unit": unit,
        })
    return rows


def _parser_record(identity: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(identity or {})
    return {
        "module": _safe_text(source.get("module")),
        "callable": _safe_text(source.get("callable")),
        "version": _safe_text(source.get("version")),
        "version_source": _safe_text(source.get("version_source")),
    }


def _result_projection(kind: str, result: Any, precision: int) -> Any:
    public = _public_value(result)
    if kind != "elf" or not isinstance(public, dict):
        return public
    public["display_quantiles"] = [{
        "fraction": _display(item.get("fraction"), precision),
        "value": _display(item.get("value"), precision),
    } for item in public.get("quantiles") or [] if isinstance(item, Mapping)]
    public["display_histogram"] = [{
        "low": _display(item.get("low"), precision),
        "high": _display(item.get("high"), precision),
        "count": _display(item.get("count"), 0),
    } for item in public.get("histogram") or [] if isinstance(item, Mapping)]
    return public


def build_task_analysis_view(
    spec: AnalysisSpec,
    targets: Sequence[Mapping[str, Any]],
    *,
    runner: Callable[[str, str], Mapping[str, Any]],
    parser_identities: Mapping[str, Mapping[str, Any]],
    method_evidence: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if spec.analysis_id not in {*_ANALYSIS_TASKS, "task-results"}:
        raise ValueError("task analysis view requires a matching analysis kind")
    allowed = set(_ANALYSIS_TASKS.get(spec.analysis_id, ()))
    selected = [item for item in targets if not allowed or item.get("task_type") in allowed]
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    blocking: list[str] = []
    warnings: list[str] = []
    evidence_files = 0
    parser_ready = 0
    not_implemented = 0

    for target in selected:
        kind = str(target.get("task_type") or "")
        identity = _parser_record(parser_identities.get(kind))
        source, source_missing = source_identity(target, _EVIDENCE.get(kind, ()))
        evidence_files += len(source["files"])
        prefix = f'{kind or "unknown"} [{target.get("source_id")}]'
        if target.get("state") != "DONE":
            message = f"{prefix}: manifest state is not DONE"
            missing.append(message)
            rows.append({"source": source, "parser": identity, "available": False,
                         "status": "missing_prerequisite", "summary": message, "values": []})
            continue
        if not identity["module"] or not identity["callable"] or not identity["version"]:
            message = f"{prefix}: parser identity/version evidence is unavailable"
            blocking.append(message)
            not_implemented += 1
            rows.append({"source": source, "parser": identity, "available": False,
                         "status": "not_implemented", "summary": message, "values": []})
            continue
        parser_ready += 1
        if source_missing:
            message = f"{prefix}: missing evidence files: {', '.join(source_missing)}"
            missing.append(message)
            rows.append({"source": source, "parser": identity, "available": False,
                         "status": "missing_prerequisite", "summary": message, "values": []})
            continue
        method = dict(method_evidence(target) or {})
        if method.get("status") != "verified":
            details = list(method.get("missing") or method.get("issues") or
                           method.get("warnings") or ["method evidence is incomplete"])
            message = f"{prefix}: " + "; ".join(_safe_text(item) for item in details)
            blocking.append(message)
            rows.append({"source": source, "parser": identity, "method": _public_value(method),
                         "available": False, "status": "unavailable", "summary": message,
                         "values": []})
            continue
        try:
            parsed = dict(runner(str(target["path"]), kind) or {})
        except Exception as exc:  # parser failures are data, never invented values
            parsed = {"ok": False, "error": str(exc), "result": None}
        if parsed.get("ok") is not True:
            message = f"{prefix}: {_safe_text(parsed.get('error') or 'parser returned no result')}"
            blocking.append(message)
            rows.append({"source": source, "parser": identity, "method": _public_value(method),
                         "available": False, "status": "unavailable", "summary": message,
                         "values": []})
            continue
        parsed_warnings = list((parsed.get("result") or {}).get("warnings") or []) \
            if isinstance(parsed.get("result"), Mapping) else []
        warnings.extend(_safe_text(item) for item in parsed_warnings)
        rows.append({
            "source": source, "parser": identity, "method": _public_value(method),
            "available": True, "status": "available",
            "summary": _safe_text(parsed.get("summary")),
            "values": _value_rows(kind, parsed, spec.precision),
            "result": _result_projection(kind, parsed.get("result"), spec.precision),
        })

    if not selected:
        missing.append("No project member or manifest-linked descendant matches this analysis")
    available_count = sum(row["available"] is True for row in rows)
    if available_count:
        scientific_status = "verified" if not warnings else "unverified"
        capability_status = "available"
    elif not_implemented and not_implemented == len(rows):
        scientific_status = "unavailable"
        capability_status = "not_implemented"
    else:
        scientific_status = "blocked"
        capability_status = "missing_prerequisite" if missing and not blocking else "unavailable"
    next_action = (
        "Inspect the server-resolved results and evidence hashes."
        if available_count else
        "Complete the listed prerequisite on a registered project job, then refresh."
    )
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": scientific_status,
        "capability_status": capability_status,
        "available": bool(available_count),
        "rows": rows,
        "missing": list(dict.fromkeys(missing)),
        "blocking": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
        "next_action": next_action,
        "denominator": {
            "resolved_targets": len(targets),
            "matching_targets": len(selected),
            "parser_ready_targets": parser_ready,
            "available_results": available_count,
            "blocked_results": len(rows) - available_count,
            "evidence_files": evidence_files,
            "visible_rows": len(rows),
        },
    }
    payload["data_fingerprint"] = _canonical_hash(payload)
    return payload


def build_property_view(
    spec: AnalysisSpec,
    targets: Sequence[Mapping[str, Any]],
    *,
    runner: Callable[[str, Sequence[Mapping[str, Any]]], Sequence[Mapping[str, Any]]],
    parser_version: str,
) -> dict[str, Any]:
    if spec.analysis_id != "property-calculators":
        raise ValueError("property view requires property-calculators")
    target_by_id = {str(target.get("source_id")): target for target in targets}
    rows = []
    missing: list[str] = []
    blocking: list[str] = []
    for kind in ("surface_energy", "formation_binding", "vaspsol"):
        try:
            raw = list(runner(kind, targets) or [])
        except Exception as exc:
            raw = [{"ok": False, "error": str(exc)}]
        if not raw:
            missing.append(f"{kind}: no manifest-declared operands were resolved")
            continue
        for item in raw:
            public = _public_value(item)
            parser = {"module": _safe_text(item.get("parser_module")),
                      "callable": _safe_text(item.get("parser_callable")),
                      "version": _safe_text(parser_version),
                      "version_source": "vcstudio.__version__"}
            sources = [
                source_identity(target_by_id[source_id], ())[0]
                for source_id in item.get("source_ids") or []
                if source_id in target_by_id
            ]
            source_ids = [str(value) for value in item.get("source_ids") or []]
            source_complete = (
                bool(source_ids) and len(sources) == len(source_ids)
                and all(source.get("manifest_sha256") for source in sources)
            )
            parser_complete = all(parser.get(key) for key in (
                "module", "callable", "version", "version_source"))
            ok = item.get("ok") is True and source_complete and parser_complete
            if not ok:
                if item.get("ok") is True and not source_complete:
                    reason = "calculator source identities/hashes are incomplete"
                elif item.get("ok") is True and not parser_complete:
                    reason = "calculator parser identity/version is incomplete"
                else:
                    reason = item.get("error") or "calculator failed closed"
                blocking.append(f"{kind}: {_safe_text(reason)}")
            values = []
            for key, label, unit in (
                ("gamma_jm2", "Surface energy", "J/m²"),
                ("binding_energy_eV", "Binding energy", "eV"),
                ("formation_energy_eV", "Formation energy", "eV"),
                ("solvation_energy_eV", "Solvation energy", "eV"),
            ):
                if key in item:
                    value = _public_value(item.get(key))
                    values.append({"key": key, "label": label, "value": value,
                                   "display": _display(value, spec.precision), "unit": unit})
            rows.append({
                "kind": kind, "available": ok,
                "status": "available" if ok else "unavailable",
                "parser": parser,
                "source_ids": source_ids,
                "sources": sources,
                "summary": _safe_text(item.get("summary") or item.get("error")),
                "values": values, "result": public,
            })
    available = sum(row["available"] is True for row in rows)
    payload = {
        "schema": VIEW_SCHEMA, "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(), "spec_sha256": spec.semantic_sha256,
        "scientific_status": "verified" if available and not blocking else
                             "unverified" if available else "blocked",
        "capability_status": "available" if available else
                             "missing_prerequisite" if missing and not blocking else "unavailable",
        "available": bool(available), "rows": rows,
        "missing": list(dict.fromkeys(missing)),
        "blocking": list(dict.fromkeys(blocking)), "warnings": [],
        "next_action": ("Inspect calculator operands and source hashes." if available else
                        "Create or complete a manifest-bound calculator pair, then refresh."),
        "denominator": {
            "resolved_targets": len(targets), "calculator_kinds": 3,
            "attempted_results": len(rows), "available_results": available,
            "blocked_results": len(rows) - available, "visible_rows": len(rows),
        },
    }
    payload["data_fingerprint"] = _canonical_hash(payload)
    return payload


__all__ = [
    "VIEW_SCHEMA", "build_property_view", "build_task_analysis_view",
    "resolve_project_targets", "source_identity",
]
