"""Derived cross-project research index and live-provenance projections.

The index is an in-memory, rebuildable read model.  It is never an authority:
registered project.yaml files, per-job job.yaml manifests, validation records,
and frozen report revisions remain the only scientific sources.  Browser
requests select bounded filters/sort/axes; all numeric aggregation is finalized
here on the server.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from vcstudio.project.research_views import (
    ResearchViewError,
    normalize_axes,
    normalize_filters,
    normalize_sort,
)


INDEX_SCHEMA = "vcstudio.research-index/v1"
QUERY_SCHEMA = "vcstudio.research-query/v1"
PROVENANCE_SCHEMA = "vcstudio.live-provenance/v1"
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

_OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_FORMULA_TOKEN_RE = re.compile(r"([A-Z][a-z]?)(?:\d+(?:\.\d+)?)?")
_PATH_RE = re.compile(
    r"(?i)(?:^[A-Z]:[\\/]|^\\\\|^//|^/|^~[\\/]|\bfile:|[\\/].*[\\/])")
_SECRET_RE = re.compile(
    r"(?i)(?:github_pat_|gh[opusr]_|sk-|Bearer\s+|PRIVATE KEY|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[:=])")

_NUMERIC_FIELDS = {
    "energy_eV": {"label": "Energy", "unit": "eV"},
    "barrier_eV": {"label": "Barrier", "unit": "eV"},
}
_SORT_FIELDS = {
    "project": "project_name",
    "job": "job_id",
    "formula": "formula",
    "facet": "facet",
    "adsorbate": "adsorbate",
    "task": "task_type",
    "state": "state",
    "method": "method_fingerprint",
    "evidence": "evidence_level",
    "energy_eV": "energy_eV",
    "barrier_eV": "barrier_eV",
}

# Layout coordinates are presentation metadata, not browser-derived chemistry.
_PERIOD_ROWS = (
    (("H", 1), ("He", 18)),
    (("Li", 1), ("Be", 2), ("B", 13), ("C", 14), ("N", 15),
     ("O", 16), ("F", 17), ("Ne", 18)),
    (("Na", 1), ("Mg", 2), ("Al", 13), ("Si", 14), ("P", 15),
     ("S", 16), ("Cl", 17), ("Ar", 18)),
    (("K", 1), ("Ca", 2), ("Sc", 3), ("Ti", 4), ("V", 5), ("Cr", 6),
     ("Mn", 7), ("Fe", 8), ("Co", 9), ("Ni", 10), ("Cu", 11),
     ("Zn", 12), ("Ga", 13), ("Ge", 14), ("As", 15), ("Se", 16),
     ("Br", 17), ("Kr", 18)),
    (("Rb", 1), ("Sr", 2), ("Y", 3), ("Zr", 4), ("Nb", 5), ("Mo", 6),
     ("Tc", 7), ("Ru", 8), ("Rh", 9), ("Pd", 10), ("Ag", 11),
     ("Cd", 12), ("In", 13), ("Sn", 14), ("Sb", 15), ("Te", 16),
     ("I", 17), ("Xe", 18)),
    (("Cs", 1), ("Ba", 2), ("La", 3), ("Hf", 4), ("Ta", 5), ("W", 6),
     ("Re", 7), ("Os", 8), ("Ir", 9), ("Pt", 10), ("Au", 11),
     ("Hg", 12), ("Tl", 13), ("Pb", 14), ("Bi", 15), ("Po", 16),
     ("At", 17), ("Rn", 18)),
    (("Fr", 1), ("Ra", 2), ("Ac", 3), ("Rf", 4), ("Db", 5), ("Sg", 6),
     ("Bh", 7), ("Hs", 8), ("Mt", 9), ("Ds", 10), ("Rg", 11),
     ("Cn", 12), ("Nh", 13), ("Fl", 14), ("Mc", 15), ("Lv", 16),
     ("Ts", 17), ("Og", 18)),
    tuple((symbol, index + 3) for index, symbol in enumerate(
        ("Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho",
         "Er", "Tm", "Yb", "Lu"))),
    tuple((symbol, index + 3) for index, symbol in enumerate(
        ("Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es",
         "Fm", "Md", "No", "Lr"))),
)
_PERIODIC_POSITION = {
    symbol: {"period": period, "group": group}
    for period, row in enumerate(_PERIOD_ROWS, start=1)
    for symbol, group in row
}


class ResearchExplorerError(ValueError):
    """A derived-index request violates the bounded public contract."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any, length: int = 24) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()[:length]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _display(value: Any, *, precision: int = 6) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.{precision}g}"


def _path_key(value: Any) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(
        os.path.expanduser(str(value or "")))))


def _safe_opaque(value: Any, *, prefix: str, seed: Any) -> str:
    text = str(value or "").strip()
    if _OPAQUE_RE.fullmatch(text) and not _PATH_RE.search(text) and not _SECRET_RE.search(text):
        return text
    return f"{prefix}-{_digest(seed)}"


def _safe_label(value: Any, *, fallback: str = "", maximum: int = 160) -> str:
    text = str(value or "").strip()
    if (not text or len(text) > maximum or _PATH_RE.search(text)
            or _SECRET_RE.search(text)
            or any(ord(char) < 32 or ord(char) == 127 for char in text)):
        return fallback
    return text


