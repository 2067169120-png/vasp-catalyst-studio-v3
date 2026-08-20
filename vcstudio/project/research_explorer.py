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
MAX_REGISTRY_RECORDS = 512
MAX_REGISTRY_BYTES = 8 * 1024 * 1024
MAX_PROJECT_BYTES = 2 * 1024 * 1024
MAX_PROJECT_MEMBERS = 4096
MAX_MEMBER_BYTES = 16 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ENTRIES = 8192
MAX_NESTING_DEPTH = 32
MAX_SCATTER_POINTS = 2000
MAX_PROVENANCE_ATTEMPTS = 128
MAX_SOURCE_FILES = 50_000
MAX_SOURCE_FILE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PUBLIC_FAILURES = 64

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


class ResearchIndexLimitError(ResearchExplorerError):
    """An authority payload exceeds a hard read-model resource budget."""


def _bounded_payload_size(value: Any, *, label: str, maximum: int,
                          max_depth: int = MAX_NESTING_DEPTH) -> int:
    """Measure an authority payload without serialising an unbounded tree."""
    total = 0
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            raise ResearchIndexLimitError(
                f"{label} exceeds the nesting-depth limit")
        nodes += 1
        if nodes > maximum:
            raise ResearchIndexLimitError(f"{label} exceeds the byte limit")
        if isinstance(current, Mapping):
            total += 2
            for key, item in current.items():
                total += len(str(key).encode("utf-8", errors="replace")) + 2
                stack.append((item, depth + 1))
        elif isinstance(current, (list, tuple)):
            total += 2
            stack.extend((item, depth + 1) for item in current)
        elif current is None:
            total += 4
        elif isinstance(current, bool):
            total += 5
        elif isinstance(current, (int, float)):
            total += len(str(current))
        else:
            total += len(str(current).encode("utf-8", errors="replace")) + 2
        if total > maximum:
            raise ResearchIndexLimitError(f"{label} exceeds the byte limit")
    return total


def _raw_project_member_count(project: Mapping[str, Any]) -> int:
    """Count member declarations before path normalisation or manifest I/O."""
    members = project.get("members")
    members = members if isinstance(members, Mapping) else {}
    count = int(bool(members.get("clean_slab"))) + int(bool(members.get("gas_ref")))
    for key in ("configs", "submitted_job_dirs"):
        values = members.get(key) if key == "configs" else (
            (project.get("launch") or {}).get(key)
            if isinstance(project.get("launch"), Mapping) else None)
        if isinstance(values, (list, tuple)):
            count += len(values)
        elif values:
            count += 1
    for values in (members.get("molecules"), project.get("species_ref_jobs")):
        if isinstance(values, Mapping):
            count += len(values)
        elif isinstance(values, (list, tuple)):
            count += len(values)
        elif values:
            count += 1
    return count


def _raw_project_members(project: Mapping[str, Any]) -> list[Any]:
    members = project.get("members")
    members = members if isinstance(members, Mapping) else {}
    values: list[Any] = [members.get("clean_slab"), members.get("gas_ref")]
    for candidate in (members.get("configs"), members.get("molecules"),
                      project.get("species_ref_jobs")):
        if isinstance(candidate, Mapping):
            values.extend(candidate.values())
        elif isinstance(candidate, (list, tuple)):
            values.extend(candidate)
        elif candidate:
            values.append(candidate)
    launch = project.get("launch")
    launch = launch if isinstance(launch, Mapping) else {}
    submitted = launch.get("submitted_job_dirs")
    if isinstance(submitted, (list, tuple)):
        values.extend(submitted)
    elif submitted:
        values.append(submitted)
    return [value for value in values if value]


