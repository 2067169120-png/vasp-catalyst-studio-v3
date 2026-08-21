"""Read-only scientific insight views over frozen report revisions.

The mutable project tree is never a source for this module.  Every operation
starts from an authoritative :class:`ReportService` history entry, revalidates
the complete on-disk bundle through the service host, and only then reads the
frozen contracts/model/manifest.  Public projections deliberately omit paths.
"""
from __future__ import annotations

import copy
import contextlib
import errno
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from vcstudio import __version__
from vcstudio.project.report_service import (
    _exclusive_file_lock,
    _history_paths,
    _read_history,
    _validated_history_bundle,
)
from vcstudio.shared.credential_classifier import (
    contains_local_path,
    is_sensitive_key,
    looks_like_credential,
)
from vcstudio.shared.secrets import contains_credential


DIFF_SCHEMA = "vcstudio.report-scientific-diff/v1"
GRAPH_SCHEMA = "vcstudio.report-evidence-graph/v1"
CAPSULE_SCHEMA = "vcstudio.report-si-capsule/v1"
CAPSULE_PREVIEW_SCHEMA = "vcstudio.report-si-capsule-preview/v1"
CAPSULE_RECEIPT_SCHEMA = "vcstudio.report-si-capsule-receipt/v1"
CAPSULE_LOCATOR_SCHEMA = "vcstudio.report-si-capsule-locator/v1"
DESTINATION_SCHEMA = "vcstudio.report-capsule-destination/v1"
DESTINATION_IDENTITY_SCHEMA = "vcstudio.filesystem-directory-identity/v1"

CAPSULE_MAX_MEMBERS = 32
CAPSULE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
CAPSULE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
CAPSULE_MAX_ARCHIVE_BYTES = CAPSULE_MAX_TOTAL_BYTES + 4 * 1024 * 1024

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
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


def _bounded_canonical_bytes(value: Any, maximum: int, *, newline: bool) -> bytes:
    """Encode canonical JSON while bounding the accumulating byte buffer."""

    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    output = bytearray()
    reserve = 1 if newline else 0
    for text_chunk in encoder.iterencode(value):
        for offset in range(0, len(text_chunk), 64 * 1024):
            encoded = text_chunk[offset:offset + 64 * 1024].encode("utf-8")
            if len(output) + len(encoded) + reserve > maximum:
                raise CapsuleQuotaError("capsule member byte limit exceeded")
            output.extend(encoded)
    if newline:
        output.extend(b"\n")
    return bytes(output)


def _bounded_utf8_bytes(value: str, maximum: int) -> bytes:
    output = bytearray()
    for offset in range(0, len(value), 64 * 1024):
        encoded = value[offset:offset + 64 * 1024].encode("utf-8")
        if len(output) + len(encoded) > maximum:
            raise CapsuleQuotaError("capsule member byte limit exceeded")
        output.extend(encoded)
    return bytes(output)


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
        contains_local_path(value)
        or looks_like_credential(value)
        or contains_credential(value)
    )


def redact(value: Any, *, key: str = "") -> Any:
    """Return a JSON-safe, deterministic value with paths/secrets removed."""

    normalized_key = str(key).strip().lower().replace("-", "_")
    if normalized_key in _SENSITIVE_KEYS or is_sensitive_key(key):
        return "[redacted-secret]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
            if not (is_sensitive_key(item_key)
                    or _is_path_or_secret(str(item_key)))
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


def evidence_graph(
    service: Any,
    path: str,
    revision_id: str,
    *,
    frozen_bundle: FrozenRevision | None = None,
) -> dict[str, Any]:
    """Build the graph from a revalidated revision or a stricter recapture.

    ``frozen_bundle`` is an internal extension seam for consumers such as the
    reproducibility archive that recapture every hash-bound JSON member through
    a single regular-file descriptor before graph construction.  Public callers
    omit it and retain the original authoritative ``ReportService`` journey.
    """

    bundle = frozen_bundle or load_frozen_revision(service, path, revision_id)
    if bundle.revision_id != str(revision_id or ""):
        raise StaleRevisionError("frozen evidence graph revision binding mismatch")
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


@dataclass(frozen=True)
class CapsuleLimits:
    """Hard bounds for both capsule members and the ZIP container."""

    max_members: int = CAPSULE_MAX_MEMBERS
    max_member_bytes: int = CAPSULE_MAX_MEMBER_BYTES
    max_total_bytes: int = CAPSULE_MAX_TOTAL_BYTES
    max_archive_bytes: int = CAPSULE_MAX_ARCHIVE_BYTES

    def __post_init__(self) -> None:
        values = (
            self.max_members,
            self.max_member_bytes,
            self.max_total_bytes,
            self.max_archive_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
               for value in values):
            raise ValueError("capsule limits must be positive integers")
        if self.max_member_bytes > self.max_total_bytes:
            raise ValueError("capsule member limit exceeds total limit")


class CapsuleQuotaError(RuntimeError):
    """The frozen capsule cannot be represented within its declared bounds."""


@dataclass(frozen=True)
class _ArchivePlan:
    primary: tuple[tuple[str, str, int], ...]
    generated: dict[str, bytes]
    member_count: int
    total_size: int


def _capsule_primary_members(
    bundle: FrozenRevision,
    notebook_payload: Mapping[str, Any] | None,
    maximum_member_bytes: int,
) -> Iterable[tuple[str, bytes]]:
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
    for name, value in sorted(payloads.items()):
        yield name, _bounded_canonical_bytes(
            redact(value), maximum_member_bytes, newline=True)
    yield (
        "references/references.bib",
        _bounded_utf8_bytes(
            _references_bib(bundle.model), maximum_member_bytes),
    )


def _quota_add(
    name: str,
    data: bytes,
    *,
    count: int,
    total: int,
    limits: CapsuleLimits,
) -> tuple[int, int]:
    size = len(data)
    if size > limits.max_member_bytes:
        raise CapsuleQuotaError("capsule member byte limit exceeded")
    count += 1
    if count > limits.max_members:
        raise CapsuleQuotaError("capsule member count limit exceeded")
    total += size
    if total > limits.max_total_bytes:
        raise CapsuleQuotaError("capsule total byte limit exceeded")
    if not name or name.startswith(("/", "\\")) or ".." in Path(name).parts:
        raise CapsuleQuotaError("capsule member name is unsafe")
    return count, total


def _capsule_plan(
    bundle: FrozenRevision,
    notebook_payload: Mapping[str, Any] | None,
    limits: CapsuleLimits,
) -> _ArchivePlan:
    count = 0
    total = 0
    primary: list[tuple[str, str, int]] = []
    for name, data in _capsule_primary_members(
            bundle, notebook_payload, limits.max_member_bytes):
        count, total = _quota_add(
            name, data, count=count, total=total, limits=limits)
        primary.append((name, _sha256_bytes(data), len(data)))
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
            {"name": name, "sha256": digest, "size": size}
            for name, digest, size in primary
        ],
    }
    manifest_bytes = _canonical_bytes(manifest) + b"\n"
    count, total = _quota_add(
        "capsule-manifest.json", manifest_bytes,
        count=count, total=total, limits=limits)
    sums = "".join(
        f"{digest}  {name}\n" for name, digest, _size in sorted([
            *primary,
            ("capsule-manifest.json", _sha256_bytes(manifest_bytes),
             len(manifest_bytes)),
        ])
    )
    sums_bytes = sums.encode("ascii")
    count, total = _quota_add(
        "SHA256SUMS", sums_bytes,
        count=count, total=total, limits=limits)
    return _ArchivePlan(
        primary=tuple(primary),
        generated={
            "capsule-manifest.json": manifest_bytes,
            "SHA256SUMS": sums_bytes,
        },
        member_count=count,
        total_size=total,
    )


def _planned_member_bytes(
    bundle: FrozenRevision,
    notebook_payload: Mapping[str, Any] | None,
    plan: _ArchivePlan,
    limits: CapsuleLimits,
) -> Iterable[tuple[str, bytes]]:
    expected = {name: (digest, size) for name, digest, size in plan.primary}
    for name, data in _capsule_primary_members(
            bundle, notebook_payload, limits.max_member_bytes):
        if expected.get(name) != (_sha256_bytes(data), len(data)):
            raise RuntimeError("capsule member changed during bounded construction")
        yield name, data
    yield from sorted(plan.generated.items())


def capsule_members(
    bundle: FrozenRevision,
    *,
    notebook_payload: Mapping[str, Any] | None = None,
    limits: CapsuleLimits | None = None,
) -> dict[str, bytes]:
    """Compatibility projection with explicit aggregate bounds.

    Production export uses :func:`_write_capsule_archive`; this helper remains
    useful for verification and callers that intentionally request bounded
    in-memory member bytes.
    """

    selected_limits = limits or CapsuleLimits()
    plan = _capsule_plan(bundle, notebook_payload, selected_limits)
    members = dict(_planned_member_bytes(
        bundle, notebook_payload, plan, selected_limits))
    if len(members) != plan.member_count or sum(map(len, members.values())) != plan.total_size:
        raise RuntimeError("capsule member plan changed during construction")
    return members


class _BoundedArchiveFile:
    """Seekable file wrapper that rejects ZIP growth before the write occurs."""

    def __init__(self, handle: Any, maximum: int) -> None:
        self._handle = handle
        self._maximum = maximum
        self._largest_offset = 0

    def write(self, data: bytes) -> int:
        end = self._handle.tell() + len(data)
        if end > self._maximum:
            raise CapsuleQuotaError("capsule archive byte limit exceeded")
        written = self._handle.write(data)
        self._largest_offset = max(self._largest_offset, self._handle.tell())
        return written

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    @property
    def largest_offset(self) -> int:
        return self._largest_offset


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _write_capsule_archive(
    handle: Any,
    bundle: FrozenRevision,
    notebook_payload: Mapping[str, Any] | None,
    limits: CapsuleLimits,
) -> tuple[str, int, int]:
    plan = _capsule_plan(bundle, notebook_payload, limits)
    sink = _BoundedArchiveFile(handle, limits.max_archive_bytes)
    with zipfile.ZipFile(
        sink, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True,
    ) as archive:
        streamed_total = 0
        streamed_count = 0
        for name, data in _planned_member_bytes(
                bundle, notebook_payload, plan, limits):
            streamed_count += 1
            streamed_total += len(data)
            if (streamed_count > limits.max_members
                    or len(data) > limits.max_member_bytes
                    or streamed_total > limits.max_total_bytes):
                raise CapsuleQuotaError("capsule streaming quota exceeded")
            with archive.open(_zip_info(name), "w") as member:
                for offset in range(0, len(data), 1024 * 1024):
                    member.write(data[offset:offset + 1024 * 1024])
        if streamed_count != plan.member_count or streamed_total != plan.total_size:
            raise RuntimeError("capsule stream did not match its frozen plan")
    handle.flush()
    os.fsync(handle.fileno())
    size = sink.largest_offset
    if size > limits.max_archive_bytes:
        raise CapsuleQuotaError("capsule archive byte limit exceeded")
    handle.seek(0)
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest(), size, plan.member_count


