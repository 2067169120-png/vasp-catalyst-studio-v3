"""Read-only scientific insight views over frozen report revisions.

The mutable project tree is never a source for this module.  Every operation
starts from an authoritative :class:`ReportService` history entry, revalidates
the complete on-disk bundle through the service host, and only then reads the
frozen contracts/model/manifest.  Public projections deliberately omit paths.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import secrets
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from vcstudio import __version__
from vcstudio.project.report_service import (
    _exclusive_file_lock,
    _history_paths,
    _read_history,
    _validated_history_bundle,
)


DIFF_SCHEMA = "vcstudio.report-scientific-diff/v1"
GRAPH_SCHEMA = "vcstudio.report-evidence-graph/v1"
CAPSULE_SCHEMA = "vcstudio.report-si-capsule/v1"
DESTINATION_SCHEMA = "vcstudio.report-capsule-destination/v1"

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ABS_WINDOWS = re.compile(r"(?i)(?:^|[\s\"'])(?:[a-z]:[\\/]|\\\\)")
_ABS_POSIX = re.compile(r"(?:^|[\s\"'])/(?!/)")
_FILE_URI = re.compile(r"(?i)\bfile:(?:/{0,3}|\\)")
_SECRET_VALUE = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+\S+"
    r"|\b(?:token|secret|password|api[_-]?key|authorization)\s*[:=]\s*[\"']?[^\s,\"'}]+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|https?://[^\s/:]+:[^\s/@]+@)"
)
_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "credential", "credentials",
    "authorization", "cookie", "cookies", "private_key", "api_key",
})
_PATH_KEYS = frozenset({
    "path", "root", "directory", "dir", "locator", "destination",
    "out_dir", "temp_root", "manifest_path", "recovery_path",
})


class StaleRevisionError(RuntimeError):
    """A selected history revision failed byte/hash/contract revalidation."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_path_or_secret(value: str) -> bool:
    return bool(
        _ABS_WINDOWS.search(value)
        or _ABS_POSIX.search(value)
        or _FILE_URI.search(value)
        or _SECRET_VALUE.search(value)
    )


def redact(value: Any, *, key: str = "") -> Any:
    """Return a JSON-safe, deterministic value with paths/secrets removed."""

    normalized_key = str(key).strip().lower().replace("-", "_")
    if normalized_key in _SENSITIVE_KEYS:
        return "[redacted-secret]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if normalized_key in _PATH_KEYS or normalized_key.endswith("_path"):
            return "[redacted-local-path]"
        return "[redacted-sensitive-value]" if _is_path_or_secret(value) else value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def _json_file(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError("frozen report member must be a JSON object")
    return value


@dataclass(frozen=True)
class FrozenRevision:
    project_id: str
    revision_id: str
    entry: dict[str, Any]
    manifest: dict[str, Any]
    spec: dict[str, Any]
    snapshot: dict[str, Any]
    validation: dict[str, Any]
    model: dict[str, Any]
    file_hashes: dict[str, str]

    def public_identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "report_id": self.entry["report_id"],
            "revision_id": self.revision_id,
            "sequence": self.entry["sequence"],
            "manifest_sha256": self.entry["manifest_sha256"],
        }


