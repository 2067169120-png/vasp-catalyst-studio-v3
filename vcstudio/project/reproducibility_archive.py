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
    TrustedDirectoryHandle,
    TrustedDirectorySelection,
    capture_trusted_directory,
    evidence_graph,
    load_frozen_revision,
    open_trusted_directory,
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
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'(=])(?:~/|/+)(?=[A-Za-z0-9._~-])"
)
_FILE_URI = re.compile(r"(?i)\bfile:(?:/{1,3}|\\)")
_SECRET_LITERAL = re.compile(
    r"(?ix)(?:"
    r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}"
    r"|glpat-[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9]{20,}"
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{16,})"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bBearer\s+(?!\[redacted)[^\s<]+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE\s+KEY-----"
    r"|https?://[^\s/@]+(?::[^\s/@]*)?@)"
)
_ASSIGNMENT = re.compile(
    r"(?ix)(?<![A-Za-z0-9_.-])[\"']?"
    r"(?P<key>[A-Za-z][A-Za-z0-9_.-]{1,96})[\"']?\s*[=:]\s*[\"']?"
    r"(?P<value>[^\s,;\"'}]*)"
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
_NESTED_ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".tgz", ".tar.gz", ".tbz", ".tbz2", ".tar.bz2",
    ".txz", ".tar.xz", ".gz", ".bz2", ".xz", ".7z", ".rar",
)
_SENSITIVE_KEY_EXACT = frozenset({
    "password", "passwd", "secret", "token", "client_secret", "access_token",
    "refresh_token", "auth", "cookie", "cookies",
    "id_token", "api_key", "aws_secret_access_key", "secret_access_key",
    "private_key", "authorization", "credential", "credentials",
})
_SENSITIVE_KEY_SUFFIXES = (
    "_password", "_secret", "_token", "_api_key", "_access_key",
    "_access_key_id", "_private_key", "_authorization", "_credential",
    "_credentials",
)
_SENSITIVE_KEY_COMPACT_SUFFIXES = (
    "password", "secret", "secretkey", "token", "apikey", "accesskey", "accesskeyid",
    "privatekey", "authorization", "credential", "credentials",
)


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
        summary = {
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
        return _public_safe_value(summary)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _canonical_json_file(value: Any) -> bytes:
    return _canonical_bytes(_archive_safe_value(value)) + b"\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_key(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _sensitive_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    compact = normalized.replace("_", "")
    return (
        normalized in _SENSITIVE_KEY_EXACT
        or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)
        or compact.endswith(_SENSITIVE_KEY_COMPACT_SUFFIXES)
    )


def _text_safety_risks(value: Any, *, include_paths: bool = True) -> list[str]:
    text = str(value or "")
    risks: list[str] = []
    if _SECRET_LITERAL.search(text):
        risks.append("secret_literal")
    for assignment in _ASSIGNMENT.finditer(text):
        if (
            _sensitive_key(assignment.group("key"))
            and assignment.group("value").casefold() != "[redacted-secret]"
        ):
            risks.append("secret_literal")
            break
    if include_paths and (
        _WINDOWS_PATH.search(text) or _FILE_URI.search(text)
        or _POSIX_ABSOLUTE_PATH.search(text)
    ):
        risks.append("absolute_path")
    return sorted(set(risks))


def _decoded_text_values(data: bytes) -> set[str]:
    values = {data.decode("utf-8", errors="ignore")}
    sample = data[: min(len(data), 8192)]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or (
        sample and sample.count(b"\x00") * 4 >= len(sample)
    ):
        values.add(data.decode("utf-16-le", errors="ignore"))
        values.add(data.decode("utf-16-be", errors="ignore"))
    return values


