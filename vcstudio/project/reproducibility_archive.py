"""Deterministic, local-only archives for frozen report revisions.

The archive layer deliberately does not decide scientific validity.  It starts
from :func:`report_insights.load_frozen_revision`, which replays the existing
``ReportService`` history and host audit, and packages only members already
bound by that authoritative report bundle.  Optional integrations contribute
attachments through the small provider protocol below; the core never imports
an integration module.

Two phases are intentionally separate:

``build_archive_plan``
    Revalidate a revision and return a path-free dry-run with every inclusion
    and exclusion decision.

``export_archive``
    Revalidate and rebuild the same plan, compare its semantic hash with the
    confirmed dry-run, then publish a verified ZIP atomically without replacing
    an existing filename.

No function in this module uploads a file, contacts a repository, or requests
a DOI.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import ntpath
import os
import re
import stat
import threading
import uuid
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Protocol, Sequence

from vcstudio import __version__
from vcstudio.project.report_insights import (
    FrozenRevision,
    StaleRevisionError,
    evidence_graph,
    load_frozen_revision,
    redact,
)
from vcstudio.project.report_service import _exclusive_file_lock


ARCHIVE_SCHEMA = "vcstudio.vcs-archive/v1"
PLAN_SCHEMA = "vcstudio.vcs-archive-plan/v1"
RESULT_SCHEMA = "vcstudio.vcs-archive-result/v1"
ATTACHMENT_SCHEMA = "vcstudio.vcs-archive-attachment/v1"
ARCHIVE_VERSION = 1

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WINDOWS_PATH = re.compile(r"(?i)(?:^|[\s\"'(=])(?:[a-z]:[\\/]|\\\\)")
_POSIX_PRIVATE_PATH = re.compile(
    r"(?:^|[\s\"'(=])/(?:Users|home|root|tmp|var/tmp|private|mnt|opt|srv)(?:/|\b)"
)
_FILE_URI = re.compile(r"(?i)\bfile:(?:/{1,3}|\\)")
_SECRET_LITERAL = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bBearer\s+(?!\[redacted)[^\s<]+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|https?://[^\s/:]+:[^\s/@]+@)"
)
_POTCAR_RAW = re.compile(
    r"(?is)(?:parameters\s+from\s+PSCTR\s+are:|End\s+of\s+Dataset)"
)
_POTCAR_KEYS = frozenset({
    "potcar_text", "potcar_content", "raw_potcar", "potcar_raw", "pseudopotential_text",
})
_LICENSE_KEYS = (
    "archive_license", "dataset_license", "data_license", "license", "licence", "rights",
)
_ATTRIBUTION_KEYS = ("attribution", "credit", "creator", "creators", "authors")
_RESERVED_WINDOWS = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})
_TEXT_SUFFIXES = frozenset({
    ".bib", ".cff", ".csv", ".html", ".json", ".md", ".svg", ".txt", ".xml", ".yaml", ".yml",
})


class ArchiveVerificationError(RuntimeError):
    """A generated or existing archive failed integrity verification."""


@dataclass(frozen=True)
class ArchiveAttachment:
    """One optional, already-frozen attachment supplied by an extension.

    Providers must supply an expected hash and size even for inline payloads.
    Path-backed files are opened with the same regular-file and TOCTOU checks as
    report artifacts.  Third-party attachments are distributable only when the
    provider explicitly declares a license, attribution, and redistributability.
    """

    archive_path: str
    logical_role: str
    sha256: str
    size: int
    license_id: str
    attribution: str
    redistributable: bool
    third_party: bool = False
    sensitive_risk: str = "low"
    data: bytes | None = field(default=None, repr=False, compare=False)
    source_path: str | os.PathLike[str] | None = field(default=None, repr=False, compare=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


class ArchiveAttachmentProvider(Protocol):
    """Extension seam for frozen notebook ledgers or other governed exports."""

    provider_id: str

    def frozen_attachments(self, bundle: FrozenRevision) -> Iterable[ArchiveAttachment]: ...


@dataclass(frozen=True)
class _PreparedMember:
    archive_path: str
    logical_role: str
    data: bytes = field(repr=False, compare=False)
    license_id: str = "NOASSERTION"
    attribution: str = ""
    license_status: str = "missing"
    sensitive_risk: str = "low"
    authority: str = "frozen_report_revision"
    provider_id: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def decision(self) -> dict[str, Any]:
        return {
            "archive_path": self.archive_path,
            "logical_role": self.logical_role,
            "size": len(self.data),
            "sha256": self.sha256,
            "license": self.license_id,
            "license_status": self.license_status,
            "attribution": self.attribution,
            "sensitive_risk": self.sensitive_risk,
            "authority": self.authority,
            "provider_id": self.provider_id,
            "decision": "include",
            "exclusion_reason": None,
        }


@dataclass(frozen=True)
class ArchivePlan:
    """Private immutable plan plus its path-free public dry-run projection."""

    project_id: str
    revision_id: str
    report_id: str
    source_manifest_sha256: str
    plan_sha256: str
    archive_name: str
    archive_sha256: str
    archive_size: int
    readiness: Mapping[str, Any]
    decisions: tuple[Mapping[str, Any], ...]
    providers: tuple[ArchiveAttachmentProvider, ...] = field(
        default_factory=tuple, repr=False, compare=False)
    _archive_bytes: bytes = field(default=b"", repr=False, compare=False)

    def public_summary(self) -> dict[str, Any]:
        included = sum(1 for item in self.decisions if item.get("decision") == "include")
        excluded = len(self.decisions) - included
        return {
            "schema": PLAN_SCHEMA,
            "ok": True,
            "status": "dry_run_ready",
            "project_id": self.project_id,
            "revision": {
                "report_id": self.report_id,
                "revision_id": self.revision_id,
                "manifest_sha256": self.source_manifest_sha256,
            },
            "archive": {
                "name": self.archive_name,
                "sha256": self.archive_sha256,
                "size": self.archive_size,
                "version": ARCHIVE_VERSION,
            },
            "plan_sha256": self.plan_sha256,
            "readiness": copy.deepcopy(dict(self.readiness)),
            "denominator": {
                "decisions": len(self.decisions),
                "included": included,
                "excluded": excluded,
            },
            "decisions": copy.deepcopy([dict(item) for item in self.decisions]),
            "boundaries": {
                "local_only": True,
                "uploaded": False,
                "doi_requested": False,
                "doi_assigned": False,
                "scientific_gate_reused": True,
            },
            "error": None,
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _canonical_json_file(value: Any) -> bytes:
    return _canonical_bytes(_archive_safe_value(value)) + b"\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _archive_safe_value(value: Any, *, key: str = "", parent_key: str = "") -> Any:
    """Redact public risks and replace any embedded raw POTCAR payload."""

    normalized = str(key).strip().lower().replace("-", "_")
    parent = str(parent_key).strip().lower().replace("-", "_")
    if normalized in _POTCAR_KEYS:
        return "[excluded-potcar-content]"
    if isinstance(value, Mapping):
        potcar_context = "potcar" in normalized or "potcar" in parent
        lowered = {
            str(item_key).lower().replace("-", "_"): item_value
            for item_key, item_value in value.items()
        }
        if potcar_context and any(
            item_key in lowered
            for item_key in ("element", "symbol", "variant", "titel", "title", "sha256")
        ):
            identity_source = {
                item_key: redact(item_value, key=item_key)
                for item_key, item_value in sorted(lowered.items())
                if item_key in {"element", "symbol", "variant", "titel", "title", "sha256"}
            }
            element = identity_source.get("element") or identity_source.get("symbol")
            result = {
                "identity_sha256": _sha256(_canonical_bytes(identity_source)),
                "license_notice": (
                    "POTCAR contents are excluded. Use only a separately licensed local PAW dataset."
                ),
            }
            if element not in (None, ""):
                result["element"] = element
            return result
        return {
            str(item_key): _archive_safe_value(
                item_value, key=str(item_key), parent_key=normalized or parent,
            )
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [
            _archive_safe_value(item, parent_key=normalized or parent) for item in value
        ]
    safe = redact(value, key=key)
    if isinstance(safe, str):
        if ("potcar" in normalized or "potcar" in parent) and _POTCAR_RAW.search(safe):
            return "[excluded-potcar-content]"
        if _POTCAR_RAW.search(safe):
            return "[excluded-potcar-content]"
    return safe


def _safe_archive_path(value: str) -> str:
    name = str(value or "")
    if not name or "\\" in name or "\x00" in name:
        raise ValueError("archive member path is invalid")
    if name.startswith("/") or ntpath.isabs(name) or ntpath.splitdrive(name)[0]:
        raise ValueError("archive member path must be relative")
    pure = PurePosixPath(name)
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("archive member path contains traversal")
    for part in parts:
        if part.endswith((" ", ".")) or any(ord(char) < 32 for char in part):
            raise ValueError("archive member path is not portable")
        if part.split(".", 1)[0].upper() in _RESERVED_WINDOWS:
            raise ValueError("archive member path uses a reserved device name")
    return pure.as_posix()


def _payload_risks(name: str, data: bytes) -> list[str]:
    risks: list[str] = []
    basename = PurePosixPath(name).name.upper()
    if basename == "POTCAR" or basename.startswith("POTCAR."):
        risks.append("potcar_raw_filename")
    decoded = data.decode("utf-8", errors="ignore")
    if _POTCAR_RAW.search(decoded):
        risks.append("potcar_raw_content")
    if _SECRET_LITERAL.search(decoded):
        risks.append("secret_literal")
    if _WINDOWS_PATH.search(decoded) or _FILE_URI.search(decoded):
        risks.append("absolute_path")
    if PurePosixPath(name).suffix.lower() in _TEXT_SUFFIXES and _POSIX_PRIVATE_PATH.search(decoded):
        risks.append("absolute_path")
    return sorted(set(risks))


def _safe_read_bound_file(
    path: str | os.PathLike[str], *, expected_sha256: str, expected_size: int,
) -> bytes:
    """Read one hash-bound regular file without following a swapped symlink."""

    expected = str(expected_sha256 or "").lower()
    if not _HASH.fullmatch(expected):
        raise ValueError("authoritative member SHA-256 is invalid")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError("authoritative member size is invalid")
    candidate = os.fspath(path)
    before = os.lstat(candidate)
    if _is_symlink_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("authoritative member is not a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        before_identity = (before.st_dev, before.st_ino)
        opened_identity = (opened.st_dev, opened.st_ino)
        if before_identity != opened_identity or not stat.S_ISREG(opened.st_mode):
            raise StaleRevisionError("authoritative member changed during open")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > expected_size:
                raise StaleRevisionError("authoritative member size changed during read")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != opened_identity
            or after.st_size != opened.st_size
            or getattr(after, "st_mtime_ns", None) != getattr(opened, "st_mtime_ns", None)
        ):
            raise StaleRevisionError("authoritative member changed during read")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) != expected_size or _sha256(payload) != expected:
        raise StaleRevisionError("authoritative member hash binding changed")
    return payload


def _is_symlink_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & 0x400
    )


def _safe_json_object(data: bytes, *, role: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt evidence must fail closed
        raise StaleRevisionError(f"{role} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise StaleRevisionError(f"{role} must be a JSON object")
    return value


def _recapture_frozen_revision(bundle: FrozenRevision) -> FrozenRevision:
    """Recapture manifest/contracts/model through hash-bound regular-file FDs."""

    manifest_path = Path(str(bundle.entry["manifest"]))
    manifest_lstat = os.lstat(manifest_path)
    if _is_symlink_or_reparse(manifest_lstat) or not stat.S_ISREG(manifest_lstat.st_mode):
        raise StaleRevisionError("report manifest is not a regular non-symlink file")
    manifest_data = _safe_read_bound_file(
        manifest_path,
        expected_sha256=str(bundle.entry["manifest_sha256"]),
        expected_size=manifest_lstat.st_size,
    )
    manifest = _safe_json_object(manifest_data, role="report manifest")

    model_record = manifest.get("model_file")
    if not isinstance(model_record, Mapping):
        raise StaleRevisionError("report manifest model record is invalid")
    model_data = _safe_read_bound_file(
        _manifest_record_path(bundle, model_record),
        expected_sha256=str(model_record.get("sha256") or ""),
        expected_size=model_record.get("size"),
    )
    model = _safe_json_object(model_data, role="report model")

    contracts = manifest.get("contracts")
    if not isinstance(contracts, Mapping) or set(contracts) != {
        "spec", "snapshot", "validation",
    }:
        raise StaleRevisionError("report manifest contracts are incomplete")
    captured_contracts: dict[str, dict[str, Any]] = {}
    file_hashes = {
        "manifest": _sha256(manifest_data),
        "model": _sha256(model_data),
    }
    for name in ("spec", "snapshot", "validation"):
        record = contracts[name]
        if not isinstance(record, Mapping):
            raise StaleRevisionError("report manifest contract record is invalid")
        data = _safe_read_bound_file(
            _manifest_record_path(bundle, record),
            expected_sha256=str(record.get("file_sha256") or ""),
            expected_size=record.get("size"),
        )
        captured_contracts[name] = _safe_json_object(data, role=f"report {name}")
        file_hashes[f"contract:{name}"] = _sha256(data)

    # Scientific/semantic validation remains exclusively owned by
    # ReportService and its host audit.  This recapture layer verifies only that
    # the bytes being archived are the exact file-hash-bound members that the
    # already-completed authoritative audit accepted; it must not introduce a
    # second scientific gate with subtly different semantics.
    if manifest != bundle.manifest:
        raise StaleRevisionError("report manifest changed during archive capture")
    return replace(
        bundle,
        manifest=manifest,
        spec=captured_contracts["spec"],
        snapshot=captured_contracts["snapshot"],
        validation=captured_contracts["validation"],
        model=model,
        file_hashes={**bundle.file_hashes, **file_hashes},
    )


def _license_record(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        identifier = str(redact(value)).strip()
        return (identifier or "NOASSERTION", "declared" if identifier else "missing")
    if isinstance(value, Mapping):
        for key in ("spdx", "spdx_id", "license_id", "id", "name", "url"):
            candidate = value.get(key)
            if candidate:
                identifier = str(redact(candidate, key=key)).strip()
                if identifier and "redacted" not in identifier:
                    return identifier, "declared"
    return "NOASSERTION", "missing"


def _first_mapping_value(sources: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Any:
    for source in sources:
        for key in keys:
            if source.get(key) not in (None, "", [], {}):
                return source[key]
    return None


def _citation_sources(bundle: FrozenRevision) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for source in (
        bundle.model.get("citation"), bundle.model.get("metadata"), bundle.model,
        bundle.snapshot.get("metadata"), bundle.snapshot,
    ):
        if isinstance(source, Mapping):
            result.append(source)
    return result


def _attribution_text(value: Any) -> str:
    if isinstance(value, str):
        safe = str(redact(value)).strip()
        return "" if "redacted" in safe else safe
    if isinstance(value, Mapping):
        for key in ("name", "family-names", "family_names", "title"):
            if value.get(key):
                return _attribution_text(value[key])
    if isinstance(value, (list, tuple)):
        names = [_attribution_text(item) for item in value]
        return "; ".join(item for item in names if item)
    return ""


def _project_rights(bundle: FrozenRevision) -> tuple[str, str, str]:
    sources = _citation_sources(bundle)
    license_id, status = _license_record(_first_mapping_value(sources, _LICENSE_KEYS))
    attribution = _attribution_text(_first_mapping_value(sources, _ATTRIBUTION_KEYS))
    return license_id, status, attribution


def _citation_authors(bundle: FrozenRevision) -> list[dict[str, str]]:
    raw = _first_mapping_value(_citation_sources(bundle), ("authors", "creators"))
    if isinstance(raw, (str, Mapping)):
        raw = [raw]
    authors: list[dict[str, str]] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(item, str):
            name = _attribution_text(item)
            if name:
                authors.append({"name": name})
            continue
        if not isinstance(item, Mapping):
            continue
        author: dict[str, str] = {}
        aliases = {
            "family-names": ("family-names", "family_names", "family", "last_name"),
            "given-names": ("given-names", "given_names", "given", "first_name"),
            "name": ("name",),
            "orcid": ("orcid",),
        }
        for target, keys in aliases.items():
            for key in keys:
                if item.get(key):
                    value = _attribution_text(item[key])
                    if value:
                        author[target] = value
                    break
        if author.get("family-names") or author.get("name"):
            authors.append(author)
    return authors


def _yaml_scalar(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _citation_cff(bundle: FrozenRevision, authors: Sequence[Mapping[str, str]]) -> bytes:
    title = str(redact(bundle.model.get("title") or f"Report {bundle.entry['report_id']}"))
    lines = [
        "cff-version: 1.2.0",
        "message: " + _yaml_scalar(
            "Review the archive readiness record before citing or depositing this frozen revision."
        ),
        "type: dataset",
        "title: " + _yaml_scalar(title),
        "version: " + _yaml_scalar(bundle.revision_id),
    ]
    if authors:
        lines.append("authors:")
        for author in authors:
            ordered = [key for key in ("family-names", "given-names", "name", "orcid") if author.get(key)]
            lines.append(f"  - {ordered[0]}: {_yaml_scalar(author[ordered[0]])}")
            for key in ordered[1:]:
                lines.append(f"    {key}: {_yaml_scalar(author[key])}")
    else:
        lines.append("authors: []")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _potcar_identity_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            normalized = {str(key).lower().replace("-", "_"): val for key, val in item.items()}
            potcar_context = "potcar" in parent or any("potcar" in key for key in normalized)
            identity_source = {}
            if potcar_context or any(key in normalized for key in ("titel", "variant")):
                aliases = {
                    "element": ("element", "symbol"),
                    "variant": ("variant", "label", "name"),
                    "titel": ("titel", "title"),
                    "sha256": ("sha256", "digest", "hash"),
                }
                for target, keys in aliases.items():
                    for key in keys:
                        if normalized.get(key) not in (None, ""):
                            safe = _archive_safe_value(normalized[key], key=key, parent_key="potcar")
                            if isinstance(safe, (str, int, float, bool)):
                                identity_source[target] = safe
                            break
                if identity_source:
                    # Public archives never repeat TITEL/variant strings or a
                    # reversible POTCAR fragment.  The identity digest binds the
                    # complete frozen identity record while the element remains
                    # useful for a depositor's license checklist.
                    identity = {
                        "element": identity_source.get("element"),
                        "identity_sha256": _sha256(_canonical_bytes(identity_source)),
                    }
                    if identity["element"] in (None, ""):
                        identity.pop("element")
                    identity["license_notice"] = (
                        "POTCAR contents are excluded. Use only a separately licensed local PAW dataset."
                    )
                    marker = _sha256(_canonical_bytes(identity))
                    if marker not in seen:
                        seen.add(marker)
                        records.append(identity)
            for key, child in item.items():
                visit(child, f"{parent}.{str(key).lower()}" if parent else str(key).lower())
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, parent)

    visit(value)
    records.sort(key=lambda item: _canonical_bytes(item))
    return records


def _parser_records(bundle: FrozenRevision) -> list[dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}

    def visit(value: Any, parent: str = "") -> None:
        if isinstance(value, Mapping):
            lowered = {str(key).lower().replace("-", "_"): item for key, item in value.items()}
            parser_context = "parser" in parent or any("parser" in key for key in lowered)
            if parser_context:
                name = lowered.get("parser_id") or lowered.get("parser") or lowered.get("name")
                version = lowered.get("parser_version") or lowered.get("version")
                if name or version:
                    safe_name = str(_archive_safe_value(name or "unknown-parser"))
                    safe_version = str(_archive_safe_value(version or "unknown"))
                    records[(safe_name, safe_version)] = {
                        "parser": safe_name, "version": safe_version,
                    }
            for key, item in value.items():
                visit(item, f"{parent}.{str(key).lower()}" if parent else str(key).lower())
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, parent)

    visit(bundle.snapshot)
    visit(bundle.model)
    records[("vcstudio", str(__version__))] = {
        "parser": "vcstudio", "version": str(__version__),
    }
    return [records[key] for key in sorted(records)]


def _manifest_record_path(bundle: FrozenRevision, record: Mapping[str, Any]) -> Path:
    relative = str(record.get("path") or "")
    safe = _safe_archive_path(relative)
    base = Path(str(bundle.entry["manifest"])).parent.resolve()
    candidate = (base / Path(*PurePosixPath(safe).parts)).resolve()
    try:
        if os.path.commonpath((str(base), str(candidate))) != str(base):
            raise ValueError("authoritative member escapes the report bundle")
    except ValueError as exc:
        raise ValueError("authoritative member escapes the report bundle") from exc
    return candidate


def _matching_figure(bundle: FrozenRevision, digest: str) -> Mapping[str, Any]:
    for figure in bundle.model.get("figures") or []:
        if isinstance(figure, Mapping) and str(
            figure.get("asset_sha256") or figure.get("sha256") or ""
        ).lower() == digest:
            return figure
    return {}


def _third_party_rights(record: Mapping[str, Any]) -> tuple[bool, bool, str, str, str]:
    source_kind = str(record.get("source_kind") or record.get("origin") or "").lower()
    third_party = bool(record.get("third_party")) or source_kind in {
        "external", "published", "third_party", "third-party",
    }
    redistributable = record.get("redistributable") is True
    license_id, license_status = _license_record(
        _first_mapping_value([record], _LICENSE_KEYS)
    )
    attribution = _attribution_text(_first_mapping_value([record], _ATTRIBUTION_KEYS))
    return third_party, redistributable, license_id, license_status, attribution


def _excluded_decision(
    *, archive_path: str | None, logical_role: str, reason: str,
    sensitive_risk: str, size: int | None = None, sha256: str | None = None,
    license_id: str = "NOASSERTION", license_status: str = "missing",
    attribution: str = "", authority: str = "policy", provider_id: str | None = None,
) -> dict[str, Any]:
    return {
        "archive_path": archive_path,
        "logical_role": logical_role,
        "size": size,
        "sha256": sha256,
        "license": license_id,
        "license_status": license_status,
        "attribution": attribution,
        "sensitive_risk": sensitive_risk,
        "authority": authority,
        "provider_id": provider_id,
        "decision": "exclude",
        "exclusion_reason": reason,
    }


def _append_member(
    members: list[_PreparedMember], exclusions: list[dict[str, Any]], member: _PreparedMember,
) -> None:
    try:
        safe_name = _safe_archive_path(member.archive_path)
    except ValueError:
        exclusions.append(_excluded_decision(
            archive_path=None, logical_role=member.logical_role,
            reason="unsafe_archive_path", sensitive_risk="high",
            size=len(member.data), sha256=member.sha256,
            license_id=member.license_id, license_status=member.license_status,
            attribution=member.attribution, authority=member.authority,
            provider_id=member.provider_id,
        ))
        return
    normalized = _PreparedMember(**{**member.__dict__, "archive_path": safe_name})
    risks = _payload_risks(safe_name, member.data)
    if risks:
        exclusions.append(_excluded_decision(
            archive_path=safe_name, logical_role=member.logical_role,
            reason=";".join(risks), sensitive_risk="high",
            size=len(member.data), sha256=member.sha256,
            license_id=member.license_id, license_status=member.license_status,
            attribution=member.attribution, authority=member.authority,
            provider_id=member.provider_id,
        ))
        return
    members.append(normalized)


def _core_member(
    path: str, role: str, data: bytes, rights: tuple[str, str, str],
    *, risk: str = "low", authority: str = "frozen_report_revision",
) -> _PreparedMember:
    license_id, license_status, attribution = rights
    return _PreparedMember(
        path, role, data, license_id, attribution, license_status, risk, authority,
    )


def _provider_members(
    bundle: FrozenRevision,
    providers: Sequence[ArchiveAttachmentProvider],
    members: list[_PreparedMember],
    exclusions: list[dict[str, Any]],
) -> None:
    for provider in providers:
        provider_id = str(getattr(provider, "provider_id", "") or "").strip()
        if not _TOKEN.fullmatch(provider_id):
            exclusions.append(_excluded_decision(
                archive_path=None, logical_role="extension_attachment",
                reason="provider_id_invalid", sensitive_risk="high",
                authority="extension_provider",
            ))
            continue
        try:
            attachments = list(provider.frozen_attachments(bundle))
        except Exception:
            exclusions.append(_excluded_decision(
                archive_path=None, logical_role="extension_attachment",
                reason="provider_failed", sensitive_risk="unknown",
                authority="extension_provider", provider_id=provider_id,
            ))
            continue
        for raw in attachments:
            if not isinstance(raw, ArchiveAttachment):
                exclusions.append(_excluded_decision(
                    archive_path=None, logical_role="extension_attachment",
                    reason="attachment_contract_invalid", sensitive_risk="high",
                    authority="extension_provider", provider_id=provider_id,
                ))
                continue
            path = None
            try:
                path = _safe_archive_path(raw.archive_path)
                if not _HASH.fullmatch(str(raw.sha256).lower()):
                    raise ValueError("attachment hash invalid")
                if isinstance(raw.size, bool) or not isinstance(raw.size, int) or raw.size < 0:
                    raise ValueError("attachment size invalid")
                if (raw.data is None) == (raw.source_path is None):
                    raise ValueError("attachment must use exactly one payload source")
                if raw.data is not None:
                    payload = bytes(raw.data)
                    if len(payload) != raw.size or _sha256(payload) != raw.sha256.lower():
                        raise StaleRevisionError("attachment payload binding mismatch")
                else:
                    payload = _safe_read_bound_file(
                        raw.source_path, expected_sha256=raw.sha256,
                        expected_size=raw.size,
                    )
            except Exception as exc:  # noqa: BLE001 - one extension cannot weaken the archive
                reason = "symbolic_link_or_toctou_rejected" if (
                    "symlink" in str(exc).lower() or "changed" in str(exc).lower()
                ) else "attachment_contract_invalid"
                exclusions.append(_excluded_decision(
                    archive_path=path, logical_role=raw.logical_role,
                    reason=reason, sensitive_risk="high", size=raw.size,
                    sha256=(raw.sha256 if _HASH.fullmatch(str(raw.sha256).lower()) else None),
                    license_id=str(raw.license_id or "NOASSERTION"),
                    license_status=("declared" if raw.license_id and raw.license_id != "NOASSERTION" else "missing"),
                    attribution=str(redact(raw.attribution)), authority="extension_provider",
                    provider_id=provider_id,
                ))
                continue
            license_id = str(raw.license_id or "NOASSERTION").strip() or "NOASSERTION"
            attribution = str(redact(raw.attribution)).strip()
            license_status = "declared" if license_id != "NOASSERTION" else "missing"
            if raw.third_party and (
                not raw.redistributable or license_status != "declared" or not attribution
            ):
                exclusions.append(_excluded_decision(
                    archive_path=path, logical_role=raw.logical_role,
                    reason="third_party_license_or_attribution_missing",
                    sensitive_risk=raw.sensitive_risk, size=len(payload),
                    sha256=_sha256(payload), license_id=license_id,
                    license_status=license_status, attribution=attribution,
                    authority="extension_provider", provider_id=provider_id,
                ))
                continue
            _append_member(members, exclusions, _PreparedMember(
                archive_path=path, logical_role=raw.logical_role, data=payload,
                license_id=license_id, attribution=attribution,
                license_status=license_status, sensitive_risk=raw.sensitive_risk,
                authority="extension_provider", provider_id=provider_id,
            ))


def _static_policy_exclusions() -> list[dict[str, Any]]:
    policies = (
        ("**/POTCAR", "licensed_pseudopotential_content", "raw_potcar_forbidden", "critical"),
        ("**/*secret*; **/*key*; credential stores", "credentials", "secrets_forbidden", "critical"),
        ("absolute filesystem locators", "local_paths", "absolute_paths_forbidden", "high"),
        ("**/__pycache__/**; **/.cache/**", "cache", "cache_not_authoritative", "low"),
        ("**/*.tmp; **/~*; staging/recovery files", "temporary_files", "temporary_files_not_authoritative", "low"),
        ("live project/job files outside the frozen report bundle", "mutable_live_files", "mutable_live_files_forbidden", "high"),
        ("unlicensed third-party sources", "third_party_data", "license_or_attribution_required", "high"),
    )
    return [
        _excluded_decision(
            archive_path=pattern, logical_role=role, reason=reason,
            sensitive_risk=risk, authority="archive_policy",
        )
        for pattern, role, reason, risk in policies
    ]


def _readiness(
    bundle: FrozenRevision,
    graph: Mapping[str, Any],
    members: Sequence[_PreparedMember],
    exclusions: Sequence[Mapping[str, Any]],
    authors: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    gaps: list[dict[str, str]] = []

    def gap(code: str, message: str) -> None:
        if code not in {item["code"] for item in gaps}:
            gaps.append({"code": code, "message": message})

    if not authors:
        gap("citation_authors_missing", "CITATION.cff has no authoritative author record.")
    if any(member.license_status != "declared" for member in members):
        gap("license_assertion_missing", "One or more project-controlled members use NOASSERTION.")
    if graph.get("status") != "ready":
        gap("provenance_graph_incomplete", "The frozen evidence graph has unresolved links.")
    if str(bundle.entry.get("scientific_status") or "") != "final":
        gap("scientific_revision_not_final", "The selected revision is not scientifically final.")
    if not any(member.logical_role == "rendered_report" for member in members):
        gap("rendered_report_missing", "No authoritative rendered report could be included.")
    expected_assets = len(bundle.manifest.get("assets") or [])
    included_assets = sum(member.logical_role == "report_figure" for member in members)
    if included_assets != expected_assets:
        gap("report_figures_incomplete", "Not every authoritative report figure is distributable.")
    high_exclusions = [
        item for item in exclusions
        if item.get("authority") != "archive_policy" and item.get("sensitive_risk") in {"high", "critical"}
    ]
    if high_exclusions:
        gap("governed_member_excluded", "At least one discovered governed member was excluded.")
    gaps.sort(key=lambda item: item["code"])
    return {
        "status": "ready_for_human_submission_review" if not gaps else "not_ready",
        "machine_integrity": "pass",
        "scientific_status": str(bundle.entry.get("scientific_status") or "unknown"),
        "scientific_qualification": str(bundle.entry.get("scientific_qualification") or "unknown"),
        "human_depositor_review": "required",
        "repository_submission": "not_performed",
        "doi_request": "not_performed",
        "gaps": gaps,
    }


def _readme(bundle: FrozenRevision, readiness: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]) -> bytes:
    included = sum(item.get("decision") == "include" for item in decisions)
    excluded = len(decisions) - included
    gaps = readiness.get("gaps") or []
    lines = [
        "# VASP Catalyst Studio reproducibility archive",
        "",
        "This deterministic local archive was built from a revalidated, frozen ReportService revision.",
        "It does not upload data, create a repository deposit, request a DOI, or promote scientific status.",
        "",
        "## Frozen binding",
        "",
        f"- Project ID: `{bundle.project_id}`",
        f"- Report ID: `{bundle.entry['report_id']}`",
        f"- Revision ID: `{bundle.revision_id}`",
        f"- Source manifest SHA-256: `{bundle.entry['manifest_sha256']}`",
        f"- Scientific status: `{bundle.entry.get('scientific_status') or 'unknown'}`",
        f"- Scientific qualification: `{bundle.entry.get('scientific_qualification') or 'unknown'}`",
        "",
        "## Archive policy",
        "",
        "Only hash-bound report members and explicitly governed provider attachments are eligible.",
        "Raw POTCAR data, secrets, absolute paths, caches, temporary files, mutable live files,",
        "and unlicensed third-party data are excluded. POTCAR records contain identity only.",
        "",
        "## Dry-run denominator",
        "",
        f"- Included decisions: {included}",
        f"- Excluded decisions: {excluded}",
        f"- Submission readiness: `{readiness.get('status')}`",
        "",
        "## Readiness gaps",
        "",
    ]
    if gaps:
        lines.extend(f"- `{item['code']}`: {item['message']}" for item in gaps)
    else:
        lines.append("- No machine-detected gap; human depositor review is still required.")
    lines.extend([
        "",
        "## Verification",
        "",
        "Verify every line in `SHA256SUMS`, then inspect `metadata/archive-manifest.json`.",
        "Archive integrity is not evidence that a journal, repository, or human reviewer accepted it.",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


def _archive_bytes(members: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True,
    ) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def _semantic_plan_payload(
    bundle: FrozenRevision,
    readiness: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": PLAN_SCHEMA,
        "archive_version": ARCHIVE_VERSION,
        "project_id": bundle.project_id,
        "report_id": bundle.entry["report_id"],
        "revision_id": bundle.revision_id,
        "source_manifest_sha256": bundle.entry["manifest_sha256"],
        "readiness": readiness,
        "decisions": decisions,
    }


def build_archive_plan(
    service: Any,
    path: str,
    revision_id: str,
    *,
    attachment_providers: Sequence[ArchiveAttachmentProvider] = (),
) -> ArchivePlan:
    """Build a deterministic, path-free dry-run from one revalidated revision."""

    providers = tuple(attachment_providers)
    bundle = _recapture_frozen_revision(
        load_frozen_revision(service, path, revision_id)
    )
    graph = evidence_graph(
        service, path, revision_id, frozen_bundle=bundle,
    )
    rights = _project_rights(bundle)
    members: list[_PreparedMember] = []
    exclusions = _static_policy_exclusions()

    contract_payloads = {
        "contracts/report-spec.json": ("frozen_report_spec", bundle.spec),
        "contracts/report-snapshot.json": ("frozen_report_snapshot", bundle.snapshot),
        "contracts/validation-result.json": ("frozen_validation", bundle.validation),
        "model/report-model.json": ("frozen_report_model", bundle.model),
        "metadata/source-report-manifest.json": ("source_report_manifest", bundle.manifest),
        "provenance/evidence-graph.json": ("provenance_graph", graph),
    }
    for archive_path, (role, payload) in sorted(contract_payloads.items()):
        _append_member(
            members, exclusions,
            _core_member(archive_path, role, _canonical_json_file(payload), rights),
        )

    input_manifest = {
        "input_fingerprint": bundle.snapshot.get("input_fingerprint"),
        "snapshot_sha256": bundle.entry["snapshot_sha256"],
        "resolved_scope": bundle.snapshot.get("resolved_scope") or {},
        "sources": bundle.snapshot.get("sources") or [],
    }
    _append_member(members, exclusions, _core_member(
        "inputs/input-manifest.json", "frozen_input_manifest",
        _canonical_json_file(input_manifest), rights,
    ))
    potcar_identities = _potcar_identity_records({
        "snapshot": bundle.snapshot, "model": bundle.model,
    })
    _append_member(members, exclusions, _core_member(
        "inputs/potcar-identities.json", "potcar_irreversible_identity",
        _canonical_json_file({
            "records": potcar_identities,
            "license_notice": (
                "No POTCAR content is distributed. Supply a separately licensed local PAW dataset."
            ),
        }), rights, risk="license_notice",
    ))

    methods = {
        key: bundle.model.get(key)
        for key in ("methods", "methodology", "calculation_details", "method_consistency")
        if bundle.model.get(key) is not None
    }
    _append_member(members, exclusions, _core_member(
        "methods/methods.json", "frozen_methods", _canonical_json_file(methods), rights,
    ))
    _append_member(members, exclusions, _core_member(
        "environment/environment.json", "frozen_environment",
        _canonical_json_file({
            "vcstudio_version": __version__,
            "frozen_environment": bundle.snapshot.get("environment") or bundle.model.get("environment") or {},
            "contract_schemas": {
                "spec": bundle.spec.get("schema"),
                "snapshot": bundle.snapshot.get("schema"),
                "validation": bundle.validation.get("schema"),
            },
        }), rights,
    ))
    _append_member(members, exclusions, _core_member(
        "environment/parsers.json", "parser_identity_and_version",
        _canonical_json_file({"parsers": _parser_records(bundle)}), rights,
    ))

    manifest_files = bundle.manifest.get("files") or {}
    for fmt, record in sorted(manifest_files.items()):
        if not isinstance(record, Mapping):
            raise StaleRevisionError("report manifest format record is invalid")
        expected_hash = str(record.get("sha256") or "").lower()
        expected_size = record.get("size")
        source = _manifest_record_path(bundle, record)
        payload = _safe_read_bound_file(
            source, expected_sha256=expected_hash, expected_size=expected_size,
        )
        _append_member(members, exclusions, _core_member(
            f"artifacts/reports/report.{fmt}", "rendered_report", payload, rights,
        ))

    for record in bundle.manifest.get("assets") or []:
        if not isinstance(record, Mapping):
            raise StaleRevisionError("report manifest asset record is invalid")
        expected_hash = str(record.get("sha256") or "").lower()
        expected_size = record.get("size")
        figure = _matching_figure(bundle, expected_hash)
        third_party, redistributable, license_id, license_status, attribution = (
            _third_party_rights(figure)
        )
        archive_path = f"artifacts/figures/{PurePosixPath(str(record.get('path') or '')).name}"
        if third_party and (not redistributable or license_status != "declared" or not attribution):
            exclusions.append(_excluded_decision(
                archive_path=archive_path, logical_role="report_figure",
                reason="third_party_license_or_attribution_missing",
                sensitive_risk="high", size=expected_size, sha256=expected_hash,
                license_id=license_id, license_status=license_status,
                attribution=attribution, authority="frozen_report_revision",
            ))
            continue
        source = _manifest_record_path(bundle, record)
        payload = _safe_read_bound_file(
            source, expected_sha256=expected_hash, expected_size=expected_size,
        )
        asset_rights = (
            (license_id, license_status, attribution)
            if license_status == "declared" else rights
        )
        _append_member(members, exclusions, _core_member(
            archive_path, "report_figure", payload, asset_rights,
        ))

    _provider_members(bundle, providers, members, exclusions)

    lowered_names: dict[str, str] = {}
    unique_members: list[_PreparedMember] = []
    for member in sorted(members, key=lambda item: item.archive_path):
        folded = member.archive_path.casefold()
        if folded in lowered_names:
            exclusions.append(_excluded_decision(
                archive_path=member.archive_path, logical_role=member.logical_role,
                reason="portable_name_collision", sensitive_risk="high",
                size=len(member.data), sha256=member.sha256,
                license_id=member.license_id, license_status=member.license_status,
                attribution=member.attribution, authority=member.authority,
                provider_id=member.provider_id,
            ))
        else:
            lowered_names[folded] = member.archive_path
            unique_members.append(member)
    members = unique_members

    authors = _citation_authors(bundle)
    readiness = _readiness(bundle, graph, members, exclusions, authors)
    preliminary_decisions = [member.decision() for member in members] + exclusions
    preliminary_decisions.sort(key=lambda item: (
        0 if item.get("decision") == "include" else 1,
        str(item.get("archive_path") or ""), str(item.get("logical_role") or ""),
    ))

    _append_member(members, exclusions, _core_member(
        "CITATION.cff", "citation_metadata", _citation_cff(bundle, authors), rights,
    ))
    _append_member(members, exclusions, _core_member(
        "README.md", "archive_readme", _readme(bundle, readiness, preliminary_decisions), rights,
    ))
    license_inventory = {
        "project_rights": {
            "license": rights[0], "license_status": rights[1], "attribution": rights[2],
        },
        "notice": (
            "Software licensing does not automatically grant rights to project data. "
            "NOASSERTION requires depositor review before repository submission."
        ),
    }
    _append_member(members, exclusions, _core_member(
        "LICENSES/README.json", "license_inventory",
        _canonical_json_file(license_inventory), rights,
    ))

    decisions_without_manifest = [member.decision() for member in members] + exclusions
    decisions_without_manifest.sort(key=lambda item: (
        0 if item.get("decision") == "include" else 1,
        str(item.get("archive_path") or ""), str(item.get("logical_role") or ""),
    ))
    archive_manifest = {
        "schema": ARCHIVE_SCHEMA,
        "archive_version": ARCHIVE_VERSION,
        "tool": {"name": "vcstudio", "version": __version__},
        "source_binding": {
            "project_id": bundle.project_id,
            "report_id": bundle.entry["report_id"],
            "revision_id": bundle.revision_id,
            "sequence": bundle.entry["sequence"],
            "source_manifest_sha256": bundle.entry["manifest_sha256"],
            "spec_sha256": bundle.entry["spec_sha256"],
            "snapshot_sha256": bundle.entry["snapshot_sha256"],
            "validation_sha256": bundle.entry["validation_sha256"],
            "report_model_sha256": bundle.entry["report_model_sha256"],
        },
        "determinism": {
            "member_order": "UTF-8 path sort",
            "timestamp": "1980-01-01T00:00:00Z",
            "compression": "stored",
            "json": "UTF-8 RFC8259 canonical key order",
            "permissions": "0644 regular file",
        },
        "readiness": readiness,
        "decisions": decisions_without_manifest,
        "files": [
            {
                "name": member.archive_path,
                "logical_role": member.logical_role,
                "sha256": member.sha256,
                "size": len(member.data),
                "license": member.license_id,
                "license_status": member.license_status,
                "attribution": member.attribution,
                "authority": member.authority,
                "provider_id": member.provider_id,
            }
            for member in sorted(members, key=lambda item: item.archive_path)
        ],
        "boundaries": {
            "local_only": True, "uploaded": False, "doi_requested": False,
            "doi_assigned": False, "scientific_status_promoted": False,
        },
    }
    manifest_member = _core_member(
        "metadata/archive-manifest.json", "archive_manifest",
        _canonical_json_file(archive_manifest), rights,
    )
    _append_member(members, exclusions, manifest_member)

    all_member_bytes = {
        member.archive_path: member.data for member in sorted(members, key=lambda item: item.archive_path)
    }
    sums = "".join(
        f"{_sha256(data)}  {name}\n" for name, data in sorted(all_member_bytes.items())
    ).encode("ascii")
    sums_member = _core_member("SHA256SUMS", "checksum_index", sums, rights)
    _append_member(members, exclusions, sums_member)
    all_member_bytes = {
        member.archive_path: member.data for member in sorted(members, key=lambda item: item.archive_path)
    }

    final_decisions = [member.decision() for member in members] + exclusions
    final_decisions.sort(key=lambda item: (
        0 if item.get("decision") == "include" else 1,
        str(item.get("archive_path") or ""), str(item.get("logical_role") or ""),
    ))
    semantic = _semantic_plan_payload(bundle, readiness, final_decisions)
    plan_sha256 = _sha256(_canonical_bytes(semantic))
    safe_revision = re.sub(r"[^A-Za-z0-9._-]", "-", bundle.revision_id)
    archive_name = (
        f"vcs-archive-{safe_revision}-{bundle.entry['manifest_sha256'][:12]}-"
        f"{plan_sha256[:12]}-v{ARCHIVE_VERSION}.zip"
    )
    payload = _archive_bytes(all_member_bytes)
    verification = verify_archive_bytes(payload)
    if verification["ok"] is not True:
        raise ArchiveVerificationError(str(verification.get("error") or "archive verification failed"))
    return ArchivePlan(
        project_id=bundle.project_id,
        revision_id=bundle.revision_id,
        report_id=str(bundle.entry["report_id"]),
        source_manifest_sha256=str(bundle.entry["manifest_sha256"]),
        plan_sha256=plan_sha256,
        archive_name=archive_name,
        archive_sha256=_sha256(payload),
        archive_size=len(payload),
        readiness=readiness,
        decisions=tuple(copy.deepcopy(final_decisions)),
        providers=providers,
        _archive_bytes=payload,
    )


def verify_archive_bytes(data: bytes, *, expected_sha256: str | None = None) -> dict[str, Any]:
    """Verify deterministic ZIP metadata, manifest records, and SHA256SUMS."""

    try:
        payload = bytes(data)
        digest = _sha256(payload)
        if expected_sha256 is not None and digest != str(expected_sha256).lower():
            raise ArchiveVerificationError("archive SHA-256 mismatch")
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if names != sorted(names) or len(names) != len(set(names)):
                raise ArchiveVerificationError("archive names are duplicate or unsorted")
            folded = [name.casefold() for name in names]
            if len(folded) != len(set(folded)):
                raise ArchiveVerificationError("archive names are not portable")
            for info in infos:
                if _safe_archive_path(info.filename) != info.filename:
                    raise ArchiveVerificationError("archive contains an unsafe member path")
                mode = (info.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode) or mode not in {0, stat.S_IFREG}:
                    raise ArchiveVerificationError("archive contains a non-regular member")
                if info.date_time != (1980, 1, 1, 0, 0, 0):
                    raise ArchiveVerificationError("archive member timestamp is not deterministic")
                if info.compress_type != zipfile.ZIP_STORED:
                    raise ArchiveVerificationError("archive compression is not deterministic")
            required = {"metadata/archive-manifest.json", "SHA256SUMS", "README.md", "CITATION.cff"}
            if not required.issubset(names):
                raise ArchiveVerificationError("archive required members are missing")
            manifest = json.loads(archive.read("metadata/archive-manifest.json"))
            if manifest.get("schema") != ARCHIVE_SCHEMA:
                raise ArchiveVerificationError("archive manifest schema is invalid")
            declared = manifest.get("files")
            if not isinstance(declared, list):
                raise ArchiveVerificationError("archive manifest file inventory is invalid")
            for record in declared:
                name = str((record or {}).get("name") or "")
                if name not in names:
                    raise ArchiveVerificationError("archive manifest member is missing")
                member = archive.read(name)
                if len(member) != record.get("size") or _sha256(member) != record.get("sha256"):
                    raise ArchiveVerificationError("archive manifest member hash mismatch")
            sums_text = archive.read("SHA256SUMS").decode("ascii")
            sum_names: list[str] = []
            for line in sums_text.splitlines():
                if "  " not in line:
                    raise ArchiveVerificationError("SHA256SUMS line is invalid")
                expected, name = line.split("  ", 1)
                if not _HASH.fullmatch(expected) or name == "SHA256SUMS" or name not in names:
                    raise ArchiveVerificationError("SHA256SUMS member is invalid")
                if _sha256(archive.read(name)) != expected:
                    raise ArchiveVerificationError("SHA256SUMS member hash mismatch")
                sum_names.append(name)
            if sum_names != sorted(name for name in names if name != "SHA256SUMS"):
                raise ArchiveVerificationError("SHA256SUMS coverage is incomplete")
            for name in names:
                risks = _payload_risks(name, archive.read(name))
                if risks:
                    raise ArchiveVerificationError(
                        f"archive member violates public safety policy: {name}: {','.join(risks)}"
                    )
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "status": "verified",
            "sha256": digest,
            "size": len(payload),
            "member_count": len(names),
            "checksums": "pass",
            "determinism_metadata": "pass",
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - verifier is a public fail-closed seam
        return {
            "schema": RESULT_SCHEMA,
            "ok": False,
            "status": "tampered_or_invalid",
            "sha256": _sha256(bytes(data)),
            "size": len(bytes(data)),
            "member_count": None,
            "checksums": "fail",
            "determinism_metadata": "fail",
            "error": str(redact(str(exc))),
        }


def verify_archive_file(
    path: str | os.PathLike[str], *, expected_sha256: str | None = None,
) -> dict[str, Any]:
    candidate = os.fspath(path)
    before = os.lstat(candidate)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return {
            "schema": RESULT_SCHEMA, "ok": False, "status": "tampered_or_invalid",
            "sha256": None, "size": None, "member_count": None,
            "checksums": "fail", "determinism_metadata": "fail",
            "error": "archive is not a regular non-symlink file",
        }
    return verify_archive_bytes(Path(candidate).read_bytes(), expected_sha256=expected_sha256)


def _write_recovery_record(directory: Path, archive_name: str, reason: str, temp_name: str) -> Path:
    record = {
        "schema": "vcstudio.vcs-archive-recovery/v1",
        "archive_name": archive_name,
        "temporary_name": temp_name,
        "reason": str(redact(reason)),
        "action": "Inspect and remove the named temporary file after verifying no final archive exists.",
    }
    recovery = directory / f".{archive_name}.recovery.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(recovery, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical_bytes(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return recovery


def export_archive(
    service: Any,
    path: str,
    revision_id: str,
    destination_dir: str | os.PathLike[str],
    *,
    expected_plan_sha256: str,
    attachment_providers: Sequence[ArchiveAttachmentProvider] = (),
) -> dict[str, Any]:
    """Confirm a dry-run and atomically publish one local, non-overwriting ZIP."""

    expected_plan = str(expected_plan_sha256 or "").lower()
    if not _HASH.fullmatch(expected_plan):
        raise ValueError("confirmed archive plan hash is invalid")
    plan = build_archive_plan(
        service, path, revision_id, attachment_providers=attachment_providers,
    )
    if plan.plan_sha256 != expected_plan:
        raise StaleRevisionError("archive dry-run changed; review and confirm a new plan")
    destination = Path(destination_dir)
    if not destination.is_dir():
        raise ValueError("archive destination directory is invalid")
    destination_stat = os.lstat(destination)
    if _is_symlink_or_reparse(destination_stat) or not stat.S_ISDIR(destination_stat.st_mode):
        raise ValueError("archive destination must be a regular non-symlink directory")
    target = destination / plan.archive_name
    lock_path = destination / f".{plan.archive_name}.lock"
    temp = destination / f".{plan.archive_name}.tmp-{uuid.uuid4().hex}"
    final_created = False
    with _exclusive_file_lock(lock_path):
        if target.exists() or target.is_symlink():
            raise FileExistsError("archive filename already exists and will not be overwritten")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(plan._archive_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            staged = verify_archive_file(temp, expected_sha256=plan.archive_sha256)
            if staged.get("ok") is not True:
                raise ArchiveVerificationError(
                    str(staged.get("error") or "staged archive verification failed")
                )
            # A hard link makes the complete staged inode visible under the final
            # name atomically and fails if another process created that name.
            os.link(temp, target)
            final_created = True
            temp.unlink()
            final = verify_archive_file(target, expected_sha256=plan.archive_sha256)
            if final.get("ok") is not True:
                raise ArchiveVerificationError(
                    str(final.get("error") or "published archive verification failed")
                )
        except Exception as exc:
            if descriptor >= 0:
                os.close(descriptor)
            cleanup_errors: list[str] = []
            if final_created:
                try:
                    target.unlink()
                except Exception as cleanup_exc:  # noqa: BLE001 - retain recovery evidence
                    cleanup_errors.append(str(cleanup_exc))
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            except Exception as cleanup_exc:  # noqa: BLE001 - retain recovery evidence
                cleanup_errors.append(str(cleanup_exc))
            if cleanup_errors:
                _write_recovery_record(
                    destination, plan.archive_name,
                    f"{exc}; cleanup: {'; '.join(cleanup_errors)}", temp.name,
                )
            raise
    return {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "status": "verified_local_archive",
        "project_id": plan.project_id,
        "revision": {
            "report_id": plan.report_id,
            "revision_id": plan.revision_id,
            "manifest_sha256": plan.source_manifest_sha256,
        },
        "plan_sha256": plan.plan_sha256,
        "file": {
            "name": plan.archive_name,
            "sha256": plan.archive_sha256,
            "size": plan.archive_size,
        },
        "verification": final,
        "readiness": copy.deepcopy(dict(plan.readiness)),
        "boundaries": {
            "local_only": True,
            "uploaded": False,
            "doi_requested": False,
            "doi_assigned": False,
            "published": False,
        },
        "error": None,
    }


class ArchiveConfirmations:
    """Bounded, one-use private plans for Web dry-run/confirm orchestration."""

    DEFAULT_TTL_SECONDS = 15 * 60
    DEFAULT_MAX_ITEMS = 32

    def __init__(
        self, *, clock: Any, ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> None:
        if ttl_seconds <= 0 or max_items <= 0:
            raise ValueError("archive confirmation bounds must be positive")
        self._clock = clock
        self._ttl_seconds = float(ttl_seconds)
        self._max_items = int(max_items)
        self._lock = threading.RLock()
        self._items: dict[str, tuple[float, Any]] = {}

    def _prune(self, now: float) -> None:
        for token in [
            token for token, (created, _record) in self._items.items()
            if now - created >= self._ttl_seconds
        ]:
            self._items.pop(token, None)
        while len(self._items) >= self._max_items:
            oldest = min(self._items, key=lambda token: self._items[token][0])
            self._items.pop(oldest, None)

    def register(self, record: Any) -> str:
        with self._lock:
            now = float(self._clock())
            self._prune(now)
            token = "archive-confirm." + uuid.uuid4().hex
            self._items[token] = (now, record)
            return token

    def consume(self, token: str) -> Any:
        with self._lock:
            now = float(self._clock())
            self._prune(now)
            selected = self._items.pop(str(token or ""), None)
        if selected is None:
            raise ValueError("archive confirmation is invalid, expired, or already used")
        return selected[1]


__all__ = [
    "ARCHIVE_SCHEMA", "ATTACHMENT_SCHEMA", "PLAN_SCHEMA", "RESULT_SCHEMA",
    "ArchiveAttachment", "ArchiveAttachmentProvider", "ArchiveConfirmations",
    "ArchivePlan", "ArchiveVerificationError", "build_archive_plan",
    "export_archive", "verify_archive_bytes", "verify_archive_file",
]