def authority_budget_failures(records: Sequence[Mapping[str, Any]], *,
                              registry_total: int | None = None
                              ) -> list[dict[str, str]]:
    """Preflight registry/project/member budgets before expanding any members."""
    failures: list[dict[str, str]] = []
    if (len(records) > MAX_REGISTRY_RECORDS
            or (registry_total is not None and registry_total > MAX_REGISTRY_RECORDS)):
        return [{"project_ref": "registry", "code": "registry_limit_exceeded"}]
    try:
        _bounded_payload_size(
            records, label="registry", maximum=MAX_REGISTRY_BYTES)
    except ResearchIndexLimitError:
        return [{"project_ref": "registry", "code": "registry_limit_exceeded"}]
    for record in records:
        project_id = _safe_opaque(
            record.get("project_id"), prefix="project", seed=record.get("project_id"))
        project = record.get("project")
        if not isinstance(project, Mapping):
            continue
        try:
            _bounded_payload_size(
                project, label="project", maximum=MAX_PROJECT_BYTES)
            if _raw_project_member_count(project) > MAX_PROJECT_MEMBERS:
                raise ResearchIndexLimitError("project exceeds the member limit")
            for member in _raw_project_members(project):
                _bounded_payload_size(
                    member, label="member", maximum=MAX_MEMBER_BYTES)
        except ResearchIndexLimitError:
            failures.append({
                "project_ref": project_id,
                "code": "project_limit_exceeded",
            })
    return failures


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
        method.get("engine") or inputs.get("engine") or "unknown",
        fallback="unknown", maximum=32).lower()
    schema = str(method.get("schema") or "").strip()
    if len(schema) > 96 or any(ord(char) < 32 or ord(char) == 127 for char in schema):
        schema = ""
    schema_match = re.fullmatch(
        r"vcstudio\.method-fingerprint/([a-z0-9_.+-]+)/v([1-9][0-9]*)", schema)
    schema_verified = bool(schema_match and schema_match.group(1) == engine)
    fingerprint = ""
    if identity:
        # Engine semantics are part of a computational-method identity.  The
        # same nominal functional in two engines is not silently comparable.
        fingerprint = f"method-{_digest({'engine': engine, 'identity': identity}, 20)}"
    # A fingerprint identifies a cohort; it is not itself verification that
    # the cohort evidence is complete.
    status = str(method.get("status") or "unverified").lower()
    if engine == "unknown" or not fingerprint or not schema_verified:
        status = "unverified"
    return {
        "fingerprint": fingerprint,
        "status": status if status in {"verified", "unverified", "incompatible"}
        else "unverified",
        "engine": engine,
        "schema": schema if schema_verified else "",
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
        raw = row.get("configuration_id") or row.get("job_id")
        identity = str(raw or "").strip()
        if (not _OPAQUE_RE.fullmatch(identity) or _PATH_RE.search(identity)
                or _SECRET_RE.search(identity)):
            raise ResearchExplorerError(
                "summary row lacks one unique opaque configuration identity")
        if identity in result:
            raise ResearchExplorerError(
                "summary contains duplicate configuration identities")
        result[identity] = row
    return result


def _matching_summary_row(member: Mapping[str, str], manifest: Mapping[str, Any],
                          rows: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    candidates = (
        str(manifest.get("job_id") or ""),
        str(manifest.get("job_uuid") or ""),
    )
    return next((rows[key] for key in candidates if key and key in rows), {})


def _energy_projection(manifest: Mapping[str, Any], summary_row: Mapping[str, Any],
                       summary: Mapping[str, Any]) -> dict[str, Any]:
    delta = _finite(summary_row.get("delta_e"))
    if delta is not None:
        reference_mode = _safe_label(
            summary_row.get("reference_mode") or summary.get("reference_mode"),
            maximum=32).lower()
        reference_species = _safe_label(
            summary_row.get("reference_species"), maximum=64)
        reference_source = _safe_label(
            summary_row.get("reference_source"), maximum=96)
        method_check = summary_row.get("method_check")
        method_check = method_check if isinstance(method_check, Mapping) else {}
        quantity = (
            "slab_difference" if reference_mode == "none"
            else "adsorption_energy"
        )
        contract: dict[str, Any] | None = None
        if (summary_row.get("reference_valid") is True
                and method_check.get("status") == "verified"
                and reference_source
                and ((reference_mode == "species" and reference_species)
                     or reference_mode == "none")):
            contract = {
                "schema": "vcstudio.energy-contract/v1",
                "quantity": quantity,
                "unit": "eV",
                "reference_mode": reference_mode,
                "reference_species": reference_species or None,
                "reference_source": reference_source,
                "formula": (
                    "E(config)-E(clean)-E(reference)"
                    if reference_mode == "species" else
                    "E(config)-E(clean)"
                ),
            }
        return {
            "energy_eV": delta,
            "energy_quantity": quantity,
            "energy_origin": "analysis",
            "energy_contract_id": (
                f"energy-{_digest(contract, 20)}" if contract else ""),
            "energy_contract_status": "verified" if contract else "unverified",
            "energy_reference_mode": reference_mode or "missing",
        }
    results = manifest.get("results")
    results = results if isinstance(results, Mapping) else {}
    fields = (
        ("energy_e0_eV", ("energy_e0_eV",)),
        ("energy_eV", ("energy_eV",)),
        ("final_energy_eV", ("final_energy_eV",)),
        ("energy.value_eV", ("energy", "value_eV")),
    )
    energy = None
    source_field = ""
    for label, field_path in fields:
        energy = _finite(_nested(results, field_path))
        if energy is not None:
            source_field = label
            break
    quantity = "total_energy" if energy is not None else "missing"
    contract = ({
        "schema": "vcstudio.energy-contract/v1",
        "quantity": quantity,
        "unit": "eV",
        "reference_mode": "absolute_electronic_total",
        "source_field": source_field,
    } if energy is not None else None)
    return {
        "energy_eV": energy,
        "energy_quantity": quantity,
        "energy_origin": "job_manifest" if energy is not None else "missing",
        "energy_contract_id": (
            f"energy-{_digest(contract, 20)}" if contract else ""),
        "energy_contract_status": "verified" if contract else "missing",
        "energy_reference_mode": (
            "absolute_electronic_total" if energy is not None else "missing"),
    }


def _manifest_total_energy(manifest: Mapping[str, Any]) -> tuple[float | None, str]:
    results = manifest.get("results")
    results = results if isinstance(results, Mapping) else {}
    for label, field_path in (
            ("energy_e0_eV", ("energy_e0_eV",)),
            ("energy_eV", ("energy_eV",)),
            ("final_energy_eV", ("final_energy_eV",)),
            ("energy.value_eV", ("energy", "value_eV"))):
        energy = _finite(_nested(results, field_path))
        if energy is not None:
            return energy, label
    return None, ""


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
    hashes = (
        results.get("fetched_sha256") or results.get("sha256")
        or results.get("hashes")
    )
    if has_numeric and hashes:
        return "observed"
    if has_numeric:
        return "inferred"
    return "observed"


def _provenance_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    inputs = manifest.get("inputs")
    inputs = inputs if isinstance(inputs, Mapping) else {}
    attempts = manifest.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    if len(attempts) > MAX_PROVENANCE_ATTEMPTS:
        raise ResearchIndexLimitError("manifest exceeds the provenance-attempt limit")
    return {
        "input_hash_bound": bool(inputs.get("sha256")),
        "attempts": [
            {
                "kind": _safe_label(
                    (attempt if isinstance(attempt, Mapping) else {}).get("kind")
                    or (attempt if isinstance(attempt, Mapping) else {}).get("action"),
                    fallback="resume", maximum=48),
            }
            for attempt in attempts
        ],
        "parser": _safe_label(_nested(
            manifest, ("results", "parser"), ("results", "parsed_by")),
            maximum=96),
    }


def _authority_projection(value: Any, *, job_id: str, source_id: str,
                          quantity_sha256: Mapping[str, str]
                          ) -> dict[str, Any]:
    value = value if isinstance(value, Mapping) else {}
    authority = str(value.get("authority") or "").lower()
    status = str(value.get("status") or "").lower()
    identity_bound = (
        str(value.get("job_id") or "") == job_id
        and str(value.get("source_id") or "") == source_id
    )
    base_verified = (
        authority in {"validation_result", "accepted_ledger"}
        and status == "verified"
        and value.get("hash_bound") is True
        and value.get("current") is True
        and value.get("output_hash_bound") is True
        and identity_bound
    )
    declared = value.get("verified_quantities")
    declared = declared if isinstance(declared, Mapping) else {}
    verified_quantities = {
        str(quantity): str(digest)
        for quantity, digest in declared.items()
        if (base_verified and quantity in quantity_sha256
            and str(digest) == quantity_sha256[quantity])
    }
    return {
        "authority": authority if authority in {
            "validation_result", "accepted_ledger"} else "missing",
        "status": "verified" if verified_quantities else "unverified",
        "hash_bound": bool(value.get("hash_bound")),
        "current": bool(value.get("current")),
        "output_hash_bound": bool(value.get("output_hash_bound")),
        "identity_bound": identity_bound,
        "verified_quantities": verified_quantities,
    }


def _evidence_level(manifest: Mapping[str, Any], method: Mapping[str, Any],
                    provenance: str, authority: Mapping[str, Any], *,
                    numeric_quantities: Sequence[str]) -> str:
    has_numeric = bool(numeric_quantities)
    verified_quantities = authority.get("verified_quantities")
    verified_quantities = (
        verified_quantities if isinstance(verified_quantities, Mapping) else {})
    if (has_numeric and manifest.get("state") == "DONE"
            and method.get("status") == "verified"
            and provenance == "observed"
            and all(quantity in verified_quantities
                    for quantity in numeric_quantities)):
        return "verified"
    if has_numeric and provenance == "observed":
        return "unverified"
    if has_numeric or manifest:
        return "diagnostic"
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
        "method_fingerprint_schema",
        "quantity_evidence",
        "provenance_status", "validation_status", "energy_eV", "energy_quantity",
        "energy_contract_id", "energy_contract_status", "energy_reference_mode",
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
        def project_digest(project: Any) -> str:
            if not isinstance(project, Mapping):
                return "unreadable"
            try:
                _bounded_payload_size(
                    project, label="project", maximum=MAX_PROJECT_BYTES)
                return hashlib.sha256(_canonical_bytes(project)).hexdigest()
            except (ResearchIndexLimitError, TypeError, ValueError, RecursionError):
                return "limit-exceeded"

        payload = {
            "registry_total": registry_total,
            "records": [
                {
                    "project_id": record.get("project_id"),
                    "identity": record.get("identity_fingerprint"),
                    "project_sha256": project_digest(record.get("project")),
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
                validation_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]
                                              ] | None = None,
                report_binding_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]
                                                  ] | None = None,
                registry_total: int | None = None,
                registry_state: str = "unknown",
                registry_failures: Sequence[Mapping[str, Any]] = (),
                source_version: Any = None) -> dict[str, Any]:
        # Authority reads and publication of the replacement snapshot are one
        # transaction.  Queries cannot combine entries from one rebuild with
        # freshness/cursors from another.
        with self._lock:
            return self._rebuild_locked(
                records,
                manifest_loader=manifest_loader,
                summary_loader=summary_loader,
                job_id_resolver=job_id_resolver,
                method_resolver=method_resolver,
                validation_resolver=validation_resolver,
                report_binding_resolver=report_binding_resolver,
                registry_total=registry_total,
                registry_state=registry_state,
                registry_failures=registry_failures,
                source_version=source_version,
            )

    def _rebuild_locked(self, records: Sequence[Mapping[str, Any]], *,
                        manifest_loader: Callable[[str], Mapping[str, Any] | None],
                        summary_loader: Callable[[Mapping[str, Any]], Mapping[str, Any]],
                        job_id_resolver: Callable[[str, Mapping[str, Any]], str],
                        method_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]],
                        validation_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]
                                                      ] | None,
                        report_binding_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]
                                                          ] | None,
                        registry_total: int | None,
                        registry_state: str,
                        registry_failures: Sequence[Mapping[str, Any]],
                        source_version: Any) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        failures = [
            {
                "project_ref": _safe_label(item.get("project_ref"), fallback="registry"),
                "code": _safe_label(item.get("code"), fallback="unavailable"),
            }
            for item in registry_failures
        ]
        existing_failure_keys = {
            (str(item.get("project_ref") or ""), str(item.get("code") or ""))
            for item in registry_failures
        }
        budget_candidates = authority_budget_failures(
            records, registry_total=registry_total)
        budget_failures = [
            item for item in budget_candidates
            if (item["project_ref"], item["code"]) not in existing_failure_keys
        ]
        failures.extend(budget_failures)
        registry_blocked = any(
            item["code"] == "registry_limit_exceeded" for item in budget_candidates)
        registry_blocked = registry_blocked or any(
            str(item.get("code") or "").startswith("source_")
            for item in registry_failures)
        blocked_projects = {
            item["project_ref"] for item in budget_candidates
            if item["code"] == "project_limit_exceeded"
        }
        indexed_projects = 0
        for record in (() if registry_blocked else records):
            project_id = _safe_opaque(
                record.get("project_id"), prefix="project", seed=record.get("project_id"))
            if project_id in blocked_projects:
                continue
            try:
                project = record.get("project")
                if not isinstance(project, Mapping):
                    raise ValueError("project authority is unreadable")
                summary = summary_loader(project)
                _bounded_payload_size(
                    summary, label="summary", maximum=MAX_SUMMARY_BYTES)
                rows = _summary_rows(summary)
                project_name = _safe_label(
                    project.get("name"), fallback=project_id, maximum=96)
                project_formula = _project_text(
                    project, "formula", "material_formula", "substrate_formula", "substrate")
                project_facet = _project_text(
                    project, "facet", "surface_facet", "miller_index")
                report = project.get("autopilot_report")
                report = copy.deepcopy(report) if isinstance(report, Mapping) else {}
                project_entries: list[dict[str, Any]] = []
                member_count = 0
                for member in _member_records(project):
                    if len(entries) + len(project_entries) >= MAX_TOTAL_ENTRIES:
                        raise ResearchIndexLimitError("index exceeds the entry limit")
                    _bounded_payload_size(
                        member, label="member", maximum=MAX_MEMBER_BYTES)
                    manifest = manifest_loader(member["path"])
                    if isinstance(manifest, Mapping):
                        _bounded_payload_size(
                            manifest, label="manifest", maximum=MAX_MANIFEST_BYTES)
                        manifest = copy.deepcopy(manifest)
                    else:
                        manifest = {}
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
                    energy = _energy_projection(manifest, summary_row, summary)
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
                    operand_energy, operand_energy_source = _manifest_total_energy(manifest)
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
                        "method_fingerprint_schema": method["schema"],
                        "engine": method["engine"],
                        "evidence_level": "missing",
                        "quantity_evidence": {},
                        "provenance_status": provenance,
                        "validation_status": _validation_status(manifest),
                        **energy,
                        "barrier_eV": barrier,
                        "attempt_count": len(attempts),
                        "_quantity_sha256": {},
                        "_manifest": manifest,
                        "_path": member["path"],
                        "_method": method,
                        "_validation_authority": {},
                        "_report": report,
                        "_report_binding": {},
                        "_path_key": member["path_key"],
                        "_summary_row": copy.deepcopy(dict(summary_row)),
                        "_reference_mode": _safe_label(
                            summary_row.get("reference_mode")
                            or (summary.get("reference_mode")
                                if isinstance(summary, Mapping) else ""),
                            maximum=32).lower(),
                        "_operand_energy_eV": operand_energy,
                        "_operand_energy_source": operand_energy_source,
                    }
                    project_entries.append(entry)
                    member_count += 1
                if member_count == 0:
                    # Empty projects are still indexed explicitly as missing evidence.
                    if len(entries) >= MAX_TOTAL_ENTRIES:
                        raise ResearchIndexLimitError("index exceeds the entry limit")
                    entries.append({
                        "project_id": project_id, "project_name": project_name,
                        "job_id": f"job-{_digest([project_id, 'missing'])}",
                        "source_id": f"source-{_digest([project_id, 'missing'])}",
                        "role": "missing", "formula": project_formula,
                        "elements": _formula_elements(project_formula)[1],
                        "facet": project_facet, "adsorbate": "", "task_type": "unknown",
                        "state": "MISSING", "method_fingerprint": "",
                        "method_status": "unverified",
                        "method_fingerprint_schema": "", "engine": "unknown",
                        "evidence_level": "missing", "provenance_status": "missing",
                        "quantity_evidence": {},
                        "validation_status": "missing", "energy_eV": None,
                        "energy_quantity": "missing", "energy_origin": "missing",
                        "energy_contract_id": "", "energy_contract_status": "missing",
                        "energy_reference_mode": "missing",
                        "barrier_eV": None, "attempt_count": 0,
                        "_quantity_sha256": {},
                        "_method": {}, "_provenance": {
                            "input_hash_bound": False, "attempts": [], "parser": ""},
                        "_validation_authority": {},
                        "_report_binding": {}, "_path_key": "",
                        "_summary_row": {}, "_reference_mode": "",
                        "_operand_energy_eV": None, "_operand_energy_source": "",
                    })
                else:
                    self._bind_project_operands(project_entries)
                    self._finalize_energy_contracts(project_entries)
                    for entry in project_entries:
                        manifest = entry["_manifest"]
                        quantity_sha256: dict[str, str] = {}
                        if entry["energy_eV"] is not None:
                            quantity_sha256["energy_eV"] = hashlib.sha256(
                                _canonical_bytes({
                                    "quantity": entry["energy_quantity"],
                                    "value": entry["energy_eV"],
                                    "unit": "eV",
                                    "energy_contract_id": entry["energy_contract_id"],
                                    "method_fingerprint": entry["method_fingerprint"],
                                    "engine": entry["engine"],
                                })).hexdigest()
                        if entry["barrier_eV"] is not None:
                            quantity_sha256["barrier_eV"] = hashlib.sha256(
                                _canonical_bytes({
                                    "quantity": "activation_barrier",
                                    "value": entry["barrier_eV"],
                                    "unit": "eV",
                                    "method_fingerprint": entry["method_fingerprint"],
                                    "engine": entry["engine"],
                                    "task_type": entry["task_type"],
                                })).hexdigest()
                        authority_raw: Mapping[str, Any] = {}
                        if validation_resolver is not None and manifest:
                            try:
                                authority_raw = validation_resolver({
                                    "project_id": project_id,
                                    "project": project,
                                    "record": record,
                                    "path": entry["_path"],
                                    "manifest": manifest,
                                    "job_id": entry["job_id"],
                                    "source_id": entry["source_id"],
                                    "summary": summary,
                                    "summary_row": entry["_summary_row"],
                                    "energy": {
                                        key: entry[key] for key in (
                                            "energy_eV", "energy_quantity",
                                            "energy_origin", "energy_contract_id",
                                            "energy_contract_status",
                                            "energy_reference_mode")
                                    },
                                    "barrier_eV": entry["barrier_eV"],
                                    "quantity_sha256": copy.deepcopy(quantity_sha256),
                                })
                            except Exception:  # noqa: BLE001 authority is unavailable
                                authority_raw = {}
                        authority = _authority_projection(
                            authority_raw, job_id=entry["job_id"],
                            source_id=entry["source_id"],
                            quantity_sha256=quantity_sha256)
                        entry["_quantity_sha256"] = quantity_sha256
                        entry["_validation_authority"] = authority
                        entry["quantity_evidence"] = {
                            quantity: (
                                "verified"
                                if quantity in authority["verified_quantities"] else
                                "unverified"
                                if entry["provenance_status"] == "observed" else
                                "diagnostic"
                            )
                            for quantity in quantity_sha256
                        }
                        entry["evidence_level"] = _evidence_level(
                            manifest, entry["_method"], entry["provenance_status"],
                            authority, numeric_quantities=tuple(quantity_sha256))
                        entry["_provenance"] = _provenance_projection(manifest)
                    if report_binding_resolver is not None:
                        try:
                            raw_binding = report_binding_resolver({
                                "project_id": project_id,
                                "project": project,
                                "record": record,
                                "report": report,
                                "summary": summary,
                                "entries": project_entries,
                            })
                        except Exception:  # noqa: BLE001 report lineage is absent
                            raw_binding = {}
                        binding = self._report_binding_projection(
                            raw_binding, project_entries)
                        for entry in project_entries:
                            entry["_report_binding"] = binding
                    for entry in project_entries:
                        entry.pop("_manifest", None)
                        entry.pop("_path", None)
                        entry.pop("_report", None)
                    entries.extend(project_entries)
                indexed_projects += 1
            except ResearchIndexLimitError:
                failures.append({
                    "project_ref": project_id,
                    "code": "project_limit_exceeded",
                })
            except Exception:  # noqa: BLE001 one project makes completeness partial
                failures.append({
                    "project_ref": project_id,
                    "code": "project_index_unavailable",
                })

        entries.sort(key=lambda item: (
            item["project_id"], item["job_id"], item["source_id"]))
        source_fingerprint = self.source_fingerprint(
            records, registry_total=registry_total,
            registry_failures=[*registry_failures, *budget_failures],
            source_version=source_version)
        snapshot_identity = {
            'source': source_fingerprint,
            'rows': [{key: value for key, value in entry.items() if not key.startswith('_')}
                     for entry in entries],
        }
        snapshot_id = f"index-{_digest(snapshot_identity)}"
        status = "partial" if failures else "ready"
        failure_total = len(failures)
        bounded_failures = []
        seen_failures = set()
        for item in failures:
            key = (str(item.get("project_ref") or "registry"),
                   str(item.get("code") or "unavailable"))
            if key in seen_failures:
                continue
            seen_failures.add(key)
            bounded_failures.append(item)
            if len(bounded_failures) >= MAX_PUBLIC_FAILURES:
                break
        snapshot = {
            "schema": INDEX_SCHEMA,
            "snapshot_id": snapshot_id,
            "source_fingerprint": source_fingerprint,
            "built_at": self._clock(),
            "built_monotonic": self._monotonic(),
            "status": status,
            "registry_total": registry_total,
            "registry_state": _safe_label(
                registry_state, fallback="unknown", maximum=32),
            "indexed_projects": indexed_projects,
            "indexed_jobs": len(entries),
            "failed_sources": failure_total,
            "failures": bounded_failures,
            "entries": entries,
        }
        # Every authority mapping was copied or projected while building.  The
        # snapshot is private and never handed to callers, so publishing this
        # immutable replacement does not duplicate the entire index.
        self._snapshot = snapshot
        return self._status_from_snapshot(self._snapshot, now=self._monotonic())

    def fail_closed(self, *, source_fingerprint: str, registry_total: int | None,
                    registry_state: str = "unknown",
                    failures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Publish a bounded non-scientific snapshot after an unstable read."""
        with self._lock:
            safe_failures = [{
                "project_ref": _safe_label(
                    item.get("project_ref"), fallback="registry", maximum=96),
                "code": _safe_label(
                    item.get("code"), fallback="source_unavailable", maximum=96),
            } for item in failures[:64]]
            snapshot_identity = {
                "source": source_fingerprint, "failures": safe_failures}
            snapshot = {
                "schema": INDEX_SCHEMA,
                "snapshot_id": f"index-{_digest(snapshot_identity)}",
                "source_fingerprint": source_fingerprint,
                "built_at": self._clock(),
                "built_monotonic": self._monotonic(),
                "status": "partial",
                "registry_total": registry_total,
                "registry_state": _safe_label(
                    registry_state, fallback="unknown", maximum=32),
                "indexed_projects": 0,
                "indexed_jobs": 0,
                "failed_sources": len(safe_failures),
                "failures": safe_failures,
                "entries": [],
            }
            self._snapshot = snapshot
            return self._status_from_snapshot(snapshot, now=self._monotonic())

    @staticmethod
    def _bind_project_operands(entries: list[dict[str, Any]]) -> None:
        by_path = {
            str(entry.get("_path_key") or ""): entry
            for entry in entries if entry.get("_path_key")
        }
        clean = next(
            (entry for entry in entries if entry.get("role") == "clean_slab"), None)
        gas = next(
            (entry for entry in entries if entry.get("role") == "gas_reference"), None)

        def operand(role: str, entry: Mapping[str, Any] | None) -> dict[str, Any]:
            return {
                "role": role,
                "status": "observed" if entry else "missing",
                "job_id": entry.get("job_id") if entry else None,
                "source_id": entry.get("source_id") if entry else None,
            }

        for config in entries:
            if (config.get("role") != "configuration"
                    or config.get("energy_origin") != "analysis"):
                config["_operands"] = []
                continue
            row = config.get("_summary_row")
            row = row if isinstance(row, Mapping) else {}
            mode = str(config.get("_reference_mode") or "")
            reference: Mapping[str, Any] | None = None
            raw_reference = row.get("reference_job")
            if raw_reference:
                reference = by_path.get(_path_key(raw_reference))
            if reference is None and mode == "species":
                wanted = str(row.get("reference_species") or "")
                reference = next((
                    entry for entry in entries
                    if entry.get("role") == "species_reference"
                    and str(entry.get("adsorbate") or "") == wanted
                ), None)
            if reference is None and mode == "single":
                reference = gas
            config["_operands"] = [
                operand("configuration", config),
                operand("clean_slab", clean),
                operand("reference", reference),
            ]

    @staticmethod
    def _finalize_energy_contracts(entries: list[dict[str, Any]]) -> None:
        """Prove single-reference arithmetic only after all operands are bound."""
        by_identity = {
            (str(entry.get("job_id") or ""), str(entry.get("source_id") or "")): entry
            for entry in entries
        }
        for config in entries:
            if (config.get("role") != "configuration"
                    or config.get("energy_origin") != "analysis"
                    or config.get("_reference_mode") != "single"):
                continue
            config["energy_contract_id"] = ""
            config["energy_contract_status"] = "unverified"
            row = config.get("_summary_row")
            row = row if isinstance(row, Mapping) else {}
            method_check = row.get("method_check")
            method_check = method_check if isinstance(method_check, Mapping) else {}
            operands = config.get("_operands")
            operands = operands if isinstance(operands, list) else []
            if (row.get("reference_valid") is not True
                    or method_check.get("status") != "verified"
                    or len(operands) != 3):
                continue
            roles = {str(item.get("role") or ""): item for item in operands}
            if set(roles) != {"configuration", "clean_slab", "reference"}:
                continue
            resolved: dict[str, Mapping[str, Any]] = {}
            valid = True
            for role, operand in roles.items():
                identity = (
                    str(operand.get("job_id") or ""),
                    str(operand.get("source_id") or ""),
                )
                entry = by_identity.get(identity)
                if (entry is None or entry.get("state") != "DONE"
                        or entry.get("method_status") != "verified"
                        or entry.get("provenance_status") != "observed"
                        or _finite(entry.get("_operand_energy_eV")) is None):
                    valid = False
                    break
                resolved[role] = entry
            if not valid or len({item["job_id"] for item in resolved.values()}) != 3:
                continue
            reference = resolved["reference"]
            reference_formula, _elements = _formula_elements(reference.get("formula"))
            adsorbate_formula, _ads_elements = _formula_elements(config.get("adsorbate"))
            reference_source = _safe_label(row.get("reference_source"), maximum=96)
            if (not reference_formula or not adsorbate_formula
                    or reference_formula != adsorbate_formula or not reference_source):
                continue
            expected = (
                float(resolved["configuration"]["_operand_energy_eV"])
                - float(resolved["clean_slab"]["_operand_energy_eV"])
                - float(reference["_operand_energy_eV"])
            )
            if not math.isclose(
                    expected, float(config["energy_eV"]), rel_tol=0.0, abs_tol=1e-8):
                continue
            reference_job = _path_key(row.get("reference_job")) \
                if row.get("reference_job") else ""
            if reference_job and reference_job != str(reference.get("_path_key") or ""):
                continue
            contract = {
                "schema": "vcstudio.energy-contract/single-gas/v1",
                "quantity": "adsorption_energy",
                "unit": "eV",
                "reference_mode": "single",
                "reference": {
                    "project_id": reference["project_id"],
                    "job_id": reference["job_id"],
                    "source_id": reference["source_id"],
                    "energy_quantity": "total_energy",
                    "source": reference_source,
                    "formula": reference_formula,
                    "coefficient": 1,
                },
                "formula": "E(config)-E(clean)-1*E(gas_reference)",
            }
            config["energy_contract_id"] = f"energy-{_digest(contract, 20)}"
            config["energy_contract_status"] = "verified"

    @staticmethod
    def _report_binding_projection(value: Any,
                                   entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        value = value if isinstance(value, Mapping) else {}
        revision = _safe_label(value.get("revision_id"), maximum=128)
        if (not revision or value.get("current") is not True
                or value.get("frozen_graph_revalidated") is not True):
            return {}
        bound_jobs = {
            str(item) for item in value.get("bound_job_ids") or []
            if _OPAQUE_RE.fullmatch(str(item)) and not _PATH_RE.search(str(item))
        }
        valid: list[dict[str, str]] = []
        for raw in value.get("bound_analyses") or []:
            if not isinstance(raw, Mapping):
                continue
            quantity = str(raw.get("quantity") or "")
            if quantity not in {"energy_eV", "barrier_eV"}:
                continue
            match = next((entry for entry in entries if (
                str(entry.get("job_id") or "") == str(raw.get("job_id") or "")
                and str(entry.get("source_id") or "") == str(raw.get("source_id") or "")
                and (quantity != "energy_eV"
                     or str(entry.get("energy_contract_id") or "")
                     == str(raw.get("energy_contract_id") or ""))
                and str((entry.get("_quantity_sha256") or {}).get(quantity) or "")
                == str(raw.get("quantity_sha256") or "")
            )), None)
            if match is None:
                continue
            operand_jobs = {
                str(item.get("job_id"))
                for item in match.get("_operands") or []
                if isinstance(item, Mapping) and item.get("job_id")
            }
            operand_roles = {
                str(item.get("role") or "")
                for item in match.get("_operands") or []
                if isinstance(item, Mapping) and item.get("job_id")
            }
            if (operand_roles != {"configuration", "clean_slab", "reference"}
                    or len(operand_jobs) != 3
                    or str(match["job_id"]) not in bound_jobs
                    or not operand_jobs.issubset(bound_jobs)):
                continue
            valid.append({
                "job_id": str(match["job_id"]),
                "source_id": str(match["source_id"]),
                "quantity": quantity,
                "energy_contract_id": (
                    str(match.get("energy_contract_id") or "")
                    if quantity == "energy_eV" else ""),
                "quantity_sha256": str(
                    (match.get("_quantity_sha256") or {}).get(quantity) or ""),
            })
        if not valid:
            return {}
        return {
            "revision_id": revision,
            "current": True,
            "frozen_graph_revalidated": True,
            "bound_job_ids": sorted(bound_jobs),
            "bound_analyses": valid,
        }

    def _status_from_snapshot(self, snapshot: Mapping[str, Any] | None, *,
                              now: float) -> dict[str, Any]:
        if snapshot is None:
            return {
                "schema": INDEX_SCHEMA, "status": "unavailable", "snapshot_id": None,
                "built_at": None, "age_seconds": None,
                "max_age_seconds": self.max_age_seconds,
                "registry_total": None, "indexed_projects": 0, "indexed_jobs": 0,
                "registry_state": "unknown",
                "failed_sources": None, "failures": [], "rebuildable": True,
            }
        age = max(0.0, now - float(snapshot["built_monotonic"]))
        status = "stale" if age > self.max_age_seconds else snapshot["status"]
        return {
            key: copy.deepcopy(snapshot[key])
            for key in (
                "schema", "snapshot_id", "built_at", "registry_total",
                "registry_state",
                "indexed_projects", "indexed_jobs", "failed_sources", "failures",
                "source_fingerprint")
        } | {
            "status": status,
            "age_seconds": round(age, 6),
            "max_age_seconds": self.max_age_seconds,
            "rebuildable": True,
        }

    def index_status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_from_snapshot(
                self._snapshot, now=self._monotonic())

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
                        filters: Mapping[str, Any]) -> list[Mapping[str, Any]]:
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

        return [entry for entry in entries if allowed(entry)]

    @staticmethod
    def _method_cohort(entries: Sequence[Mapping[str, Any]], *, enabled: bool
                       ) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
        counts = Counter(
            str(entry.get("method_fingerprint") or "") for entry in entries
            if (entry.get("method_fingerprint")
                and entry.get("method_status") == "verified"))
        selected = None
        output = list(entries)
        missing_fingerprints = sum(
            1 for entry in entries if not entry.get("method_fingerprint"))
        nonverified = sum(
            1 for entry in entries if entry.get("method_status") != "verified")
        if enabled:
            if counts:
                selected = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
                output = [
                    entry for entry in entries
                    if (entry.get("method_fingerprint") == selected
                        and entry.get("method_status") == "verified")
                ]
            else:
                # Compatibility cannot be proven from an absent fingerprint.
                # With the default guard on, scientific projections fail closed.
                output = []
        status = (
            "compatible" if selected else
            "blocked_missing_method" if enabled else
            "mixed_or_unverified" if len(counts) > 1 or nonverified
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
            "nonverified_rows": nonverified,
        }

    @staticmethod
    def _energy_cohort(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        numeric = [entry for entry in entries if _finite(entry.get("energy_eV")) is not None]
        counts = Counter(
            str(entry.get("energy_contract_id") or "") for entry in numeric
            if (entry.get("energy_contract_status") == "verified"
                and entry.get("energy_contract_id"))
        )
        missing = len(numeric) - sum(counts.values())
        status = (
            "missing" if not numeric else
            "compatible" if len(counts) == 1 and missing == 0 else
            "unavailable_incompatible_energy_contract"
        )
        selected = next(iter(counts)) if status == "compatible" else None
        return {
            "status": status,
            "selected_energy_contract_id": selected,
            "cohorts": [
                {"energy_contract_id": key, "sample_count": value}
                for key, value in sorted(counts.items())
            ],
            "numeric_rows": len(numeric),
            "unverified_contract_rows": missing,
        }

    @staticmethod
    def _sort(entries: Sequence[Mapping[str, Any]], sort: Mapping[str, str]
              ) -> list[Mapping[str, Any]]:
        field = _SORT_FIELDS[sort["key"]]
        present = [item for item in entries if item.get(field) not in (None, "")]
        missing = [item for item in entries if item.get(field) in (None, "")]
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
    def _histogram(entries: Sequence[Mapping[str, Any]], metric: str, *,
                   method_ready: bool, energy: Mapping[str, Any],
                   bins: int = 12) -> dict[str, Any]:
        if not method_ready:
            return {
                "status": "unavailable_method_compatibility", "metric": metric,
                "unit": _NUMERIC_FIELDS[metric]["unit"], "bins": [],
                "sample_count": 0, "missing_count": len(entries),
            }
        if (metric == "energy_eV"
                and energy.get("status") == "unavailable_incompatible_energy_contract"):
            return {
                "status": "unavailable_incompatible_energy_contract", "metric": metric,
                "unit": _NUMERIC_FIELDS[metric]["unit"], "bins": [],
                "sample_count": 0, "missing_count": len(entries),
            }
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
                 *, method_ready: bool, energy: Mapping[str, Any]) -> dict[str, Any]:
        if not method_ready:
            return {
                "status": "blocked_mixed_or_unverified_methods",
                "axes": copy.deepcopy(dict(axes)),
                "points": [], "sample_count": 0, "missing_count": len(entries),
                "reason": (
                    "Select one verified method fingerprint before plotting a scatter view."),
            }
        x_axis, y_axis = axes["x"], axes["y"]
        if ("energy_eV" in {x_axis, y_axis}
                and energy.get("status") == "unavailable_incompatible_energy_contract"):
            return {
                "status": "unavailable_incompatible_energy_contract",
                "axes": copy.deepcopy(dict(axes)), "points": [],
                "sample_count": 0, "missing_count": len(entries),
                "units": {x_axis: _NUMERIC_FIELDS[x_axis]["unit"],
                          y_axis: _NUMERIC_FIELDS[y_axis]["unit"]},
                "reason": "Energy quantity/reference contracts are not one proven cohort.",
            }
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

        projected = complete[:MAX_SCATTER_POINTS]
        points = [{
            "project_id": item[0]["project_id"], "job_id": item[0]["job_id"],
            "source_id": item[0]["source_id"], "label": item[0]["project_name"],
            "x": item[1], "y": item[2],
            "x_display": _display(item[1]), "y_display": _display(item[2]),
            "x_percent": percent(item[1], x_min, x_max),
            "y_percent": percent(item[2], y_min, y_max),
            "top_percent": 100.0 - percent(item[2], y_min, y_max),
            "method_fingerprint": item[0].get("method_fingerprint") or "",
        } for item in projected]
        return {
            "status": "ready", "axes": copy.deepcopy(dict(axes)), "points": points,
            "sample_count": len(complete), "visible_count": len(points),
            "truncated": len(complete) > len(points),
            "point_limit": MAX_SCATTER_POINTS,
            "missing_count": len(entries) - len(complete),
            "units": {x_axis: _NUMERIC_FIELDS[x_axis]["unit"],
                      y_axis: _NUMERIC_FIELDS[y_axis]["unit"]},
            "ranges": {x_axis: {"min": x_min, "max": x_max},
                       y_axis: {"min": y_min, "max": y_max}},
        }

    @staticmethod
    def _periodic(entries: Sequence[Mapping[str, Any]], *, method_ready: bool,
                  energy: Mapping[str, Any]) -> dict[str, Any]:
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
            energy_ready = (
                method_ready and energy.get("status") in {"compatible", "missing"})
            mean = sum(energies) / len(energies) if energies and energy_ready else None
            cells.append({
                "element": element, **_PERIODIC_POSITION[element],
                "sample_count": len(rows),
                "project_count": len({row["project_id"] for row in rows}),
                "numeric_energy_count": len(energies),
                "mean_energy_eV": mean,
                "mean_energy_display": _display(mean),
                "unit": "eV",
            })
        if not method_ready:
            status = "unavailable_method_compatibility"
        elif energy.get("status") == "unavailable_incompatible_energy_contract":
            status = "unavailable_incompatible_energy_contract"
        else:
            status = "ready" if cells else "missing"
        return {
            "status": status, "cells": cells,
            "sample_count": len(entries), "element_count": len(cells),
            "missing_element_rows": sum(1 for item in entries if not item.get("elements")),
        }

    def query(self, request: Any = None) -> dict[str, Any]:
        normalized = self._normalize_request(request)
        with self._lock:
            snapshot = self._snapshot
            freshness = self._status_from_snapshot(
                self._snapshot, now=self._monotonic())
        if snapshot is None or freshness["status"] in {"stale", "partial", "unavailable"}:
            status = freshness["status"]
            return {
                "ok": False, "schema": QUERY_SCHEMA, "status": status,
                "request": normalized, "freshness": freshness,
                "method_compatibility": None,
                "energy_compatibility": None,
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
        method_ready = (
            compatibility["status"] == "compatible"
            or (compatibility["status"] == "explicitly_unfiltered"
                and len(compatibility["cohorts"]) == 1
                and compatibility["nonverified_rows"] == 0)
        )
        energy_compatibility = self._energy_cohort(compatible)
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
            "energy_compatibility": energy_compatibility,
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
            "histogram": self._histogram(
                compatible, histogram_metric, method_ready=method_ready,
                energy=energy_compatibility),
            "scatter": self._scatter(
                compatible, normalized["axes"], method_ready=method_ready,
                energy=energy_compatibility),
            "periodic_table": self._periodic(
                compatible, method_ready=method_ready, energy=energy_compatibility),
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
            snapshot = self._snapshot
            freshness = self._status_from_snapshot(
                self._snapshot, now=self._monotonic())
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
        graph_entries = list(selected)
        selected_keys = {
            (entry["job_id"], entry["source_id"]) for entry in graph_entries
        }
        for entry in selected:
            for operand in entry.get("_operands") or []:
                if not isinstance(operand, Mapping) or not operand.get("job_id"):
                    continue
                match = next((candidate for candidate in snapshot["entries"] if (
                    candidate["project_id"] == project
                    and candidate["job_id"] == operand["job_id"]
                    and candidate["source_id"] == operand["source_id"]
                )), None)
                if match is not None and (match["job_id"], match["source_id"]) not in selected_keys:
                    graph_entries.append(match)
                    selected_keys.add((match["job_id"], match["source_id"]))
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []

        def edge(source: str, target: str, relation: str, layer: str,
                 **extra: Any) -> None:
            edges.append({
                "source": source, "target": target, "type": relation, "layer": layer,
                **extra,
            })

        project_node = f"project:{project}"
        nodes.append(self._node(
            project_node, "project", "data", "observed",
            selected[0]["project_name"], {"project_id": project}))
        for entry in graph_entries:
            provenance = entry.get("_provenance")
            provenance = provenance if isinstance(provenance, Mapping) else {}
            input_node = f"input:{entry['source_id']}"
            input_hash_bound = provenance.get("input_hash_bound") is True
            input_status = entry["provenance_status"] if input_hash_bound else "missing"
            nodes.append(self._node(
                input_node, "input", "data", input_status, entry["source_id"],
                {"source_id": entry["source_id"], "hash_bound": input_hash_bound}))
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

            attempts = provenance.get("attempts")
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
            parser_name = _safe_label(
                provenance.get("parser"), fallback="registered parser", maximum=96)
            parser_status = (
                "observed" if provenance.get("parser") else
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

            analysis_records = []
            if entry["energy_eV"] is not None:
                analysis_records.append((
                    "energy_eV", entry["energy_quantity"], {
                        "quantity": "energy_eV",
                        "energy_quantity": entry["energy_quantity"],
                        "energy_eV": entry["energy_eV"],
                        "energy_contract_id": entry["energy_contract_id"],
                        "unit": "eV",
                        "evidence_level": (
                            entry.get("quantity_evidence") or {}).get(
                                "energy_eV", "unverified"),
                    }))
            if entry["barrier_eV"] is not None:
                analysis_records.append((
                    "barrier_eV", "activation_barrier", {
                        "quantity": "barrier_eV",
                        "barrier_eV": entry["barrier_eV"],
                        "unit": "eV",
                        "evidence_level": (
                            entry.get("quantity_evidence") or {}).get(
                                "barrier_eV", "unverified"),
                    }))
            if not analysis_records:
                analysis_records.append((
                    "missing", "missing analysis", {
                        "quantity": None, "evidence_level": "missing",
                    }))
            analysis_ids = []
            for quantity, label, record in analysis_records:
                analysis_id = f"analysis:{entry['job_id']}:{quantity}"
                analysis_ids.append(analysis_id)
                nodes.append(self._node(
                    analysis_id, "analysis", "logical",
                    entry["provenance_status"] if quantity != "missing" else "missing",
                    label, record))
                edge(parser_id, analysis_id, "analyzed_as", "logical")

            method_id = f"method:{entry['method_fingerprint'] or entry['job_id']}"
            nodes.append(self._node(
                method_id, "method", "logical",
                ("observed" if entry["method_status"] == "verified" else
                 "inferred" if entry["method_fingerprint"] else "missing"),
                entry["method_fingerprint"] or "missing method fingerprint",
                {"method_fingerprint": entry["method_fingerprint"],
                 "method_status": entry["method_status"], "engine": entry["engine"]}))
            validation_id = f"validation:{entry['job_id']}"
            authority = entry.get("_validation_authority")
            authority = authority if isinstance(authority, Mapping) else {}
            validation_origin = (
                "observed" if authority.get("status") == "verified" else
                "inferred" if entry["validation_status"] != "missing" else "missing")
            nodes.append(self._node(
                validation_id, "validation", "logical", validation_origin,
                entry["validation_status"],
                {"status": entry["validation_status"],
                 "evidence_level": entry["evidence_level"],
                 "quantity_evidence": entry.get("quantity_evidence") or {},
                 "authority": authority.get("authority") or "missing",
                 "authority_status": authority.get("status") or "unverified"}))
            for analysis_id in analysis_ids:
                edge(method_id, analysis_id, "governs", "logical")
                edge(validation_id, analysis_id, "qualifies", "logical")

        for entry in selected:
            if (entry.get("role") != "configuration"
                    or entry.get("energy_origin") != "analysis"):
                continue
            analysis_id = f"analysis:{entry['job_id']}:energy_eV"
            for operand in entry.get("_operands") or []:
                operand = operand if isinstance(operand, Mapping) else {}
                role = str(operand.get("role") or "reference")
                if operand.get("job_id"):
                    operand_id = f"job:{operand['job_id']}"
                else:
                    operand_id = f"operand-missing:{entry['job_id']}:{role}"
                    nodes.append(self._node(
                        operand_id, "input", "data", "missing",
                        f"missing {role}", {"operand_role": role, "job_id": None}))
                    missing.append({
                        "from": analysis_id, "expected": f"{role} operand",
                        "reason": f"adsorption analysis has no bound {role} job",
                    })
                edge(
                    operand_id, analysis_id, "uses_operand", "logical",
                    operand_role=role)

        report_id = f"report-live:{project}"
        bindings = [
            entry.get("_report_binding") for entry in selected
            if isinstance(entry.get("_report_binding"), Mapping)
        ]
        binding = next((item for item in bindings if item), {})
        revision = str(binding.get("revision_id") or "")
        nodes.append(self._node(
            report_id, "report", "logical", "observed" if binding else "missing",
            revision or "no revalidated report binding",
            {"revision_id": revision or None,
             "binding_status": "verified" if binding else "unverified",
             "frozen_graph_separate": True,
             "frozen_graph_endpoint": "report_evidence_graph"}))
        bound_analyses = {
            (str(item.get("job_id") or ""), str(item.get("source_id") or ""),
             str(item.get("quantity") or ""),
             str(item.get("energy_contract_id") or ""),
             str(item.get("quantity_sha256") or ""))
            for item in binding.get("bound_analyses") or []
            if isinstance(item, Mapping)
        }
        reported: set[tuple[str, str]] = set()
        for entry in selected:
            for quantity, digest in (entry.get("_quantity_sha256") or {}).items():
                key = (
                    str(entry["job_id"]), str(entry["source_id"]), quantity,
                    str(entry.get("energy_contract_id") or "")
                    if quantity == "energy_eV" else "",
                    str(digest),
                )
                if key in bound_analyses:
                    edge(
                        f"analysis:{entry['job_id']}:{quantity}", report_id,
                        "reported_in", "logical", quantity=quantity)
                    reported.add((str(entry["job_id"]), quantity))
        for entry in selected:
            for quantity in (entry.get("_quantity_sha256") or {"missing": ""}):
                if (str(entry["job_id"]), quantity) in reported:
                    continue
                missing.append({
                    "from": f"analysis:{entry['job_id']}:{quantity}",
                    "expected": (
                        "revalidated frozen report analysis/evidence binding"),
                    "reason": (
                        f"no current frozen revision proves {quantity} for this live analysis"),
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