def _structured_safety_risks(
    value: Any, *, max_depth: int = 8, max_nodes: int = 1024,
) -> list[str]:
    """Bounded provider-metadata scan over keys, values, and nested paths."""

    risks: set[str] = set()
    seen: set[int] = set()
    nodes = 0

    def visit(item: Any, depth: int, key: str = "") -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            risks.add("metadata_structure_unbounded")
            return
        if key and _sensitive_key(key):
            risks.add("secret_key")
        if isinstance(item, Mapping):
            marker = id(item)
            if marker in seen:
                risks.add("metadata_cycle")
                return
            seen.add(marker)
            for child_key, child in item.items():
                key_text = str(child_key)
                risks.update(_text_safety_risks(key_text))
                visit(child, depth + 1, key_text)
            seen.remove(marker)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            marker = id(item)
            if marker in seen:
                risks.add("metadata_cycle")
                return
            seen.add(marker)
            for child in item:
                visit(child, depth + 1, key)
            seen.remove(marker)
            return
        if isinstance(item, os.PathLike):
            risks.update(_text_safety_risks(os.fspath(item)))
            return
        if isinstance(item, str):
            risks.update(_text_safety_risks(item))
            return
        if isinstance(item, (bytes, bytearray, memoryview)):
            for decoded in _decoded_text_values(bytes(item)):
                risks.update(_text_safety_risks(decoded))
            return
        if item is not None and not isinstance(item, (bool, int, float)):
            risks.add("metadata_value_unsupported")

    visit(value, 0)
    return sorted(risks)


def _nested_archive(name: str, data: bytes) -> bool:
    lowered = str(name or "").lower()
    if lowered.endswith(_NESTED_ARCHIVE_SUFFIXES):
        return True
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")):
        return True
    if len(data) >= 262 and data[257:262] == b"ustar":
        return True
    if len(data) >= 512:
        checksum_field = data[148:156].strip(b" \x00")
        try:
            expected_checksum = int(checksum_field, 8)
        except ValueError:
            expected_checksum = -1
        if expected_checksum >= 0:
            header = data[:148] + (b" " * 8) + data[156:512]
            if sum(header) == expected_checksum:
                return True
    try:
        return zipfile.is_zipfile(io.BytesIO(data))
    except Exception:  # pragma: no cover - is_zipfile is deliberately best-effort
        return False


def _archive_safe_value(value: Any, *, key: str = "", parent_key: str = "") -> Any:
    """Redact public risks and replace any embedded raw POTCAR payload."""

    normalized = _normalized_key(key)
    parent = _normalized_key(parent_key)
    if _sensitive_key(normalized):
        return "[redacted-secret]"
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
        if "secret_literal" in _text_safety_risks(safe, include_paths=False):
            return "[redacted-secret]"
        if ("potcar" in normalized or "potcar" in parent) and _POTCAR_RAW.search(safe):
            return "[excluded-potcar-content]"
        if _POTCAR_RAW.search(safe):
            return "[excluded-potcar-content]"
    return safe


def _public_safe_value(value: Any, *, key: str = "") -> Any:
    """Final DTO defense that preserves safe relative archive member names."""

    if _sensitive_key(key):
        return "[redacted-secret]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _public_safe_value(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_public_safe_value(item) for item in value]
    if isinstance(value, str):
        risks = _text_safety_risks(value)
        if "secret_literal" in risks:
            return "[redacted-secret]"
        if "absolute_path" in risks:
            return "[redacted-local-path]"
        if _POTCAR_RAW.search(value):
            return "[excluded-potcar-content]"
    return copy.deepcopy(value)


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
    name_risks = _text_safety_risks(name)
    if "secret_literal" in name_risks:
        risks.append("secret_in_path")
    if "absolute_path" in name_risks:
        risks.append("absolute_path")
    if _nested_archive(name, data):
        risks.append("nested_archive_forbidden")
    for decoded in _decoded_text_values(data):
        if _POTCAR_RAW.search(decoded):
            risks.append("potcar_raw_content")
        risks.extend(_text_safety_risks(decoded, include_paths=True))
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
    def normalize(candidate: Any) -> tuple[str, str] | None:
        raw_identifier = str(candidate).strip()
        if _text_safety_risks(raw_identifier):
            return None
        identifier = str(redact(raw_identifier)).strip()
        if not identifier or "redacted" in identifier.lower():
            return None
        if identifier.casefold() in {"noassertion", "none", "unknown", "unspecified"}:
            return "NOASSERTION", "missing"
        return identifier, "declared"

    if isinstance(value, str):
        return normalize(value) or ("NOASSERTION", "missing")
    if isinstance(value, Mapping):
        for key in ("spdx", "spdx_id", "license_id", "id", "name", "url"):
            candidate = value.get(key)
            if candidate:
                result = normalize(candidate)
                if result is not None:
                    return result
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
        if _text_safety_risks(value):
            return ""
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