def load_frozen_revision(service: Any, path: str, revision_id: str) -> FrozenRevision:
    """Select and revalidate one exact ReportService history revision."""

    wanted = str(revision_id or "").strip()
    if not _TOKEN.fullmatch(wanted):
        raise ValueError("revision_id is invalid")
    context = service._project_context(str(path or "").strip())
    history_path, lock_path = _history_paths(context["project_root"])
    if not history_path.is_file():
        raise LookupError("report revision history is unavailable")
    with _exclusive_file_lock(lock_path):
        history = _read_history(history_path, context["project_id"])
        selected: tuple[dict[str, Any], Mapping[str, Any]] | None = None
        for lineage in history["reports"].values():
            for raw_entry in lineage.get("revisions") or []:
                if str(raw_entry.get("revision_id") or "") == wanted:
                    if selected is not None:
                        raise RuntimeError("revision_id is not unique in report history")
                    selected = (copy.deepcopy(dict(raw_entry)), lineage)
        if selected is None:
            raise LookupError("revision_id is not present in authoritative report history")
        entry, lineage = selected
        try:
            _validated_history_bundle(entry, lineage)
            audit = service._host._report_workbench_validate_history_entry(
                copy.deepcopy(entry))
            if not isinstance(audit, Mapping) or audit.get("ok") is not True:
                raise RuntimeError(
                    str((audit or {}).get("error") or "revision bundle audit failed"))
            if audit.get("current") is not True:
                raise RuntimeError("revision bundle is stale")
            for field in ("scientific_status", "scientific_qualification"):
                if str(audit.get(field) or "") != str(entry.get(field) or ""):
                    raise RuntimeError(f"revision {field} binding mismatch")
        except Exception as exc:
            raise StaleRevisionError(str(exc)) from exc

        manifest = _json_file(str(entry["manifest"]))
        spec = _json_file(str(entry["contract_files"]["spec"]))
        snapshot = _json_file(str(entry["contract_files"]["snapshot"]))
        validation = _json_file(str(entry["contract_files"]["validation"]))
        model = _json_file(str(entry["model_file"]))
        file_hashes = {
            "manifest": _sha256_file(entry["manifest"]),
            "model": _sha256_file(entry["model_file"]),
            **{
                f"contract:{name}": _sha256_file(member)
                for name, member in sorted(entry["contract_files"].items())
            },
            **{
                f"artifact:{name}": _sha256_file(member)
                for name, member in sorted(entry["files"].items())
            },
        }
    return FrozenRevision(
        project_id=context["project_id"], revision_id=wanted, entry=entry,
        manifest=manifest, spec=spec, snapshot=snapshot, validation=validation,
        model=model, file_hashes=file_hashes,
    )


