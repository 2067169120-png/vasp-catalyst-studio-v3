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
import base64
import hashlib
import io
import json
import ntpath
import os
import posixpath
import re
import secrets
import stat
import tempfile
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlsplit

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
CONFIRMATION_SCHEMA = "vcstudio.vcs-archive-confirmation/v1"
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
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{16,}"
    r"|AIza[0-9A-Za-z_-]{30,})"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bBearer\s+(?!\[redacted)[^\s<]+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE\s+KEY-----"
    r"|[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]*(?::[^\s/@]*)?@)"
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
_OOXML_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})
_NESTED_ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".tgz", ".tar.gz", ".tbz", ".tbz2", ".tar.bz2",
    ".txz", ".tar.xz", ".gz", ".bz2", ".xz", ".7z", ".rar",
)
_CONTAINER_MAGIC = (
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"BZh",
    b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07",
    b"\x28\xb5\x2f\xfd",  # Zstandard
    b"\x04\x22\x4d\x18",  # LZ4 frame
    b"\x02\x21\x4c\x18",  # legacy LZ4 frame
)
_REPORT_AUTHORITY = "report_service_frozen_revision"
_ARTIFACT_RIGHTS_SCHEMA = "vcstudio.report-artifact-rights/v1"
_REPORT_MEDIA_TYPES = {
    "html": "text/html",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
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
class ArchiveLimits:
    """Hard fail-closed budgets for every untrusted archive seam."""

    max_providers: int = 16
    max_attachments_per_provider: int = 128
    max_members: int = 512
    max_path_length: int = 240
    max_metadata_depth: int = 8
    max_metadata_nodes: int = 1024
    max_metadata_bytes: int = 256 * 1024
    max_member_bytes: int = 64 * 1024 * 1024
    max_total_member_bytes: int = 256 * 1024 * 1024
    max_archive_bytes: int = 320 * 1024 * 1024
    max_office_members: int = 512
    max_office_total_bytes: int = 128 * 1024 * 1024
    max_compression_ratio: int = 200

    def validate(self) -> "ArchiveLimits":
        for item in self.__dataclass_fields__:
            value = getattr(self, item)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("archive limits must be positive integers")
        return self


DEFAULT_ARCHIVE_LIMITS = ArchiveLimits()


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
    inventory_sha256: str
    rights_sha256: str
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
            "inventory_sha256": self.inventory_sha256,
            "rights_sha256": self.rights_sha256,
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
    """Classify one scalar without ever returning or interpolating its value."""

    try:
        if isinstance(value, str):
            text = value
        elif isinstance(value, os.PathLike):
            text = os.fspath(value)
            if not isinstance(text, str):
                return ["uninspectable_value"]
        elif value is None:
            text = ""
        elif isinstance(value, (bool, int, float)):
            text = str(value)
        else:
            text = str(value)
    except Exception:  # noqa: BLE001 - classifier failures are themselves sensitive
        return ["uninspectable_value"]
    risks: set[str] = set()
    try:
        if _SECRET_LITERAL.search(text):
            risks.add("secret_literal")
        for assignment in _ASSIGNMENT.finditer(text):
            if (
                _sensitive_key(assignment.group("key"))
                and assignment.group("value").casefold() != "[redacted-secret]"
            ):
                risks.add("secret_literal")
                break
        if include_paths and (
            _WINDOWS_PATH.search(text) or _FILE_URI.search(text)
            or _POSIX_ABSOLUTE_PATH.search(text)
        ):
            risks.add("absolute_path")
    except Exception:  # noqa: BLE001 - no classifier exception may echo input
        risks.add("uninspectable_value")
    return sorted(risks)


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
    max_bytes: int = 256 * 1024,
) -> list[str]:
    """Bounded provider-metadata scan over keys, values, and nested paths."""

    risks: set[str] = set()
    seen: set[int] = set()
    nodes = 0
    scanned_bytes = 0

    def account(value: Any) -> bool:
        nonlocal scanned_bytes
        try:
            if isinstance(value, str):
                size = len(value.encode("utf-8", errors="replace"))
            elif isinstance(value, (bytes, bytearray, memoryview)):
                size = len(value)
            else:
                size = len(str(value).encode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - opaque metadata must fail closed
            risks.add("metadata_value_unsupported")
            return False
        scanned_bytes += size
        if scanned_bytes > max_bytes:
            risks.add("metadata_structure_unbounded")
            return False
        return True

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
                try:
                    key_text = str(child_key)
                except Exception:  # noqa: BLE001 - do not stringify in an error
                    risks.add("metadata_value_unsupported")
                    continue
                if not account(key_text):
                    continue
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
            try:
                path_value = os.fspath(item)
            except Exception:  # noqa: BLE001
                risks.add("metadata_value_unsupported")
                return
            if account(path_value):
                risks.update(_text_safety_risks(path_value))
            return
        if isinstance(item, str):
            if account(item):
                risks.update(_text_safety_risks(item))
            return
        if isinstance(item, (bytes, bytearray, memoryview)):
            if account(item):
                for decoded in _decoded_text_values(bytes(item)):
                    risks.update(_text_safety_risks(decoded))
            return
        if item is not None and not isinstance(item, (bool, int, float)):
            risks.add("metadata_value_unsupported")

    visit(value, 0)
    return sorted(risks)


class _ReadOnlyBytes(io.RawIOBase):
    """Seekable, zero-copy bytes view used by ZIP readers instead of BytesIO."""

    def __init__(self, data: bytes) -> None:
        self._view = memoryview(data)
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._position + offset
        elif whence == os.SEEK_END:
            position = len(self._view) + offset
        else:
            raise ValueError("invalid seek mode")
        if position < 0 and whence == os.SEEK_SET:
            raise ValueError("negative seek position")
        self._position = max(0, min(position, len(self._view)))
        return self._position

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            end = len(self._view)
        else:
            end = min(len(self._view), self._position + size)
        result = self._view[self._position:end].tobytes()
        self._position = end
        return result

    def readinto(self, buffer: Any) -> int:
        count = min(len(buffer), len(self._view) - self._position)
        if count <= 0:
            return 0
        buffer[:count] = self._view[self._position:self._position + count]
        self._position += count
        return count


def _looks_like_tar(data: bytes) -> bool:
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
    return False


def _container_kind(name: str, data: bytes) -> str | None:
    """Identify opaque/compressed formats by bytes, never extension alone."""

    lowered = str(name or "").lower()
    suffix = Path(lowered).suffix
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "ooxml" if suffix in _OOXML_SUFFIXES else "zip"
    signatures = (
        (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"),
        (b"\xfd7zXZ\x00", "xz"), (b"7z\xbc\xaf\x27\x1c", "7z"),
        (b"Rar!\x1a\x07", "rar"), (b"\x28\xb5\x2f\xfd", "zstd"),
        (b"\x04\x22\x4d\x18", "lz4"), (b"\x02\x21\x4c\x18", "lz4"),
    )
    for magic, kind in signatures:
        if data.startswith(magic):
            return kind
    if _looks_like_tar(data):
        return "tar"
    if lowered.endswith(_NESTED_ARCHIVE_SUFFIXES):
        return "declared_container"
    try:
        if zipfile.is_zipfile(_ReadOnlyBytes(data)):
            return "ooxml" if suffix in _OOXML_SUFFIXES else "zip"
    except Exception:  # pragma: no cover - is_zipfile is deliberately best-effort
        return "invalid_container"
    return None


def _read_stream_limited(stream: Any, limit: int) -> bytes:
    payload = bytearray()
    while len(payload) <= limit:
        block = stream.read(min(1024 * 1024, limit + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
    if len(payload) > limit or stream.read(1):
        raise ArchiveVerificationError("member exceeds the uncompressed byte budget")
    return bytes(payload)


class _ArchiveHTMLScanner(HTMLParser):
    _URL_ATTRIBUTES = frozenset({"src", "href", "poster", "data", "action", "formaction"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: set[str] = set()
        self.asset_refs: set[str] = set()
        self.risks: set[str] = set()
        self._style_depth = 0

    @staticmethod
    def _srcset_urls(value: str) -> list[str]:
        urls: list[str] = []
        position = 0
        while position < len(value):
            while position < len(value) and (value[position].isspace() or value[position] == ","):
                position += 1
            if position >= len(value):
                break
            start = position
            while position < len(value) and not value[position].isspace():
                position += 1
            url = value[start:position]
            if not url.casefold().startswith("data:"):
                url = url.rstrip(",")
            if url:
                urls.append(url)
            depth = 0
            while position < len(value):
                char = value[position]
                if char == "(":
                    depth += 1
                elif char == ")" and depth:
                    depth -= 1
                elif char == "," and depth == 0:
                    position += 1
                    break
                position += 1
        return urls

    @staticmethod
    def _css_urls(value: str) -> list[str]:
        urls = [
            match.group("value").strip()
            for match in re.finditer(
                r"(?is)url\(\s*(?P<quote>['\"]?)(?P<value>.*?)(?P=quote)\s*\)",
                value,
            )
        ]
        urls.extend(
            match.group("value").strip()
            for match in re.finditer(
                r"(?is)@import\s+(?!url\()[\"'](?P<value>[^\"']+)[\"']",
                value,
            )
        )
        return urls

    def _inspect_url(self, tag: str, key: str, value: str, *, asset: bool) -> None:
        self.risks.update(_text_safety_risks(value, include_paths=False))
        try:
            parsed = urlsplit(value)
        except Exception:  # noqa: BLE001
            self.risks.add("invalid_html_reference")
            return
        if parsed.username is not None or parsed.password is not None:
            self.risks.add("secret_literal")
        if parsed.scheme.casefold() == "data":
            try:
                header, encoded = value.split(",", 1)
                if ";base64" in header.casefold():
                    embedded = base64.b64decode(encoded, validate=True)
                else:
                    embedded = unquote(encoded).encode("utf-8")
                if len(embedded) > DEFAULT_ARCHIVE_LIMITS.max_member_bytes:
                    self.risks.add("member_byte_limit")
                else:
                    for decoded in _decoded_text_values(embedded):
                        self.risks.update(_text_safety_risks(decoded))
                    if _container_kind("embedded.bin", embedded) is not None:
                        self.risks.add("nested_archive_forbidden")
            except Exception:  # noqa: BLE001
                self.risks.add("invalid_html_reference")
        if parsed.scheme.casefold() == "file" or "\\" in value:
            self.risks.add("absolute_path")
        if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("#"):
            return
        path = unquote(parsed.path)
        if path.startswith("/"):
            self.risks.add("unresolved_html_reference")
        else:
            self.refs.add(path)
            if asset:
                self.asset_refs.add(path)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "style":
            self._style_depth += 1
        for key, raw in attrs:
            value = str(raw or "").strip()
            lowered_key = key.casefold()
            if lowered_key in {"srcset", "imagesrcset"}:
                urls = self._srcset_urls(value)
                if value and not urls:
                    self.risks.add("invalid_html_reference")
                for url in urls:
                    self._inspect_url(tag, key, url, asset=True)
                continue
            if lowered_key == "style":
                for url in self._css_urls(value):
                    self._inspect_url(tag, key, url, asset=True)
                self.risks.update(_text_safety_risks(value, include_paths=False))
                continue
            if key.casefold() not in self._URL_ATTRIBUTES:
                self.risks.update(_text_safety_risks(value))
                continue
            asset = (
                (tag.casefold() in {"img", "source", "script", "video", "audio"}
                 and lowered_key == "src")
                or lowered_key in {"poster", "data"}
            )
            self._inspect_url(tag, key, value, asset=asset)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "style" and self._style_depth:
            self._style_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            for url in self._css_urls(data):
                self._inspect_url("style", "css-url", url, asset=True)
        self.risks.update(_text_safety_risks(data))

    def handle_comment(self, data: str) -> None:
        self.risks.update(_text_safety_risks(data))


def _html_scan(data: bytes) -> tuple[list[str], set[str], set[str]]:
    try:
        text = data.decode("utf-8")
        scanner = _ArchiveHTMLScanner()
        scanner.feed(text)
        scanner.close()
    except Exception:  # noqa: BLE001 - malformed HTML is not archive-safe
        return ["invalid_html"], set(), set()
    if _POTCAR_RAW.search(text):
        scanner.risks.add("potcar_raw_content")
    return sorted(scanner.risks), scanner.refs, scanner.asset_refs


def _css_scan(data: bytes) -> tuple[list[str], set[str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["invalid_css"], set()
    scanner = _ArchiveHTMLScanner()
    for url in scanner._css_urls(text):
        scanner._inspect_url("style", "css-url", url, asset=True)
    scanner.risks.update(_text_safety_risks(text, include_paths=False))
    if _POTCAR_RAW.search(text):
        scanner.risks.add("potcar_raw_content")
    return sorted(scanner.risks), scanner.refs


def _pdf_lexical_risks(data: bytes) -> set[str]:
    text = data.decode("latin-1", errors="ignore")
    risks = set(_text_safety_risks(text, include_paths=False))
    if _POTCAR_RAW.search(text):
        risks.add("potcar_raw_content")
    for match in re.finditer(r"(?s)\((?:\\.|[^\\)])*\)", text):
        risks.update(_text_safety_risks(match.group(0)[1:-1], include_paths=True))
    for match in re.finditer(r"(?s)(?<!<)<([0-9A-Fa-f\s]+)>(?!>)", text):
        compact = re.sub(r"\s+", "", match.group(1))
        if len(compact) % 2:
            compact += "0"
        try:
            decoded = bytes.fromhex(compact)
        except ValueError:
            continue
        for value in _decoded_text_values(decoded):
            risks.update(_text_safety_risks(value, include_paths=True))
            if _POTCAR_RAW.search(value):
                risks.add("potcar_raw_content")
    return risks


_PDF_WHITESPACE = b"\x00\x09\x0a\x0c\x0d\x20"
_PDF_DELIMITERS = b"()<>[]{}/%"
_PDF_MAX_DICTIONARY_BYTES = 8192
_PDF_MAX_TOKENS = 1024
_PDF_MAX_VALUE_DEPTH = 8
_PDF_MAX_CONTAINER_ITEMS = 128


def _pdf_skip_literal_string(data: bytes, position: int) -> int:
    depth = 1
    current = position + 1
    while current < len(data):
        value = data[current]
        if value == 0x5C:  # backslash escape / line continuation
            current += 1
            if current < len(data) and data[current] == 0x0D:
                current += 1
                if current < len(data) and data[current] == 0x0A:
                    current += 1
            elif current < len(data):
                current += 1
            continue
        if value == 0x28:
            depth += 1
            if depth > _PDF_MAX_VALUE_DEPTH:
                raise ValueError("PDF literal string nesting is unbounded")
        elif value == 0x29:
            depth -= 1
            if depth == 0:
                return current + 1
        current += 1
    raise ValueError("PDF literal string is unterminated")


def _pdf_name(value: bytes) -> str:
    output = bytearray()
    position = 0
    while position < len(value):
        if value[position] == 0x23:  # #xx name escape
            if position + 2 >= len(value):
                raise ValueError("PDF name escape is invalid")
            try:
                output.append(int(value[position + 1:position + 3], 16))
            except ValueError as exc:
                raise ValueError("PDF name escape is invalid") from exc
            position += 3
        else:
            output.append(value[position])
            position += 1
    return output.decode("latin-1")


def _pdf_dictionary_tokens(data: bytes) -> list[tuple[str, Any]]:
    if len(data) > _PDF_MAX_DICTIONARY_BYTES:
        raise ValueError("PDF stream dictionary exceeds its byte budget")
    tokens: list[tuple[str, Any]] = []
    position = 0
    while position < len(data):
        value = data[position]
        if value in _PDF_WHITESPACE:
            position += 1
            continue
        if value == 0x25:  # comment
            position += 1
            while position < len(data) and data[position] not in b"\r\n":
                position += 1
            continue
        if data.startswith(b"<<", position):
            tokens.append(("dict_start", None))
            position += 2
        elif data.startswith(b">>", position):
            tokens.append(("dict_end", None))
            position += 2
        elif value == 0x5B:
            tokens.append(("array_start", None))
            position += 1
        elif value == 0x5D:
            tokens.append(("array_end", None))
            position += 1
        elif value == 0x28:
            position = _pdf_skip_literal_string(data, position)
            tokens.append(("opaque", None))
        elif value == 0x3C:
            end = data.find(b">", position + 1)
            if end < 0 or any(
                byte not in _PDF_WHITESPACE and chr(byte) not in "0123456789abcdefABCDEF"
                for byte in data[position + 1:end]
            ):
                raise ValueError("PDF hex string is invalid")
            tokens.append(("opaque", None))
            position = end + 1
        elif value == 0x2F:
            end = position + 1
            while (
                end < len(data) and data[end] not in _PDF_WHITESPACE
                and data[end] not in _PDF_DELIMITERS
            ):
                end += 1
            if end == position + 1:
                raise ValueError("PDF name is empty")
            tokens.append(("name", _pdf_name(data[position + 1:end])))
            position = end
        else:
            end = position
            while (
                end < len(data) and data[end] not in _PDF_WHITESPACE
                and data[end] not in _PDF_DELIMITERS
            ):
                end += 1
            if end == position:
                raise ValueError("PDF dictionary token is unsupported")
            raw = data[position:end]
            if re.fullmatch(rb"[-+]?\d+", raw):
                tokens.append(("integer", int(raw)))
            elif re.fullmatch(rb"[-+]?(?:\d+\.\d*|\.\d+)", raw):
                tokens.append(("real", raw.decode("ascii")))
            elif raw == b"null":
                tokens.append(("null", None))
            elif raw in {b"true", b"false"}:
                tokens.append(("boolean", raw == b"true"))
            else:
                tokens.append(("keyword", raw.decode("latin-1")))
            position = end
        if len(tokens) > _PDF_MAX_TOKENS:
            raise ValueError("PDF stream dictionary token budget exceeded")
    return tokens


def _pdf_parsed_value(
    tokens: Sequence[tuple[str, Any]], position: int, *, depth: int = 0,
) -> tuple[Any, int]:
    if depth > _PDF_MAX_VALUE_DEPTH or position >= len(tokens):
        raise ValueError("PDF dictionary value nesting is invalid")
    kind, value = tokens[position]
    if kind == "name":
        return value, position + 1
    if kind == "integer":
        if (
            position + 2 < len(tokens)
            and tokens[position + 1][0] == "integer"
            and tokens[position + 2] == ("keyword", "R")
        ):
            return ("indirect", value, tokens[position + 1][1]), position + 3
        return value, position + 1
    if kind in {"real", "opaque", "boolean"}:
        return (kind, value), position + 1
    if kind == "null":
        return None, position + 1
    if kind == "keyword":
        return ("keyword", value), position + 1
    if kind == "array_start":
        result: list[Any] = []
        current = position + 1
        while current < len(tokens) and tokens[current][0] != "array_end":
            item, current = _pdf_parsed_value(tokens, current, depth=depth + 1)
            result.append(item)
            if len(result) > _PDF_MAX_CONTAINER_ITEMS:
                raise ValueError("PDF array item budget exceeded")
        if current >= len(tokens):
            raise ValueError("PDF array is unterminated")
        return result, current + 1
    if kind == "dict_start":
        result_dict: dict[str, Any] = {}
        current = position + 1
        while current < len(tokens) and tokens[current][0] != "dict_end":
            if tokens[current][0] != "name" or tokens[current][1] in result_dict:
                raise ValueError("PDF dictionary key is invalid or duplicate")
            key = str(tokens[current][1])
            item, current = _pdf_parsed_value(tokens, current + 1, depth=depth + 1)
            result_dict[key] = item
            if len(result_dict) > _PDF_MAX_CONTAINER_ITEMS:
                raise ValueError("PDF dictionary item budget exceeded")
        if current >= len(tokens):
            raise ValueError("PDF dictionary is unterminated")
        return result_dict, current + 1
    raise ValueError("PDF dictionary value is unsupported")


def _pdf_top_dictionary(data: bytes) -> Mapping[str, Any]:
    tokens = _pdf_dictionary_tokens(data)
    value, position = _pdf_parsed_value(tokens, 0)
    if not isinstance(value, Mapping) or position != len(tokens):
        raise ValueError("PDF stream dictionary is invalid")
    return value


def _pdf_filter_configuration(
    dictionary: Mapping[str, Any],
) -> tuple[list[str], list[Mapping[str, Any] | None]]:
    filter_value = dictionary.get("Filter")
    if "Filter" not in dictionary:
        filters: list[str] = []
    elif isinstance(filter_value, str):
        filters = [filter_value]
    elif isinstance(filter_value, list) and all(isinstance(item, str) for item in filter_value):
        filters = list(filter_value)
    else:
        raise ValueError("PDF filter declaration is invalid")
    if len(filters) > 8:
        raise ValueError("PDF filter chain is unbounded")
    parameter_value = dictionary.get("DecodeParms")
    if parameter_value is None:
        parameters: list[Mapping[str, Any] | None] = [None] * len(filters)
    elif isinstance(parameter_value, Mapping):
        if len(filters) != 1:
            raise ValueError("PDF DecodeParms do not match the filter chain")
        parameters = [parameter_value]
    elif isinstance(parameter_value, list):
        if len(parameter_value) != len(filters) or not all(
            item is None or isinstance(item, Mapping) for item in parameter_value
        ):
            raise ValueError("PDF DecodeParms do not match the filter chain")
        parameters = list(parameter_value)
    else:
        raise ValueError("PDF DecodeParms are invalid")
    return filters, parameters


def _pdf_predictor_parameters(
    parameters: Mapping[str, Any] | None,
) -> tuple[int, int, int, int]:
    values = dict(parameters or {})
    allowed = {"Predictor", "Columns", "Colors", "BitsPerComponent"}
    if set(values) - allowed:
        raise ValueError("PDF predictor parameters are unsupported")
    predictor = values.get("Predictor", 1)
    columns = values.get("Columns", 1)
    colors = values.get("Colors", 1)
    bits = values.get("BitsPerComponent", 8)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in (
        predictor, columns, colors, bits,
    )):
        raise ValueError("PDF predictor parameters are invalid")
    if columns <= 0 or colors <= 0 or bits not in {1, 2, 4, 8, 16}:
        raise ValueError("PDF predictor parameters are invalid")
    return predictor, columns, colors, bits


_PDF_MAX_PREDICTOR_WORK_BYTES = 16 * 1024 * 1024
_PDF_MAX_PACKED_TIFF_SAMPLES = 1024 * 1024


def _pdf_packed_sample(row: bytearray, index: int, bits: int) -> int:
    bit_offset = index * bits
    shift = 8 - bits - (bit_offset % 8)
    return (row[bit_offset // 8] >> shift) & ((1 << bits) - 1)


def _pdf_set_packed_sample(row: bytearray, index: int, bits: int, value: int) -> None:
    bit_offset = index * bits
    shift = 8 - bits - (bit_offset % 8)
    mask = ((1 << bits) - 1) << shift
    byte_index = bit_offset // 8
    row[byte_index] = (row[byte_index] & ~mask) | ((value << shift) & mask)


def _pdf_paeth(left: int, up: int, up_left: int) -> int:
    prediction = left + up - up_left
    distances = (
        (abs(prediction - left), left),
        (abs(prediction - up), up),
        (abs(prediction - up_left), up_left),
    )
    return min(distances, key=lambda item: item[0])[1]


def _undo_pdf_predictor(
    data: bytes, parameters: Mapping[str, Any] | None, limits: ArchiveLimits,
) -> bytes:
    predictor, columns, colors, bits = _pdf_predictor_parameters(parameters)
    if predictor == 1:
        return data
    row_bits = columns * colors * bits
    if row_bits <= 0 or row_bits > limits.max_member_bytes * 8:
        raise ValueError("PDF predictor row exceeds its bound")
    row_bytes = (row_bits + 7) // 8
    if predictor == 2:
        if len(data) % row_bytes:
            raise ValueError("PDF TIFF predictor rows are incomplete")
        sample_count = columns * colors
        row_count = len(data) // row_bytes
        total_samples = sample_count * row_count
        if len(data) > _PDF_MAX_PREDICTOR_WORK_BYTES:
            raise ValueError("PDF predictor work budget exceeded")
        if bits < 8 and total_samples > _PDF_MAX_PACKED_TIFF_SAMPLES:
            raise ValueError("PDF packed TIFF predictor work budget exceeded")
        modulus = 1 << bits
        output = bytearray(len(data))
        for offset in range(0, len(data), row_bytes):
            row = bytearray(data[offset:offset + row_bytes])
            if bits == 8:
                for index in range(colors, sample_count):
                    row[index] = (row[index] + row[index - colors]) & 0xFF
            elif bits == 16:
                for index in range(colors, sample_count):
                    current_offset = index * 2
                    previous_offset = (index - colors) * 2
                    current = (row[current_offset] << 8) | row[current_offset + 1]
                    previous = (row[previous_offset] << 8) | row[previous_offset + 1]
                    decoded = (current + previous) & 0xFFFF
                    row[current_offset] = decoded >> 8
                    row[current_offset + 1] = decoded & 0xFF
            else:
                for index in range(colors, sample_count):
                    decoded = (
                        _pdf_packed_sample(row, index, bits)
                        + _pdf_packed_sample(row, index - colors, bits)
                    ) % modulus
                    _pdf_set_packed_sample(row, index, bits, decoded)
            output[offset:offset + row_bytes] = row
        return bytes(output)
    if not 10 <= predictor <= 15:
        raise ValueError("PDF predictor is unsupported")
    encoded_row_bytes = row_bytes + 1
    if len(data) % encoded_row_bytes:
        raise ValueError("PDF PNG predictor rows are incomplete")
    decoded_size = (len(data) // encoded_row_bytes) * row_bytes
    if decoded_size > _PDF_MAX_PREDICTOR_WORK_BYTES:
        raise ValueError("PDF predictor work budget exceeded")
    bytes_per_pixel = max(1, (colors * bits + 7) // 8)
    previous = bytes(row_bytes)
    output = bytearray()
    for offset in range(0, len(data), encoded_row_bytes):
        filter_type = data[offset]
        if filter_type > 4:
            raise ValueError("PDF PNG predictor filter is unsupported")
        encoded = data[offset + 1:offset + encoded_row_bytes]
        decoded = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = decoded[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            up = previous[index]
            up_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor_value = 0
            elif filter_type == 1:
                predictor_value = left
            elif filter_type == 2:
                predictor_value = up
            elif filter_type == 3:
                predictor_value = (left + up) // 2
            else:
                predictor_value = _pdf_paeth(left, up, up_left)
            decoded[index] = (value + predictor_value) & 0xFF
        output.extend(decoded)
        previous = bytes(decoded)
        if len(output) > limits.max_member_bytes:
            raise ValueError("PDF predictor output exceeds its bound")
    return bytes(output)


def _decode_pdf_stream(
    dictionary: Mapping[str, Any], payload: bytes, limits: ArchiveLimits,
) -> tuple[bytes | None, str | None]:
    try:
        filters, decode_parameters = _pdf_filter_configuration(dictionary)
    except ValueError:
        return None, "pdf_filter_or_decode_parameters_invalid"
    decoded = payload.strip(b"\r\n")
    for filter_name, parameters in zip(filters, decode_parameters, strict=True):
        try:
            if filter_name == "ASCII85Decode":
                if parameters:
                    return None, "pdf_filter_or_decode_parameters_invalid"
                value = decoded.strip()
                if value.startswith(b"<~"):
                    value = value[2:]
                if value.endswith(b"~>"):
                    value = value[:-2]
                decoded = base64.a85decode(value, adobe=False)
            elif filter_name == "ASCIIHexDecode":
                if parameters:
                    return None, "pdf_filter_or_decode_parameters_invalid"
                value = re.sub(rb"\s+", b"", decoded).rstrip(b">")
                if len(value) % 2:
                    value += b"0"
                decoded = bytes.fromhex(value.decode("ascii"))
            elif filter_name == "FlateDecode":
                decompressor = zlib.decompressobj()
                expanded = decompressor.decompress(decoded, limits.max_member_bytes + 1)
                if len(expanded) > limits.max_member_bytes or decompressor.unconsumed_tail:
                    return None, "pdf_stream_byte_limit"
                expanded += decompressor.flush(
                    max(0, limits.max_member_bytes + 1 - len(expanded)),
                )
                if len(expanded) > limits.max_member_bytes:
                    return None, "pdf_stream_byte_limit"
                if not decompressor.eof or decompressor.unused_data:
                    return None, "pdf_stream_invalid"
                decoded = _undo_pdf_predictor(expanded, parameters, limits)
            elif filter_name in {
                "DCTDecode", "JPXDecode", "CCITTFaxDecode", "JBIG2Decode",
            }:
                return None, None  # governed image stream; raw bytes were scanned
            else:
                return None, "pdf_filter_unsupported"
        except Exception:  # noqa: BLE001 - malformed/hostile streams fail closed
            return None, "pdf_stream_invalid"
        if len(decoded) > limits.max_member_bytes:
            return None, "pdf_stream_byte_limit"
    return decoded, None


def _pdf_only_space_or_comments(data: bytes) -> bool:
    position = 0
    while position < len(data):
        if data[position] in _PDF_WHITESPACE:
            position += 1
        elif data[position] == 0x25:
            position += 1
            while position < len(data) and data[position] not in b"\r\n":
                position += 1
        else:
            return False
    return True


def _pdf_enclosing_dictionary(data: bytes) -> bytes:
    stack: list[int] = []
    spans: list[tuple[int, int]] = []
    position = 0
    while position < len(data):
        value = data[position]
        if value == 0x25:
            position += 1
            while position < len(data) and data[position] not in b"\r\n":
                position += 1
        elif value == 0x28:
            position = _pdf_skip_literal_string(data, position)
        elif data.startswith(b"<<", position):
            stack.append(position)
            if len(stack) > _PDF_MAX_VALUE_DEPTH:
                raise ValueError("PDF dictionary nesting is unbounded")
            position += 2
        elif data.startswith(b">>", position):
            if not stack:
                raise ValueError("PDF dictionary delimiter is unbalanced")
            start = stack.pop()
            spans.append((start, position + 2))
            position += 2
        elif value == 0x3C:
            end = data.find(b">", position + 1)
            if end < 0:
                raise ValueError("PDF hex string is unterminated")
            position = end + 1
        else:
            position += 1
    if stack:
        raise ValueError("PDF dictionary delimiter is unbalanced")
    candidates = [span for span in spans if _pdf_only_space_or_comments(data[span[1]:])]
    if not candidates:
        raise ValueError("PDF stream dictionary is unavailable")
    start, end = max(candidates, key=lambda span: span[1] - span[0])
    if end - start > _PDF_MAX_DICTIONARY_BYTES:
        raise ValueError("PDF stream dictionary exceeds its byte budget")
    return data[start:end]


def _pdf_stream_records(
    data: bytes, limits: ArchiveLimits,
) -> tuple[list[tuple[Mapping[str, Any], bytes]], set[str]]:
    """Read PDF streams by declared /Length and a grammar-valid end marker."""

    records: list[tuple[Mapping[str, Any], bytes]] = []
    risks: set[str] = set()
    stream_token = re.compile(rb"(?<![A-Za-z])stream(?:\r\n|\n|\r)")
    for match in stream_token.finditer(data):
        if len(records) >= limits.max_office_members:
            risks.add("pdf_stream_count_limit")
            break
        prefix = data[max(0, match.start() - 8192):match.start()]
        try:
            dictionary_bytes = _pdf_enclosing_dictionary(prefix)
            dictionary = _pdf_top_dictionary(dictionary_bytes)
        except ValueError:
            risks.add("pdf_stream_dictionary_invalid")
            continue
        length = dictionary.get("Length")
        if isinstance(length, bool) or not isinstance(length, int):
            length = -1
        if length < 0:
            risks.add("pdf_stream_length_invalid")
            continue
        if length > limits.max_member_bytes:
            risks.add("pdf_stream_byte_limit")
            continue
        start = match.end()
        end = start + length
        if end > len(data):
            risks.add("pdf_stream_length_invalid")
            continue
        remainder = data[end:end + 32]
        boundary = re.match(rb"(?:\r\n|\n|\r)?endstream(?=[\s<>{}\[\]()/%%]|$)", remainder)
        if boundary is None:
            risks.add("pdf_stream_boundary_invalid")
            continue
        records.append((dictionary, data[start:end]))
    return records, risks


def _pdf_safety_risks(data: bytes, limits: ArchiveLimits) -> list[str]:
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
        return ["invalid_pdf"]
    risks = _pdf_lexical_risks(data)
    # PDF names such as /Type and /Catalog are lexical tokens, not filesystem
    # paths.  Local paths and URLs that can carry credentials occur in strings.
    total_decoded = 0
    stream_records, stream_risks = _pdf_stream_records(data, limits)
    risks.update(stream_risks)
    for dictionary, stream_payload in stream_records:
        decoded, error = _decode_pdf_stream(dictionary, stream_payload, limits)
        if error:
            risks.add(error)
            continue
        if decoded is None:
            raw_text = stream_payload.decode("latin-1", errors="ignore")
            risks.update(_text_safety_risks(raw_text, include_paths=False))
            continue
        total_decoded += len(decoded)
        if total_decoded > limits.max_total_member_bytes:
            risks.add("pdf_stream_total_byte_limit")
            break
        if _container_kind("pdf-stream.bin", decoded) is not None:
            risks.add("nested_archive_forbidden")
        risks.update(_pdf_lexical_risks(decoded))
    return sorted(risks)


def _office_xml_risks(name: str, data: bytes) -> set[str]:
    risks: set[str] = set()
    decoded = data.decode("utf-8", errors="ignore")
    if _POTCAR_RAW.search(decoded):
        risks.add("potcar_raw_content")
    try:
        root = ET.fromstring(data)
    except Exception:  # noqa: BLE001
        return {"office_xml_invalid"}
    for element in root.iter():
        for value in (element.text, element.tail):
            if value:
                risks.update(_text_safety_risks(value))
        for attr_name, raw_value in element.attrib.items():
            value = str(raw_value)
            local_name = attr_name.rsplit("}", 1)[-1].casefold()
            package_part_name = (
                (name == "[content_types].xml" and local_name == "partname")
                or local_name == "selectedstyle"
            )
            relationship_locator = (
                name.endswith(".rels")
                and local_name in {"target", "targetmode"}
            )
            include_paths = not (package_part_name or relationship_locator)
            risks.update(_text_safety_risks(value, include_paths=include_paths))
    return risks


_OOXML_PACKAGE_ROOTS = frozenset({
    "word", "xl", "ppt", "docprops", "customxml", "_rels",
})


def _ooxml_relationship_base(name: str) -> str | None:
    lowered = name.casefold()
    if lowered == "_rels/.rels":
        return ""
    marker = "/_rels/"
    if marker not in lowered or not lowered.endswith(".rels"):
        return None
    prefix, relation_name = name.rsplit(marker, 1)
    source_name = relation_name[:-5]
    return posixpath.dirname(f"{prefix}/{source_name}")


def _ooxml_reference_risks(
    payloads: Mapping[str, bytes], member_hashes: Mapping[str, str],
) -> set[str]:
    """Require every internal OPC relationship/PartName to close over read bytes."""

    risks: set[str] = set()
    folded_names = {name.casefold(): name for name in member_hashes}
    content_types = payloads.get("[Content_Types].xml")
    if content_types is not None:
        try:
            root = ET.fromstring(content_types)
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1].casefold() != "override":
                    continue
                raw_part = str(element.attrib.get("PartName") or "")
                if not raw_part.startswith("/") or "\\" in raw_part:
                    risks.add("office_part_binding_invalid")
                    continue
                part = unquote(raw_part.lstrip("/"))
                try:
                    safe_part = _safe_archive_path(part)
                except ValueError:
                    risks.add("office_part_binding_invalid")
                    continue
                root_name = safe_part.split("/", 1)[0].casefold()
                if (
                    root_name not in _OOXML_PACKAGE_ROOTS
                    or safe_part.casefold() not in folded_names
                    or not _HASH.fullmatch(member_hashes[folded_names[safe_part.casefold()]])
                ):
                    risks.add("office_part_binding_invalid")
        except Exception:  # noqa: BLE001
            risks.add("office_xml_invalid")
    for name, data in payloads.items():
        if not name.casefold().endswith(".rels"):
            continue
        base = _ooxml_relationship_base(name)
        if base is None:
            risks.add("office_relationship_invalid")
            continue
        try:
            root = ET.fromstring(data)
        except Exception:  # noqa: BLE001
            risks.add("office_xml_invalid")
            continue
        relationship_ids: set[str] = set()
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1].casefold() != "relationship":
                continue
            relationship_id = str(element.attrib.get("Id") or "")
            if not relationship_id or relationship_id in relationship_ids:
                risks.add("office_relationship_invalid")
            relationship_ids.add(relationship_id)
            target = str(element.attrib.get("Target") or "").strip()
            mode = str(element.attrib.get("TargetMode") or "Internal").strip().casefold()
            if not target or "\\" in target:
                risks.add("office_relationship_invalid")
                continue
            try:
                parsed = urlsplit(target)
            except Exception:  # noqa: BLE001
                risks.add("office_relationship_invalid")
                continue
            if parsed.username is not None or parsed.password is not None:
                risks.add("secret_literal")
            if mode == "external":
                if parsed.scheme.casefold() == "file" or not parsed.scheme:
                    risks.add("absolute_path")
                continue
            if mode != "internal" or parsed.scheme or parsed.netloc:
                risks.add("office_relationship_invalid")
                continue
            path = unquote(parsed.path)
            if path.startswith("/"):
                candidate = posixpath.normpath(path.lstrip("/"))
                if candidate.split("/", 1)[0].casefold() not in _OOXML_PACKAGE_ROOTS:
                    risks.add("absolute_path")
                    continue
            else:
                candidate = posixpath.normpath(posixpath.join(base, path))
            if candidate in {"", ".", ".."} or candidate.startswith("../"):
                risks.add("office_relationship_invalid")
                continue
            try:
                safe_candidate = _safe_archive_path(candidate)
            except ValueError:
                risks.add("office_relationship_invalid")
                continue
            bound_name = folded_names.get(safe_candidate.casefold())
            if bound_name is None or not _HASH.fullmatch(member_hashes[bound_name]):
                risks.add("office_relationship_target_missing")
    return risks


def _ooxml_safety_risks(name: str, data: bytes, limits: ArchiveLimits) -> list[str]:
    suffix = Path(name.lower()).suffix
    required = {
        ".docx": {"[Content_Types].xml", "_rels/.rels", "word/document.xml"},
        ".xlsx": {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"},
        ".pptx": {"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"},
    }.get(suffix)
    if required is None:
        return ["nested_archive_forbidden"]
    risks: set[str] = set()
    total = 0
    names: list[str] = []
    payloads: dict[str, bytes] = {}
    member_hashes: dict[str, str] = {}
    try:
        with zipfile.ZipFile(_ReadOnlyBytes(data), "r") as office:
            for index, info in enumerate(office.filelist):
                if index >= limits.max_office_members:
                    risks.add("office_member_count_limit")
                    break
                try:
                    safe_name = _safe_archive_path(
                        info.filename, max_length=limits.max_path_length,
                    )
                except ValueError:
                    risks.add("unsafe_office_member_path")
                    continue
                if info.is_dir() or safe_name.endswith("/"):
                    continue
                names.append(safe_name)
                if info.flag_bits & 0x1 or info.compress_type not in {
                    zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                }:
                    risks.add("office_container_unsupported")
                    continue
                if info.file_size > limits.max_member_bytes:
                    risks.add("office_member_byte_limit")
                    continue
                total += info.file_size
                if total > limits.max_office_total_bytes:
                    risks.add("office_total_byte_limit")
                    continue
                if info.file_size and (
                    info.compress_size <= 0
                    or info.file_size > info.compress_size * limits.max_compression_ratio
                ):
                    risks.add("office_compression_ratio_limit")
                    continue
                lowered = safe_name.casefold()
                if any(part in lowered for part in (
                    "vbaproject", "/embeddings/", "/activex/", "oleobject",
                )) or lowered.endswith((".exe", ".dll", ".js", ".vbs", ".bin")):
                    risks.add("office_active_content_forbidden")
                    continue
                if not lowered.endswith((
                    ".xml", ".rels", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
                    ".tif", ".tiff", ".emf", ".wmf", ".svg", ".odttf", ".ttf",
                )):
                    risks.add("office_member_type_unsupported")
                    continue
                with office.open(info, "r") as stream:
                    member = _read_stream_limited(stream, limits.max_member_bytes)
                member_hashes[safe_name] = _sha256(member)
                if _container_kind(safe_name, member) is not None:
                    risks.add("nested_archive_forbidden")
                    continue
                if lowered.endswith((".xml", ".rels")):
                    payloads[safe_name] = member
                    risks.update(_office_xml_risks(lowered, member))
                else:
                    decoded = member.decode("utf-8", errors="ignore")
                    if _POTCAR_RAW.search(decoded):
                        risks.add("potcar_raw_content")
                    risks.update(_text_safety_risks(decoded, include_paths=False))
    except Exception:  # noqa: BLE001
        return ["office_container_invalid"]
    if len(names) != len(set(names)) or len({item.casefold() for item in names}) != len(names):
        risks.add("office_member_name_collision")
    if not required.issubset(names):
        risks.add("office_structure_incomplete")
    risks.update(_ooxml_reference_risks(payloads, member_hashes))
    return sorted(risks)


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
    if isinstance(value, str) and normalized in {"archive_path", "name"}:
        try:
            return _safe_archive_path(value)
        except ValueError:
            pass
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
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return copy.deepcopy(value)
    return "[redacted-unsupported-value]"


def _safe_archive_path(
    value: str, *, max_length: int = DEFAULT_ARCHIVE_LIMITS.max_path_length,
) -> str:
    name = str(value or "")
    if not name or "\\" in name or "\x00" in name:
        raise ValueError("archive member path is invalid")
    if len(name.encode("utf-8")) > max_length:
        raise ValueError("archive member path exceeds the portable length budget")
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


def _payload_risks(
    name: str, data: bytes, *, limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> list[str]:
    risks: list[str] = []
    if len(data) > limits.max_member_bytes:
        return ["member_byte_limit"]
    basename = PurePosixPath(name).name.upper()
    if basename == "POTCAR" or basename.startswith("POTCAR."):
        risks.append("potcar_raw_filename")
    name_risks = _text_safety_risks(name)
    if "secret_literal" in name_risks:
        risks.append("secret_in_path")
    if "absolute_path" in name_risks:
        risks.append("absolute_path")
    kind = _container_kind(name, data)
    suffix = Path(name.lower()).suffix
    if suffix in _OOXML_SUFFIXES and kind != "ooxml":
        risks.append("office_container_invalid")
    elif kind == "ooxml":
        risks.extend(_ooxml_safety_risks(name, data, limits))
    elif kind is not None:
        risks.append("nested_archive_forbidden")
    elif suffix == ".pdf":
        risks.extend(_pdf_safety_risks(data, limits))
    elif suffix in {".html", ".htm"}:
        html_risks, _refs, _asset_refs = _html_scan(data)
        risks.extend(html_risks)
    elif suffix == ".css":
        css_risks, _refs = _css_scan(data)
        risks.extend(css_risks)
    else:
        include_paths = suffix not in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
        }
        for decoded in _decoded_text_values(data):
            if _POTCAR_RAW.search(decoded):
                risks.append("potcar_raw_content")
            risks.extend(_text_safety_risks(decoded, include_paths=include_paths))
    return sorted(set(risks))


def _entity_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev), int(value.st_ino), stat.S_IFMT(value.st_mode),
        int(getattr(value, "st_file_attributes", 0)),
    )


def _path_ancestor_chain(path: str | os.PathLike[str]) -> tuple[tuple[str, tuple[int, int, int, int]], ...]:
    """Capture every namespace ancestor without resolving reparse points."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    chain: list[tuple[str, tuple[int, int, int, int]]] = []
    for ancestor in reversed((absolute.parent, *absolute.parent.parents)):
        current = os.lstat(ancestor)
        if _is_symlink_or_reparse(current) or not stat.S_ISDIR(current.st_mode):
            raise ValueError("authoritative member ancestor is not a physical directory")
        chain.append((str(ancestor), _entity_identity(current)))
    return tuple(chain)


def _verify_path_ancestor_chain(
    chain: Sequence[tuple[str, tuple[int, int, int, int]]],
) -> None:
    for path, expected in chain:
        current = os.lstat(path)
        if _is_symlink_or_reparse(current) or _entity_identity(current) != expected:
            raise StaleRevisionError("authoritative member ancestor entity changed")


def _safe_read_bound_file(
    path: str | os.PathLike[str], *, expected_sha256: str | None, expected_size: int,
    max_bytes: int = DEFAULT_ARCHIVE_LIMITS.max_member_bytes,
) -> bytes:
    """Read one hash-bound regular file without following a swapped symlink."""

    expected = None if expected_sha256 is None else str(expected_sha256 or "").lower()
    if expected is not None and not _HASH.fullmatch(expected):
        raise ValueError("authoritative member SHA-256 is invalid")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError("authoritative member size is invalid")
    if expected_size > max_bytes:
        raise ArchiveVerificationError("authoritative member exceeds the byte budget")
    candidate = os.path.abspath(os.fspath(path))
    ancestor_chain = _path_ancestor_chain(candidate)
    before = os.lstat(candidate)
    if _is_symlink_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("authoritative member is not a regular non-symlink file")
    if int(before.st_size) != expected_size:
        raise StaleRevisionError("authoritative member declared size changed")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_descriptor = None
    if os.name != "nt":
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_descriptor = os.open(os.path.dirname(candidate), parent_flags)
        parent_opened = os.fstat(parent_descriptor)
        if _entity_identity(parent_opened) != ancestor_chain[-1][1]:
            os.close(parent_descriptor)
            raise StaleRevisionError("authoritative member ancestor changed during open")
        descriptor = os.open(os.path.basename(candidate), flags, dir_fd=parent_descriptor)
    else:
        descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        before_identity = _entity_identity(before)
        opened_identity = _entity_identity(opened)
        if before_identity != opened_identity or not stat.S_ISREG(opened.st_mode):
            raise StaleRevisionError("authoritative member changed during open")
        chunks = bytearray()
        while len(chunks) <= expected_size:
            block = os.read(descriptor, min(1024 * 1024, expected_size + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > expected_size or os.read(descriptor, 1):
            raise StaleRevisionError("authoritative member size changed during read")
        after = os.fstat(descriptor)
        if parent_descriptor is not None:
            named_after = os.stat(
                os.path.basename(candidate), dir_fd=parent_descriptor, follow_symlinks=False,
            )
        else:
            named_after = os.lstat(candidate)
        if (
            _entity_identity(after) != opened_identity
            or _is_symlink_or_reparse(named_after)
            or _entity_identity(named_after) != opened_identity
            or after.st_size != opened.st_size
            or getattr(after, "st_mtime_ns", None) != getattr(opened, "st_mtime_ns", None)
        ):
            raise StaleRevisionError("authoritative member changed during read")
        _verify_path_ancestor_chain(ancestor_chain)
    finally:
        os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    payload = bytes(chunks)
    if len(payload) != expected_size or (
        expected is not None and _sha256(payload) != expected
    ):
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
    base = Path(os.path.abspath(str(bundle.entry["manifest"]))).parent
    candidate = Path(os.path.abspath(base / Path(*PurePosixPath(safe).parts)))
    try:
        if os.path.normcase(os.path.commonpath((str(base), str(candidate)))) != os.path.normcase(str(base)):
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


def _strict_artifact_rights(
    record: Mapping[str, Any],
) -> tuple[bool, str | None, tuple[str, str, str], tuple[Any, ...]]:
    """Validate one server-owned rights declaration without source guessing."""

    rights = record.get("rights")
    if not isinstance(rights, Mapping):
        extensions = record.get("extensions")
        rights = extensions.get("rights") if isinstance(extensions, Mapping) else None
    missing = ("NOASSERTION", "missing", "")
    if not isinstance(rights, Mapping):
        return False, "asset_rights_binding_missing", missing, ()
    if _structured_safety_risks(rights):
        return False, "asset_rights_sensitive", missing, ()
    if rights.get("schema") != _ARTIFACT_RIGHTS_SCHEMA:
        return False, "asset_rights_binding_missing", missing, ()
    source_kind = str(rights.get("source_kind") or "").strip().casefold()
    external = {"external", "published", "third_party", "third-party"}
    internal = {"first_party", "first-party", "project", "original", "local"}
    if source_kind not in external | internal:
        return False, "asset_rights_binding_missing", missing, ()
    third_party = rights.get("third_party")
    if third_party is not None and not isinstance(third_party, bool):
        return False, "asset_rights_conflict", missing, ()
    inferred_third_party = source_kind in external
    if third_party is not None and third_party != inferred_third_party:
        return False, "asset_rights_conflict", missing, ()
    redistributable = rights.get("redistributable")
    redistribution_status = str(rights.get("redistributable_status") or "").casefold()
    if redistributable is False:
        return False, "asset_not_redistributable", missing, ()
    if redistributable is not True or redistribution_status != "declared":
        return False, "asset_rights_binding_missing", missing, ()
    license_id, computed_status = _license_record(rights.get("license"))
    declared_status = str(rights.get("license_status") or "").casefold()
    if computed_status != "declared" or declared_status != "declared":
        return False, "asset_license_missing", missing, ()
    if not _TOKEN.fullmatch(license_id):
        return False, "asset_license_missing", missing, ()
    attribution = _attribution_text(rights.get("attribution"))
    if inferred_third_party and not attribution:
        return False, "third_party_license_or_attribution_missing", (
            license_id, "declared", "",
        ), ()
    signature = (
        license_id, attribution, source_kind, inferred_third_party,
        redistributable, redistribution_status,
    )
    return True, None, (license_id, "declared", attribution), signature


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
    manifest_ok, manifest_reason, manifest_rights, manifest_signature = (
        _strict_artifact_rights(manifest_record)
    )
    if not manifest_ok:
        return manifest_ok, manifest_reason, manifest_rights
    figures = _matching_figures(bundle, digest)
    if not figures:
        return False, "asset_model_binding_missing", ("NOASSERTION", "missing", "")
    for figure in figures:
        figure_ok, figure_reason, _figure_rights, figure_signature = (
            _strict_artifact_rights(figure)
        )
        if not figure_ok:
            return False, figure_reason, ("NOASSERTION", "missing", "")
        if figure_signature != manifest_signature:
            return False, "asset_rights_conflict", ("NOASSERTION", "missing", "")
    return True, None, manifest_rights


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
    *, limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> None:
    if len(member.data) > limits.max_member_bytes:
        exclusions.append(_excluded_decision(
            archive_path=None, logical_role=member.logical_role,
            reason="member_byte_limit", sensitive_risk="high",
            size=len(member.data), sha256=member.sha256,
            license_id=member.license_id, license_status=member.license_status,
            attribution=member.attribution, authority=member.authority,
            provider_id=member.provider_id,
        ))
        return
    if len(members) >= limits.max_members or (
        sum(len(item.data) for item in members) + len(member.data)
        > limits.max_total_member_bytes
    ):
        exclusions.append(_excluded_decision(
            archive_path=None, logical_role=member.logical_role,
            reason="archive_member_or_total_byte_limit", sensitive_risk="high",
            size=len(member.data), sha256=member.sha256,
            license_id=member.license_id, license_status=member.license_status,
            attribution=member.attribution, authority=member.authority,
            provider_id=member.provider_id,
        ))
        return
    try:
        safe_name = _safe_archive_path(
            member.archive_path, max_length=limits.max_path_length,
        )
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
    if any(item.archive_path.casefold() == safe_name.casefold() for item in members):
        exclusions.append(_excluded_decision(
            archive_path=safe_name, logical_role=member.logical_role,
            reason="portable_name_collision", sensitive_risk="high",
            size=len(member.data), sha256=member.sha256,
            license_id=member.license_id, license_status=member.license_status,
            attribution=member.attribution, authority=member.authority,
            provider_id=member.provider_id,
        ))
        return
    risks = _payload_risks(safe_name, member.data, limits=limits)
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


def _provider_metadata_rights_reason(
    raw: ArchiveAttachment,
) -> str | None:
    """Reject an explicit metadata rights declaration that denies or conflicts."""

    if not isinstance(raw.metadata, Mapping) or "rights" not in raw.metadata:
        return None
    rights = raw.metadata.get("rights")
    if not isinstance(rights, Mapping):
        return "attachment_rights_conflict"
    redistributable = rights.get("redistributable")
    if redistributable is False:
        return "attachment_not_redistributable"
    if redistributable is not True or raw.redistributable is not True:
        return "attachment_rights_conflict"
    third_party = rights.get("third_party")
    if third_party is not None and (
        not isinstance(third_party, bool) or third_party != raw.third_party
    ):
        return "attachment_rights_conflict"
    source_kind = str(rights.get("source_kind") or "").strip().casefold()
    if source_kind:
        inferred = source_kind in {"external", "published", "third_party", "third-party"}
        if source_kind not in {
            "external", "published", "third_party", "third-party",
            "first_party", "first-party", "project", "original", "local",
        } or inferred != raw.third_party:
            return "attachment_rights_conflict"
    metadata_license, metadata_status = _license_record(rights.get("license"))
    raw_license, raw_status = _license_record(raw.license_id)
    if metadata_status != "declared" or raw_status != "declared":
        return "attachment_license_missing"
    if metadata_license != raw_license:
        return "attachment_rights_conflict"
    return None


def _inline_attachment_nbytes(value: Any) -> int:
    """Inspect only exact builtin buffer types without invoking user code."""

    if type(value) is bytes or type(value) is bytearray:
        return len(value)
    if type(value) is memoryview:
        if not value.contiguous or not value.c_contiguous:
            raise ValueError("attachment memoryview must be C-contiguous")
        return int(value.nbytes)
    raise ValueError("attachment inline payload type is invalid")


def _materialize_inline_attachment(value: Any) -> bytes:
    """Materialize only after exact-type, byte, and total-budget preflight."""

    if type(value) is bytes:
        return value
    if type(value) is bytearray:
        return bytes(value)
    if type(value) is memoryview:
        return value.tobytes(order="C")
    raise ValueError("attachment inline payload type is invalid")


def _provider_members(
    bundle: FrozenRevision,
    providers: Sequence[ArchiveAttachmentProvider],
    members: list[_PreparedMember],
    exclusions: list[dict[str, Any]],
    *, limits: ArchiveLimits,
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
            attachments = iter(provider.frozen_attachments(bundle))
        except Exception:
            exclusions.append(_excluded_decision(
                archive_path=None, logical_role="extension_attachment",
                reason="provider_failed", sensitive_risk="unknown",
                authority="extension_provider", provider_id=provider_id,
            ))
            continue
        for attachment_index in range(limits.max_attachments_per_provider + 1):
            try:
                raw = next(attachments)
            except StopIteration:
                break
            except Exception:
                exclusions.append(_excluded_decision(
                    archive_path=None, logical_role="extension_attachment",
                    reason="provider_failed", sensitive_risk="unknown",
                    authority="extension_provider", provider_id=provider_id,
                ))
                break
            if attachment_index >= limits.max_attachments_per_provider:
                exclusions.append(_excluded_decision(
                    archive_path=None, logical_role="extension_attachment",
                    reason="provider_attachment_count_limit", sensitive_risk="high",
                    authority="extension_provider", provider_id=provider_id,
                ))
                break
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
                metadata_risks = _structured_safety_risks(
                    raw.metadata,
                    max_depth=limits.max_metadata_depth,
                    max_nodes=limits.max_metadata_nodes,
                    max_bytes=limits.max_metadata_bytes,
                )
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
            metadata_rights_reason = _provider_metadata_rights_reason(raw)
            if metadata_rights_reason is not None:
                exclusions.append(_excluded_decision(
                    archive_path=None, logical_role="extension_attachment",
                    reason=metadata_rights_reason, sensitive_risk="high",
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
                path = _safe_archive_path(
                    raw.archive_path, max_length=limits.max_path_length,
                )
                if not isinstance(raw.sha256, str) or not _HASH.fullmatch(raw.sha256.lower()):
                    raise ValueError("attachment hash invalid")
                if isinstance(raw.size, bool) or not isinstance(raw.size, int) or raw.size < 0:
                    raise ValueError("attachment size invalid")
                if raw.size > limits.max_member_bytes:
                    raise ArchiveVerificationError("attachment exceeds member byte limit")
                if not isinstance(raw.redistributable, bool) or not isinstance(raw.third_party, bool):
                    raise ValueError("attachment rights booleans are invalid")
                if (raw.data is None) == (raw.source_path is None):
                    raise ValueError("attachment must use exactly one payload source")
            except Exception:  # noqa: BLE001 - one extension cannot weaken the archive
                exclusions.append(_excluded_decision(
                    archive_path=path, logical_role="extension_attachment",
                    reason="attachment_contract_invalid", sensitive_risk="high",
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
            if license_status == "declared" and not _TOKEN.fullmatch(license_id):
                license_id, license_status = "NOASSERTION", "missing"
            attribution = str(redact(raw.attribution)).strip()
            if not raw.redistributable:
                exclusions.append(_excluded_decision(
                    archive_path=path, logical_role=raw.logical_role,
                    reason="attachment_not_redistributable",
                    sensitive_risk=raw.sensitive_risk, size=raw.size,
                    sha256=raw.sha256.lower(), license_id=license_id,
                    license_status=license_status, attribution=attribution,
                    authority="extension_provider", provider_id=provider_id,
                ))
                continue
            if license_status != "declared" or (raw.third_party and not attribution):
                missing_reason = (
                    "third_party_license_or_attribution_missing"
                    if raw.third_party else "attachment_license_missing"
                )
                exclusions.append(_excluded_decision(
                    archive_path=path, logical_role=raw.logical_role,
                    reason=missing_reason,
                    sensitive_risk=raw.sensitive_risk, size=raw.size,
                    sha256=raw.sha256.lower(), license_id=license_id,
                    license_status=license_status, attribution=attribution,
                    authority="extension_provider", provider_id=provider_id,
                ))
                continue
            remaining = limits.max_total_member_bytes - sum(
                len(member.data) for member in members
            )
            if raw.size > remaining:
                exclusions.append(_excluded_decision(
                    archive_path=path, logical_role=raw.logical_role,
                    reason="archive_member_or_total_byte_limit",
                    sensitive_risk="high", size=raw.size,
                    sha256=raw.sha256.lower(), license_id=license_id,
                    license_status=license_status, attribution=attribution,
                    authority="extension_provider", provider_id=provider_id,
                ))
                continue
            try:
                if raw.data is not None:
                    actual_size = _inline_attachment_nbytes(raw.data)
                    if actual_size > limits.max_member_bytes:
                        exclusions.append(_excluded_decision(
                            archive_path=path, logical_role=raw.logical_role,
                            reason="member_byte_limit", sensitive_risk="high",
                            size=actual_size, sha256=raw.sha256.lower(),
                            license_id=license_id, license_status=license_status,
                            attribution=attribution, authority="extension_provider",
                            provider_id=provider_id,
                        ))
                        continue
                    if actual_size > remaining:
                        exclusions.append(_excluded_decision(
                            archive_path=path, logical_role=raw.logical_role,
                            reason="archive_member_or_total_byte_limit",
                            sensitive_risk="high", size=actual_size,
                            sha256=raw.sha256.lower(), license_id=license_id,
                            license_status=license_status, attribution=attribution,
                            authority="extension_provider", provider_id=provider_id,
                        ))
                        continue
                    if actual_size != raw.size:
                        raise StaleRevisionError("attachment payload binding mismatch")
                    payload = _materialize_inline_attachment(raw.data)
                    if len(payload) != raw.size or _sha256(payload) != raw.sha256.lower():
                        raise StaleRevisionError("attachment payload binding mismatch")
                else:
                    payload = _safe_read_bound_file(
                        raw.source_path, expected_sha256=raw.sha256,
                        expected_size=raw.size,
                        max_bytes=min(limits.max_member_bytes, remaining),
                    )
            except Exception as exc:  # noqa: BLE001 - one extension cannot weaken the archive
                reason = "symbolic_link_or_toctou_rejected" if (
                    "symlink" in str(exc).lower() or "changed" in str(exc).lower()
                ) else "attachment_contract_invalid"
                exclusions.append(_excluded_decision(
                    archive_path=path, logical_role="extension_attachment",
                    reason=reason, sensitive_risk="high", size=raw.size,
                    sha256=raw.sha256.lower(), license_id=license_id,
                    license_status=license_status, attribution=attribution,
                    authority="extension_provider", provider_id=provider_id,
                ))
                continue
            _append_member(members, exclusions, _PreparedMember(
                archive_path=path, logical_role=raw.logical_role, data=payload,
                license_id=license_id, attribution=attribution,
                license_status=license_status, sensitive_risk=raw.sensitive_risk,
                authority="extension_provider", provider_id=provider_id,
            ), limits=limits)


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


def _archive_bytes(
    members: Mapping[str, bytes], *, limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> bytes:
    if len(members) > limits.max_members:
        raise ArchiveVerificationError("archive member count limit exceeded")
    if sum(len(data) for data in members.values()) > limits.max_total_member_bytes:
        raise ArchiveVerificationError("archive total byte limit exceeded")
    with tempfile.TemporaryFile(mode="w+b") as output:
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True,
        ) as archive:
            for name, data in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        output.seek(0, os.SEEK_END)
        if output.tell() > limits.max_archive_bytes:
            raise ArchiveVerificationError("final archive byte limit exceeded")
        output.seek(0)
        return _read_stream_limited(output, limits.max_archive_bytes)


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


def _report_record_rights(
    record: Mapping[str, Any], *, fmt: str,
) -> tuple[bool, str | None, tuple[str, str, str]]:
    expected_media = _REPORT_MEDIA_TYPES.get(fmt)
    if (
        expected_media is None
        or record.get("logical_role") != "rendered_report"
        or record.get("format") != fmt
        or record.get("media_type") != expected_media
        or record.get("authority") != _REPORT_AUTHORITY
    ):
        return False, "report_authority_binding_invalid", ("NOASSERTION", "missing", "")
    eligible, reason, rights, _signature = _strict_artifact_rights(record)
    return eligible, reason, rights


def _asset_record_descriptor_valid(record: Mapping[str, Any]) -> bool:
    media_type = str(record.get("media_type") or "")
    return (
        record.get("logical_role") == "report_figure"
        and record.get("authority") == _REPORT_AUTHORITY
        and media_type.startswith("image/")
    )


def _decision_hashes(decisions: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    included = [item for item in decisions if item.get("decision") == "include"]
    inventory = [{
        key: item.get(key)
        for key in ("archive_path", "logical_role", "size", "sha256", "authority", "provider_id")
    } for item in included]
    rights = [{
        key: item.get(key)
        for key in (
            "archive_path", "decision", "exclusion_reason", "license",
            "license_status", "attribution", "authority", "provider_id",
        )
    } for item in decisions]
    return _sha256(_canonical_bytes(inventory)), _sha256(_canonical_bytes(rights))


def build_archive_plan(
    service: Any,
    path: str,
    revision_id: str,
    *,
    attachment_providers: Sequence[ArchiveAttachmentProvider] = (),
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> ArchivePlan:
    """Build a deterministic, path-free dry-run from one revalidated revision."""

    limits = limits.validate()
    providers_list: list[ArchiveAttachmentProvider] = []
    provider_iterator = iter(attachment_providers)
    for index in range(limits.max_providers + 1):
        try:
            provider = next(provider_iterator)
        except StopIteration:
            break
        if index >= limits.max_providers:
            raise ArchiveVerificationError("archive provider count limit exceeded")
        providers_list.append(provider)
    providers = tuple(providers_list)
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
            limits=limits,
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
    ), limits=limits)
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
    ), limits=limits)

    methods = {
        key: bundle.model.get(key)
        for key in ("methods", "methodology", "calculation_details", "method_consistency")
        if bundle.model.get(key) is not None
    }
    _append_member(members, exclusions, _core_member(
        "methods/methods.json", "frozen_methods", _canonical_json_file(methods), rights,
    ), limits=limits)
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
    ), limits=limits)
    _append_member(members, exclusions, _core_member(
        "environment/parsers.json", "parser_identity_and_version",
        _canonical_json_file({"parsers": _parser_records(bundle)}), rights,
    ), limits=limits)

    raw_assets = bundle.manifest.get("assets") or []
    if not isinstance(raw_assets, list) or len(raw_assets) > limits.max_members:
        raise StaleRevisionError("report manifest asset inventory exceeds its bound")
    assets_by_source: dict[str, Mapping[str, Any]] = {}
    included_assets: dict[str, tuple[_PreparedMember, Mapping[str, Any]]] = {}
    for record in raw_assets:
        if not isinstance(record, Mapping):
            raise StaleRevisionError("report manifest asset record is invalid")
        try:
            source_name = _safe_archive_path(
                str(record.get("path") or ""), max_length=limits.max_path_length,
            )
        except ValueError as exc:
            raise StaleRevisionError("report manifest asset path is invalid") from exc
        if source_name in assets_by_source:
            raise StaleRevisionError("report manifest asset path is duplicate")
        assets_by_source[source_name] = record
        expected_hash = str(record.get("sha256") or "").lower()
        expected_size = record.get("size")
        archive_path = f"artifacts/reports/{source_name}"
        if not _asset_record_descriptor_valid(record):
            exclusions.append(_excluded_decision(
                archive_path=archive_path, logical_role="report_figure",
                reason="asset_authority_binding_invalid", sensitive_risk="high",
                size=expected_size, sha256=expected_hash,
                authority="frozen_report_revision",
            ))
            continue
        eligible, exclusion_reason, asset_rights = _asset_rights(
            bundle, record, expected_hash,
        )
        license_id, license_status, attribution = asset_rights
        if not eligible:
            exclusions.append(_excluded_decision(
                archive_path=archive_path, logical_role="report_figure",
                reason=str(exclusion_reason or "asset_rights_binding_missing"),
                sensitive_risk="high", size=expected_size, sha256=expected_hash,
                license_id=license_id, license_status=license_status,
                attribution=attribution, authority="frozen_report_revision",
            ))
            continue
        payload = _safe_read_bound_file(
            _manifest_record_path(bundle, record),
            expected_sha256=expected_hash, expected_size=expected_size,
            max_bytes=limits.max_member_bytes,
        )
        member = _core_member(
            archive_path, "report_figure", payload, asset_rights,
            authority=_REPORT_AUTHORITY,
        )
        before = len(members)
        _append_member(members, exclusions, member, limits=limits)
        if len(members) == before + 1:
            included_assets[source_name] = (members[-1], record)

    manifest_files = bundle.manifest.get("files") or {}
    if not isinstance(manifest_files, Mapping) or len(manifest_files) > len(_REPORT_MEDIA_TYPES):
        raise StaleRevisionError("report manifest file inventory is invalid")
    for fmt, record in sorted(manifest_files.items()):
        if not isinstance(record, Mapping):
            raise StaleRevisionError("report manifest format record is invalid")
        expected_hash = str(record.get("sha256") or "").lower()
        expected_size = record.get("size")
        eligible, exclusion_reason, report_rights = _report_record_rights(
            record, fmt=str(fmt),
        )
        archive_path = f"artifacts/reports/report.{fmt}"
        if not eligible:
            exclusions.append(_excluded_decision(
                archive_path=archive_path, logical_role="rendered_report",
                reason=str(exclusion_reason or "report_rights_binding_missing"),
                sensitive_risk="high", size=expected_size, sha256=expected_hash,
                authority="frozen_report_revision",
            ))
            continue
        source = _manifest_record_path(bundle, record)
        payload = _safe_read_bound_file(
            source, expected_sha256=expected_hash, expected_size=expected_size,
            max_bytes=limits.max_member_bytes,
        )
        if fmt == "html":
            refs = record.get("relative_refs")
            if not isinstance(refs, list) or len(refs) > limits.max_members:
                raise StaleRevisionError("report HTML reference inventory is invalid")
            html_risks, _all_refs, actual_asset_refs = _html_scan(payload)
            if html_risks:
                exclusions.append(_excluded_decision(
                    archive_path=archive_path, logical_role="rendered_report",
                    reason=";".join(html_risks), sensitive_risk="high",
                    size=expected_size, sha256=expected_hash,
                    license_id=report_rights[0], license_status=report_rights[1],
                    attribution=report_rights[2], authority=_REPORT_AUTHORITY,
                ))
                continue
            declared_refs: set[str] = set()
            refs_valid = True
            for ref in refs:
                if not isinstance(ref, Mapping):
                    refs_valid = False
                    break
                ref_path = str(ref.get("path") or "")
                asset = assets_by_source.get(ref_path)
                if (
                    ref.get("logical_role") != "report_figure"
                    or asset is None
                    or ref.get("sha256") != asset.get("sha256")
                    or ref.get("size") != asset.get("size")
                    or ref_path not in included_assets
                ):
                    refs_valid = False
                    break
                declared_refs.add(ref_path)
            if not refs_valid or declared_refs != actual_asset_refs:
                exclusions.append(_excluded_decision(
                    archive_path=archive_path, logical_role="rendered_report",
                    reason="html_reference_binding_invalid", sensitive_risk="high",
                    size=expected_size, sha256=expected_hash,
                    license_id=report_rights[0], license_status=report_rights[1],
                    attribution=report_rights[2], authority=_REPORT_AUTHORITY,
                ))
                continue
        _append_member(members, exclusions, _core_member(
            archive_path, "rendered_report", payload, report_rights,
            authority=_REPORT_AUTHORITY,
        ), limits=limits)

    _provider_members(bundle, providers, members, exclusions, limits=limits)

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
    ), limits=limits)
    _append_member(members, exclusions, _core_member(
        "README.md", "archive_readme", _readme(bundle, readiness, preliminary_decisions), rights,
    ), limits=limits)
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
    ), limits=limits)

    decisions_without_manifest = [member.decision() for member in members] + exclusions
    decisions_without_manifest.sort(key=lambda item: (
        0 if item.get("decision") == "include" else 1,
        str(item.get("archive_path") or ""), str(item.get("logical_role") or ""),
    ))
    declared_inventory_sha256, declared_rights_sha256 = _decision_hashes(
        decisions_without_manifest,
    )
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
        "inventory_sha256": declared_inventory_sha256,
        "rights_sha256": declared_rights_sha256,
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
    before_manifest = len(members)
    _append_member(members, exclusions, manifest_member, limits=limits)
    if len(members) != before_manifest + 1:
        raise ArchiveVerificationError("archive manifest could not fit within archive limits")

    all_member_bytes = {
        member.archive_path: member.data for member in sorted(members, key=lambda item: item.archive_path)
    }
    sums = "".join(
        f"{_sha256(data)}  {name}\n" for name, data in sorted(all_member_bytes.items())
    ).encode("ascii")
    sums_member = _core_member("SHA256SUMS", "checksum_index", sums, rights)
    before_sums = len(members)
    _append_member(members, exclusions, sums_member, limits=limits)
    if len(members) != before_sums + 1:
        raise ArchiveVerificationError("checksum index could not fit within archive limits")
    all_member_bytes = {
        member.archive_path: member.data for member in sorted(members, key=lambda item: item.archive_path)
    }

    final_decisions = [member.decision() for member in members] + exclusions
    final_decisions.sort(key=lambda item: (
        0 if item.get("decision") == "include" else 1,
        str(item.get("archive_path") or ""), str(item.get("logical_role") or ""),
    ))
    inventory_sha256, rights_sha256 = _decision_hashes(final_decisions)
    semantic = _semantic_plan_payload(bundle, readiness, final_decisions)
    plan_sha256 = _sha256(_canonical_bytes(semantic))
    safe_revision = re.sub(r"[^A-Za-z0-9._-]", "-", bundle.revision_id)
    archive_name = (
        f"vcs-archive-{safe_revision}-{bundle.entry['manifest_sha256'][:12]}-"
        f"{plan_sha256[:12]}-v{ARCHIVE_VERSION}.zip"
    )
    payload = _archive_bytes(all_member_bytes, limits=limits)
    verification = verify_archive_bytes(payload, limits=limits)
    if verification["ok"] is not True:
        raise ArchiveVerificationError(str(verification.get("error") or "archive verification failed"))
    return ArchivePlan(
        project_id=bundle.project_id,
        revision_id=bundle.revision_id,
        report_id=str(bundle.entry["report_id"]),
        source_manifest_sha256=str(bundle.entry["manifest_sha256"]),
        plan_sha256=plan_sha256,
        inventory_sha256=inventory_sha256,
        rights_sha256=rights_sha256,
        archive_name=archive_name,
        archive_sha256=_sha256(payload),
        archive_size=len(payload),
        readiness=readiness,
        decisions=tuple(copy.deepcopy(final_decisions)),
        providers=providers,
        _archive_bytes=payload,
    )


def verify_archive_bytes(
    data: bytes, *, expected_sha256: str | None = None,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> dict[str, Any]:
    """Verify deterministic ZIP metadata, manifest records, and SHA256SUMS."""

    payload = b""
    supplied_size: int | None = None
    try:
        limits = limits.validate()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ArchiveVerificationError("archive payload type is invalid")
        supplied_size = len(data)
        if len(data) > limits.max_archive_bytes:
            raise ArchiveVerificationError("final archive byte limit exceeded")
        payload = bytes(data)
        digest = _sha256(payload)
        if expected_sha256 is not None and digest != str(expected_sha256).lower():
            raise ArchiveVerificationError("archive SHA-256 mismatch")
        with zipfile.ZipFile(_ReadOnlyBytes(payload), "r") as archive:
            infos = archive.filelist
            if len(infos) > limits.max_members:
                raise ArchiveVerificationError("archive member count limit exceeded")
            names = [info.filename for info in infos]
            if names != sorted(names) or len(names) != len(set(names)):
                raise ArchiveVerificationError("archive names are duplicate or unsorted")
            folded = [name.casefold() for name in names]
            if len(folded) != len(set(folded)):
                raise ArchiveVerificationError("archive names are not portable")
            total_size = 0
            member_bytes: dict[str, bytes] = {}
            for info in infos:
                if _safe_archive_path(
                    info.filename, max_length=limits.max_path_length,
                ) != info.filename:
                    raise ArchiveVerificationError("archive contains an unsafe member path")
                mode = (info.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode) or mode not in {0, stat.S_IFREG}:
                    raise ArchiveVerificationError("archive contains a non-regular member")
                if info.date_time != (1980, 1, 1, 0, 0, 0):
                    raise ArchiveVerificationError("archive member timestamp is not deterministic")
                if info.compress_type != zipfile.ZIP_STORED:
                    raise ArchiveVerificationError("archive compression is not deterministic")
                if info.file_size > limits.max_member_bytes:
                    raise ArchiveVerificationError("archive member byte limit exceeded")
                total_size += info.file_size
                if total_size > limits.max_total_member_bytes:
                    raise ArchiveVerificationError("archive total byte limit exceeded")
                if info.compress_size != info.file_size:
                    raise ArchiveVerificationError("stored archive member size is inconsistent")
                with archive.open(info, "r") as member_stream:
                    member_bytes[info.filename] = _read_stream_limited(
                        member_stream, limits.max_member_bytes,
                    )
            required = {"metadata/archive-manifest.json", "SHA256SUMS", "README.md", "CITATION.cff"}
            if not required.issubset(names):
                raise ArchiveVerificationError("archive required members are missing")
            manifest = json.loads(member_bytes["metadata/archive-manifest.json"].decode("utf-8"))
            if manifest.get("schema") != ARCHIVE_SCHEMA:
                raise ArchiveVerificationError("archive manifest schema is invalid")
            declared = manifest.get("files")
            if not isinstance(declared, list) or len(declared) > limits.max_members:
                raise ArchiveVerificationError("archive manifest file inventory is invalid")
            declared_by_name: dict[str, Mapping[str, Any]] = {}
            for record in declared:
                if not isinstance(record, Mapping):
                    raise ArchiveVerificationError("archive manifest file record is invalid")
                name = str((record or {}).get("name") or "")
                if name in {"metadata/archive-manifest.json", "SHA256SUMS"}:
                    raise ArchiveVerificationError("archive fixed member is self-declared")
                if name in declared_by_name:
                    raise ArchiveVerificationError("archive manifest member is duplicate")
                if name not in member_bytes:
                    raise ArchiveVerificationError("archive manifest member is missing")
                declared_by_name[name] = record
                member = member_bytes[name]
                if len(member) != record.get("size") or _sha256(member) != record.get("sha256"):
                    raise ArchiveVerificationError("archive manifest member hash mismatch")
                if not isinstance(record.get("logical_role"), str) or not record.get("logical_role"):
                    raise ArchiveVerificationError("archive manifest logical role is invalid")
                if record.get("license_status") not in {"declared", "missing"}:
                    raise ArchiveVerificationError("archive manifest license status is invalid")
                if not isinstance(record.get("license"), str) or not isinstance(
                    record.get("attribution"), str,
                ):
                    raise ArchiveVerificationError("archive manifest rights are invalid")
            actual_inventory = set(names)
            if actual_inventory != set(declared_by_name) | {
                "metadata/archive-manifest.json", "SHA256SUMS",
            }:
                raise ArchiveVerificationError("archive actual inventory differs from its manifest")
            decisions = manifest.get("decisions")
            if not isinstance(decisions, list) or len(decisions) > limits.max_members * 4:
                raise ArchiveVerificationError("archive decision inventory is invalid")
            included_decisions: dict[str, Mapping[str, Any]] = {}
            for decision in decisions:
                if not isinstance(decision, Mapping):
                    raise ArchiveVerificationError("archive decision record is invalid")
                if decision.get("decision") == "include":
                    name = str(decision.get("archive_path") or "")
                    if name in included_decisions:
                        raise ArchiveVerificationError("archive include decision is duplicate")
                    included_decisions[name] = decision
            if set(included_decisions) != set(declared_by_name):
                raise ArchiveVerificationError("archive decisions do not match file inventory")
            crosswalk = {
                "name": "archive_path", "logical_role": "logical_role",
                "size": "size", "sha256": "sha256", "license": "license",
                "license_status": "license_status", "attribution": "attribution",
                "authority": "authority", "provider_id": "provider_id",
            }
            for name, record in declared_by_name.items():
                decision = included_decisions[name]
                if any(record.get(left) != decision.get(right) for left, right in crosswalk.items()):
                    raise ArchiveVerificationError(
                        "archive file, decision, rights, hash, or size binding differs"
                    )
            inventory_sha256, rights_sha256 = _decision_hashes(decisions)
            if (
                manifest.get("inventory_sha256") != inventory_sha256
                or manifest.get("rights_sha256") != rights_sha256
            ):
                raise ArchiveVerificationError("archive inventory or rights digest mismatch")
            sums_text = member_bytes["SHA256SUMS"].decode("ascii")
            sum_names: list[str] = []
            for line in sums_text.splitlines():
                if "  " not in line:
                    raise ArchiveVerificationError("SHA256SUMS line is invalid")
                expected, name = line.split("  ", 1)
                if not _HASH.fullmatch(expected) or name == "SHA256SUMS" or name not in names:
                    raise ArchiveVerificationError("SHA256SUMS member is invalid")
                if _sha256(member_bytes[name]) != expected:
                    raise ArchiveVerificationError("SHA256SUMS member hash mismatch")
                sum_names.append(name)
            if sum_names != sorted(name for name in names if name != "SHA256SUMS"):
                raise ArchiveVerificationError("SHA256SUMS coverage is incomplete")
            for name in names:
                risks = _payload_risks(name, member_bytes[name], limits=limits)
                if risks:
                    raise ArchiveVerificationError("archive member violates public safety policy")
                local_refs: set[str] = set()
                if name.lower().endswith((".html", ".htm")):
                    _risks, refs, _asset_refs = _html_scan(member_bytes[name])
                    local_refs.update(refs)
                elif name.lower().endswith(".css"):
                    _risks, refs = _css_scan(member_bytes[name])
                    local_refs.update(refs)
                for ref in local_refs:
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), ref))
                    if (
                        resolved.startswith("../") or resolved == ".."
                        or resolved not in member_bytes
                    ):
                        raise ArchiveVerificationError(
                            "archive HTML/CSS contains an unresolved local reference"
                        )
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "status": "verified",
            "sha256": digest,
            "size": supplied_size,
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
            "sha256": _sha256(payload) if payload else None,
            "size": supplied_size,
            "member_count": None,
            "checksums": "fail",
            "determinism_metadata": "fail",
            "error": str(_public_safe_value(str(exc))),
        }


def verify_archive_file(
    path: str | os.PathLike[str], *, expected_sha256: str | None = None,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> dict[str, Any]:
    candidate = os.path.abspath(os.fspath(path))
    try:
        before = os.lstat(candidate)
    except Exception as exc:  # noqa: BLE001
        return {
            "schema": RESULT_SCHEMA, "ok": False, "status": "tampered_or_invalid",
            "sha256": None, "size": None, "member_count": None,
            "checksums": "fail", "determinism_metadata": "fail",
            "error": str(_public_safe_value(str(exc))),
        }
    if _is_symlink_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        return {
            "schema": RESULT_SCHEMA, "ok": False, "status": "tampered_or_invalid",
            "sha256": None, "size": None, "member_count": None,
            "checksums": "fail", "determinism_metadata": "fail",
            "error": "archive is not a regular non-symlink file",
        }
    try:
        payload = _safe_read_bound_file(
            candidate, expected_sha256=None, expected_size=before.st_size,
            max_bytes=limits.max_archive_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "schema": RESULT_SCHEMA, "ok": False, "status": "tampered_or_invalid",
            "sha256": None, "size": None, "member_count": None,
            "checksums": "fail", "determinism_metadata": "fail",
            "error": str(_public_safe_value(str(exc))),
        }
    return verify_archive_bytes(
        payload, expected_sha256=expected_sha256, limits=limits,
    )


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


def _trusted_read_regular(
    directory: TrustedDirectoryHandle, name: str,
    *,
    expected_entity: tuple[int, int, int, int] | None = None,
    max_bytes: int = DEFAULT_ARCHIVE_LIMITS.max_archive_bytes,
) -> bytes:
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
        identity = _entity_identity(opened)
        if (
            _is_symlink_or_reparse(named_after)
            or not stat.S_ISREG(named_after.st_mode)
            or (expected_entity is not None and identity != expected_entity)
            or _entity_identity(named_before) != identity
            or _entity_identity(named_after) != identity
        ):
            raise ArchiveVerificationError("archive stage changed during no-follow open")
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            block = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > max_bytes or os.read(descriptor, 1):
            raise ArchiveVerificationError("archive stage exceeds final archive byte limit")
        closed = os.fstat(descriptor)
        if (
            _entity_identity(closed) != identity
            or closed.st_size != opened.st_size
            or getattr(closed, "st_mtime_ns", None) != getattr(opened, "st_mtime_ns", None)
        ):
            raise ArchiveVerificationError("archive stage changed during read")
        directory.verify_path()
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _trusted_verify_archive(
    directory: TrustedDirectoryHandle, name: str, *, expected_sha256: str,
    expected_entity: tuple[int, int, int, int] | None = None,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> dict[str, Any]:
    return verify_archive_bytes(
        _trusted_read_regular(
            directory, name, expected_entity=expected_entity,
            max_bytes=limits.max_archive_bytes,
        ),
        expected_sha256=expected_sha256, limits=limits,
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
    expected_source_manifest_sha256: str | None = None,
    expected_archive_sha256: str | None = None,
    expected_inventory_sha256: str | None = None,
    expected_rights_sha256: str | None = None,
    expected_destination_identity_sha256: str | None = None,
    attachment_providers: Sequence[ArchiveAttachmentProvider] = (),
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> dict[str, Any]:
    """Confirm a dry-run and atomically publish one local, non-overwriting ZIP."""

    expected_plan = str(expected_plan_sha256 or "").lower()
    if not _HASH.fullmatch(expected_plan):
        raise ValueError("confirmed archive plan hash is invalid")
    expected_hashes = {
        "source manifest": expected_source_manifest_sha256,
        "archive": expected_archive_sha256,
        "inventory": expected_inventory_sha256,
        "rights": expected_rights_sha256,
        "destination": expected_destination_identity_sha256,
    }
    for label, value in expected_hashes.items():
        if value is not None and not _HASH.fullmatch(str(value).lower()):
            raise ValueError(f"confirmed archive {label} hash is invalid")
    limits = limits.validate()
    selection = (
        destination_dir if isinstance(destination_dir, TrustedDirectorySelection)
        else capture_trusted_directory(destination_dir)
    )
    if expected_destination_identity_sha256 is not None and not secrets.compare_digest(
        destination_identity_sha256(selection),
        str(expected_destination_identity_sha256).lower(),
    ):
        raise StaleRevisionError("archive destination identity changed")
    with open_trusted_directory(selection) as destination:
        plan = build_archive_plan(
            service, path, revision_id, attachment_providers=attachment_providers,
            limits=limits,
        )
        if plan.plan_sha256 != expected_plan:
            raise StaleRevisionError("archive dry-run changed; review and confirm a new plan")
        confirmed_values = {
            "source manifest": (expected_source_manifest_sha256, plan.source_manifest_sha256),
            "archive": (expected_archive_sha256, plan.archive_sha256),
            "inventory": (expected_inventory_sha256, plan.inventory_sha256),
            "rights": (expected_rights_sha256, plan.rights_sha256),
        }
        for label, (expected_value, actual_value) in confirmed_values.items():
            if expected_value is not None and not secrets.compare_digest(
                str(expected_value).lower(), actual_value,
            ):
                raise StaleRevisionError(f"archive confirmed {label} identity changed")
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
            staged_entity: tuple[int, int, int, int] | None = None
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    view = memoryview(plan._archive_bytes)
                    for offset in range(0, len(view), 1024 * 1024):
                        handle.write(view[offset:offset + 1024 * 1024])
                    handle.flush()
                    os.fsync(handle.fileno())
                    staged_entity = _entity_identity(os.fstat(handle.fileno()))
                staged = _trusted_verify_archive(
                    destination, temp_name, expected_sha256=plan.archive_sha256,
                    expected_entity=staged_entity, limits=limits,
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
                    expected_entity=staged_entity, limits=limits,
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
        "inventory_sha256": plan.inventory_sha256,
        "rights_sha256": plan.rights_sha256,
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


@dataclass(frozen=True)
class ArchiveConfirmationReceipt:
    project_id: str
    report_id: str
    revision_id: str
    source_manifest_sha256: str
    plan_sha256: str
    rights_sha256: str
    inventory_sha256: str
    archive_name: str
    archive_sha256: str
    archive_size: int

    @classmethod
    def from_plan(cls, plan: ArchivePlan) -> "ArchiveConfirmationReceipt":
        return cls(
            project_id=plan.project_id,
            report_id=plan.report_id,
            revision_id=plan.revision_id,
            source_manifest_sha256=plan.source_manifest_sha256,
            plan_sha256=plan.plan_sha256,
            rights_sha256=plan.rights_sha256,
            inventory_sha256=plan.inventory_sha256,
            archive_name=plan.archive_name,
            archive_sha256=plan.archive_sha256,
            archive_size=plan.archive_size,
        )

    def identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "report_id": self.report_id,
            "revision_id": self.revision_id,
            "source_manifest_sha256": self.source_manifest_sha256,
            "plan_sha256": self.plan_sha256,
            "rights_sha256": self.rights_sha256,
            "inventory_sha256": self.inventory_sha256,
            "archive_name": self.archive_name,
            "archive_sha256": self.archive_sha256,
            "archive_size": self.archive_size,
        }


@dataclass(frozen=True)
class ArchiveConfirmationClaim:
    claim_id: str
    idempotency_key: str
    envelope_sha256: str
    receipt: ArchiveConfirmationReceipt
    plan: ArchivePlan = field(repr=False, compare=False)
    replayed: bool = False
    replay_result: Any = field(default=None, repr=False, compare=False)


def destination_identity_sha256(
    selection: TrustedDirectorySelection | Sequence[Any],
) -> str:
    """Hash a physical directory identity without exposing its local path."""

    identity = selection.identity if isinstance(selection, TrustedDirectorySelection) else selection
    if not isinstance(identity, (tuple, list)) or not identity:
        raise ValueError("destination identity is invalid")
    if len(identity) == 3 and isinstance(identity[0], str):
        raw_chain: Sequence[Any] = (identity,)
    else:
        raw_chain = identity
    if len(raw_chain) > 256:
        raise ValueError("destination identity is invalid")
    chain: list[dict[str, Any]] = []
    for item in raw_chain:
        if (
            not isinstance(item, (tuple, list)) or len(item) != 3
            or item[0] not in {"posix", "windows"}
            or isinstance(item[1], bool) or not isinstance(item[1], int)
        ):
            raise ValueError("destination identity is invalid")
        entity_id = item[2]
        if item[0] == "posix":
            if isinstance(entity_id, bool) or not isinstance(entity_id, int):
                raise ValueError("destination identity is invalid")
        elif not (
            (isinstance(entity_id, int) and not isinstance(entity_id, bool))
            or (
                isinstance(entity_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", entity_id) is not None
            )
        ):
            raise ValueError("destination identity is invalid")
        chain.append({
            "kind": item[0], "volume_or_device": item[1],
            "file_id_or_inode": entity_id,
        })
    return _sha256(_canonical_bytes({"ancestor_chain": chain}))


class ArchiveConfirmations:
    """TTL-bound two-phase confirmations with idempotent CAS completion."""

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
        self._previews: dict[str, tuple[float, ArchivePlan]] = {}
        self._challenges: dict[str, tuple[float, ArchivePlan, dict[str, Any]]] = {}
        self._operations: dict[str, dict[str, Any]] = {}

    def _prune(self, now: float) -> None:
        for token in [
            token for token, (created, _plan) in self._previews.items()
            if now - created >= self._ttl_seconds
        ]:
            self._previews.pop(token, None)
        for token in [
            token for token, (created, _plan, _challenge) in self._challenges.items()
            if now - created >= self._ttl_seconds
        ]:
            self._challenges.pop(token, None)
        for key in [
            key for key, record in self._operations.items()
            if now - float(record["created_at"]) >= self._ttl_seconds
        ]:
            self._operations.pop(key, None)

    def _make_space(self) -> None:
        while (
            len(self._previews) + len(self._challenges) + len(self._operations)
            >= self._max_items
        ):
            candidates = [
                (created, "preview", token)
                for token, (created, _plan) in self._previews.items()
            ] + [
                (created, "challenge", token)
                for token, (created, _plan, _challenge) in self._challenges.items()
            ] + [
                (float(record["created_at"]), "operation", key)
                for key, record in self._operations.items()
            ]
            if not candidates:
                break
            _created, kind, key = min(candidates)
            if kind == "preview":
                self._previews.pop(key, None)
            elif kind == "challenge":
                self._challenges.pop(key, None)
            else:
                self._operations.pop(key, None)

    def register_plan(self, plan: ArchivePlan) -> dict[str, Any]:
        if not isinstance(plan, ArchivePlan):
            raise TypeError("archive plan is required")
        receipt = ArchiveConfirmationReceipt.from_plan(plan)
        with self._lock:
            now = float(self._clock())
            self._prune(now)
            self._make_space()
            token = "archive-preview." + secrets.token_urlsafe(24)
            self._previews[token] = (now, plan)
        public = _public_safe_value({
            "schema": CONFIRMATION_SCHEMA,
            "phase": "preview",
            **receipt.identity(),
            "created_at": now,
            "expires_at": now + self._ttl_seconds,
            "ttl_seconds": self._ttl_seconds,
        })
        public["preview_token"] = token
        return public

    def bind_destination(
        self, preview_token: str, *, destination_identity_sha256: str,
    ) -> dict[str, Any]:
        destination_digest = str(destination_identity_sha256 or "").lower()
        if not _HASH.fullmatch(destination_digest):
            raise ValueError("archive destination identity digest is invalid")
        with self._lock:
            now = float(self._clock())
            self._prune(now)
            selected = self._previews.pop(str(preview_token or ""), None)
            if selected is None:
                raise ValueError("archive preview is invalid or expired")
            plan = selected[1]
            receipt = ArchiveConfirmationReceipt.from_plan(plan)
            token = "archive-confirm." + secrets.token_urlsafe(24)
            challenge = {
                "schema": CONFIRMATION_SCHEMA,
                "phase": "confirmation",
                "confirmation_token": token,
                **receipt.identity(),
                "destination_identity_sha256": destination_digest,
                "nonce": secrets.token_urlsafe(24),
                "created_at": now,
                "expires_at": now + self._ttl_seconds,
                "ttl_seconds": self._ttl_seconds,
            }
            self._challenges[token] = (now, plan, challenge)
        public = _public_safe_value({
            key: value for key, value in challenge.items()
            if key != "confirmation_token"
        })
        public["confirmation_token"] = token
        return public

    @staticmethod
    def _challenge_keys() -> frozenset[str]:
        return frozenset({
            "schema", "phase", "confirmation_token", "project_id", "report_id",
            "revision_id", "source_manifest_sha256", "plan_sha256", "rights_sha256",
            "inventory_sha256", "archive_name", "archive_sha256", "archive_size",
            "destination_identity_sha256", "nonce", "created_at", "expires_at",
            "ttl_seconds",
        })

    def _validated_challenge_locked(
        self, envelope: Mapping[str, Any], *, now: float,
    ) -> tuple[str, ArchivePlan, dict[str, Any], str]:
        if not isinstance(envelope, Mapping) or set(envelope) != self._challenge_keys():
            raise ValueError("archive confirmation envelope shape is invalid")
        token = str(envelope.get("confirmation_token") or "")
        selected = self._challenges.get(token)
        supplied_hash = _sha256(_canonical_bytes(dict(envelope)))
        if selected is None:
            raise ValueError("archive confirmation is invalid, expired, or already used")
        stored = selected[2]
        stored_hash = _sha256(_canonical_bytes(stored))
        if not secrets.compare_digest(supplied_hash, stored_hash):
            raise ValueError("archive confirmation envelope binding mismatch")
        if now >= float(stored["expires_at"]):
            self._challenges.pop(token, None)
            raise ValueError("archive confirmation is invalid, expired, or already used")
        return token, selected[1], stored, supplied_hash

    def peek_confirmed(
        self, envelope: Mapping[str, Any], *, confirmed: bool,
        idempotency_key: str,
    ) -> ArchiveConfirmationReceipt:
        if confirmed is not True:
            raise ValueError("explicit archive confirmation is required")
        if not _TOKEN.fullmatch(str(idempotency_key or "")):
            raise ValueError("archive idempotency key is invalid")
        with self._lock:
            now = float(self._clock())
            self._prune(now)
            _token, plan, _stored, _payload_hash = self._validated_challenge_locked(
                envelope, now=now,
            )
            return ArchiveConfirmationReceipt.from_plan(plan)

    def claim_confirmed(
        self, envelope: Mapping[str, Any], *, confirmed: bool,
        idempotency_key: str,
    ) -> ArchiveConfirmationClaim:
        if confirmed is not True:
            raise ValueError("explicit archive confirmation is required")
        operation_key = str(idempotency_key or "").strip()
        if not _TOKEN.fullmatch(operation_key):
            raise ValueError("archive idempotency key is invalid")
        envelope_hash = _sha256(_canonical_bytes(dict(envelope))) if isinstance(envelope, Mapping) else ""
        with self._lock:
            now = float(self._clock())
            self._prune(now)
            replay = self._operations.get(operation_key)
            if replay is not None:
                if not secrets.compare_digest(str(replay["envelope_sha256"]), envelope_hash):
                    raise ValueError("archive idempotency key was reused for different input")
                if replay["state"] == "pending":
                    raise ValueError("archive confirmation operation is already in progress")
                return ArchiveConfirmationClaim(
                    claim_id=str(replay["claim_id"]), idempotency_key=operation_key,
                    envelope_sha256=envelope_hash, receipt=replay["receipt"],
                    plan=replay["plan"], replayed=True,
                    replay_result=copy.deepcopy(replay["result"]),
                )
            token, plan, _stored, validated_hash = self._validated_challenge_locked(
                envelope, now=now,
            )
            self._challenges.pop(token, None)
            claim_id = "archive-claim." + secrets.token_urlsafe(24)
            receipt = ArchiveConfirmationReceipt.from_plan(plan)
            self._operations[operation_key] = {
                "state": "pending", "claim_id": claim_id,
                "envelope_sha256": validated_hash, "created_at": now,
                "receipt": receipt, "plan": plan, "result": None,
            }
            return ArchiveConfirmationClaim(
                claim_id=claim_id, idempotency_key=operation_key,
                envelope_sha256=validated_hash, receipt=receipt, plan=plan,
            )

    def complete_confirmed(
        self, claim: ArchiveConfirmationClaim, result: Any,
    ) -> Any:
        if not isinstance(claim, ArchiveConfirmationClaim) or claim.replayed:
            raise ValueError("archive confirmation claim is invalid")
        with self._lock:
            record = self._operations.get(claim.idempotency_key)
            if (
                record is None or record.get("state") != "pending"
                or not secrets.compare_digest(str(record.get("claim_id")), claim.claim_id)
                or not secrets.compare_digest(
                    str(record.get("envelope_sha256")), claim.envelope_sha256,
                )
            ):
                raise ValueError("archive confirmation claim CAS failed")
            frozen_result = copy.deepcopy(result)
            frozen_result = _public_safe_value(frozen_result)
            record["state"] = "complete"
            record["result"] = frozen_result
            return copy.deepcopy(frozen_result)

__all__ = [
    "ARCHIVE_SCHEMA", "ATTACHMENT_SCHEMA", "CONFIRMATION_SCHEMA", "PLAN_SCHEMA",
    "RESULT_SCHEMA", "ArchiveAttachment", "ArchiveAttachmentProvider",
    "ArchiveConfirmationClaim", "ArchiveConfirmationReceipt", "ArchiveConfirmations",
    "ArchiveLimits", "ArchivePlan", "ArchiveVerificationError", "build_archive_plan",
    "destination_identity_sha256", "export_archive", "verify_archive_bytes",
    "verify_archive_file",
]