def build_capsule_bytes(bundle: FrozenRevision, *,
                        notebook_payload: Mapping[str, Any] | None = None,
                        limits: CapsuleLimits | None = None) -> bytes:
    """Build bounded compatibility bytes through a file-backed temporary file."""

    selected_limits = limits or CapsuleLimits()
    with tempfile.TemporaryFile(mode="w+b") as output:
        _digest, size, _count = _write_capsule_archive(
            output, bundle, notebook_payload, selected_limits)
        output.seek(0)
        data = output.read(selected_limits.max_archive_bytes + 1)
    if len(data) != size or len(data) > selected_limits.max_archive_bytes:
        raise CapsuleQuotaError("capsule archive byte limit exceeded")
    return data


def _has_reparse_attribute(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & marker)


DirectoryEntityIdentity = tuple[str, int, int | str]
DirectoryIdentityChain = tuple[DirectoryEntityIdentity, ...]


def _windows_open_entity(
    path: Path | str,
    *,
    expect_directory: bool,
    share_delete: bool,
) -> tuple[Any, dict[str, Any]]:
    """Open one Windows namespace entry without following its final reparse point."""

    import ctypes
    from ctypes import wintypes

    class _FileId128(ctypes.Structure):
        _fields_ = [("identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", _FileId128),
        ]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path), 0, 0x1 | 0x2 | (0x4 if share_delete else 0), None, 3,
        0x02000000 | 0x00200000, None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "filesystem identity unavailable")
    try:
        attributes = _FileAttributeTagInfo()
        if not get_information(
                handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise OSError(ctypes.get_last_error(), "filesystem attributes unavailable")
        is_directory = bool(int(attributes.file_attributes) & 0x10)
        if int(attributes.file_attributes) & 0x400:
            raise ValueError("filesystem entity must not be a reparse point")
        if is_directory != expect_directory:
            raise ValueError("filesystem entity type changed")
        information = _FileIdInfo()
        if not get_information(
                handle, 18, ctypes.byref(information), ctypes.sizeof(information)):
            raise OSError(ctypes.get_last_error(), "filesystem identity unavailable")
        return handle, {
            "volume_serial": str(information.volume_serial_number),
            "file_id": bytes(information.file_id.identifier).hex(),
        }
    except Exception:
        close_handle(handle)
        raise


def _windows_path_identity(
    path: Path,
    *,
    expect_directory: bool,
) -> dict[str, Any]:
    """Return the Windows volume serial and 128-bit file ID for *path*."""

    handle, identity = _windows_open_entity(
        path, expect_directory=expect_directory, share_delete=True)
    _close_windows_handle(handle)
    return identity


def _windows_descriptor_identity(descriptor: int) -> dict[str, Any]:
    """Return FILE_ID_INFO for an already-open Python file descriptor.

    Python 3.10 exposes the legacy 64-bit Windows file index through
    ``stat_result.st_ino`` while FILE_ID_INFO is a 128-bit identity.  Comparing
    those representations rejects the same entity on some filesystems, so all
    Windows descriptor-to-path checks use the native handle representation on
    both sides.
    """

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileId128(ctypes.Structure):
        _fields_ = [("identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", _FileId128),
        ]

    get_information = ctypes.WinDLL(
        "kernel32", use_last_error=True,
    ).GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    information = _FileIdInfo()
    if not get_information(
            handle, 18, ctypes.byref(information), ctypes.sizeof(information)):
        raise OSError(ctypes.get_last_error(), "filesystem identity unavailable")
    return {
        "platform": "windows",
        "volume_serial": str(information.volume_serial_number),
        "file_id": bytes(information.file_id.identifier).hex(),
    }


def _descriptor_matches_identity(
    descriptor: int,
    current: os.stat_result,
    identity: Mapping[str, Any],
) -> bool:
    if identity.get("platform") == "windows":
        return _windows_descriptor_identity(descriptor) == dict(identity)
    return _stat_matches_identity(current, identity)


def _physical_identity(
    path: Path,
    *,
    expect_directory: bool,
) -> dict[str, Any]:
    """Take one no-follow physical identity snapshot for a file or directory."""

    current = os.lstat(path)
    if stat.S_ISLNK(current.st_mode) or _has_reparse_attribute(current):
        raise CapsuleExportError("capsule filesystem identity is unsafe")
    expected = stat.S_ISDIR if expect_directory else stat.S_ISREG
    if not expected(current.st_mode):
        raise CapsuleExportError("capsule filesystem entity type changed")
    if os.name == "nt":
        physical = _windows_path_identity(
            path, expect_directory=expect_directory)
        return {"platform": "windows", **physical}
    return {
        "platform": "posix",
        "device": str(current.st_dev),
        "inode": str(current.st_ino),
    }


def _stat_matches_identity(
    current: os.stat_result,
    identity: Mapping[str, Any],
) -> bool:
    if identity.get("platform") == "windows":
        try:
            file_id = int.from_bytes(
                bytes.fromhex(str(identity["file_id"])), "little")
            volume = int(str(identity["volume_serial"]))
        except (KeyError, TypeError, ValueError):
            return False
        return current.st_dev == volume and current.st_ino == file_id
    return (
        str(current.st_dev) == str(identity.get("device"))
        and str(current.st_ino) == str(identity.get("inode"))
    )


def _absolute_no_follow_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _directory_ancestors(path: Path | str) -> tuple[Path, ...]:
    absolute = _absolute_no_follow_path(path)
    values: list[Path] = []
    cursor = absolute
    while True:
        values.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    return tuple(reversed(values))


def _posix_directory_chain(
    path: Path | str,
) -> tuple[DirectoryIdentityChain, tuple[int, ...]]:
    """Open every POSIX component relative to its pinned parent."""

    ancestors = _directory_ancestors(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptors: list[int] = []
    identities: list[DirectoryEntityIdentity] = []
    try:
        for index, item in enumerate(ancestors):
            if index == 0:
                before = os.lstat(item)
                descriptor = os.open(item, flags)
                descriptors.append(descriptor)
                after = os.lstat(item)
            else:
                name = item.name
                parent_descriptor = descriptors[-1]
                before = os.stat(
                    name, dir_fd=parent_descriptor, follow_symlinks=False)
                descriptor = os.open(name, flags, dir_fd=parent_descriptor)
                descriptors.append(descriptor)
                after = os.stat(
                    name, dir_fd=parent_descriptor, follow_symlinks=False)
            opened = os.fstat(descriptor)
            snapshots = (before, opened, after)
            if any(
                _has_reparse_attribute(value)
                or stat.S_ISLNK(value.st_mode)
                or not stat.S_ISDIR(value.st_mode)
                for value in snapshots
            ):
                raise ValueError(
                    "destination ancestors must be non-reparse directories")
            entity = ("posix", int(opened.st_dev), int(opened.st_ino))
            if any(
                ("posix", int(value.st_dev), int(value.st_ino)) != entity
                for value in (before, after)
            ):
                raise ValueError("destination ancestor changed during capture")
            identities.append(entity)
        return tuple(identities), tuple(descriptors)
    except Exception as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if isinstance(exc, OSError) and exc.errno in {
            errno.ELOOP, errno.ENOTDIR,
        }:
            # Linux commonly reports O_NOFOLLOW|O_DIRECTORY on a symlink as
            # ENOTDIR rather than ELOOP.  Do not let that platform detail (or
            # the path attached to the OSError) escape the trust boundary.
            raise ValueError(
                "destination ancestors must be non-symlink directories"
            ) from None
        raise


def _windows_directory_chain(
    path: Path | str,
) -> tuple[DirectoryIdentityChain, tuple[Any, ...]]:
    """Open and pin every Windows ancestor while capturing FILE_ID_INFO."""

    handles: list[Any] = []
    identities: list[DirectoryEntityIdentity] = []
    try:
        for item in _directory_ancestors(path):
            before = os.lstat(item)
            if (
                _has_reparse_attribute(before)
                or stat.S_ISLNK(before.st_mode)
                or not stat.S_ISDIR(before.st_mode)
            ):
                raise ValueError(
                    "destination ancestors must be non-reparse directories")
            handle, physical = _windows_open_entity(
                item, expect_directory=True, share_delete=False)
            handles.append(handle)
            after = os.lstat(item)
            if (
                _has_reparse_attribute(after)
                or stat.S_ISLNK(after.st_mode)
                or not stat.S_ISDIR(after.st_mode)
            ):
                raise ValueError(
                    "destination ancestors must be non-reparse directories")
            checked_handle, checked = _windows_open_entity(
                item, expect_directory=True, share_delete=False)
            _close_windows_handle(checked_handle)
            if checked != physical:
                raise ValueError("destination ancestor changed during capture")
            file_id = str(physical["file_id"])
            if not re.fullmatch(r"[0-9a-f]{32}", file_id):
                raise ValueError("destination ancestor identity is invalid")
            identities.append((
                "windows",
                int(str(physical["volume_serial"])),
                file_id,
            ))
        return tuple(identities), tuple(handles)
    except Exception:
        for handle in reversed(handles):
            _close_windows_handle(handle)
        raise


def _open_directory_chain(
    path: Path | str,
) -> tuple[DirectoryIdentityChain, tuple[Any, ...]]:
    if os.name == "nt":
        return _windows_directory_chain(path)
    return _posix_directory_chain(path)


def _close_directory_chain(handles: Iterable[Any]) -> None:
    for handle in reversed(tuple(handles)):
        if os.name == "nt":
            _close_windows_handle(handle)
        else:
            os.close(int(handle))


def _identity_chain_payload(identity: DirectoryIdentityChain) -> dict[str, Any]:
    ancestors = []
    for platform, volume, entity in identity:
        if platform == "windows":
            ancestors.append({
                "platform": "windows",
                "volume_serial": str(volume),
                "file_id": str(entity),
            })
        elif platform == "posix":
            ancestors.append({
                "platform": "posix",
                "device": str(volume),
                "inode": str(entity),
            })
        else:  # pragma: no cover - only internally constructed chains arrive here
            raise ValueError("destination identity platform is invalid")
    return {
        "schema": DESTINATION_IDENTITY_SCHEMA,
        "platform": "windows" if os.name == "nt" else "posix",
        "ancestors": ancestors,
    }


def _identity_chain_from_payload(
    value: Mapping[str, Any],
) -> DirectoryIdentityChain:
    expected_platform = "windows" if os.name == "nt" else "posix"
    ancestors = value.get("ancestors")
    if (
        value.get("schema") != DESTINATION_IDENTITY_SCHEMA
        or value.get("platform") != expected_platform
        or not isinstance(ancestors, list)
        or not ancestors
        or len(ancestors) > 256
    ):
        raise CapsuleExportError("capsule destination identity is invalid")
    result: list[DirectoryEntityIdentity] = []
    for item in ancestors:
        if not isinstance(item, Mapping) or item.get("platform") != expected_platform:
            raise CapsuleExportError("capsule destination identity is invalid")
        try:
            if expected_platform == "windows":
                volume = int(str(item["volume_serial"]))
                entity: int | str = str(item["file_id"])
                if volume < 0 or not re.fullmatch(r"[0-9a-f]{32}", entity):
                    raise ValueError
            else:
                volume = int(str(item["device"]))
                entity = int(str(item["inode"]))
                if volume < 0 or entity < 0:
                    raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise CapsuleExportError(
                "capsule destination identity is invalid") from exc
        result.append((expected_platform, volume, entity))
    return tuple(result)


def _directory_identity(path: Path) -> dict[str, Any]:
    """Bind every physical ancestor, rejecting symlinks and reparse points."""

    identity, handles = _open_directory_chain(path)
    try:
        return _identity_chain_payload(identity)
    finally:
        _close_directory_chain(handles)


def _assert_directory_identity(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        frozen = _identity_chain_from_payload(expected)
        current, handles = _open_directory_chain(path)
    except Exception as exc:
        if isinstance(exc, CapsuleExportError):
            raise
        raise CapsuleExportError("capsule destination identity changed") from exc
    try:
        if current != frozen:
            raise CapsuleExportError("capsule destination identity changed")
    finally:
        _close_directory_chain(handles)


def _operation_lock_path(binding_sha256: str) -> Path:
    root = Path(tempfile.gettempdir()) / "vcstudio-capsule-operation-locks"
    return root / f"{binding_sha256}.lock"


def _capsule_locator_root() -> Path:
    return Path(tempfile.gettempdir()) / "vcstudio-capsule-receipt-locators"


@contextlib.contextmanager
def _capsule_destination_lock(
    destination: Path,
    identity: Mapping[str, Any],
) -> Iterable[None]:
    """Lock a verified destination and recheck its ancestor chain on both sides."""

    frozen = _identity_chain_from_payload(identity)
    _assert_directory_identity(destination, identity)
    selection = TrustedDirectorySelection(str(destination), frozen)
    with open_trusted_directory(selection) as trusted:
        lock_name = ".vcstudio-capsule.lock"
        lock_path = destination / lock_name
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if trusted.dir_fd is not None:
            try:
                before = os.stat(
                    lock_name, dir_fd=trusted.dir_fd, follow_symlinks=False)
                if _is_symlink_or_reparse(before) or not stat.S_ISREG(before.st_mode):
                    raise CapsuleExportError("capsule destination lock is unsafe")
                lock_identity: dict[str, Any] | None = {
                    "platform": "posix",
                    "device": str(before.st_dev),
                    "inode": str(before.st_ino),
                }
            except FileNotFoundError:
                lock_identity = None
            try:
                descriptor = os.open(
                    lock_name, flags, 0o600, dir_fd=trusted.dir_fd)
            except OSError as exc:
                raise CapsuleExportError(
                    "capsule destination lock is unavailable") from exc
        else:
            if os.path.lexists(lock_path):
                lock_identity = _physical_identity(
                    lock_path, expect_directory=False)
            else:
                lock_identity = None
            try:
                descriptor = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise CapsuleExportError(
                    "capsule destination lock is unavailable") from exc
        handle = os.fdopen(descriptor, "r+b", closefd=True)
        locked = False
        try:
            opened = os.fstat(handle.fileno())
            if trusted.dir_fd is not None:
                named = os.stat(
                    lock_name, dir_fd=trusted.dir_fd, follow_symlinks=False)
                current_lock = {
                    "platform": "posix",
                    "device": str(named.st_dev),
                    "inode": str(named.st_ino),
                }
                opened_identity = {
                    "platform": "posix",
                    "device": str(opened.st_dev),
                    "inode": str(opened.st_ino),
                }
                safe_lock = (
                    not _is_symlink_or_reparse(named)
                    and stat.S_ISREG(named.st_mode)
                    and current_lock == opened_identity
                )
            else:
                current_lock = _physical_identity(
                    lock_path, expect_directory=False)
                opened_identity = _windows_descriptor_identity(handle.fileno())
                safe_lock = (
                    stat.S_ISREG(opened.st_mode)
                    and opened_identity == current_lock
                )
            if not safe_lock:
                raise CapsuleExportError("capsule destination lock is unsafe")
            if lock_identity is not None and current_lock != lock_identity:
                raise CapsuleExportError("capsule destination lock changed")
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            else:  # pragma: no cover - exercised by the Linux CI matrix
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            trusted.verify_path()
            yield
            trusted.verify_path()
        finally:
            try:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - exercised by the Linux CI matrix
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


@dataclass(frozen=True)
class TrustedDirectorySelection:
    """Private path plus the OS identity captured by a destination token."""

    path: str
    identity: DirectoryIdentityChain

    def __fspath__(self) -> str:
        return self.path


@dataclass
class TrustedDirectoryHandle:
    """Pinned directory capability used for relative archive filesystem calls."""

    selection: TrustedDirectorySelection
    dir_fd: int | None = None
    ancestor_fds: tuple[int, ...] = ()
    windows_handle: Any = None
    windows_ancestor_handles: tuple[Any, ...] = ()
    windows_guard_handle: Any = None

    @property
    def path(self) -> str:
        return self.selection.path

    def verify_path(self) -> None:
        current, handles = _open_directory_chain(self.selection.path)
        try:
            if current != self.selection.identity:
                raise ValueError("destination directory ancestor chain changed")
        finally:
            _close_directory_chain(handles)


def _is_symlink_or_reparse(value: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & reparse_flag
    )


def _close_windows_handle(handle: Any) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _windows_handle_final_path(handle: Any) -> str:
    """Return one normalized DOS path for an already-open Windows handle."""

    import ctypes
    from ctypes import wintypes

    get_final_path = ctypes.WinDLL(
        "kernel32", use_last_error=True,
    ).GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = get_final_path(handle, buffer, size, 0)
        if length == 0:
            raise OSError(ctypes.get_last_error(), "unable to resolve trusted handle path")
        if length < size:
            value = buffer.value
            break
        size = int(length) + 1
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _windows_directory_guard(path: str) -> Any:
    """Pin a Windows directory namespace with an unshared delete-on-close child."""

    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    guard_path = os.path.join(path, f".vcs-directory-guard-{secrets.token_hex(16)}")
    handle = create_file(
        guard_path,
        0x80000000 | 0x40000000 | 0x00010000,
        0x1 | 0x2,
        None,
        1,
        0x2 | 0x100 | 0x04000000 | 0x00200000,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "unable to pin destination directory")
    return handle


def capture_trusted_directory(directory: str | os.PathLike[str]) -> TrustedDirectorySelection:
    """Capture every non-reparse ancestor's stable physical identity."""

    requested = os.path.abspath(os.fspath(directory))
    identity, handles = _open_directory_chain(requested)
    try:
        if not identity:
            raise ValueError("destination ancestor identity is unavailable")
        return TrustedDirectorySelection(requested, identity)
    finally:
        _close_directory_chain(handles)


@contextlib.contextmanager
def open_trusted_directory(selection: TrustedDirectorySelection):
    """Open and pin the complete ancestor chain captured by ``selection``."""

    if not isinstance(selection, TrustedDirectorySelection):
        raise TypeError("trusted destination selection is required")
    identity, handles = _open_directory_chain(selection.path)
    if identity != selection.identity:
        _close_directory_chain(handles)
        raise ValueError("destination directory ancestor chain changed")
    if os.name == "nt":
        handle = handles[-1]
        ancestor_handles = tuple(handles[:-1])
        guard_handle = None
        try:
            guard_handle = _windows_directory_guard(selection.path)
            trusted_path = _windows_handle_final_path(handle)
            guard_parent = os.path.dirname(_windows_handle_final_path(guard_handle))
            if guard_parent != trusted_path:
                raise ValueError("destination directory guard escaped trusted entity")
            trusted = TrustedDirectoryHandle(
                selection=selection, windows_handle=handle,
                windows_ancestor_handles=ancestor_handles,
                windows_guard_handle=guard_handle,
            )
            trusted.verify_path()
            try:
                yield trusted
            finally:
                trusted.verify_path()
        finally:
            if guard_handle is not None:
                _close_windows_handle(guard_handle)
            _close_directory_chain(handles)
    else:
        descriptor = int(handles[-1])
        ancestor_fds = tuple(int(item) for item in handles[:-1])
        trusted = TrustedDirectoryHandle(
            selection=selection,
            dir_fd=descriptor,
            ancestor_fds=ancestor_fds,
        )
        try:
            trusted.verify_path()
            try:
                yield trusted
            finally:
                trusted.verify_path()
        finally:
            _close_directory_chain(handles)


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
        self._items: dict[
            str,
            tuple[
                str,
                float,
                str | None,
                dict[str, Any],
                TrustedDirectorySelection,
            ],
        ] = {}
        self._consumed: dict[
            str,
            tuple[
                str,
                str,
                float,
                str | None,
                dict[str, Any],
                TrustedDirectorySelection,
            ],
        ] = {}

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            token
            for token, (
                _target,
                created_at,
                _binding,
                _identity,
                _selection,
            )
            in self._items.items()
            if now - created_at >= self._ttl_seconds
        ]
        for token in expired:
            self._items.pop(token, None)
        expired_operations = [
            operation_key
            for operation_key, (
                _token,
                _target,
                created_at,
                _binding,
                _identity,
                _selection,
            )
            in self._consumed.items()
            if now - created_at >= self._ttl_seconds
        ]
        for operation_key in expired_operations:
            self._consumed.pop(operation_key, None)

    def register(self, directory: str, *, binding: str | None = None) -> dict[str, Any]:
        target = str(_absolute_no_follow_path(str(directory or "")))
        try:
            selection = capture_trusted_directory(target)
            identity = _identity_chain_payload(selection.identity)
            with open_trusted_directory(selection):
                _assert_directory_identity(Path(target), identity)
        except Exception as exc:
            raise ValueError(
                f"{self._purpose} destination directory is invalid") from exc
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
            self._items[token] = (
                target,
                now,
                None if binding is None else str(binding),
                identity,
                selection,
            )
        display_name = redact(os.path.basename(target) or "selected directory")
        return {"schema": self._schema, "destination_token": token,
                "display_name": str(display_name)}

    def consume(
        self,
        token: str,
        *,
        expected_binding: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        supplied_token = str(token or "")
        operation_key = None if idempotency_key is None else str(idempotency_key or "").strip()
        if operation_key is not None and not _TOKEN.fullmatch(operation_key):
            raise ValueError(f"{self._purpose} idempotency key is invalid")
        with self._lock:
            now = float(self._clock())
            self._prune_expired_locked(now)
            if operation_key is not None:
                replay = self._consumed.get(operation_key)
                if replay is not None:
                    if replay[0] != supplied_token:
                        raise ValueError(
                            f"{self._purpose} idempotency key was reused for different input")
                    if expected_binding is not None and replay[3] != str(expected_binding):
                        raise ValueError(
                            f"{self._purpose} destination token binding mismatch")
                    try:
                        _assert_directory_identity(Path(replay[1]), replay[4])
                        with open_trusted_directory(replay[5]):
                            pass
                    except CapsuleExportError as exc:
                        raise ValueError(
                            f"{self._purpose} destination identity changed") from exc
                    except Exception as exc:
                        raise ValueError(
                            f"{self._purpose} destination identity changed") from exc
                    return replay[1]
            selected = self._items.get(supplied_token)
            if selected is None:
                raise ValueError(
                    f"{self._purpose} destination token is invalid or expired")
            binding = selected[2]
            self._items.pop(supplied_token, None)
            if expected_binding is not None and binding != str(expected_binding):
                raise ValueError(f"{self._purpose} destination token binding mismatch")
            try:
                _assert_directory_identity(Path(selected[0]), selected[3])
                with open_trusted_directory(selected[4]):
                    pass
            except CapsuleExportError as exc:
                raise ValueError(
                    f"{self._purpose} destination identity changed") from exc
            except Exception as exc:
                raise ValueError(
                    f"{self._purpose} destination identity changed") from exc
            if operation_key is not None:
                while len(self._consumed) >= self._max_items:
                    oldest = min(
                        self._consumed,
                        key=lambda key: self._consumed[key][2],
                    )
                    self._consumed.pop(oldest, None)
                self._consumed[operation_key] = (
                    supplied_token,
                    selected[0],
                    now,
                    binding,
                    selected[3],
                    selected[4],
                )
            return selected[0]

    def consume_trusted(
        self,
        token: str,
        *,
        expected_binding: str | None = None,
    ) -> TrustedDirectorySelection:
        """Consume one token as a pinned archive directory capability.

        This intentionally remains one-shot.  Capsule publication uses the
        idempotent ``consume`` path above because its durable receipt owns
        replay and crash recovery; archive dry-run confirmation instead passes
        this immutable selection directly to its atomic exporter.
        """

        with self._lock:
            now = float(self._clock())
            self._prune_expired_locked(now)
            selected = self._items.pop(str(token or ""), None)
        if selected is None:
            raise ValueError(
                f"{self._purpose} destination token is invalid or expired")
        binding = selected[2]
        if expected_binding is not None and binding != str(expected_binding):
            raise ValueError(f"{self._purpose} destination token binding mismatch")
        try:
            _assert_directory_identity(Path(selected[0]), selected[3])
            with open_trusted_directory(selected[4]):
                pass
        except Exception as exc:
            raise ValueError(
                f"{self._purpose} destination directory changed") from exc
        return selected[4]

    def destination_identity(
        self,
        token: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Return the already-consumed selection identity without exposing it."""

        with self._lock:
            selected = self._consumed.get(str(idempotency_key or "").strip())
            if selected is None or selected[0] != str(token or ""):
                raise ValueError(
                    f"{self._purpose} destination token is invalid or expired")
            return copy.deepcopy(selected[4])

    def recovery_candidates(self) -> tuple[tuple[Path, dict[str, Any]], ...]:
        """Return a bounded, de-duplicated set of private recovery locations."""

        with self._lock:
            now = float(self._clock())
            self._prune_expired_locked(now)
            values = [
                (item[0], item[3]) for item in self._items.values()
            ] + [
                (item[1], item[4]) for item in self._consumed.values()
            ]
        found: dict[str, tuple[Path, dict[str, Any]]] = {}
        for raw_path, identity in values:
            normalized = os.path.normcase(str(raw_path))
            found.setdefault(
                normalized, (Path(raw_path), copy.deepcopy(identity)))
        return tuple(found[key] for key in sorted(found))


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


class CapsuleExportError(RuntimeError):
    """A safe, path-free capsule transaction failure."""


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_transaction_json(path: Path, value: Mapping[str, Any]) -> None:
    authoritative = copy.deepcopy(dict(value))
    authoritative.pop("transaction_sha256", None)
    authoritative["transaction_sha256"] = _sha256_bytes(
        _canonical_bytes(authoritative))
    payload = _canonical_bytes(authoritative) + b"\n"
    if len(payload) > 1024 * 1024:
        raise CapsuleExportError("capsule transaction receipt is too large")
    if os.path.lexists(path):
        _physical_identity(path, expect_directory=False)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError as exc:
        raise CapsuleExportError("capsule transaction receipt write failed") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception as exc:
        if descriptor >= 0:
            os.close(descriptor)
        _safe_unlink(temporary)
        if isinstance(exc, CapsuleExportError):
            raise
        raise CapsuleExportError("capsule transaction receipt write failed") from exc


def _read_bounded_regular_file(
    path: Path,
    maximum: int,
) -> tuple[bytes, dict[str, Any]]:
    """Read one bounded regular entity without following its final component."""

    before = _physical_identity(path, expect_directory=False)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _descriptor_matches_identity(descriptor, opened, before)
        ):
            raise CapsuleExportError("capsule filesystem entity type changed")
        chunks = bytearray()
        while len(chunks) <= maximum:
            block = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > maximum or os.read(descriptor, 1):
            raise CapsuleExportError("capsule filesystem entity is too large")
        after_stat = os.fstat(descriptor)
        if (
            opened.st_dev != after_stat.st_dev
            or opened.st_ino != after_stat.st_ino
            or opened.st_size != after_stat.st_size
        ):
            raise CapsuleExportError("capsule filesystem entity changed")
    finally:
        os.close(descriptor)
    after = _physical_identity(path, expect_directory=False)
    if after != before:
        raise CapsuleExportError("capsule filesystem entity changed")
    return bytes(chunks), before


def _read_transaction_json(path: Path) -> dict[str, Any]:
    try:
        raw, _identity = _read_bounded_regular_file(path, 1024 * 1024)
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        if isinstance(exc, CapsuleExportError):
            raise
        raise CapsuleExportError("capsule transaction receipt is invalid") from exc
    if not isinstance(value, dict):
        raise CapsuleExportError("capsule transaction receipt is invalid")
    supplied = value.pop("transaction_sha256", None)
    if not isinstance(supplied, str) or not _HASH.fullmatch(supplied):
        raise CapsuleExportError("capsule transaction receipt is invalid")
    if not secrets.compare_digest(supplied, _sha256_bytes(_canonical_bytes(value))):
        raise CapsuleExportError("capsule transaction receipt is invalid")
    value["transaction_sha256"] = supplied
    return value


def _digest_bounded_regular_file(
    path: Path,
    maximum: int,
) -> tuple[str, int, dict[str, Any]]:
    before = _physical_identity(path, expect_directory=False)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _descriptor_matches_identity(descriptor, opened, before)
        ):
            raise CapsuleExportError("capsule filesystem entity type changed")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > maximum:
                raise CapsuleExportError("capsule filesystem entity is too large")
            digest.update(block)
        closed = os.fstat(descriptor)
        if (
            opened.st_dev != closed.st_dev
            or opened.st_ino != closed.st_ino
            or opened.st_size != closed.st_size
            or closed.st_size != size
        ):
            raise CapsuleExportError("capsule filesystem entity changed")
    finally:
        os.close(descriptor)
    after = _physical_identity(path, expect_directory=False)
    if after != before:
        raise CapsuleExportError("capsule filesystem entity changed")
    return digest.hexdigest(), size, before


def _atomic_no_replace(source: Path, target: Path) -> None:
    """Publish a complete same-directory file atomically without replacement."""

    if os.name == "nt":
        try:
            os.rename(source, target)
        except FileExistsError as exc:
            raise FileExistsError(
                "capsule destination already contains this archive") from exc
        except OSError as exc:
            raise CapsuleExportError(
                "atomic no-overwrite capsule publication failed") from exc
        return
    try:
        os.link(source, target)
    except FileExistsError as exc:
        raise FileExistsError(
            "capsule destination already contains this archive") from exc
    except OSError as exc:
        raise CapsuleExportError(
            "atomic no-overwrite capsule publication is unavailable") from exc
    else:
        source.unlink()


def _build_capsule_path(
    path: Path,
    bundle: FrozenRevision,
    notebook_payload: Mapping[str, Any],
    limits: CapsuleLimits,
) -> tuple[str, int, int]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CapsuleExportError(
            "capsule prepared archive could not be created") from exc
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            descriptor = -1
            return _write_capsule_archive(
                handle, bundle, notebook_payload, limits)
    except Exception as exc:
        if descriptor >= 0:
            os.close(descriptor)
        _safe_unlink(path)
        if isinstance(exc, (CapsuleQuotaError, CapsuleExportError, RuntimeError)):
            raise
        raise CapsuleExportError(
            "capsule prepared archive could not be created") from exc


def _report_binding(bundle: FrozenRevision) -> dict[str, Any]:
    source_hashes = {
        "spec_sha256": bundle.entry["spec_sha256"],
        "snapshot_sha256": bundle.entry["snapshot_sha256"],
        "validation_sha256": bundle.entry["validation_sha256"],
        "report_model_sha256": bundle.entry["report_model_sha256"],
        "manifest_sha256": bundle.entry["manifest_sha256"],
    }
    return {
        "revision": bundle.public_identity(),
        "source_hashes": source_hashes,
        "file_set_sha256": _sha256_bytes(_canonical_bytes(bundle.file_hashes)),
    }


def _project_binding(
    service: Any,
    path: str,
    *,
    expected_project_id: str | None,
    identity_fingerprint: str | None,
) -> dict[str, str]:
    try:
        context = service._project_context(str(path or "").strip())
    except Exception as exc:
        raise CapsuleExportError("capsule project identity is unavailable") from exc
    project_id = str(context.get("project_id") or "")
    if not project_id or (expected_project_id is not None
                          and project_id != str(expected_project_id)):
        raise CapsuleExportError("capsule project identity mismatch")
    fingerprint = str(identity_fingerprint or "").strip()
    if fingerprint and not _HASH.fullmatch(fingerprint):
        raise CapsuleExportError("capsule project identity fingerprint is invalid")
    physical = {
        "project_id": project_id,
        "project_path": os.path.normcase(os.path.realpath(str(
            context.get("project_path") or ""))),
        "project_root": os.path.normcase(os.path.realpath(str(
            context.get("project_root") or ""))),
    }
    context_sha256 = _sha256_bytes(_canonical_bytes(physical))
    return {
        "project_id": project_id,
        "context_sha256": context_sha256,
        "identity_fingerprint": fingerprint or context_sha256,
    }


def _trusted_notebook_snapshot(
    service: Any,
    path: str,
    bundle: FrozenRevision,
    maximum_member_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from vcstudio.project.research_notebook import ARCHIVE_SCHEMA

    provider = getattr(
        getattr(service, "_host", None),
        "_research_notebook_archive_payload",
        None,
    )
    if not callable(provider):
        raise CapsuleExportError(
            "trusted research notebook snapshot provider is unavailable")
    try:
        supplied = provider(path, bundle.project_id, bundle.revision_id)
    except Exception as exc:
        raise CapsuleExportError(
            "trusted research notebook snapshot is unavailable") from exc
    if not isinstance(supplied, Mapping):
        raise CapsuleExportError("trusted research notebook snapshot is invalid")
    snapshot = copy.deepcopy(dict(supplied))
    revision = snapshot.get("ledger_revision")
    head = snapshot.get("ledger_head_digest")
    records = snapshot.get("records")
    denominator = snapshot.get("denominator")
    limitations = snapshot.get("limitations")
    if (
        snapshot.get("schema") != ARCHIVE_SCHEMA
        or snapshot.get("project_id") != bundle.project_id
        or snapshot.get("bound_report_revision_id") != bundle.revision_id
        or snapshot.get("integrity_status") != "current"
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or (revision == 0 and head is not None)
        or (revision > 0 and (
            not isinstance(head, str) or not _HASH.fullmatch(head)))
        or not isinstance(records, list)
        or not isinstance(denominator, Mapping)
        or not isinstance(limitations, list)
        or revision != len(records)
    ):
        raise CapsuleExportError("trusted research notebook snapshot is invalid")
    counts = [denominator.get(key) for key in ("records", "active", "review_todo")]
    if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts)
            or counts[0] != len(records)
            or counts[1] > counts[0]
            or counts[2] > counts[1]):
        raise CapsuleExportError("trusted research notebook denominator is invalid")
    safe_snapshot = redact(snapshot)
    if not isinstance(safe_snapshot, dict):
        raise CapsuleExportError("trusted research notebook snapshot is invalid")
    snapshot_sha256 = _sha256_bytes(_bounded_canonical_bytes(
        safe_snapshot, maximum_member_bytes, newline=True))
    plan = {
        "provider": "project-research-notebook",
        "operation": "archive-payload",
        "mode": "frozen-preview-cas",
        "schema": ARCHIVE_SCHEMA,
        "bound_report_revision_id": bundle.revision_id,
    }
    binding = {
        "ledger_revision": revision,
        "ledger_head_digest": head,
        "snapshot_sha256": snapshot_sha256,
        "integrity_status": "current",
    }
    return safe_snapshot, plan, binding


@dataclass(frozen=True)
class ResearchNotebookArchiveProvider:
    """Adapt the frozen notebook ledger to the governed archive provider seam.

    The provider deliberately recaptures the selected ``ReportService``
    revision before asking the host for a notebook snapshot.  It therefore
    cannot turn a live notebook read into an attachment for a stale or merely
    caller-constructed report bundle.
    """

    service: Any
    project_path: str
    license_id: str = "NOASSERTION"
    attribution: str = ""
    redistributable: bool = False
    maximum_member_bytes: int = CAPSULE_MAX_MEMBER_BYTES

    provider_id = "project-research-notebook"

    def __post_init__(self) -> None:
        if not isinstance(self.redistributable, bool):
            raise TypeError("notebook redistributable declaration must be boolean")
        if (
            isinstance(self.maximum_member_bytes, bool)
            or not isinstance(self.maximum_member_bytes, int)
            or self.maximum_member_bytes <= 0
        ):
            raise ValueError("notebook attachment member limit is invalid")

    def frozen_attachments(self, bundle: FrozenRevision) -> Iterable[Any]:
        from vcstudio.project.reproducibility_archive import ArchiveAttachment

        if not isinstance(bundle, FrozenRevision):
            raise TypeError("frozen report revision is required")
        current = load_frozen_revision(
            self.service, self.project_path, bundle.revision_id)
        if (
            current.project_id != bundle.project_id
            or _report_binding(current) != _report_binding(bundle)
        ):
            raise StaleRevisionError(
                "notebook archive report revision binding changed")
        notebook, provider_plan, notebook_binding = _trusted_notebook_snapshot(
            self.service,
            self.project_path,
            current,
            self.maximum_member_bytes,
        )
        payloads = {
            "extensions/research-notebook/ledger.json": (
                "research_notebook_ledger",
                notebook,
            ),
            "extensions/research-notebook/limitations.json": (
                "research_notebook_limitations",
                {
                    "schema": "vcstudio.research-notebook-limitations/v1",
                    "project_id": current.project_id,
                    "bound_report_revision_id": current.revision_id,
                    "limitations": list(notebook.get("limitations") or []),
                },
            ),
        }
        for archive_path, (logical_role, payload) in sorted(payloads.items()):
            data = _bounded_canonical_bytes(
                redact(payload), self.maximum_member_bytes, newline=True)
            yield ArchiveAttachment(
                archive_path=archive_path,
                logical_role=logical_role,
                sha256=_sha256_bytes(data),
                size=len(data),
                license_id=str(redact(self.license_id)),
                attribution=str(redact(self.attribution)),
                redistributable=self.redistributable,
                third_party=False,
                sensitive_risk="low",
                data=data,
                metadata={
                    "provider_plan": copy.deepcopy(provider_plan),
                    "notebook_binding": copy.deepcopy(notebook_binding),
                    "report_binding": _report_binding(current),
                },
            )


@dataclass
class _CapsuleReceipt:
    receipt_id: str
    receipt_token: str
    idempotency_key: str
    request_sha256: str
    created_at: float
    expires_at: float
    project_binding: dict[str, str]
    report_binding: dict[str, Any]
    notebook_binding: dict[str, Any]
    provider_plan: dict[str, Any]
    destination_token: str
    destination_dir: Path
    destination_identity: dict[str, Any]
    target: Path
    temporary: Path
    archive_identity: dict[str, Any]
    transaction: Path
    archive_sha256: str
    archive_size: int
    member_count: int
    status: str
    result: dict[str, Any] | None
    failure_message: str | None


class CapsuleExportReceipts:
    """Bounded preview/confirm authority for immutable capsule exports."""

    DEFAULT_TTL_SECONDS = 15 * 60
    DEFAULT_MAX_ITEMS = 64

    def __init__(
        self,
        destinations: CapsuleDestinations,
        *,
        clock: Any = time.time,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_items: int = DEFAULT_MAX_ITEMS,
        limits: CapsuleLimits | None = None,
        fault_hook: Any = None,
    ) -> None:
        if not isinstance(destinations, CapsuleDestinations):
            raise TypeError("capsule receipt registry requires capsule destinations")
        if ttl_seconds <= 0 or max_items <= 0:
            raise ValueError("capsule receipt bounds must be positive")
        self.destinations = destinations
        self._clock = clock
        self._ttl_seconds = float(ttl_seconds)
        self._max_items = int(max_items)
        self._limits = limits or CapsuleLimits()
        self._fault_hook = fault_hook
        self._lock = threading.RLock()
        self._records: dict[str, _CapsuleReceipt] = {}
        self._idempotency: dict[str, str] = {}
        self._recover_startup()

    def _fault(self, stage: str) -> None:
        if callable(self._fault_hook):
            self._fault_hook(stage)

    @staticmethod
    def _operation_key(value: Any) -> str:
        key = str(value or "").strip()
        if not _TOKEN.fullmatch(key):
            raise ValueError("capsule idempotency key is invalid")
        return key

    def _discard(self, record: _CapsuleReceipt, *, transaction: bool) -> None:
        try:
            with _capsule_destination_lock(
                    record.destination_dir, record.destination_identity):
                live = self._load_transaction(
                    record.destination_dir, record.receipt_id)
                if (
                    live.request_sha256 != record.request_sha256
                    or live.archive_identity != record.archive_identity
                ):
                    return
                if self._matches_archive(record.temporary, record):
                    _safe_unlink(record.temporary)
                if transaction:
                    _safe_unlink(record.transaction)
                    expected_locator = self._locator_payload(record)
                    for locator in (
                        self._locator_path(record.receipt_id),
                        self._operation_locator_path(
                            record.idempotency_key, record.project_binding),
                    ):
                        if not os.path.lexists(locator):
                            continue
                        supplied = _read_transaction_json(locator)
                        supplied.pop("transaction_sha256", None)
                        if supplied == expected_locator:
                            _safe_unlink(locator)
                    _fsync_directory(record.destination_dir)
        except (CapsuleExportError, OSError):
            # Never delete an entity that no longer has a trustworthy receipt.
            return

    def _prune_locked(self, now: float) -> None:
        expired = [
            receipt_id for receipt_id, record in self._records.items()
            if now >= record.expires_at
        ]
        for receipt_id in expired:
            record = self._records.pop(receipt_id)
            self._idempotency.pop(record.idempotency_key, None)
            self._discard(record, transaction=True)

    def _evict_locked(self) -> None:
        while len(self._records) >= self._max_items:
            receipt_id = min(
                self._records,
                key=lambda item: self._records[item].created_at,
            )
            record = self._records.pop(receipt_id)
            self._idempotency.pop(record.idempotency_key, None)
            self._discard(record, transaction=True)

    @staticmethod
    def _transaction_payload(record: _CapsuleReceipt) -> dict[str, Any]:
        return {
            "schema": CAPSULE_RECEIPT_SCHEMA,
            "status": record.status,
            "receipt_id": record.receipt_id,
            "receipt_token": record.receipt_token,
            "confirmation_seal_sha256": _sha256_bytes(
                record.receipt_token.encode("utf-8")),
            "idempotency_key": record.idempotency_key,
            "operation_binding_sha256": _sha256_bytes(
                record.idempotency_key.encode("utf-8")),
            "request_sha256": record.request_sha256,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "project_binding": record.project_binding,
            "report_binding": record.report_binding,
            "notebook_binding": record.notebook_binding,
            "provider_plan": record.provider_plan,
            "destination_token": record.destination_token,
            "destination_binding_sha256": _sha256_bytes(
                record.destination_token.encode("utf-8")),
            "destination_identity": record.destination_identity,
            "archive": {
                "name": record.target.name,
                "sha256": record.archive_sha256,
                "size": record.archive_size,
                "member_count": record.member_count,
                "identity": record.archive_identity,
            },
            "temporary_name": record.temporary.name,
            "result": record.result,
            "failure": record.failure_message,
        }

    def _write_transaction(self, record: _CapsuleReceipt) -> None:
        _assert_directory_identity(
            record.destination_dir, record.destination_identity)
        _atomic_transaction_json(
            record.transaction, self._transaction_payload(record))
        self._ensure_locator(record)
        _assert_directory_identity(
            record.destination_dir, record.destination_identity)

    @staticmethod
    def _receipt_digest(receipt_id: str) -> str:
        return _sha256_bytes(receipt_id.encode("utf-8"))[:24]

    @classmethod
    def _transaction_path(cls, destination: Path, receipt_id: str) -> Path:
        return destination / f".vcstudio-capsule-{cls._receipt_digest(receipt_id)}.json"

    @classmethod
    def _locator_path(cls, receipt_id: str) -> Path:
        return _capsule_locator_root() / f"{cls._receipt_digest(receipt_id)}.json"

    @staticmethod
    def _operation_locator_path(
        operation_key: str,
        project_binding: Mapping[str, Any],
    ) -> Path:
        digest = _sha256_bytes(operation_key.encode("utf-8"))
        context = str(project_binding.get("context_sha256") or "")
        if not _HASH.fullmatch(context):
            raise CapsuleExportError("capsule project identity is invalid")
        return _capsule_locator_root() / f"{context}.{digest}.operation.json"

    @staticmethod
    def _locator_payload(record: _CapsuleReceipt) -> dict[str, Any]:
        return {
            "schema": CAPSULE_LOCATOR_SCHEMA,
            "receipt_id": record.receipt_id,
            "idempotency_key": record.idempotency_key,
            "operation_binding_sha256": _sha256_bytes(
                record.idempotency_key.encode("utf-8")),
            "request_sha256": record.request_sha256,
            "project_context_sha256": record.project_binding["context_sha256"],
            "destination_token": record.destination_token,
            "destination_binding_sha256": _sha256_bytes(
                record.destination_token.encode("utf-8")),
            "destination_path": str(record.destination_dir),
            "destination_identity": record.destination_identity,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
        }

    def _ensure_locator(self, record: _CapsuleReceipt) -> None:
        root = _capsule_locator_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        expected = self._locator_payload(record)
        for locator in (
            self._locator_path(record.receipt_id),
            self._operation_locator_path(
                record.idempotency_key, record.project_binding),
        ):
            if os.path.lexists(locator):
                current = _read_transaction_json(locator)
                supplied_checksum = current.pop("transaction_sha256", None)
                if current != expected or supplied_checksum is None:
                    raise CapsuleExportError("capsule receipt locator conflicts")
                continue
            _atomic_transaction_json(locator, expected)

    def _read_locator(self, receipt_id: str) -> dict[str, Any]:
        locator = self._locator_path(receipt_id)
        if not os.path.lexists(locator):
            raise CapsuleExportError("capsule receipt is invalid or expired")
        value = _read_transaction_json(locator)
        if (
            value.get("schema") != CAPSULE_LOCATOR_SCHEMA
            or value.get("receipt_id") != receipt_id
            or not isinstance(value.get("idempotency_key"), str)
            or not _TOKEN.fullmatch(value["idempotency_key"])
            or not isinstance(value.get("request_sha256"), str)
            or not _HASH.fullmatch(value["request_sha256"])
            or not isinstance(value.get("project_context_sha256"), str)
            or not _HASH.fullmatch(value["project_context_sha256"])
            or not isinstance(value.get("destination_token"), str)
            or not _TOKEN.fullmatch(value["destination_token"])
            or not isinstance(value.get("destination_path"), str)
            or not os.path.isabs(value["destination_path"])
            or not isinstance(value.get("destination_identity"), Mapping)
        ):
            raise CapsuleExportError("capsule receipt locator is invalid")
        for supplied, raw in (
            (value.get("operation_binding_sha256"), value["idempotency_key"]),
            (value.get("destination_binding_sha256"), value["destination_token"]),
        ):
            if (
                not isinstance(supplied, str)
                or not _HASH.fullmatch(supplied)
                or not secrets.compare_digest(
                    supplied, _sha256_bytes(raw.encode("utf-8")))
            ):
                raise CapsuleExportError("capsule receipt locator is invalid")
        expected_name = f"{self._receipt_digest(receipt_id)}.json"
        if locator.name != expected_name:
            raise CapsuleExportError("capsule receipt locator is invalid")
        return value

    def _record_from_locator(self, locator: Mapping[str, Any]) -> _CapsuleReceipt:
        receipt_id = str(locator["receipt_id"])
        destination = _absolute_no_follow_path(locator["destination_path"])
        expected_identity = copy.deepcopy(dict(locator["destination_identity"]))
        _assert_directory_identity(destination, expected_identity)
        record = self._load_transaction(destination, receipt_id)
        if (
            record.idempotency_key != locator["idempotency_key"]
            or record.request_sha256 != locator["request_sha256"]
            or record.destination_token != locator["destination_token"]
            or record.destination_identity != expected_identity
        ):
            raise CapsuleExportError("capsule receipt locator is invalid")
        return record

    def _recover_operation_locator_locked(
        self,
        operation_key: str,
        project_binding: Mapping[str, Any],
    ) -> _CapsuleReceipt | None:
        locator_path = self._operation_locator_path(
            operation_key, project_binding)
        if not os.path.lexists(locator_path):
            return None
        locator = _read_transaction_json(locator_path)
        if locator.get("idempotency_key") != operation_key:
            raise CapsuleExportError("capsule receipt locator is invalid")
        if locator.get("project_context_sha256") != project_binding.get(
                "context_sha256"):
            raise CapsuleExportError("capsule receipt locator is invalid")
        receipt_id = locator.get("receipt_id")
        if not isinstance(receipt_id, str):
            raise CapsuleExportError("capsule receipt locator is invalid")
        verified = self._read_locator(receipt_id)
        record = self._record_from_locator(verified)
        self._adopt_locked(record)
        return record

    def _record_from_transaction(
        self,
        destination: Path,
        transaction: Mapping[str, Any],
    ) -> _CapsuleReceipt:
        """Strictly reconstruct one path-free receipt authority."""

        value = dict(transaction)
        receipt_id = value.get("receipt_id")
        receipt_token = value.get("receipt_token")
        operation_key = value.get("idempotency_key")
        destination_token = value.get("destination_token")
        request_sha256 = value.get("request_sha256")
        status = value.get("status")
        created_at = value.get("created_at")
        expires_at = value.get("expires_at")
        project_binding = value.get("project_binding")
        report_binding = value.get("report_binding")
        notebook_binding = value.get("notebook_binding")
        provider_plan = value.get("provider_plan")
        destination_identity = value.get("destination_identity")
        archive = value.get("archive")
        if (
            value.get("schema") != CAPSULE_RECEIPT_SCHEMA
            or status not in {"prepared", "publishing", "succeeded", "invalid"}
            or not isinstance(receipt_id, str)
            or not receipt_id.startswith("capsule-receipt.")
            or not _TOKEN.fullmatch(receipt_id)
            or not isinstance(receipt_token, str)
            or not receipt_token.startswith("capsule-confirm.")
            or not _TOKEN.fullmatch(receipt_token)
            or not isinstance(operation_key, str)
            or not _TOKEN.fullmatch(operation_key)
            or not isinstance(destination_token, str)
            or not _TOKEN.fullmatch(destination_token)
            or not isinstance(request_sha256, str)
            or not _HASH.fullmatch(request_sha256)
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(created_at))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= float(created_at)
            or not isinstance(project_binding, Mapping)
            or not isinstance(report_binding, Mapping)
            or not isinstance(notebook_binding, Mapping)
            or not isinstance(provider_plan, Mapping)
            or not isinstance(destination_identity, Mapping)
            or not isinstance(archive, Mapping)
        ):
            raise CapsuleExportError("capsule transaction receipt is invalid")
        for supplied, raw in (
            (value.get("confirmation_seal_sha256"), receipt_token),
            (value.get("operation_binding_sha256"), operation_key),
            (value.get("destination_binding_sha256"), destination_token),
        ):
            if (
                not isinstance(supplied, str)
                or not _HASH.fullmatch(supplied)
                or not secrets.compare_digest(
                    supplied, _sha256_bytes(raw.encode("utf-8")))
            ):
                raise CapsuleExportError("capsule transaction receipt is invalid")
        revision = report_binding.get("revision")
        revision_id = revision.get("revision_id") if isinstance(revision, Mapping) else None
        project_id = project_binding.get("project_id")
        if (
            not isinstance(project_id, str)
            or not project_id
            or not isinstance(revision_id, str)
            or not _TOKEN.fullmatch(revision_id)
            or request_sha256 != _sha256_bytes(_canonical_bytes({
                "project_binding": dict(project_binding),
                "revision_id": revision_id,
                "destination_token": destination_token,
            }))
        ):
            raise CapsuleExportError("capsule transaction receipt is invalid")
        safe_revision = re.sub(r"[^A-Za-z0-9._-]", "-", revision_id)
        name = archive.get("name")
        archive_sha256 = archive.get("sha256")
        archive_size = archive.get("size")
        member_count = archive.get("member_count")
        archive_identity = archive.get("identity")
        receipt_digest = self._receipt_digest(receipt_id)
        temporary_name = value.get("temporary_name")
        expected_temporary = f".{safe_revision}.{receipt_digest}.capsule.tmp"
        expected_transaction = self._transaction_path(destination, receipt_id)
        if (
            name != f"{safe_revision}-si-capsule.zip"
            or not isinstance(archive_sha256, str)
            or not _HASH.fullmatch(archive_sha256)
            or isinstance(archive_size, bool)
            or not isinstance(archive_size, int)
            or archive_size <= 0
            or archive_size > self._limits.max_archive_bytes
            or isinstance(member_count, bool)
            or not isinstance(member_count, int)
            or member_count <= 0
            or member_count > self._limits.max_members
            or not isinstance(archive_identity, Mapping)
            or temporary_name != expected_temporary
            or not os.path.lexists(expected_transaction)
        ):
            raise CapsuleExportError("capsule transaction receipt is invalid")
        frozen_identity = copy.deepcopy(dict(destination_identity))
        _assert_directory_identity(destination, frozen_identity)
        result = value.get("result")
        failure = value.get("failure")
        if result is not None and not isinstance(result, Mapping):
            raise CapsuleExportError("capsule transaction receipt is invalid")
        if failure is not None and not isinstance(failure, str):
            raise CapsuleExportError("capsule transaction receipt is invalid")
        return _CapsuleReceipt(
            receipt_id=receipt_id,
            receipt_token=receipt_token,
            idempotency_key=operation_key,
            request_sha256=request_sha256,
            created_at=float(created_at),
            expires_at=float(expires_at),
            project_binding=copy.deepcopy(dict(project_binding)),
            report_binding=copy.deepcopy(dict(report_binding)),
            notebook_binding=copy.deepcopy(dict(notebook_binding)),
            provider_plan=copy.deepcopy(dict(provider_plan)),
            destination_token=destination_token,
            destination_dir=destination,
            destination_identity=frozen_identity,
            target=destination / str(name),
            temporary=destination / expected_temporary,
            archive_identity=copy.deepcopy(dict(archive_identity)),
            transaction=expected_transaction,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
            member_count=member_count,
            status=str(status),
            result=(None if result is None else copy.deepcopy(dict(result))),
            failure_message=failure,
        )

    def _load_transaction(
        self,
        destination: Path,
        receipt_id: str,
    ) -> _CapsuleReceipt:
        transaction_path = self._transaction_path(destination, receipt_id)
        if not os.path.lexists(transaction_path):
            raise CapsuleExportError("capsule receipt is invalid or expired")
        transaction = _read_transaction_json(transaction_path)
        if transaction.get("receipt_id") != receipt_id:
            raise CapsuleExportError("capsule transaction receipt is invalid")
        return self._record_from_transaction(destination, transaction)

    def _adopt_locked(self, record: _CapsuleReceipt) -> None:
        receipt_conflict = self._records.get(record.receipt_id)
        operation_receipt = self._idempotency.get(record.idempotency_key)
        if (
            receipt_conflict is not None
            and receipt_conflict.request_sha256 != record.request_sha256
        ) or (
            operation_receipt is not None
            and operation_receipt != record.receipt_id
        ):
            raise CapsuleExportError("capsule transaction receipt conflicts")
        self._records[record.receipt_id] = record
        self._idempotency[record.idempotency_key] = record.receipt_id

    def _recover_destination_locked(
        self,
        destination: Path,
        identity: Mapping[str, Any],
    ) -> None:
        _assert_directory_identity(destination, identity)
        entries = []
        try:
            with os.scandir(destination) as scanner:
                for entry in scanner:
                    if re.fullmatch(r"\.vcstudio-capsule-[0-9a-f]{24}\.json", entry.name):
                        entries.append(entry.name)
                        if len(entries) > self._max_items:
                            raise CapsuleExportError(
                                "capsule transaction receipt bound exceeded")
        except OSError as exc:
            raise CapsuleExportError("capsule receipt recovery failed") from exc
        now = float(self._clock())
        for name in sorted(entries):
            path = destination / name
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                record = self._record_from_transaction(
                    destination, _read_transaction_json(path))
                if self._transaction_path(destination, record.receipt_id).name != name:
                    continue
                if now >= record.expires_at:
                    continue
                self._adopt_locked(record)
            except CapsuleExportError:
                continue

    def _recover_startup(self) -> None:
        with self._lock:
            for destination, identity in self.destinations.recovery_candidates():
                try:
                    with _capsule_destination_lock(destination, identity):
                        self._recover_destination_locked(destination, identity)
                except (CapsuleExportError, OSError):
                    continue

    def _preview_dto(
        self, record: _CapsuleReceipt, *, replayed: bool,
    ) -> dict[str, Any]:
        return {
            "schema": CAPSULE_PREVIEW_SCHEMA,
            "ok": True,
            "status": ("complete" if record.status == "succeeded"
                       else "awaiting_confirmation"),
            "project_id": record.project_binding["project_id"],
            "revision": copy.deepcopy(record.report_binding["revision"]),
            "notebook": copy.deepcopy(record.notebook_binding),
            "provider_plan": copy.deepcopy(record.provider_plan),
            "destination_binding_sha256": _sha256_bytes(
                record.destination_token.encode("utf-8")),
            "receipt_id": record.receipt_id,
            "receipt_token": record.receipt_token,
            "archive": {
                "name": record.target.name,
                "sha256": record.archive_sha256,
                "size": record.archive_size,
                "member_count": record.member_count,
            },
            "ttl_seconds": self._ttl_seconds,
            "replayed": bool(replayed),
            "file": (None if record.result is None
                     else copy.deepcopy(record.result["file"])),
            "error": None,
        }

    def preview(
        self,
        service: Any,
        path: str,
        revision_id: str,
        destination_token: str,
        idempotency_key: str,
        *,
        project_id: str | None = None,
        identity_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        operation_key = self._operation_key(idempotency_key)
        wanted_revision = str(revision_id or "").strip()
        supplied_destination = str(destination_token or "").strip()
        if not _TOKEN.fullmatch(wanted_revision):
            raise ValueError("capsule revision_id is invalid")
        if not _TOKEN.fullmatch(supplied_destination):
            raise ValueError("capsule destination token is invalid")
        project_binding = _project_binding(
            service, path, expected_project_id=project_id,
            identity_fingerprint=identity_fingerprint)
        request_sha256 = _sha256_bytes(_canonical_bytes({
            "project_binding": project_binding,
            "revision_id": wanted_revision,
            "destination_token": supplied_destination,
        }))
        operation_binding = _sha256_bytes(operation_key.encode("utf-8"))
        with _exclusive_file_lock(_operation_lock_path(operation_binding)):
            with self._lock:
                now = float(self._clock())
                self._prune_locked(now)
                existing_id = self._idempotency.get(operation_key)
                if existing_id is not None:
                    existing = self._records[existing_id]
                    with _capsule_destination_lock(
                            existing.destination_dir,
                            existing.destination_identity):
                        existing = self._load_transaction(
                            existing.destination_dir, existing.receipt_id)
                        self._adopt_locked(existing)
                    if existing.request_sha256 != request_sha256:
                        raise ValueError(
                            "capsule idempotency key was reused for different input")
                    if existing.status == "invalid":
                        raise CapsuleExportError(
                            existing.failure_message or "capsule preview is invalid")
                    return self._preview_dto(existing, replayed=True)

                recovered = self._recover_operation_locator_locked(
                    operation_key, project_binding)
                if recovered is not None:
                    if now >= recovered.expires_at:
                        raise CapsuleExportError(
                            "capsule receipt is invalid or expired")
                    if recovered.request_sha256 != request_sha256:
                        raise ValueError(
                            "capsule idempotency key was reused for different input")
                    if recovered.status == "invalid":
                        raise CapsuleExportError(
                            recovered.failure_message
                            or "capsule preview is invalid")
                    return self._preview_dto(recovered, replayed=True)

                destination = self.destinations.consume(
                    supplied_destination,
                    idempotency_key=("preview-" + request_sha256),
                )
                destination_dir = Path(destination)
                destination_identity = self.destinations.destination_identity(
                    supplied_destination,
                    idempotency_key=("preview-" + request_sha256),
                )
                self._evict_locked()
                with _capsule_destination_lock(
                        destination_dir, destination_identity):
                    self._recover_destination_locked(
                        destination_dir, destination_identity)
                    existing_id = self._idempotency.get(operation_key)
                    if existing_id is not None:
                        existing = self._records[existing_id]
                        if existing.request_sha256 != request_sha256:
                            raise ValueError(
                                "capsule idempotency key was reused for different input")
                        if existing.status == "invalid":
                            raise CapsuleExportError(
                                existing.failure_message or "capsule preview is invalid")
                        return self._preview_dto(existing, replayed=True)
                    bundle = load_frozen_revision(service, path, wanted_revision)
                    if bundle.project_id != project_binding["project_id"]:
                        raise CapsuleExportError(
                            "capsule report project binding mismatch")
                    notebook, provider_plan, notebook_binding = (
                        _trusted_notebook_snapshot(
                            service, path, bundle,
                            self._limits.max_member_bytes))
                    report_binding = _report_binding(bundle)
                    # Complete every count/member/aggregate quota plan before the
                    # first temporary archive entity is materialized.
                    _capsule_plan(bundle, notebook, self._limits)
                    safe_revision = re.sub(
                        r"[^A-Za-z0-9._-]", "-", bundle.revision_id)
                    target = destination_dir / f"{safe_revision}-si-capsule.zip"
                    if os.path.lexists(target):
                        raise FileExistsError(
                            "capsule destination already contains this archive")
                    receipt_id = "capsule-receipt." + secrets.token_urlsafe(24)
                    receipt_token = "capsule-confirm." + secrets.token_urlsafe(32)
                    receipt_digest = self._receipt_digest(receipt_id)
                    temporary = destination_dir / (
                        f".{safe_revision}.{receipt_digest}.capsule.tmp")
                    transaction = self._transaction_path(
                        destination_dir, receipt_id)
                    archive_sha256, archive_size, member_count = (
                        _build_capsule_path(
                            temporary, bundle, notebook, self._limits))
                    archive_identity = _physical_identity(
                        temporary, expect_directory=False)
                    record = _CapsuleReceipt(
                        receipt_id=receipt_id,
                        receipt_token=receipt_token,
                        idempotency_key=operation_key,
                        request_sha256=request_sha256,
                        created_at=now,
                        expires_at=now + self._ttl_seconds,
                        project_binding=project_binding,
                        report_binding=report_binding,
                        notebook_binding=notebook_binding,
                        provider_plan=provider_plan,
                        destination_token=supplied_destination,
                        destination_dir=destination_dir,
                        destination_identity=copy.deepcopy(destination_identity),
                        target=target,
                        temporary=temporary,
                        archive_identity=archive_identity,
                        transaction=transaction,
                        archive_sha256=archive_sha256,
                        archive_size=archive_size,
                        member_count=member_count,
                        status="prepared",
                        result=None,
                        failure_message=None,
                    )
                    try:
                        self._write_transaction(record)
                    except Exception:
                        if self._matches_archive(temporary, record):
                            _safe_unlink(temporary)
                        raise
                self._adopt_locked(record)
                self._fault("after_temp")
                return self._preview_dto(record, replayed=False)

    def _matches_archive(self, path: Path, record: _CapsuleReceipt) -> bool:
        try:
            digest, size, identity = _digest_bounded_regular_file(
                path, self._limits.max_archive_bytes)
            return (
                size == record.archive_size
                and digest == record.archive_sha256
                and identity == record.archive_identity
            )
        except (OSError, CapsuleExportError):
            return False

    def _invalidate(self, record: _CapsuleReceipt, message: str) -> None:
        record.status = "invalid"
        record.failure_message = message
        if self._matches_archive(record.temporary, record):
            _safe_unlink(record.temporary)
        try:
            self._write_transaction(record)
        except Exception:  # noqa: BLE001 invalidation must not mask the CAS failure
            pass

    def _finish_success(
        self, record: _CapsuleReceipt, *, replayed: bool,
    ) -> dict[str, Any]:
        result = {
            "schema": CAPSULE_SCHEMA,
            "ok": True,
            "status": "ready",
            "project_id": record.project_binding["project_id"],
            "revision": copy.deepcopy(record.report_binding["revision"]),
            "notebook": copy.deepcopy(record.notebook_binding),
            "provider_plan": copy.deepcopy(record.provider_plan),
            "receipt_id": record.receipt_id,
            "file": {
                "name": record.target.name,
                "sha256": record.archive_sha256,
                "size": record.archive_size,
            },
            "replayed": bool(replayed),
            "error": None,
        }
        record.result = copy.deepcopy(result)
        record.status = "succeeded"
        self._write_transaction(record)
        return result

    def _recover_receipt_locked(self, receipt_id: str) -> _CapsuleReceipt:
        if os.path.lexists(self._locator_path(receipt_id)):
            record = self._record_from_locator(self._read_locator(receipt_id))
            self._adopt_locked(record)
            return record
        matches: list[_CapsuleReceipt] = []
        saw_invalid = False
        for destination, identity in self.destinations.recovery_candidates():
            transaction = self._transaction_path(destination, receipt_id)
            if not os.path.lexists(transaction):
                continue
            try:
                with _capsule_destination_lock(destination, identity):
                    matches.append(self._load_transaction(destination, receipt_id))
            except CapsuleExportError:
                saw_invalid = True
        if saw_invalid:
            raise CapsuleExportError("capsule transaction receipt is invalid")
        if len(matches) != 1:
            raise CapsuleExportError("capsule receipt is invalid or expired")
        self._adopt_locked(matches[0])
        return matches[0]

    def confirm(
        self,
        service: Any,
        path: str,
        receipt_id: str,
        receipt_token: str,
        idempotency_key: str,
        *,
        project_id: str | None = None,
        identity_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        operation_key = self._operation_key(idempotency_key)
        supplied_receipt_id = str(receipt_id or "").strip()
        supplied_receipt_token = str(receipt_token or "")
        if (
            not supplied_receipt_id.startswith("capsule-receipt.")
            or not _TOKEN.fullmatch(supplied_receipt_id)
            or not supplied_receipt_token.startswith("capsule-confirm.")
            or not _TOKEN.fullmatch(supplied_receipt_token)
        ):
            raise CapsuleExportError("capsule receipt is invalid or expired")
        current_project = _project_binding(
            service, path, expected_project_id=project_id,
            identity_fingerprint=identity_fingerprint)
        operation_binding = _sha256_bytes(operation_key.encode("utf-8"))
        with _exclusive_file_lock(_operation_lock_path(operation_binding)):
            with self._lock:
                now = float(self._clock())
                self._prune_locked(now)
                record = self._records.get(supplied_receipt_id)
                if record is None:
                    record = self._recover_receipt_locked(supplied_receipt_id)
                with _capsule_destination_lock(
                        record.destination_dir, record.destination_identity):
                    # The on-disk receipt is the authority at every confirm and
                    # after every possible cross-process publication boundary.
                    record = self._load_transaction(
                        record.destination_dir, supplied_receipt_id)
                    self._adopt_locked(record)
                    if now >= record.expires_at:
                        raise CapsuleExportError(
                            "capsule receipt is invalid or expired")
                    if not secrets.compare_digest(
                            record.receipt_token, supplied_receipt_token):
                        raise CapsuleExportError("capsule receipt token is invalid")
                    if operation_key != record.idempotency_key:
                        raise ValueError(
                            "capsule idempotency key was reused for different input")
                    if current_project != record.project_binding:
                        self._invalidate(
                            record, "capsule project identity changed")
                        raise CapsuleExportError(
                            "capsule project identity changed")
                    if record.status == "succeeded":
                        if not self._matches_archive(record.target, record):
                            raise CapsuleExportError(
                                "capsule final archive verification failed")
                        replay = copy.deepcopy(record.result)
                        if replay is None:
                            raise CapsuleExportError(
                                "capsule receipt result is unavailable")
                        replay["replayed"] = True
                        return replay
                    if record.status == "invalid":
                        raise CapsuleExportError(
                            record.failure_message
                            or "capsule receipt is invalid")
                    if record.status == "publishing" and self._matches_archive(
                            record.target, record):
                        if self._matches_archive(record.temporary, record):
                            _safe_unlink(record.temporary)
                        return self._finish_success(record, replayed=True)
                    if record.status == "prepared" and os.path.lexists(record.target):
                        self._invalidate(
                            record,
                            "capsule destination already contains this archive")
                        raise FileExistsError(
                            "capsule destination already contains this archive")
                    if not self._matches_archive(record.temporary, record):
                        self._invalidate(
                            record, "capsule prepared archive is unavailable")
                        raise CapsuleExportError(
                            "capsule prepared archive is unavailable")
                    try:
                        bundle = load_frozen_revision(
                            service, path,
                            str(record.report_binding[
                                "revision"]["revision_id"]),
                        )
                        if _report_binding(bundle) != record.report_binding:
                            raise StaleRevisionError(
                                "capsule report revision changed after preview")
                        _notebook, provider_plan, notebook_binding = (
                            _trusted_notebook_snapshot(
                                service, path, bundle,
                                self._limits.max_member_bytes))
                        if (provider_plan != record.provider_plan
                                or notebook_binding != record.notebook_binding):
                            raise StaleRevisionError(
                                "research notebook changed after capsule preview")
                    except Exception as exc:
                        message = (
                            str(exc) if isinstance(
                                exc, (StaleRevisionError, CapsuleExportError))
                            else "capsule compare-and-swap validation failed"
                        )
                        safe_message = redact(message)
                        self._invalidate(record, str(safe_message))
                        if isinstance(
                                exc, (StaleRevisionError, CapsuleExportError)):
                            raise
                        raise CapsuleExportError(
                            "capsule compare-and-swap validation failed") from exc

                    _assert_directory_identity(
                        record.destination_dir, record.destination_identity)
                    record.status = "publishing"
                    self._write_transaction(record)
                    _assert_directory_identity(
                        record.destination_dir, record.destination_identity)
                    self._fault("before_rename")
                    try:
                        _assert_directory_identity(
                            record.destination_dir,
                            record.destination_identity)
                        _atomic_no_replace(record.temporary, record.target)
                        _assert_directory_identity(
                            record.destination_dir,
                            record.destination_identity)
                        _fsync_directory(record.destination_dir)
                        self._fault("after_rename")
                    except Exception:
                        if (os.path.lexists(record.target)
                                and not self._matches_archive(
                                    record.target, record)):
                            self._invalidate(
                                record,
                                "capsule destination contains different bytes")
                        raise
                    if not self._matches_archive(record.target, record):
                        self._invalidate(
                            record, "capsule final archive verification failed")
                        raise CapsuleExportError(
                            "capsule final archive verification failed")
                    return self._finish_success(record, replayed=False)


def export_capsule(service: Any, path: str, revision_id: str,
                   destination_dir: str) -> dict[str, Any]:
    """Reject the retired live-read/write seam.

    Callers must use :class:`CapsuleExportReceipts.preview` and then confirm the
    returned receipt.  Retaining this named guard makes legacy bridges fail
    closed instead of silently bypassing the transaction.
    """

    del service, path, revision_id, destination_dir
    raise CapsuleExportError(
        "capsule export requires a fresh preview and confirmation receipt")


__all__ = [
    "CAPSULE_PREVIEW_SCHEMA", "CAPSULE_RECEIPT_SCHEMA", "CAPSULE_SCHEMA",
    "DESTINATION_SCHEMA", "DIFF_SCHEMA", "GRAPH_SCHEMA",
    "CapsuleDestinations", "CapsuleExportError", "CapsuleExportReceipts",
    "CapsuleLimits", "CapsuleQuotaError", "OpaqueDestinationRegistry",
    "ResearchNotebookArchiveProvider",
    "build_capsule_bytes", "capsule_members",
    "TrustedDirectoryHandle", "TrustedDirectorySelection", "capture_trusted_directory",
    "evidence_graph", "export_capsule", "load_frozen_revision", "redact",
    "open_trusted_directory", "scientific_diff", "StaleRevisionError",
]