def _matching_figures(bundle: FrozenRevision, digest: str) -> list[Mapping[str, Any]]:
    return [
        figure for figure in bundle.model.get("figures") or []
        if isinstance(figure, Mapping) and str(
            figure.get("asset_sha256") or figure.get("sha256") or ""
        ).lower() == digest
    ]


def _rights_sources(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = []
    extensions = record.get("extensions")
    candidates = (
        record.get("rights"), record,
        extensions.get("rights") if isinstance(extensions, Mapping) else None,
        extensions,
    )
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate not in sources:
            sources.append(candidate)
    return sources


def _figure_rights_signature(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """Normalize one model figure's provenance declarations for conflict checks."""

    origins: list[bool] = []
    redistributable: list[bool] = []
    licenses: set[tuple[str, str]] = set()
    attributions: set[str] = set()
    invalid = False
    sensitive = False
    external = {"external", "published", "third_party", "third-party"}
    internal = {"first_party", "first-party", "project", "original", "local"}
    for source in _rights_sources(record):
        for key in (*_LICENSE_KEYS, *_ATTRIBUTION_KEYS, "source_kind", "origin"):
            if key in source and _structured_safety_risks(source.get(key)):
                sensitive = True
        if "third_party" in source:
            value = source.get("third_party")
            if isinstance(value, bool):
                origins.append(value)
            else:
                invalid = True
        source_kind = str(source.get("source_kind") or source.get("origin") or "").strip().lower()
        if source_kind in external:
            origins.append(True)
        elif source_kind in internal:
            origins.append(False)
        elif source_kind:
            invalid = True
        if "redistributable" in source:
            value = source.get("redistributable")
            if isinstance(value, bool):
                redistributable.append(value)
            else:
                invalid = True
        for key in _LICENSE_KEYS:
            if key not in source or source.get(key) in (None, "", [], {}):
                continue
            if key == "rights" and isinstance(source.get(key), Mapping):
                continue
            licenses.add(_license_record(source.get(key)))
        attribution = _attribution_text(_first_mapping_value([source], _ATTRIBUTION_KEYS))
        if attribution:
            attributions.add(attribution)
    origin = "third_party" if True in origins else "first_party" if origins else "missing"
    redistribution = (
        "false" if False in redistributable
        else "true" if True in redistributable else "missing"
    )
    return (
        origin, redistribution, tuple(sorted(licenses)), tuple(sorted(attributions)),
        invalid, sensitive,
    )


def _merged_asset_rights(
    manifest_record: Mapping[str, Any], model_figure: Mapping[str, Any],
) -> tuple[bool, str | None, tuple[str, str, str]]:
    """Merge two frozen rights declarations with deny/missing precedence."""

    sources = [*_rights_sources(manifest_record), *_rights_sources(model_figure)]
    origins: list[bool] = []
    redistributable: list[bool] = []
    licenses: set[str] = set()
    explicit_missing_license = False
    attributions: set[str] = set()
    invalid_boolean = False
    sensitive_rights = False
    external = {"external", "published", "third_party", "third-party"}
    internal = {"first_party", "first-party", "project", "original", "local"}
    for source in sources:
        for key in (*_LICENSE_KEYS, *_ATTRIBUTION_KEYS, "source_kind", "origin"):
            if key in source and _structured_safety_risks(source.get(key)):
                sensitive_rights = True
        if "third_party" in source:
            value = source.get("third_party")
            if isinstance(value, bool):
                origins.append(value)
            else:
                invalid_boolean = True
        source_kind = str(source.get("source_kind") or source.get("origin") or "").strip().lower()
        if source_kind in external:
            origins.append(True)
        elif source_kind in internal:
            origins.append(False)
        elif source_kind:
            invalid_boolean = True
        if "redistributable" in source:
            value = source.get("redistributable")
            if isinstance(value, bool):
                redistributable.append(value)
            else:
                invalid_boolean = True
        for key in _LICENSE_KEYS:
            if key not in source or source.get(key) in (None, "", [], {}):
                continue
            if key == "rights" and isinstance(source.get(key), Mapping):
                continue
            license_id, license_status = _license_record(source.get(key))
            if license_status != "declared":
                explicit_missing_license = True
            else:
                licenses.add(license_id)
        attribution = _attribution_text(_first_mapping_value([source], _ATTRIBUTION_KEYS))
        if attribution:
            attributions.add(attribution)
    if sensitive_rights:
        return False, "asset_rights_sensitive", ("NOASSERTION", "missing", "")
    if invalid_boolean:
        return False, "asset_rights_conflict", ("NOASSERTION", "missing", "")
    if not origins:
        return False, "asset_rights_binding_missing", ("NOASSERTION", "missing", "")
    third_party = any(origins)
    if False in redistributable:
        return False, "asset_not_redistributable", ("NOASSERTION", "missing", "")
    if not redistributable or True not in redistributable:
        return False, "asset_rights_binding_missing", ("NOASSERTION", "missing", "")
    if explicit_missing_license or not licenses:
        return False, "asset_license_missing", ("NOASSERTION", "missing", "")
    if len(licenses) != 1:
        return False, "asset_rights_conflict", ("NOASSERTION", "missing", "")
    attribution = "; ".join(sorted(attributions))
    license_id = next(iter(licenses))
    if third_party and not attribution:
        return False, "third_party_license_or_attribution_missing", (
            license_id, "declared", "",
        )
    return True, None, (license_id, "declared", attribution)


def _asset_rights(
    bundle: FrozenRevision, manifest_record: Mapping[str, Any], digest: str,
) -> tuple[bool, str | None, tuple[str, str, str]]:
    figures = _matching_figures(bundle, digest)
    if not figures:
        return False, "asset_model_binding_missing", ("NOASSERTION", "missing", "")
    if len({_figure_rights_signature(figure) for figure in figures}) != 1:
        return False, "asset_rights_conflict", ("NOASSERTION", "missing", "")
    resolved = [_merged_asset_rights(manifest_record, figure) for figure in figures]
    if len(set(resolved)) != 1:
        return False, "asset_rights_conflict", ("NOASSERTION", "missing", "")
    return resolved[0]


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
        if not _TOKEN.fullmatch(provider_id) or _text_safety_risks(provider_id):
            exclusions.append(_excluded_decision(
                archive_path=None, logical_role="extension_attachment",
                reason="provider_id_invalid_or_sensitive", sensitive_risk="high",
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
            try:
                descriptor_risks: set[str] = set()
                for field_value in (
                    raw.archive_path, raw.logical_role, raw.license_id,
                    raw.attribution, raw.sensitive_risk,
                ):
                    descriptor_risks.update(_text_safety_risks(field_value))
                metadata_risks = _structured_safety_risks(raw.metadata)
            except Exception:
                descriptor_risks = {"descriptor_scan_failed"}
                metadata_risks = {"metadata_scan_failed"}
            if descriptor_risks or metadata_risks:
                exclusions.append(_excluded_decision(
                    archive_path=None, logical_role="extension_attachment",
                    reason=(
                        "provider_metadata_secret_or_path"
                        if metadata_risks else "provider_descriptor_secret_or_path"
                    ),
                    sensitive_risk="high",
                    size=(raw.size if isinstance(raw.size, int) and not isinstance(raw.size, bool) else None),
                    sha256=(
                        raw.sha256.lower()
                        if isinstance(raw.sha256, str) and _HASH.fullmatch(raw.sha256.lower())
                        else None
                    ),
                    authority="extension_provider", provider_id=provider_id,
                ))
                continue
            path = None
            try:
                path = _safe_archive_path(raw.archive_path)
                if not isinstance(raw.sha256, str) or not _HASH.fullmatch(raw.sha256.lower()):
                    raise ValueError("attachment hash invalid")
                if isinstance(raw.size, bool) or not isinstance(raw.size, int) or raw.size < 0:
                    raise ValueError("attachment size invalid")
                if not isinstance(raw.redistributable, bool) or not isinstance(raw.third_party, bool):
                    raise ValueError("attachment rights booleans are invalid")
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
                    archive_path=path, logical_role="extension_attachment",
                    reason=reason, sensitive_risk="high",
                    size=(raw.size if isinstance(raw.size, int) and not isinstance(raw.size, bool) else None),
                    sha256=(
                        raw.sha256.lower()
                        if isinstance(raw.sha256, str) and _HASH.fullmatch(raw.sha256.lower())
                        else None
                    ),
                    license_id="NOASSERTION", license_status="missing",
                    attribution="", authority="extension_provider",
                    provider_id=provider_id,
                ))
                continue
            license_id, license_status = _license_record(raw.license_id)
            attribution = str(redact(raw.attribution)).strip()
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
        eligible, exclusion_reason, asset_rights = _asset_rights(
            bundle, record, expected_hash,
        )
        license_id, license_status, attribution = asset_rights
        archive_path = f"artifacts/figures/{PurePosixPath(str(record.get('path') or '')).name}"
        if not eligible:
            exclusions.append(_excluded_decision(
                archive_path=archive_path, logical_role="report_figure",
                reason=str(exclusion_reason or "asset_rights_binding_missing"),
                sensitive_risk="high", size=expected_size, sha256=expected_hash,
                license_id=license_id, license_status=license_status,
                attribution=attribution, authority="frozen_report_revision",
            ))
            continue
        source = _manifest_record_path(bundle, record)
        payload = _safe_read_bound_file(
            source, expected_sha256=expected_hash, expected_size=expected_size,
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
                    raise ArchiveVerificationError("archive member violates public safety policy")
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
            "error": str(_public_safe_value(str(exc))),
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


def _trusted_child_path(directory: TrustedDirectoryHandle, name: str) -> Path:
    return Path(directory.path) / name


def _trusted_exists(directory: TrustedDirectoryHandle, name: str) -> bool:
    if directory.dir_fd is not None:
        try:
            os.stat(name, dir_fd=directory.dir_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
    directory.verify_path()
    try:
        os.lstat(_trusted_child_path(directory, name))
        return True
    except FileNotFoundError:
        return False


def _trusted_open_exclusive(directory: TrustedDirectoryHandle, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory.dir_fd is not None:
        return os.open(name, flags, 0o600, dir_fd=directory.dir_fd)
    directory.verify_path()
    return os.open(_trusted_child_path(directory, name), flags, 0o600)


def _trusted_unlink(directory: TrustedDirectoryHandle, name: str) -> None:
    if directory.dir_fd is not None:
        os.unlink(name, dir_fd=directory.dir_fd)
        return
    directory.verify_path()
    os.unlink(_trusted_child_path(directory, name))


def _trusted_link(directory: TrustedDirectoryHandle, source: str, target: str) -> None:
    directory.verify_path()
    if directory.dir_fd is not None:
        os.link(
            source, target, src_dir_fd=directory.dir_fd,
            dst_dir_fd=directory.dir_fd, follow_symlinks=False,
        )
        return
    os.link(_trusted_child_path(directory, source), _trusted_child_path(directory, target))


def _trusted_read_regular(directory: TrustedDirectoryHandle, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory.dir_fd is not None:
        named_before = os.stat(name, dir_fd=directory.dir_fd, follow_symlinks=False)
    else:
        directory.verify_path()
        named_before = os.lstat(_trusted_child_path(directory, name))
    if _is_symlink_or_reparse(named_before) or not stat.S_ISREG(named_before.st_mode):
        raise ArchiveVerificationError("archive stage is not a regular file")
    if directory.dir_fd is not None:
        descriptor = os.open(name, flags, dir_fd=directory.dir_fd)
    else:
        descriptor = os.open(_trusted_child_path(directory, name), flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ArchiveVerificationError("archive stage is not a regular file")
        if directory.dir_fd is not None:
            named_after = os.stat(name, dir_fd=directory.dir_fd, follow_symlinks=False)
        else:
            directory.verify_path()
            named_after = os.lstat(_trusted_child_path(directory, name))
        identity = (int(opened.st_dev), int(opened.st_ino))
        if (
            _is_symlink_or_reparse(named_after)
            or not stat.S_ISREG(named_after.st_mode)
            or (int(named_before.st_dev), int(named_before.st_ino)) != identity
            or (int(named_after.st_dev), int(named_after.st_ino)) != identity
        ):
            raise ArchiveVerificationError("archive stage changed during no-follow open")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _trusted_verify_archive(
    directory: TrustedDirectoryHandle, name: str, *, expected_sha256: str,
) -> dict[str, Any]:
    return verify_archive_bytes(
        _trusted_read_regular(directory, name), expected_sha256=expected_sha256,
    )


def _write_recovery_record(
    directory: TrustedDirectoryHandle, archive_name: str, reason: str, temp_name: str,
) -> str:
    record = {
        "schema": "vcstudio.vcs-archive-recovery/v1",
        "archive_name": archive_name,
        "temporary_name": temp_name,
        "reason": str(_public_safe_value(str(reason))),
        "action": "Inspect and remove the named temporary file after verifying no final archive exists.",
    }
    recovery_name = f".{archive_name}.recovery.json"
    descriptor = _trusted_open_exclusive(directory, recovery_name)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical_bytes(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return recovery_name


def export_archive(
    service: Any,
    path: str,
    revision_id: str,
    destination_dir: str | os.PathLike[str] | TrustedDirectorySelection,
    *,
    expected_plan_sha256: str,
    attachment_providers: Sequence[ArchiveAttachmentProvider] = (),
) -> dict[str, Any]:
    """Confirm a dry-run and atomically publish one local, non-overwriting ZIP."""

    expected_plan = str(expected_plan_sha256 or "").lower()
    if not _HASH.fullmatch(expected_plan):
        raise ValueError("confirmed archive plan hash is invalid")
    selection = (
        destination_dir if isinstance(destination_dir, TrustedDirectorySelection)
        else capture_trusted_directory(destination_dir)
    )
    with open_trusted_directory(selection) as destination:
        plan = build_archive_plan(
            service, path, revision_id, attachment_providers=attachment_providers,
        )
        if plan.plan_sha256 != expected_plan:
            raise StaleRevisionError("archive dry-run changed; review and confirm a new plan")
        destination.verify_path()
        target_name = plan.archive_name
        lock_name = f".{plan.archive_name}.lock"
        temp_name = f".{plan.archive_name}.tmp-{uuid.uuid4().hex}"
        lock_path = (
            Path(lock_name) if destination.dir_fd is not None
            else _trusted_child_path(destination, lock_name)
        )
        final_created = False
        with _exclusive_file_lock(lock_path, dir_fd=destination.dir_fd):
            destination.verify_path()
            if _trusted_exists(destination, target_name):
                raise FileExistsError("archive filename already exists and will not be overwritten")
            descriptor = _trusted_open_exclusive(destination, temp_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(plan._archive_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                staged = _trusted_verify_archive(
                    destination, temp_name, expected_sha256=plan.archive_sha256,
                )
                if staged.get("ok") is not True:
                    raise ArchiveVerificationError(
                        str(staged.get("error") or "staged archive verification failed")
                    )
                destination.verify_path()
                # Publish relative to the pinned directory inode.  The link is
                # atomic and fails if another process already owns the name.
                _trusted_link(destination, temp_name, target_name)
                final_created = True
                _trusted_unlink(destination, temp_name)
                destination.verify_path()
                final = _trusted_verify_archive(
                    destination, target_name, expected_sha256=plan.archive_sha256,
                )
                if final.get("ok") is not True:
                    raise ArchiveVerificationError(
                        str(final.get("error") or "published archive verification failed")
                    )
                destination.verify_path()
            except Exception as exc:
                if descriptor >= 0:
                    os.close(descriptor)
                cleanup_errors: list[str] = []
                if final_created:
                    try:
                        _trusted_unlink(destination, target_name)
                    except Exception as cleanup_exc:  # noqa: BLE001 - retain recovery evidence
                        cleanup_errors.append(str(cleanup_exc))
                try:
                    _trusted_unlink(destination, temp_name)
                except FileNotFoundError:
                    pass
                except Exception as cleanup_exc:  # noqa: BLE001 - retain recovery evidence
                    cleanup_errors.append(str(cleanup_exc))
                if cleanup_errors:
                    _write_recovery_record(
                        destination, plan.archive_name,
                        f"{exc}; cleanup: {'; '.join(cleanup_errors)}", temp_name,
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

    def peek(self, token: str) -> Any:
        """Validate a confirmation for destination binding without consuming it."""

        with self._lock:
            now = float(self._clock())
            self._prune(now)
            selected = self._items.get(str(token or ""))
        if selected is None:
            raise ValueError("archive confirmation is invalid, expired, or already used")
        return selected[1]


__all__ = [
    "ARCHIVE_SCHEMA", "ATTACHMENT_SCHEMA", "PLAN_SCHEMA", "RESULT_SCHEMA",
    "ArchiveAttachment", "ArchiveAttachmentProvider", "ArchiveConfirmations",
    "ArchivePlan", "ArchiveVerificationError", "build_archive_plan",
    "export_archive", "verify_archive_bytes", "verify_archive_file",
]