def _flatten_numeric(value: Any, prefix: str = "") -> dict[str, int | float]:
    found: dict[str, int | float] = {}
    if isinstance(value, Mapping):
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            path = f"{prefix}.{key}" if prefix else str(key)
            found.update(_flatten_numeric(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(_flatten_numeric(item, f"{prefix}[{index}]"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not isinstance(value, float) or math.isfinite(value):
            found[prefix] = value
    return found


def _changed(left: Any, right: Any) -> dict[str, Any]:
    return {"changed": left != right, "left": redact(left), "right": redact(right)}


def _map_diff(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            rows.append({"key": key, "left": redact(left.get(key)), "right": redact(right.get(key))})
    return rows


def _flatten_values(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a frozen scientific structure without inventing comparison semantics."""
    found: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            path = f"{prefix}.{key}" if prefix else str(key)
            found.update(_flatten_values(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(_flatten_values(item, f"{prefix}[{index}]"))
    elif prefix:
        found[prefix] = redact(value, key=prefix.rsplit(".", 1)[-1])
    return found


def _method_matrix(bundle: FrozenRevision) -> dict[str, Any]:
    matrix: dict[str, Any] = {}
    for source_name, source in (
        ("model", bundle.model),
        ("snapshot", bundle.snapshot.get("payload") or {}),
    ):
        if not isinstance(source, Mapping):
            continue
        for key in (
            "methods", "methodology", "calculation_details", "method_consistency",
            "method_evidence", "method_status",
        ):
            if key in source:
                matrix.update(_flatten_values(source[key], f"{source_name}.{key}"))
    return matrix


def _figure_records(bundle: FrozenRevision) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    candidates = []
    for source in (bundle.model, bundle.manifest):
        for key in ("figures", "assets"):
            raw = source.get(key)
            if isinstance(raw, list):
                candidates.extend(raw)
            elif isinstance(raw, Mapping):
                candidates.extend({"id": name, **(dict(item) if isinstance(item, Mapping) else {"value": item})}
                                  for name, item in raw.items())
    for index, raw in enumerate(candidates):
        item = dict(raw) if isinstance(raw, Mapping) else {"value": raw}
        records.append(redact({
            "id": item.get("id") or item.get("figure_id") or item.get("name") or f"figure-{index + 1}",
            "sha256": item.get("sha256") or item.get("file_sha256"),
            "caption": item.get("caption") or item.get("title"),
            "source_refs": item.get("source_refs") or item.get("sources") or [],
        }))
    return sorted(records, key=lambda item: str(item.get("id")))


def scientific_diff(service: Any, path: str, left_revision_id: str,
                    right_revision_id: str) -> dict[str, Any]:
    if str(left_revision_id) == str(right_revision_id):
        raise ValueError("two different revision IDs are required")
    left = load_frozen_revision(service, path, left_revision_id)
    right = load_frozen_revision(service, path, right_revision_id)
    if left.project_id != right.project_id:
        raise RuntimeError("revisions are bound to different projects")
    numeric_left = _flatten_numeric(left.model)
    numeric_right = _flatten_numeric(right.model)
    figures_left = {str(item["id"]): item for item in _figure_records(left)}
    figures_right = {str(item["id"]): item for item in _figure_records(right)}
    methods_left = _method_matrix(left)
    methods_right = _method_matrix(right)
    result = {
        "schema": DIFF_SCHEMA,
        "ok": True,
        "status": "ready",
        "project_id": left.project_id,
        "left": left.public_identity(),
        "right": right.public_identity(),
        "scope": _changed(left.spec.get("scope"), right.spec.get("scope")),
        "hashes": _map_diff({
            "spec_sha256": left.entry["spec_sha256"],
            "snapshot_sha256": left.entry["snapshot_sha256"],
            "input_fingerprint": left.snapshot.get("input_fingerprint"),
            "report_model_sha256": left.entry["report_model_sha256"],
            **left.file_hashes,
        }, {
            "spec_sha256": right.entry["spec_sha256"],
            "snapshot_sha256": right.entry["snapshot_sha256"],
            "input_fingerprint": right.snapshot.get("input_fingerprint"),
            "report_model_sha256": right.entry["report_model_sha256"],
            **right.file_hashes,
        }),
        "validation": {
            "status": _changed(left.validation.get("status"), right.validation.get("status")),
            "checks": _changed(left.validation.get("checks") or [], right.validation.get("checks") or []),
            "blocking": _changed(left.validation.get("blocking") or [], right.validation.get("blocking") or []),
        },
        "scientific": {
            "status": _changed(left.entry["scientific_status"], right.entry["scientific_status"]),
            "qualification": _changed(left.entry["scientific_qualification"], right.entry["scientific_qualification"]),
            "claim_ceiling": _changed(left.validation.get("claim_ceiling"), right.validation.get("claim_ceiling")),
            "claims": _changed(left.validation.get("claims") or left.model.get("claims") or [],
                               right.validation.get("claims") or right.model.get("claims") or []),
        },
        "numeric_values": _map_diff(numeric_left, numeric_right),
        "method_matrix": _map_diff(methods_left, methods_right),
        "figures": _map_diff(figures_left, figures_right),
        "files": _map_diff(left.file_hashes, right.file_hashes),
        "error": None,
    }
    result["changed"] = any((
        result["scope"]["changed"], bool(result["hashes"]),
        any(item["changed"] for item in result["validation"].values()),
        any(item["changed"] for item in result["scientific"].values()),
        bool(result["numeric_values"]), bool(result["method_matrix"]),
        bool(result["figures"]), bool(result["files"]),
    ))
    return result


def _record_id(prefix: str, value: Any) -> str:
    return f"{prefix}:{hashlib.sha256(_canonical_bytes(redact(value))).hexdigest()[:20]}"


def _safe_graph_text(value: Any, *, fallback: str) -> str:
    safe = redact(str(value or ""))
    return str(safe or fallback)


def evidence_graph(service: Any, path: str, revision_id: str) -> dict[str, Any]:
    bundle = load_frozen_revision(service, path, revision_id)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    seen: set[str] = set()
    reference_nodes: dict[str, str] = {}
    pending_evidence: list[tuple[str, Mapping[str, Any], bool]] = []

    def node(kind: str, value: Any, *, node_id: str | None = None, label: str = "") -> str:
        identifier = node_id or _record_id(kind, value)
        if _is_path_or_secret(identifier):
            identifier = _record_id(kind, value)
        if identifier not in seen:
            seen.add(identifier)
            nodes.append({
                "id": identifier,
                "type": kind,
                "label": _safe_graph_text(label, fallback=identifier),
                "record": redact(value),
                "route": {
                    "revision": "publish-versions",
                    "file_hash": "publish-export",
                    "source": "project-members",
                    "validation_check": "publish-versions",
                    "snapshot_record": "publish-versions",
                    "job": "run-jobs",
                    "table": "publish-report",
                    "figure": "publish-figures",
                    "conclusion": "publish-report",
                }.get(kind),
            })
        return identifier

    def edge(source: str, target: str, relation: str) -> None:
        edges.append({"source": source, "target": target, "relation": relation})

    def register_references(identifier: str, record: Mapping[str, Any], kind: str) -> None:
        keys = [record.get("id"), record.get(f"{kind}_id"), record.get("source_id")]
        for raw in keys:
            if raw not in (None, ""):
                value = str(raw)
                reference_nodes[value] = identifier
                reference_nodes[f"{kind}:{value}"] = identifier

    def frozen_refs(record: Mapping[str, Any]) -> list[str]:
        refs: list[str] = []
        for field in (
            "source_refs", "sources", "evidence_refs", "job_refs",
            "check_refs", "table_refs", "figure_refs", "file_refs",
        ):
            raw = record.get(field)
            if isinstance(raw, str):
                refs.append(raw)
            elif isinstance(raw, list):
                refs.extend(str(item) for item in raw)
        return list(dict.fromkeys(refs))

    def link_frozen_evidence(origin: str, record: Mapping[str, Any],
                             *, missing_if_empty: bool) -> None:
        refs = frozen_refs(record)
        for key in refs:
            target = reference_nodes.get(key)
            if target:
                edge(origin, target, "supported_by")
            else:
                missing.append({
                    "from": origin, "relation": "supported_by",
                    "expected": _safe_graph_text(key, fallback="evidence_reference"),
                    "reason": "frozen evidence reference is unresolved",
                })
        if not refs and missing_if_empty:
            missing.append({
                "from": origin, "relation": "supported_by",
                "expected": "source/job/check/table/figure/file_hash",
                "reason": "claim has no frozen evidence reference",
            })

    revision_node = node("revision", bundle.public_identity(), node_id=f"revision:{bundle.revision_id}",
                         label=bundle.revision_id)
    file_nodes = {}
    for name, digest in sorted(bundle.file_hashes.items()):
        file_nodes[name] = node("file_hash", {"name": name, "sha256": digest},
                                node_id=f"file:{name}:{digest}", label=name)
        reference_nodes[name] = file_nodes[name]
        reference_nodes[f"file:{name}"] = file_nodes[name]
        reference_nodes[digest] = file_nodes[name]
        edge(revision_node, file_nodes[name], "contains")

    source_nodes: dict[str, str] = {}
    for index, source in enumerate(bundle.snapshot.get("sources") or []):
        if not isinstance(source, Mapping):
            missing.append({"from": revision_node, "relation": "uses", "expected": "source", "reason": "invalid source record"})
            continue
        source_key = str(source.get("source_id") or source.get("id") or f"source-{index + 1}")
        source_nodes[source_key] = node("source", source, label=source_key)
        reference_nodes[source_key] = source_nodes[source_key]
        reference_nodes[f"source:{source_key}"] = source_nodes[source_key]
        register_references(source_nodes[source_key], source, "source")
        if source.get("sha256"):
            reference_nodes[str(source["sha256"])] = source_nodes[source_key]
        edge(revision_node, source_nodes[source_key], "uses")

    check_nodes = []
    for index, check in enumerate(bundle.validation.get("checks") or []):
        check_id = node("validation_check", check, label=str((check or {}).get("id") if isinstance(check, Mapping) else f"check-{index + 1}"))
        check_nodes.append(check_id)
        if isinstance(check, Mapping):
            register_references(check_id, check, "check")
            pending_evidence.append((check_id, check, False))
        edge(check_id, revision_node, "validates")

    snapshot_records: dict[str, str] = {}
    for group_name in ("payload", "evidence"):
        group = getattr(bundle, group_name) if hasattr(bundle, group_name) else None
        if group is None:
            group = (bundle.snapshot.get(group_name) or {})
        if isinstance(group, Mapping):
            for name, record in sorted(group.items(), key=lambda item: str(item[0])):
                pointer = f"snapshot:{group_name}/{name}"
                snapshot_records[pointer] = node(
                    "snapshot_record", {"pointer": pointer, "record": record},
                    label=pointer,
                )
                reference_nodes[pointer] = snapshot_records[pointer]
                edge(revision_node, snapshot_records[pointer], "freezes")

    job_nodes: dict[str, str] = {}
    payload = bundle.snapshot.get("payload") or {}
    resolved_scope = bundle.snapshot.get("resolved_scope") or {}
    discovered_jobs: set[str] = set()
    for source in (payload, resolved_scope):
        if not isinstance(source, Mapping):
            continue
        for key in ("job_ids", "dependency_job_ids", "selected_configuration_ids"):
            discovered_jobs.update(str(item) for item in source.get(key) or [])
    if isinstance(payload, Mapping):
        summary = payload.get("adsorption_summary") or {}
        if isinstance(summary, Mapping):
            for collection in ("members", "rows"):
                for record in summary.get(collection) or []:
                    if not isinstance(record, Mapping):
                        continue
                    for field in ("member_id", "job_id", "configuration_id"):
                        if record.get(field):
                            discovered_jobs.add(str(record[field]))
    for identifier in sorted(discovered_jobs):
        job_nodes[identifier] = node("job", {"job_id": identifier}, label=identifier)
        reference_nodes[identifier] = job_nodes[identifier]
        reference_nodes[f"job:{identifier}"] = job_nodes[identifier]
        edge(revision_node, job_nodes[identifier], "freezes")

    table_nodes = []
    figure_nodes = []
    sections = bundle.model.get("sections") or []
    if isinstance(sections, list):
        for section_index, section in enumerate(sections):
            if not isinstance(section, Mapping):
                continue
            for block_index, block in enumerate(section.get("blocks") or []):
                if not isinstance(block, Mapping):
                    continue
                kind = str(block.get("type") or block.get("kind") or "")
                if kind == "table" or isinstance(block.get("table"), Mapping):
                    record = block.get("table") if isinstance(block.get("table"), Mapping) else block
                    table_id = node("table", record, label=str(record.get("title") or f"table-{section_index + 1}-{block_index + 1}"))
                    table_nodes.append(table_id)
                    register_references(table_id, record, "table")
                    pending_evidence.append((table_id, record, False))
                    edge(revision_node, table_id, "renders")
                if kind == "figure" or isinstance(block.get("figure"), Mapping):
                    record = block.get("figure") if isinstance(block.get("figure"), Mapping) else block
                    figure_id = node("figure", record, label=str(record.get("caption") or record.get("title") or f"figure-{section_index + 1}-{block_index + 1}"))
                    figure_nodes.append(figure_id)
                    register_references(figure_id, record, "figure")
                    pending_evidence.append((figure_id, record, False))
                    edge(revision_node, figure_id, "renders")
    for record in _figure_records(bundle):
        figure_id = node("figure", record, label=str(record.get("id")))
        if figure_id not in figure_nodes:
            figure_nodes.append(figure_id)
            register_references(figure_id, record, "figure")
            pending_evidence.append((figure_id, record, False))
            edge(revision_node, figure_id, "renders")

    for origin, record, missing_if_empty in pending_evidence:
        link_frozen_evidence(origin, record, missing_if_empty=missing_if_empty)

    claims = bundle.validation.get("claims") or bundle.model.get("claims") or []
    if isinstance(claims, Mapping):
        claims = [{"id": key, **(dict(value) if isinstance(value, Mapping) else {"text": value})}
                  for key, value in claims.items()]
    for index, claim in enumerate(claims if isinstance(claims, list) else []):
        claim_map = dict(claim) if isinstance(claim, Mapping) else {"text": claim}
        claim_id = node("conclusion", claim_map, label=str(claim_map.get("id") or claim_map.get("text") or f"claim-{index + 1}"))
        register_references(claim_id, claim_map, "claim")
        edge(revision_node, claim_id, "states")
        link_frozen_evidence(claim_id, claim_map, missing_if_empty=True)

    # Explicitly show the available typed lineage even when a model does not
    # carry finer-grained reference IDs.
    for check_id in check_nodes:
        for file_id in file_nodes.values():
            edge(check_id, file_id, "checks_hash")
    nodes.sort(key=lambda item: item["id"])
    edges.sort(key=lambda item: (item["source"], item["target"], item["relation"]))
    missing.sort(key=lambda item: (item["from"], item["expected"], item["reason"]))
    return {
        "schema": GRAPH_SCHEMA, "ok": True,
        "status": "blocked" if missing else "ready",
        "project_id": bundle.project_id, "revision": bundle.public_identity(),
        "nodes": nodes, "edges": edges, "missing_links": missing,
        "denominator": {"nodes": len(nodes), "edges": len(edges), "missing_links": len(missing)},
        "error": None,
    }


def _references_bib(model: Mapping[str, Any]) -> str:
    raw = model.get("bibtex")
    if isinstance(raw, str):
        return str(redact(raw)).strip() + ("\n" if raw.strip() else "")
    records = model.get("references") or model.get("citations") or []
    if isinstance(records, Mapping):
        records = [{"id": key, **(dict(value) if isinstance(value, Mapping) else {"title": value})}
                   for key, value in records.items()]
    chunks = []
    for index, record in enumerate(records if isinstance(records, list) else []):
        item = redact(record if isinstance(record, Mapping) else {"title": record})
        key = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or f"ref{index + 1}")) or f"ref{index + 1}"
        fields = []
        for field in ("author", "title", "journal", "year", "doi", "url"):
            value = item.get(field)
            if value not in (None, ""):
                escaped = str(value).replace("{", "\\{").replace("}", "\\}")
                fields.append(f"  {field} = {{{escaped}}}")
        chunks.append("@article{" + key + (",\n" + ",\n".join(fields) if fields else "") + "\n}")
    return "\n\n".join(chunks) + ("\n" if chunks else "")


def capsule_members(bundle: FrozenRevision, *,
                    notebook_payload: Mapping[str, Any] | None = None) -> dict[str, bytes]:
    methods = {
        key: bundle.model.get(key)
        for key in ("methods", "methodology", "calculation_details", "method_consistency")
        if bundle.model.get(key) is not None
    }
    environment = {
        "vcstudio_version": __version__,
        "frozen_environment": bundle.snapshot.get("environment") or bundle.model.get("environment") or {},
        "contract_schemas": {
            "spec": bundle.spec.get("schema"), "snapshot": bundle.snapshot.get("schema"),
            "validation": bundle.validation.get("schema"),
        },
    }
    payloads: dict[str, Any] = {
        "contracts/report-spec.json": bundle.spec,
        "contracts/report-snapshot.json": bundle.snapshot,
        "contracts/validation-result.json": bundle.validation,
        "inputs/input-manifest.json": {
            "input_fingerprint": bundle.snapshot.get("input_fingerprint"),
            "snapshot_sha256": bundle.entry["snapshot_sha256"],
            "resolved_scope": bundle.snapshot.get("resolved_scope") or {},
            "sources": bundle.snapshot.get("sources") or [],
        },
        "model/report-model.json": bundle.model,
        "metadata/report-bundle-manifest.json": bundle.manifest,
        "metadata/figures.json": _figure_records(bundle),
        "methods/methods.json": methods,
        "environment/environment.json": environment,
    }
    if notebook_payload is not None:
        payloads["research-notebook/ledger.json"] = notebook_payload
        payloads["research-notebook/limitations.json"] = {
            "schema": "vcstudio.research-notebook-limitations/v1",
            "project_id": bundle.project_id,
            "bound_report_revision_id": bundle.revision_id,
            "limitations": list(notebook_payload.get("limitations") or []),
        }
    members = {
        name: _canonical_bytes(redact(value)) + b"\n"
        for name, value in sorted(payloads.items())
    }
    members["references/references.bib"] = _references_bib(bundle.model).encode("utf-8")
    manifest = {
        "schema": CAPSULE_SCHEMA,
        "project_id": bundle.project_id,
        "revision": bundle.public_identity(),
        "source_hashes": {
            "spec_sha256": bundle.entry["spec_sha256"],
            "snapshot_sha256": bundle.entry["snapshot_sha256"],
            "validation_sha256": bundle.entry["validation_sha256"],
            "report_model_sha256": bundle.entry["report_model_sha256"],
            "manifest_sha256": bundle.entry["manifest_sha256"],
        },
        "files": [
            {"name": name, "sha256": _sha256_bytes(data), "size": len(data)}
            for name, data in sorted(members.items())
        ],
    }
    members["capsule-manifest.json"] = _canonical_bytes(manifest) + b"\n"
    sums = "".join(
        f"{_sha256_bytes(data)}  {name}\n" for name, data in sorted(members.items())
    )
    members["SHA256SUMS"] = sums.encode("ascii")
    return members


def build_capsule_bytes(bundle: FrozenRevision, *,
                        notebook_payload: Mapping[str, Any] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
        for name, data in sorted(capsule_members(
                bundle, notebook_payload=notebook_payload).items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


class OpaqueDestinationRegistry:
    """Bounded process-local destination selections with optional purpose binding."""

    DEFAULT_TTL_SECONDS = 15 * 60
    DEFAULT_MAX_ITEMS = 64

    def __init__(
        self,
        *,
        clock: Any = time.monotonic,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_items: int = DEFAULT_MAX_ITEMS,
        schema: str = DESTINATION_SCHEMA,
        purpose: str = "destination",
        token_prefix: str = "",
    ) -> None:
        if ttl_seconds <= 0 or max_items <= 0:
            raise ValueError("destination bounds must be positive")
        self._lock = threading.RLock()
        self._clock = clock
        self._ttl_seconds = float(ttl_seconds)
        self._max_items = int(max_items)
        self._schema = str(schema)
        self._purpose = str(purpose)
        self._token_prefix = str(token_prefix)
        self._items: dict[str, tuple[str, float, str | None]] = {}

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            token
            for token, (_target, created_at, _binding) in self._items.items()
            if now - created_at >= self._ttl_seconds
        ]
        for token in expired:
            self._items.pop(token, None)

    def register(self, directory: str, *, binding: str | None = None) -> dict[str, Any]:
        target = os.path.realpath(os.path.abspath(str(directory or "")))
        if not os.path.isdir(target):
            raise ValueError(f"{self._purpose} destination directory is invalid")
        with self._lock:
            now = float(self._clock())
            self._prune_expired_locked(now)
            while len(self._items) >= self._max_items:
                oldest = min(
                    self._items,
                    key=lambda item_token: self._items[item_token][1],
                )
                self._items.pop(oldest, None)
            token = self._token_prefix + secrets.token_urlsafe(24)
            while token in self._items:
                token = self._token_prefix + secrets.token_urlsafe(24)
            self._items[token] = (target, now, None if binding is None else str(binding))
        display_name = redact(os.path.basename(target) or "selected directory")
        return {"schema": self._schema, "destination_token": token,
                "display_name": str(display_name)}

    def consume(self, token: str, *, expected_binding: str | None = None) -> str:
        with self._lock:
            now = float(self._clock())
            self._prune_expired_locked(now)
            selected = self._items.pop(str(token or ""), None)
        if selected is None:
            raise ValueError(f"{self._purpose} destination token is invalid or expired")
        binding = selected[2]
        if expected_binding is not None and binding != str(expected_binding):
            raise ValueError(f"{self._purpose} destination token binding mismatch")
        return selected[0]


class CapsuleDestinations(OpaqueDestinationRegistry):
    """Capsule-only destination tokens; kept separate from report publication."""

    def __init__(
        self,
        *,
        clock: Any = time.monotonic,
        ttl_seconds: float = OpaqueDestinationRegistry.DEFAULT_TTL_SECONDS,
        max_items: int = OpaqueDestinationRegistry.DEFAULT_MAX_ITEMS,
    ) -> None:
        super().__init__(
            clock=clock,
            ttl_seconds=ttl_seconds,
            max_items=max_items,
            schema=DESTINATION_SCHEMA,
            purpose="capsule",
            token_prefix="capsule.",
        )


def export_capsule(service: Any, path: str, revision_id: str,
                   destination_dir: str) -> dict[str, Any]:
    bundle = load_frozen_revision(service, path, revision_id)
    from vcstudio.project.research_notebook import (
        ARCHIVE_SCHEMA,
        notebook_limitations,
        redact_public_text,
    )

    notebook_payload: Mapping[str, Any] = {
        "schema": ARCHIVE_SCHEMA,
        "project_id": bundle.project_id,
        "bound_report_revision_id": bundle.revision_id,
        "ledger_revision": 0,
        "ledger_head_digest": None,
        "integrity_status": "unavailable",
        "records": [],
        "denominator": {"records": 0, "active": 0, "review_todo": 0},
        "limitations": notebook_limitations(),
    }
    provider = getattr(getattr(service, "_host", None),
                       "_research_notebook_archive_payload", None)
    if callable(provider):
        try:
            supplied = provider(path, bundle.project_id, bundle.revision_id)
            if isinstance(supplied, Mapping):
                notebook_payload = supplied
        except Exception as exc:  # noqa: BLE001 archive remains honest and exportable
            notebook_payload = {
                **dict(notebook_payload),
                "integrity_status": "unavailable",
                "error": redact_public_text(str(exc)),
            }
    safe_revision = re.sub(r"[^A-Za-z0-9._-]", "-", bundle.revision_id)
    target = Path(destination_dir) / f"{safe_revision}-si-capsule.zip"
    data = build_capsule_bytes(bundle, notebook_payload=notebook_payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "schema": CAPSULE_SCHEMA, "ok": True, "status": "ready",
        "project_id": bundle.project_id, "revision": bundle.public_identity(),
        "file": {"name": target.name, "sha256": _sha256_bytes(data), "size": len(data)},
        "error": None,
    }


__all__ = [
    "CAPSULE_SCHEMA", "DESTINATION_SCHEMA", "DIFF_SCHEMA", "GRAPH_SCHEMA",
    "CapsuleDestinations", "OpaqueDestinationRegistry", "build_capsule_bytes", "capsule_members",
    "evidence_graph", "export_capsule", "load_frozen_revision", "redact",
    "scientific_diff", "StaleRevisionError",
]