def _nested(source: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        value: Any = source
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                value = None
                break
            value = value[key]
        if value not in (None, ""):
            return value
    return None


def _formula_elements(value: Any) -> tuple[str, list[str]]:
    text = _safe_label(value, maximum=96)
    if not text:
        return "", []
    compact = re.sub(r"[\s_+-]+", "", text)
    matches = list(_FORMULA_TOKEN_RE.finditer(compact))
    if not matches:
        return text, []
    joined = "".join(match.group(0) for match in matches)
    if joined != compact:
        return text, []
    elements = list(dict.fromkeys(match.group(1) for match in matches))
    if any(element not in _PERIODIC_POSITION for element in elements):
        return text, []
    return compact, elements


def _member_records(project: Mapping[str, Any]) -> list[dict[str, str]]:
    members = project.get("members")
    members = members if isinstance(members, Mapping) else {}
    species_map = {
        _path_key(path): _safe_label(species, maximum=64)
        for path, species in (project.get("config_species") or {}).items()
        if path
    }
    values: list[dict[str, str]] = []

    def add(path: Any, role: str, adsorbate: str = "") -> None:
        if not path:
            return
        key = _path_key(path)
        if any(item["path_key"] == key for item in values):
            return
        values.append({
            "path": str(path), "path_key": key, "role": role,
            "adsorbate": adsorbate,
        })

    add(members.get("clean_slab"), "clean_slab")
    add(members.get("gas_ref"), "gas_reference")
    for path in members.get("configs") or []:
        add(path, "configuration", species_map.get(_path_key(path), ""))
    for species, path in (project.get("species_ref_jobs") or {}).items():
        add(path, "species_reference", _safe_label(species, maximum=64))
    launch = project.get("launch")
    launch = launch if isinstance(launch, Mapping) else {}
    for path in launch.get("submitted_job_dirs") or []:
        add(path, "managed_descendant")
    return values


def _method_projection(method: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    method = method if isinstance(method, Mapping) else {}
    inputs = manifest.get("inputs")
    inputs = inputs if isinstance(inputs, Mapping) else {}
    identity = method.get("fingerprint") or method.get("identity")
    if not identity:
        identity = inputs.get("method_fingerprint") or inputs.get("method")
    engine = _safe_label(
        method.get("engine") or inputs.get("engine") or "vasp",
        fallback="unknown", maximum=32).lower()
    fingerprint = ""
    if identity:
        fingerprint = f"method-{_digest(identity, 20)}"
    status = str(method.get("status") or ("verified" if fingerprint else "unverified"))
    return {
        "fingerprint": fingerprint,
        "status": status if status in {"verified", "unverified", "incompatible"}
        else "unverified",
        "engine": engine,
        "missing": [
            _safe_label(item, fallback="method evidence", maximum=96)
            for item in method.get("missing") or []
        ][:16],
    }


def _summary_rows(summary: Any) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    if not isinstance(summary, Mapping):
        return result
    for row in summary.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        for raw in (
                row.get("job"), row.get("path"), row.get("source_job"),
                row.get("name"), row.get("job_id"), row.get("configuration_id")):
            if not raw:
                continue
            text = str(raw)
            result[text.casefold()] = row
            result[Path(text).name.casefold()] = row
            if _PATH_RE.search(text) or os.path.sep in text:
                result[_path_key(text)] = row
    return result


def _matching_summary_row(member: Mapping[str, str], manifest: Mapping[str, Any],
                          rows: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    candidates = (
        member.get("path_key"), Path(member.get("path") or "").name.casefold(),
        str(manifest.get("job_id") or "").casefold(),
        str(manifest.get("system") or "").casefold(),
    )
    return next((rows[key] for key in candidates if key and key in rows), {})


def _energy_projection(manifest: Mapping[str, Any], summary_row: Mapping[str, Any]) -> dict[str, Any]:
    delta = _finite(summary_row.get("delta_e"))
    if delta is not None:
        return {
            "energy_eV": delta,
            "energy_quantity": "adsorption_energy",
            "energy_origin": "analysis",
        }
    results = manifest.get("results")
    results = results if isinstance(results, Mapping) else {}
    energy = _finite(_nested(results,
        ("energy_e0_eV",), ("energy_eV",), ("final_energy_eV",),
        ("energy", "value_eV")))
    quantity = "total_energy" if energy is not None else "missing"
    return {
        "energy_eV": energy,
        "energy_quantity": quantity,
        "energy_origin": "job_manifest" if energy is not None else "missing",
    }


def _barrier(manifest: Mapping[str, Any]) -> float | None:
    results = manifest.get("results")
    results = results if isinstance(results, Mapping) else {}
    return _finite(_nested(results,
        ("barrier_eV",), ("activation_energy_eV",), ("neb", "barrier_eV"),
        ("kinetics", "barrier_eV")))


def _validation_status(manifest: Mapping[str, Any]) -> str:
    value = _nested(manifest,
        ("validation", "status"), ("results", "validation", "status"),
        ("results", "validation_status"))
    return _safe_label(value, fallback="missing", maximum=48).lower()


def _provenance_status(manifest: Mapping[str, Any], *, has_numeric: bool) -> str:
    if not manifest:
        return "missing"
    inputs = manifest.get("inputs")
    inputs = inputs if isinstance(inputs, Mapping) else {}
    if any(key in manifest or key in inputs for key in (
            "imported_at", "imported_from", "result_import", "source_manifest")):
        return "imported"
    results = manifest.get("results")
    results = results if isinstance(results, Mapping) else {}
    hashes = results.get("sha256") or results.get("hashes") or inputs.get("sha256")
    if has_numeric and hashes:
        return "observed"
    if has_numeric:
        return "inferred"
    return "observed"


def _evidence_level(manifest: Mapping[str, Any], method: Mapping[str, Any],
                    provenance: str, *, has_numeric: bool) -> str:
    validation = _validation_status(manifest)
    if (has_numeric and manifest.get("state") == "DONE"
            and method.get("status") == "verified"
            and validation in {"ready", "verified", "passed", "pass", "accepted"}):
        return "verified"
    if provenance == "imported":
        return "imported"
    if provenance == "inferred":
        return "inferred"
    if has_numeric or manifest:
        return "observed"
    return "missing"


def _project_text(project: Mapping[str, Any], *keys: str) -> str:
    containers = [project]
    for key in ("metadata", "material", "preparation"):
        value = project.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for container in containers:
        for key in keys:
            if container.get(key) not in (None, ""):
                return _safe_label(container.get(key), maximum=96)
    return ""


def _public_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "project_id", "project_name", "job_id", "source_id", "role", "formula",
        "elements", "facet", "adsorbate", "task_type", "state",
        "method_fingerprint", "method_status", "engine", "evidence_level",
        "provenance_status", "validation_status", "energy_eV", "energy_quantity",
        "barrier_eV", "attempt_count",
    )
    row = {key: copy.deepcopy(entry.get(key)) for key in fields}
    row["units"] = {"energy_eV": "eV", "barrier_eV": "eV"}
    row["display"] = {
        "energy_eV": _display(row.get("energy_eV")),
        "barrier_eV": _display(row.get("barrier_eV")),
    }
    row["missing"] = [
        field for field in ("formula", "facet", "adsorbate", "method_fingerprint",
                            "energy_eV", "barrier_eV")
        if row.get(field) in (None, "", [])
    ]
    row["drilldown"] = {
        "project": {"project_id": row["project_id"]},
        "job": {"project_id": row["project_id"], "job_id": row["job_id"]},
        "source": {"project_id": row["project_id"], "source_id": row["source_id"]},
    }
    return row


class ResearchIndexService:
    """Thread-safe in-memory read model with bounded query projections."""

    def __init__(self, *, max_age_seconds: float = 300.0,
                 monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], str] = _utc_now):
        self.max_age_seconds = max(1.0, float(max_age_seconds))
        self._monotonic = monotonic
        self._clock = clock
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = None

    @staticmethod
    def source_fingerprint(records: Sequence[Mapping[str, Any]], *,
                           registry_total: int | None,
                           registry_failures: Sequence[Mapping[str, Any]],
                           source_version: Any = None) -> str:
        payload = {
            "registry_total": registry_total,
            "records": [
                {
                    "project_id": record.get("project_id"),
                    "identity": record.get("identity_fingerprint"),
                    "project": record.get("project"),
                }
                for record in records
            ],
            "failures": [
                {"code": item.get("code"), "project_ref": item.get("project_ref")}
                for item in registry_failures
            ],
            "source_version": source_version,
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    def rebuild(self, records: Sequence[Mapping[str, Any]], *,
                manifest_loader: Callable[[str], Mapping[str, Any] | None],
                summary_loader: Callable[[Mapping[str, Any]], Mapping[str, Any]],
                job_id_resolver: Callable[[str, Mapping[str, Any]], str],
                method_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]],
                registry_total: int | None = None,
                registry_failures: Sequence[Mapping[str, Any]] = (),
                source_version: Any = None) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        failures = [
            {
                "project_ref": _safe_label(item.get("project_ref"), fallback="registry"),
                "code": _safe_label(item.get("code"), fallback="unavailable"),
            }
            for item in registry_failures
        ]
        indexed_projects = 0
        for record in records:
            project_id = _safe_opaque(
                record.get("project_id"), prefix="project", seed=record.get("project_id"))
            try:
                project = record.get("project")
                if not isinstance(project, Mapping):
                    raise ValueError("project authority is unreadable")
                summary = summary_loader(project)
                rows = _summary_rows(summary)
                project_name = _safe_label(
                    project.get("name"), fallback=project_id, maximum=96)
                project_formula = _project_text(
                    project, "formula", "material_formula", "substrate_formula", "substrate")
                project_facet = _project_text(
                    project, "facet", "surface_facet", "miller_index")
                report = project.get("autopilot_report")
                report = copy.deepcopy(report) if isinstance(report, Mapping) else {}
                member_count = 0
                for member in _member_records(project):
                    manifest = manifest_loader(member["path"])
                    manifest = copy.deepcopy(manifest) if isinstance(manifest, Mapping) else {}
                    job_id = _safe_opaque(
                        job_id_resolver(member["path"], manifest), prefix="job",
                        seed={"project": project_id, "member": member["role"],
                              "path_key": member["path_key"]})
                    source_id = _safe_opaque(
                        _nested(manifest, ("source_id",), ("inputs", "source_id")),
                        prefix="source", seed={"project": project_id, "job": job_id})
                    try:
                        method_raw = method_resolver({
                            "path": member["path"], "manifest": manifest,
                            "source_id": source_id,
                        }) if manifest else {}
                    except Exception:  # noqa: BLE001 evidence is unavailable, never guessed
                        method_raw = {"status": "unverified", "missing": ["method resolver"]}
                    method = _method_projection(method_raw, manifest)
                    summary_row = _matching_summary_row(member, manifest, rows)
                    energy = _energy_projection(manifest, summary_row)
                    barrier = _barrier(manifest)
                    formula_raw = _nested(manifest,
                        ("inputs", "formula"), ("inputs", "material_formula"),
                        ("formula",), ("system",)) or project_formula
                    formula, elements = _formula_elements(formula_raw)
                    adsorbate = member["adsorbate"] or _safe_label(
                        _nested(manifest, ("inputs", "adsorbate"), ("inputs", "species"))
                        or summary_row.get("species"), maximum=64)
                    if not elements and adsorbate:
                        _ads_formula, ads_elements = _formula_elements(adsorbate)
                        elements = ads_elements
                    has_numeric = energy["energy_eV"] is not None or barrier is not None
                    provenance = _provenance_status(manifest, has_numeric=has_numeric)
                    attempts = manifest.get("attempts")
                    attempts = attempts if isinstance(attempts, list) else []
                    entry = {
                        "project_id": project_id,
                        "project_name": project_name,
                        "job_id": job_id,
                        "source_id": source_id,
                        "role": member["role"],
                        "formula": formula,
                        "elements": elements,
                        "facet": _safe_label(
                            _nested(manifest, ("inputs", "facet"),
                                    ("inputs", "surface_facet")) or project_facet,
                            maximum=64),
                        "adsorbate": adsorbate,
                        "task_type": _safe_label(
                            manifest.get("task_type"), fallback="unknown", maximum=64),
                        "state": _safe_label(
                            manifest.get("state"), fallback="MISSING", maximum=32).upper(),
                        "method_fingerprint": method["fingerprint"],
                        "method_status": method["status"],
                        "engine": method["engine"],
                        "evidence_level": _evidence_level(
                            manifest, method, provenance, has_numeric=has_numeric),
                        "provenance_status": provenance,
                        "validation_status": _validation_status(manifest),
                        **energy,
                        "barrier_eV": barrier,
                        "attempt_count": len(attempts),
                        "_manifest": manifest,
                        "_method": method,
                        "_report": report,
                    }
                    entries.append(entry)
                    member_count += 1
                if member_count == 0:
                    # Empty projects are still indexed explicitly as missing evidence.
                    entries.append({
                        "project_id": project_id, "project_name": project_name,
                        "job_id": f"job-{_digest([project_id, 'missing'])}",
                        "source_id": f"source-{_digest([project_id, 'missing'])}",
                        "role": "missing", "formula": project_formula,
                        "elements": _formula_elements(project_formula)[1],
                        "facet": project_facet, "adsorbate": "", "task_type": "unknown",
                        "state": "MISSING", "method_fingerprint": "",
                        "method_status": "unverified", "engine": "unknown",
                        "evidence_level": "missing", "provenance_status": "missing",
                        "validation_status": "missing", "energy_eV": None,
                        "energy_quantity": "missing", "energy_origin": "missing",
                        "barrier_eV": None, "attempt_count": 0,
                        "_manifest": {}, "_method": {}, "_report": report,
                    })
                indexed_projects += 1
            except Exception:  # noqa: BLE001 one project makes completeness partial
                failures.append({
                    "project_ref": project_id,
                    "code": "project_index_unavailable",
                })

        entries.sort(key=lambda item: (
            item["project_id"], item["job_id"], item["source_id"]))
        source_fingerprint = self.source_fingerprint(
            records, registry_total=registry_total, registry_failures=registry_failures,
            source_version=source_version)
        snapshot_identity = {
            'source': source_fingerprint,
            'rows': [{key: value for key, value in entry.items() if not key.startswith('_')}
                     for entry in entries],
        }
        snapshot_id = f"index-{_digest(snapshot_identity)}"
        status = "partial" if failures else "ready"
        snapshot = {
            "schema": INDEX_SCHEMA,
            "snapshot_id": snapshot_id,
            "source_fingerprint": source_fingerprint,
            "built_at": self._clock(),
            "built_monotonic": self._monotonic(),
            "status": status,
            "registry_total": registry_total,
            "indexed_projects": indexed_projects,
            "indexed_jobs": len(entries),
            "failed_sources": len(failures),
            "failures": failures,
            "entries": entries,
        }
        with self._lock:
            self._snapshot = snapshot
        return self.index_status()

    def index_status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        if snapshot is None:
            return {
                "schema": INDEX_SCHEMA, "status": "unavailable", "snapshot_id": None,
                "built_at": None, "age_seconds": None, "max_age_seconds": self.max_age_seconds,
                "registry_total": None, "indexed_projects": 0, "indexed_jobs": 0,
                "failed_sources": None, "failures": [], "rebuildable": True,
            }
        age = max(0.0, self._monotonic() - snapshot["built_monotonic"])
        status = "stale" if age > self.max_age_seconds else snapshot["status"]
        return {
            key: copy.deepcopy(snapshot[key])
            for key in (
                "schema", "snapshot_id", "built_at", "registry_total",
                "indexed_projects", "indexed_jobs", "failed_sources", "failures",
                "source_fingerprint")
        } | {
            "status": status,
            "age_seconds": round(age, 6),
            "max_age_seconds": self.max_age_seconds,
            "rebuildable": True,
        }

    @staticmethod
    def _normalize_request(request: Any) -> dict[str, Any]:
        if request is None:
            request = {}
        if not isinstance(request, Mapping):
            raise ResearchExplorerError("research query must be an object")
        allowed = {"schema", "filters", "sort", "axes", "cursor", "limit"}
        unknown = set(request) - allowed
        if unknown:
            raise ResearchExplorerError(
                "research query contains unknown fields: " + ", ".join(sorted(unknown)))
        if request.get("schema") not in {None, QUERY_SCHEMA}:
            raise ResearchExplorerError("unsupported research query schema")
        try:
            filters = normalize_filters(request.get("filters"))
            sort = normalize_sort(request.get("sort"))
            axes = normalize_axes(request.get("axes"))
        except ResearchViewError as exc:
            raise ResearchExplorerError(str(exc)) from exc
        limit = request.get("limit", DEFAULT_PAGE_SIZE)
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_PAGE_SIZE):
            raise ResearchExplorerError(
                f"limit must be an integer between 1 and {MAX_PAGE_SIZE}")
        cursor = request.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
            raise ResearchExplorerError("cursor must be a bounded opaque string")
        return {
            "schema": QUERY_SCHEMA,
            "filters": filters,
            "sort": sort,
            "axes": axes,
            "cursor": cursor,
            "limit": limit,
        }

    @staticmethod
    def _filter_entries(entries: Sequence[Mapping[str, Any]],
                        filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        def allowed(entry: Mapping[str, Any]) -> bool:
            for key, field in (
                    ("project_ids", "project_id"), ("task_types", "task_type"),
                    ("states", "state"), ("method_fingerprints", "method_fingerprint"),
                    ("evidence_levels", "evidence_level")):
                selected = filters.get(key) or []
                if selected and str(entry.get(field) or "") not in selected:
                    return False
            elements = set(filters.get("elements") or [])
            if elements and not elements.issubset(set(entry.get("elements") or [])):
                return False
            for key in ("formula", "facet", "adsorbate"):
                wanted = str(filters.get(key) or "").casefold()
                if wanted and wanted not in str(entry.get(key) or "").casefold():
                    return False
            for prefix, field in (("energy", "energy_eV"), ("barrier", "barrier_eV")):
                value = _finite(entry.get(field))
                minimum = filters.get(f"{prefix}_min_eV")
                maximum = filters.get(f"{prefix}_max_eV")
                if (minimum is not None or maximum is not None) and value is None:
                    return False
                if minimum is not None and value is not None and value < minimum:
                    return False
                if maximum is not None and value is not None and value > maximum:
                    return False
            return True

        return [dict(entry) for entry in entries if allowed(entry)]

    @staticmethod
    def _method_cohort(entries: Sequence[Mapping[str, Any]], *, enabled: bool
                       ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        counts = Counter(
            str(entry.get("method_fingerprint") or "") for entry in entries
            if entry.get("method_fingerprint"))
        selected = None
        output = [dict(entry) for entry in entries]
        missing_fingerprints = sum(
            1 for entry in entries if not entry.get("method_fingerprint"))
        if enabled:
            if counts:
                selected = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
                output = [
                    dict(entry) for entry in entries
                    if entry.get("method_fingerprint") == selected
                ]
            else:
                # Compatibility cannot be proven from an absent fingerprint.
                # With the default guard on, scientific projections fail closed.
                output = []
        status = (
            "compatible" if selected else
            "blocked_missing_method" if enabled else
            "mixed_or_unverified" if len(counts) > 1 or missing_fingerprints
            else "explicitly_unfiltered"
        )
        return output, {
            "enabled": enabled,
            "status": status,
            "selected_method_fingerprint": selected,
            "cohorts": [
                {"method_fingerprint": key, "sample_count": value}
                for key, value in sorted(counts.items())
            ],
            "input_rows": len(entries),
            "compatible_rows": len(output),
            "excluded_rows": len(entries) - len(output),
            "missing_method_fingerprint_rows": missing_fingerprints,
        }

    @staticmethod
    def _sort(entries: Sequence[Mapping[str, Any]], sort: Mapping[str, str]
              ) -> list[dict[str, Any]]:
        field = _SORT_FIELDS[sort["key"]]
        present = [dict(item) for item in entries if item.get(field) not in (None, "")]
        missing = [dict(item) for item in entries if item.get(field) in (None, "")]
        def identity(item: Mapping[str, Any]):
            return item["project_id"], item["job_id"], item["source_id"]
        missing.sort(key=identity)

        def key(item: Mapping[str, Any]):
            value = item.get(field)
            comparable = float(value) if isinstance(value, (int, float)) else str(value).casefold()
            return comparable, identity(item)

        present.sort(key=key, reverse=sort["direction"] == "desc")
        return present + missing

    @staticmethod
    def _query_hash(request: Mapping[str, Any]) -> str:
        return hashlib.sha256(_canonical_bytes({
            "filters": request["filters"], "sort": request["sort"],
            "axes": request["axes"], "limit": request["limit"],
        })).hexdigest()

    @staticmethod
    def _cursor_encode(snapshot_id: str, query_hash: str, offset: int) -> str:
        payload = _canonical_bytes({
            "snapshot_id": snapshot_id, "query_hash": query_hash, "offset": offset,
        })
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _cursor_offset(cursor: str | None, *, snapshot_id: str,
                       query_hash: str) -> int:
        if not cursor:
            return 0
        try:
            padding = "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResearchExplorerError("cursor is invalid") from exc
        if (not isinstance(value, Mapping)
                or value.get("snapshot_id") != snapshot_id
                or value.get("query_hash") != query_hash
                or isinstance(value.get("offset"), bool)
                or not isinstance(value.get("offset"), int)
                or value["offset"] < 0):
            raise ResearchExplorerError("cursor is stale or does not match this query")
        return value["offset"]

    @staticmethod
    def _histogram(entries: Sequence[Mapping[str, Any]], metric: str,
                   *, bins: int = 12) -> dict[str, Any]:
        values = [_finite(item.get(metric)) for item in entries]
        observed = [value for value in values if value is not None]
        missing = len(values) - len(observed)
        if not observed:
            return {
                "status": "missing", "metric": metric,
                "unit": _NUMERIC_FIELDS[metric]["unit"], "bins": [],
                "sample_count": 0, "missing_count": missing,
            }
        minimum, maximum = min(observed), max(observed)
        width = (maximum - minimum) / bins if maximum > minimum else 1.0
        counts = [0] * bins
        for value in observed:
            index = min(bins - 1, int((value - minimum) / width)) if width else 0
            counts[index] += 1
        peak = max(counts) or 1
        records = []
        for index, count in enumerate(counts):
            low = minimum + index * width
            high = maximum if index == bins - 1 else minimum + (index + 1) * width
            records.append({
                "low": low, "high": high, "count": count,
                "low_display": _display(low), "high_display": _display(high),
                "percent_of_total": count * 100.0 / len(observed),
                "percent_of_peak": count * 100.0 / peak,
            })
        return {
            "status": "ready", "metric": metric,
            "unit": _NUMERIC_FIELDS[metric]["unit"], "bins": records,
            "sample_count": len(observed), "missing_count": missing,
            "range": {"min": minimum, "max": maximum},
        }

    @staticmethod
    def _scatter(entries: Sequence[Mapping[str, Any]], axes: Mapping[str, str],
                 compatibility: Mapping[str, Any]) -> dict[str, Any]:
        fingerprints = {
            str(item.get("method_fingerprint")) for item in entries
            if item.get("method_fingerprint")
        }
        missing_methods = sum(
            1 for item in entries if not item.get("method_fingerprint"))
        if (not compatibility["enabled"]
                and (len(fingerprints) > 1 or missing_methods)):
            return {
                "status": "blocked_mixed_or_unverified_methods",
                "axes": copy.deepcopy(dict(axes)),
                "points": [], "sample_count": 0, "missing_count": len(entries),
                "reason": (
                    "Select one verified method fingerprint before plotting a scatter view."),
            }
        x_axis, y_axis = axes["x"], axes["y"]
        complete = []
        for item in entries:
            x_value, y_value = _finite(item.get(x_axis)), _finite(item.get(y_axis))
            if x_value is None or y_value is None:
                continue
            complete.append((item, x_value, y_value))
        if not complete:
            return {
                "status": "missing", "axes": copy.deepcopy(dict(axes)), "points": [],
                "sample_count": 0, "missing_count": len(entries),
                "units": {x_axis: _NUMERIC_FIELDS[x_axis]["unit"],
                          y_axis: _NUMERIC_FIELDS[y_axis]["unit"]},
            }
        x_values = [item[1] for item in complete]
        y_values = [item[2] for item in complete]
        x_min, x_max = min(x_values), max(x_values)
        y_min, y_max = min(y_values), max(y_values)

        def percent(value: float, low: float, high: float) -> float:
            return 50.0 if high == low else (value - low) * 100.0 / (high - low)

        points = [{
            "project_id": item[0]["project_id"], "job_id": item[0]["job_id"],
            "source_id": item[0]["source_id"], "label": item[0]["project_name"],
            "x": item[1], "y": item[2],
            "x_display": _display(item[1]), "y_display": _display(item[2]),
            "x_percent": percent(item[1], x_min, x_max),
            "y_percent": percent(item[2], y_min, y_max),
            "top_percent": 100.0 - percent(item[2], y_min, y_max),
            "method_fingerprint": item[0].get("method_fingerprint") or "",
        } for item in complete]
        return {
            "status": "ready", "axes": copy.deepcopy(dict(axes)), "points": points,
            "sample_count": len(points), "missing_count": len(entries) - len(points),
            "units": {x_axis: _NUMERIC_FIELDS[x_axis]["unit"],
                      y_axis: _NUMERIC_FIELDS[y_axis]["unit"]},
            "ranges": {x_axis: {"min": x_min, "max": x_max},
                       y_axis: {"min": y_min, "max": y_max}},
        }

    @staticmethod
    def _periodic(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        by_element: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for item in entries:
            for element in item.get("elements") or []:
                if element in _PERIODIC_POSITION:
                    by_element[element].append(item)
        cells = []
        for element in sorted(by_element, key=lambda item: (
                _PERIODIC_POSITION[item]["period"], _PERIODIC_POSITION[item]["group"])):
            rows = by_element[element]
            energies = [
                value for value in (_finite(row.get("energy_eV")) for row in rows)
                if value is not None
            ]
            cells.append({
                "element": element, **_PERIODIC_POSITION[element],
                "sample_count": len(rows),
                "project_count": len({row["project_id"] for row in rows}),
                "numeric_energy_count": len(energies),
                "mean_energy_eV": (sum(energies) / len(energies) if energies else None),
                "mean_energy_display": _display(
                    sum(energies) / len(energies) if energies else None),
                "unit": "eV",
            })
        return {
            "status": "ready" if cells else "missing", "cells": cells,
            "sample_count": len(entries), "element_count": len(cells),
            "missing_element_rows": sum(1 for item in entries if not item.get("elements")),
        }

    def query(self, request: Any = None) -> dict[str, Any]:
        normalized = self._normalize_request(request)
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        freshness = self.index_status()
        if snapshot is None or freshness["status"] in {"stale", "partial", "unavailable"}:
            status = freshness["status"]
            return {
                "ok": False, "schema": QUERY_SCHEMA, "status": status,
                "request": normalized, "freshness": freshness,
                "method_compatibility": None,
                "table": {"columns": [], "rows": [], "sample_count": 0,
                          "visible_count": 0, "next_cursor": None},
                "histogram": None, "scatter": None, "periodic_table": None,
                "denominator": {
                    "registry_total": freshness.get("registry_total"),
                    "indexed_projects": freshness.get("indexed_projects"),
                    "indexed_jobs": freshness.get("indexed_jobs"),
                    "matched_rows": 0, "visible_rows": 0,
                },
                "error": (
                    "Derived research index is not complete and current; scientific "
                    "projections are withheld until a successful rebuild."),
            }
        filtered = self._filter_entries(snapshot["entries"], normalized["filters"])
        compatible, compatibility = self._method_cohort(
            filtered, enabled=normalized["filters"]["method_compatible"])
        ordered = self._sort(compatible, normalized["sort"])
        query_hash = self._query_hash(normalized)
        offset = self._cursor_offset(
            normalized["cursor"], snapshot_id=snapshot["snapshot_id"],
            query_hash=query_hash)
        if offset > len(ordered):
            raise ResearchExplorerError("cursor offset exceeds the result set")
        page = ordered[offset:offset + normalized["limit"]]
        next_offset = offset + len(page)
        next_cursor = (
            self._cursor_encode(snapshot["snapshot_id"], query_hash, next_offset)
            if next_offset < len(ordered) else None)
        missing_counts = {
            key: sum(1 for item in compatible if item.get(key) in (None, "", []))
            for key in ("formula", "facet", "adsorbate", "method_fingerprint",
                        "energy_eV", "barrier_eV")
        }
        histogram_metric = normalized["axes"]["x"]
        return {
            "ok": True, "schema": QUERY_SCHEMA, "status": "ready",
            "request": normalized, "freshness": freshness,
            "method_compatibility": compatibility,
            "table": {
                "columns": [
                    {"key": "project_name", "unit": None},
                    {"key": "formula", "unit": None},
                    {"key": "facet", "unit": None},
                    {"key": "adsorbate", "unit": None},
                    {"key": "task_type", "unit": None},
                    {"key": "state", "unit": None},
                    {"key": "method_fingerprint", "unit": None},
                    {"key": "evidence_level", "unit": None},
                    {"key": "energy_eV", "unit": "eV"},
                    {"key": "barrier_eV", "unit": "eV"},
                ],
                "rows": [_public_row(item) for item in page],
                "sample_count": len(ordered), "visible_count": len(page),
                "offset": offset, "limit": normalized["limit"],
                "next_cursor": next_cursor,
                "stable_sort": [normalized["sort"], "project_id", "job_id", "source_id"],
            },
            "histogram": self._histogram(compatible, histogram_metric),
            "scatter": self._scatter(compatible, normalized["axes"], compatibility),
            "periodic_table": self._periodic(compatible),
            "denominator": {
                "registry_total": freshness["registry_total"],
                "indexed_projects": freshness["indexed_projects"],
                "indexed_jobs": freshness["indexed_jobs"],
                "filter_matches_before_method_compatibility": len(filtered),
                "matched_rows": len(compatible), "visible_rows": len(page),
                "missing_by_field": missing_counts,
            },
            "error": None,
        }

    @staticmethod
    def _node(node_id: str, node_type: str, layer: str, status: str,
              label: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": node_id, "type": node_type, "layer": layer,
            "origin_status": status, "label": _safe_label(label, fallback=node_type),
            "record": copy.deepcopy(dict(record)),
        }

    def live_provenance(self, project_id: Any, *, job_id: Any = None,
                        source_id: Any = None) -> dict[str, Any]:
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        freshness = self.index_status()
        if snapshot is None or freshness["status"] != "ready":
            return {
                "ok": False, "schema": PROVENANCE_SCHEMA, "status": freshness["status"],
                "graph_kind": "live_derived", "report_frozen_graph_included": False,
                "nodes": [], "edges": [], "missing": [], "freshness": freshness,
                "error": "Live provenance requires a complete current derived index.",
            }
        project = str(project_id or "")
        if not _OPAQUE_RE.fullmatch(project) or _PATH_RE.search(project):
            raise ResearchExplorerError("project_id must be an opaque identity")
        selected = [
            item for item in snapshot["entries"] if item["project_id"] == project
            and (job_id is None or item["job_id"] == str(job_id))
            and (source_id is None or item["source_id"] == str(source_id))
        ]
        if not selected:
            raise ResearchExplorerError("provenance identity is unavailable")
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []

        def edge(source: str, target: str, relation: str, layer: str) -> None:
            edges.append({
                "source": source, "target": target, "type": relation, "layer": layer,
            })

        project_node = f"project:{project}"
        nodes.append(self._node(
            project_node, "project", "data", "observed",
            selected[0]["project_name"], {"project_id": project}))
        report_added = False
        for entry in selected:
            manifest = entry["_manifest"]
            input_node = f"input:{entry['source_id']}"
            input_hashes = _nested(manifest, ("inputs", "sha256"))
            input_status = entry["provenance_status"] if input_hashes else "missing"
            nodes.append(self._node(
                input_node, "input", "data", input_status, entry["source_id"],
                {"source_id": entry["source_id"], "hash_bound": bool(input_hashes)}))
            job_node = f"job:{entry['job_id']}"
            nodes.append(self._node(
                job_node, "job", "data", entry["provenance_status"], entry["job_id"],
                {"job_id": entry["job_id"], "task_type": entry["task_type"],
                 "state": entry["state"]}))
            edge(project_node, job_node, "contains", "data")
            edge(input_node, job_node, "executed_as", "data")
            if input_status == "missing":
                missing.append({
                    "from": input_node, "expected": "input hashes",
                    "reason": "job input hash evidence is missing",
                })

            attempts = manifest.get("attempts")
            attempts = attempts if isinstance(attempts, list) else []
            previous = job_node
            for index, attempt in enumerate(attempts):
                attempt = attempt if isinstance(attempt, Mapping) else {}
                repair_id = f"repair:{entry['job_id']}:{index + 1}"
                kind = _safe_label(
                    attempt.get("kind") or attempt.get("action"),
                    fallback="resume", maximum=48)
                nodes.append(self._node(
                    repair_id, "repair_resume", "data", "observed", kind,
                    {"attempt": index + 1, "kind": kind}))
                edge(previous, repair_id, "resumed_by", "data")
                previous = repair_id

            parser_id = f"parser:{entry['job_id']}"
            parser_name = _safe_label(_nested(manifest,
                ("results", "parser"), ("results", "parsed_by")),
                fallback="registered parser", maximum=96)
            parser_status = (
                "observed" if _nested(manifest,
                    ("results", "parser"), ("results", "parsed_by")) else
                "inferred" if entry["energy_eV"] is not None
                or entry["barrier_eV"] is not None else "missing")
            nodes.append(self._node(
                parser_id, "parser", "data", parser_status, parser_name,
                {"parser": parser_name}))
            edge(previous, parser_id, "parsed_by", "data")
            if parser_status == "missing":
                missing.append({
                    "from": job_node, "expected": "parser evidence",
                    "reason": "no parsed scientific result is recorded",
                })

            analysis_id = f"analysis:{entry['job_id']}"
            has_analysis = entry["energy_eV"] is not None or entry["barrier_eV"] is not None
            nodes.append(self._node(
                analysis_id, "analysis", "logical",
                entry["provenance_status"] if has_analysis else "missing",
                entry["energy_quantity"],
                {"energy_quantity": entry["energy_quantity"],
                 "energy_eV": entry["energy_eV"], "barrier_eV": entry["barrier_eV"],
                 "units": {"energy_eV": "eV", "barrier_eV": "eV"}}))
            edge(parser_id, analysis_id, "analyzed_as", "logical")

            method_id = f"method:{entry['method_fingerprint'] or entry['job_id']}"
            nodes.append(self._node(
                method_id, "method", "logical",
                "observed" if entry["method_fingerprint"] else "missing",
                entry["method_fingerprint"] or "missing method fingerprint",
                {"method_fingerprint": entry["method_fingerprint"],
                 "method_status": entry["method_status"], "engine": entry["engine"]}))
            edge(method_id, analysis_id, "governs", "logical")
            validation_id = f"validation:{entry['job_id']}"
            validation_origin = (
                "observed" if entry["validation_status"] != "missing" else "missing")
            nodes.append(self._node(
                validation_id, "validation", "logical", validation_origin,
                entry["validation_status"],
                {"status": entry["validation_status"],
                 "evidence_level": entry["evidence_level"]}))
            edge(validation_id, analysis_id, "qualifies", "logical")

            report = entry["_report"]
            if not report_added:
                report_added = True
                report_id = f"report-live:{project}"
                revision = _safe_label(
                    report.get("revision_id") or report.get("revision"), maximum=128)
                report_status = "observed" if revision or report.get("contracts") else "missing"
                nodes.append(self._node(
                    report_id, "report", "logical", report_status,
                    revision or "no current report revision",
                    {"revision_id": revision or None,
                     "frozen_graph_separate": True,
                     "frozen_graph_endpoint": "report_evidence_graph"}))
                edge(analysis_id, report_id, "reported_in", "logical")
                if report_status == "missing":
                    missing.append({
                        "from": analysis_id, "expected": "current report revision",
                        "reason": "live project has no current report revision",
                    })

        nodes.sort(key=lambda item: item["id"])
        edges.sort(key=lambda item: (
            item["layer"], item["source"], item["target"], item["type"]))
        missing.sort(key=lambda item: (item["from"], item["expected"]))
        return {
            "ok": True, "schema": PROVENANCE_SCHEMA,
            "status": "partial" if missing else "ready",
            "graph_kind": "live_derived",
            "report_frozen_graph_included": False,
            "layers": {
                "data": {"description": "Observed/imported input and execution lineage."},
                "logical": {"description": "Method, validation, analysis and report logic."},
            },
            "project_id": project,
            "selection": {"job_id": job_id, "source_id": source_id},
            "nodes": nodes, "edges": edges, "missing": missing,
            "denominator": {
                "nodes": len(nodes), "edges": len(edges), "missing": len(missing),
                "data_nodes": sum(1 for node in nodes if node["layer"] == "data"),
                "logical_nodes": sum(1 for node in nodes if node["layer"] == "logical"),
            },
            "freshness": freshness, "error": None,
        }


__all__ = [
    "DEFAULT_PAGE_SIZE", "INDEX_SCHEMA", "MAX_PAGE_SIZE", "PROVENANCE_SCHEMA",
    "QUERY_SCHEMA", "ResearchExplorerError", "ResearchIndexService",
]
