"""Fail-closed read-only gateway for external scientific reference data.

The browser-facing protocol is intentionally narrow: callers select a
server-owned provider and bounded filters.  Endpoints, response fields,
GraphQL documents, timeouts, byte limits, credentials, cache paths and import
locations remain server-owned.  Returned energies and structures are
``external_reference`` evidence only and cannot satisfy local validation or
publication contracts.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets as token_secrets
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence, runtime_checkable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


CATALOG_SCHEMA = "vcstudio.external-reference-catalog/v1"
SEARCH_SCHEMA = "vcstudio.external-reference-search/v1"
RESULT_SCHEMA = "vcstudio.external-reference-result/v1"
COMPARE_SCHEMA = "vcstudio.external-reference-comparison/v1"
CANDIDATE_SCHEMA = "vcstudio.external-reference-candidate/v1"
PROVIDER_SCHEMA = "vcstudio.external-reference-provider/v1"
NETWORK_SCHEMA = "vcstudio.external-reference-network/v1"
CREDENTIAL_SCHEMA = "vcstudio.external-reference-credential/v1"
IMPORT_PREVIEW_SCHEMA = "vcstudio.external-reference-import-preview/v1"

DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGE_SIZE = 25
MAX_PAGE_NUMBER = 4
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_TOKEN_TTL_SECONDS = 15 * 60
DEFAULT_TOKEN_CAPACITY_BYTES = 16 * 1024 * 1024
DEFAULT_TOKEN_RECORDS = 64
DEFAULT_CACHE_CAPACITY_BYTES = 50 * 1024 * 1024
DEFAULT_CACHE_ENTRIES = 128
APPLICATION_USER_AGENT = (
    "VASP-Catalyst-Studio/4 ExternalReferenceGateway/1 "
    "(+https://github.com/2067169120-png/vasp-catalyst-studio-v3)")

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_TOKEN_RE = re.compile(r"^external-reference\.[A-Za-z0-9_-]{32,96}$")
_IMPORT_PREVIEW_TOKEN_RE = re.compile(
    r"^external-import-preview\.[A-Za-z0-9_-]{32,96}$")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_PROJECT_ID_RE = re.compile(
    r"^(?:project-[a-f0-9]{32}|registry-[a-f0-9]{24})$")
_MATERIAL_ID_RE = re.compile(r"^(?:mp|mvc)-[0-9]{1,12}$")
_ELEMENT_RE = re.compile(r"^[A-Z][a-z]?$", re.ASCII)
_FORMULA_RE = re.compile(r"^[A-Za-z0-9*().+\-]{1,64}$", re.ASCII)
_QUERY_TEXT_RE = re.compile(r"^[A-Za-z0-9*().,+\-\[\] _]{1,96}$", re.ASCII)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LOCAL_PATH_RE = re.compile(
    r"(?i)(?:^|[\s'\"])(?:[A-Z]:[\\/]|\\\\|file:|~[\\/]|/(?:home|users?|tmp|var)/)")
_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bBearer\s+\S+|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+)")
_FORBIDDEN_QUERY_KEYS = frozenset({
    "url", "uri", "endpoint", "host", "hostname", "port", "path",
    "query", "graphql", "document", "fields", "response_fields",
    "headers", "authorization", "api_key", "token", "timeout",
    "max_bytes", "cache_path", "destination", "out_dir",
})

_CREDENTIAL_PATTERNS = {
    # Current Materials Project keys are fixed-width hexadecimal values.  A
    # wider generic "non-control text" contract would make response-echo
    # detection ambiguous after JSON escaping.
    "materials_project": re.compile(r"^[A-Fa-f0-9]{32}$", re.ASCII),
    # Catalysis-Hub keys are opaque URL-safe bearer identifiers.  Keep the
    # accepted alphabet deliberately narrower than HTTP/JSON syntax.
    "catalysis_hub": re.compile(r"^[A-Za-z0-9_-]{32,128}$", re.ASCII),
}

_ELEMENTS = frozenset(
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu "
    "Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs "
    "Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl "
    "Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh "
    "Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split())


class ExternalReferenceError(ValueError):
    """One untrusted request violates the public gateway contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


class TransportFailure(RuntimeError):
    """The bounded HTTP transport could not return one response."""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class ProviderSpec:
    """One immutable server-owned external service identity."""

    provider_id: str
    protocol: str
    endpoint: str
    endpoint_identity: str
    adapter_version: str
    label_zh: str
    label_en: str
    description_zh: str
    description_en: str
    requires_api_key: bool
    query_fields: tuple[str, ...]
    capabilities: tuple[str, ...]
    license_id: str
    license_url: str
    attribution: str
    citation_url: str

    def public(self, *, network_enabled: bool, credential_available: bool) -> dict[str, Any]:
        if not network_enabled:
            status = "network_disabled"
        elif self.requires_api_key and not credential_available:
            status = "credential_required"
        else:
            status = "available"
        return {
            "schema": PROVIDER_SCHEMA,
            "id": self.provider_id,
            "protocol": self.protocol,
            "endpoint_identity": self.endpoint_identity,
            "adapter_version": self.adapter_version,
            "label_zh": self.label_zh,
            "label_en": self.label_en,
            "description_zh": self.description_zh,
            "description_en": self.description_en,
            "requires_api_key": self.requires_api_key,
            "credential_available": credential_available,
            "network_enabled": network_enabled,
            "status": status,
            "query_fields": list(self.query_fields),
            "capabilities": list(self.capabilities),
            "license": {"id": self.license_id, "url": self.license_url},
            "attribution": self.attribution,
            "citation_url": self.citation_url,
        }


def _validate_endpoint(endpoint: str) -> None:
    parsed = urllib_parse.urlsplit(str(endpoint))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("provider endpoints must be fixed credential-free HTTPS URLs")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("local provider endpoints are forbidden")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("private, loopback and link-local provider endpoints are forbidden")
    decoded_path = urllib_parse.unquote(parsed.path)
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise ValueError("provider endpoint traversal is forbidden")


def _default_provider_specs() -> tuple[ProviderSpec, ...]:
    return (
        ProviderSpec(
            provider_id="materials_project",
            protocol="materials-project-rest",
            endpoint="https://api.materialsproject.org/materials/summary/",
            endpoint_identity="materials-project:materials-summary",
            adapter_version="mp-summary-v1",
            label_zh="Materials Project",
            label_en="Materials Project",
            description_zh="结构与材料性质摘要；需要个人 API key。",
            description_en="Structure and material-property summaries; personal API key required.",
            requires_api_key=True,
            query_fields=("formula", "chemsys", "elements", "material_ids", "page", "limit"),
            capabilities=("structure", "material_properties", "method_metadata"),
            license_id="provider-terms",
            license_url="https://materialsproject.org/about/terms",
            attribution="Materials Project; cite the database version and property-specific methods.",
            citation_url="https://materialsproject.org/about/cite",
        ),
        ProviderSpec(
            provider_id="optimade",
            protocol="optimade",
            endpoint="https://optimade.materialsproject.org/v1/structures",
            endpoint_identity="optimade:materials-project:structures-v1",
            adapter_version="optimade-v1.3-narrow",
            label_zh="OPTIMADE",
            label_en="OPTIMADE",
            description_zh="服务端固定 endpoint 的通用 OPTIMADE 结构查询。",
            description_en="Generic OPTIMADE structure query against a server-pinned endpoint.",
            requires_api_key=False,
            query_fields=("formula", "elements", "nelements_min", "nelements_max", "page", "limit"),
            capabilities=("structure", "provider_metadata"),
            license_id="provider-terms",
            license_url="https://materialsproject.org/about/terms",
            attribution="Materials Project data served through its OPTIMADE implementation; cite the provider database version and methods.",
            citation_url="https://materialsproject.org/about/cite",
        ),
        ProviderSpec(
            provider_id="catalysis_hub",
            protocol="catalysis-hub-graphql",
            endpoint="https://api.catalysis-hub.org/graphql",
            endpoint_identity="catalysis-hub:graphql:reactions",
            adapter_version="catalysis-hub-graphql-v1",
            label_zh="Catalysis-Hub",
            label_en="Catalysis-Hub",
            description_zh="催化反应能、能垒、表面与文献元数据。",
            description_en="Catalytic reaction energies, barriers, surfaces and publication metadata.",
            requires_api_key=True,
            query_fields=("reactants", "products", "chemical_composition", "surface", "facet", "page", "limit"),
            capabilities=("reaction_energy", "activation_energy", "method_metadata", "publication"),
            license_id="CC-BY-4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            attribution="Catalysis-Hub.org contributors; content is attributed under CC BY 4.0 unless noted otherwise.",
            citation_url="https://www.catalysis-hub.org/",
        ),
    )


class ProviderRegistry:
    """Immutable lookup of server-approved providers and endpoint identities."""

    def __init__(self, providers: Sequence[ProviderSpec] | None = None):
        records: dict[str, ProviderSpec] = {}
        for spec in tuple(providers or _default_provider_specs()):
            if not isinstance(spec, ProviderSpec):
                raise TypeError("provider registry accepts ProviderSpec records only")
            if not _PROVIDER_ID_RE.fullmatch(spec.provider_id):
                raise ValueError("invalid provider id")
            if spec.provider_id in records:
                raise ValueError("duplicate provider id")
            if spec.protocol not in {
                    "materials-project-rest", "optimade",
                    "catalysis-hub-graphql"}:
                raise ValueError("unsupported provider protocol")
            _validate_endpoint(spec.endpoint)
            records[spec.provider_id] = spec
        self._records = records

    def get(self, provider_id: Any) -> ProviderSpec:
        identifier = str(provider_id or "").strip().lower()
        if not _PROVIDER_ID_RE.fullmatch(identifier) or identifier not in self._records:
            raise ExternalReferenceError("unknown_provider", "provider is not registered")
        return self._records[identifier]

    def providers(self) -> tuple[ProviderSpec, ...]:
        return tuple(self._records[key] for key in sorted(self._records))


def _bounded_int(value: Any, *, field_name: str, minimum: int, maximum: int,
                 default: int | None = None) -> int:
    if value is None and default is not None:
        return int(default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalReferenceError("invalid_query", f"{field_name} must be an integer")
    number = value
    if number < minimum or number > maximum:
        raise ExternalReferenceError(
            "invalid_query", f"{field_name} is outside the allowed range")
    return number


def _bounded_text(value: Any, *, field_name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise ExternalReferenceError(
            "invalid_query", f"{field_name} must be a string")
    text = value.strip()
    if not text or _CONTROL_RE.search(text) or not pattern.fullmatch(text):
        raise ExternalReferenceError(
            "invalid_query", f"{field_name} contains unsupported characters")
    return text


def _element_list(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExternalReferenceError("invalid_query", "elements must be an array")
    elements = []
    for raw in value:
        if not isinstance(raw, str):
            raise ExternalReferenceError(
                "invalid_query", "elements contains an invalid symbol")
        element = raw.strip()
        if not _ELEMENT_RE.fullmatch(element) or element not in _ELEMENTS:
            raise ExternalReferenceError("invalid_query", "elements contains an invalid symbol")
        if element not in elements:
            elements.append(element)
    if not elements or len(elements) > 8:
        raise ExternalReferenceError("invalid_query", "elements must contain 1 to 8 symbols")
    return elements


def _material_ids(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExternalReferenceError("invalid_query", "material_ids must be an array")
    identifiers = []
    for raw in value:
        if not isinstance(raw, str):
            raise ExternalReferenceError(
                "invalid_query", "material_ids contains an invalid id")
        identifier = raw.strip().lower()
        if not _MATERIAL_ID_RE.fullmatch(identifier):
            raise ExternalReferenceError("invalid_query", "material_ids contains an invalid id")
        if identifier not in identifiers:
            identifiers.append(identifier)
    if not identifiers or len(identifiers) > 10:
        raise ExternalReferenceError("invalid_query", "material_ids must contain 1 to 10 ids")
    return identifiers


def normalize_query(spec: ProviderSpec, filters: Any) -> dict[str, Any]:
    """Validate the complete browser search contract and reject extension keys."""
    if not isinstance(filters, Mapping):
        raise ExternalReferenceError("invalid_query", "filters must be an object")
    if any(not isinstance(key, str) for key in filters):
        raise ExternalReferenceError("invalid_query", "filter keys must be strings")
    raw = dict(filters)
    forbidden = set(raw) & _FORBIDDEN_QUERY_KEYS
    unknown = set(raw) - set(spec.query_fields)
    if forbidden or unknown:
        raise ExternalReferenceError("invalid_query", "query contains a forbidden field")
    query: dict[str, Any] = {
        "page": _bounded_int(raw.get("page"), field_name="page", minimum=1,
                             maximum=MAX_PAGE_NUMBER, default=1),
        "limit": _bounded_int(raw.get("limit"), field_name="limit", minimum=1,
                              maximum=MAX_PAGE_SIZE, default=10),
    }
    if spec.protocol == "materials-project-rest":
        if raw.get("formula") is not None:
            query["formula"] = _bounded_text(
                raw["formula"], field_name="formula", pattern=_FORMULA_RE)
        if raw.get("chemsys") is not None:
            parts = str(raw["chemsys"] or "").strip().split("-")
            query["chemsys"] = "-".join(_element_list(parts))
        if raw.get("elements") is not None:
            query["elements"] = _element_list(raw["elements"])
        if raw.get("material_ids") is not None:
            query["material_ids"] = _material_ids(raw["material_ids"])
        search_keys = {"formula", "chemsys", "elements", "material_ids"}
    elif spec.protocol == "optimade":
        if raw.get("formula") is not None:
            query["formula"] = _bounded_text(
                raw["formula"], field_name="formula", pattern=_FORMULA_RE)
        if raw.get("elements") is not None:
            query["elements"] = _element_list(raw["elements"])
        for key in ("nelements_min", "nelements_max"):
            if raw.get(key) is not None:
                query[key] = _bounded_int(
                    raw[key], field_name=key, minimum=1, maximum=20)
        if ("nelements_min" in query and "nelements_max" in query
                and query["nelements_min"] > query["nelements_max"]):
            raise ExternalReferenceError(
                "invalid_query", "nelements_min must not exceed nelements_max")
        search_keys = {"formula", "elements", "nelements_min", "nelements_max"}
    else:
        for key in ("reactants", "products", "chemical_composition", "surface", "facet"):
            if raw.get(key) is not None:
                query[key] = _bounded_text(
                    raw[key], field_name=key, pattern=_QUERY_TEXT_RE)
        search_keys = {"reactants", "products", "chemical_composition", "surface", "facet"}
    if not (set(query) & search_keys):
        raise ExternalReferenceError(
            "invalid_query", "at least one bounded scientific filter is required")
    return query


@dataclass(frozen=True)
class OutboundRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class BoundedHttpTransport:
    """HTTPS-only transport with redirect, timeout and response-size bounds."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 max_response_bytes: int = MAX_RESPONSE_BYTES, opener=None,
                 max_in_flight: int = 2):
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 10.0))
        self.max_response_bytes = max(1024, min(int(max_response_bytes), MAX_RESPONSE_BYTES))
        self._opener = opener or urllib_request.build_opener(_NoRedirectHandler())
        self._slots = threading.BoundedSemaphore(max(1, min(int(max_in_flight), 4)))

    def request(self, outbound: OutboundRequest) -> TransportResponse:
        _validate_endpoint(outbound.url.split("?", 1)[0])
        if not self._slots.acquire(blocking=False):
            raise TransportFailure(
                "network_busy", "external provider transport is busy")
        finished = threading.Event()
        outcome: dict[str, Any] = {}

        def worker():
            try:
                outcome["response"] = self._request_once(outbound)
            except BaseException as exc:                 # noqa: BLE001 worker handoff
                outcome["error"] = exc
            finally:
                self._slots.release()
                finished.set()

        thread = threading.Thread(
            target=worker, name="vcs-external-reference-http", daemon=True)
        thread.start()
        if not finished.wait(self.timeout_seconds):
            raise TransportFailure(
                "request_timeout", "external provider request exceeded the wall-clock deadline")
        if "error" in outcome:
            error = outcome["error"]
            if isinstance(error, Exception):
                raise error
            raise TransportFailure(
                "network_failure", "external provider request failed")
        response = outcome.get("response")
        if not isinstance(response, TransportResponse):
            raise TransportFailure(
                "network_failure", "external provider request failed")
        return response

    def _request_once(self, outbound: OutboundRequest) -> TransportResponse:
        headers = {str(key): str(value) for key, value in outbound.headers.items()}
        for key in tuple(headers):
            if key.casefold() == "user-agent":
                headers.pop(key, None)
        headers["User-Agent"] = APPLICATION_USER_AGENT
        req = urllib_request.Request(
            outbound.url, data=outbound.body,
            headers=headers,
            method=outbound.method)
        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as response:
                body = response.read(self.max_response_bytes + 1)
                status = int(getattr(response, "status", 200))
                headers = {str(key).lower(): str(value)
                           for key, value in response.headers.items()}
        except urllib_error.HTTPError as exc:
            body = exc.read(self.max_response_bytes + 1)
            status = int(exc.code)
            headers = {str(key).lower(): str(value)
                       for key, value in (exc.headers.items() if exc.headers else [])}
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise TransportFailure(
                "network_failure", "external provider request failed") from exc
        if len(body) > self.max_response_bytes:
            raise TransportFailure(
                "response_too_large", "external provider response exceeded the byte limit",
                retryable=False)
        return TransportResponse(status=status, headers=headers, body=body)


@dataclass(frozen=True)
class AdapterResult:
    items: tuple[dict[str, Any], ...]
    provider_version: str
    total_count: int | None
    more_available: bool
    response_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalStructureCandidate:
    """Internal handoff object consumed by Structure Source Hub-like clients."""

    candidate_id: str
    provider_id: str
    source_id: str
    structure_format: str
    structure_payload: Any
    provenance: Mapping[str, Any]
    evidence_policy: Mapping[str, Any]


@runtime_checkable
class ExternalReferenceProtocol(Protocol):
    """Stable narrow core interface; browser adapters must not widen it."""

    def catalog(self) -> Mapping[str, Any]: ...

    def search(self, provider_id: Any, filters: Any) -> Mapping[str, Any]: ...

    def preview_structure_candidate(
            self, result_token: Any,
            item_id: Any) -> ExternalStructureCandidate: ...

    def claim_structure_candidate(
            self, result_token: Any, item_id: Any, *,
            confirmed: bool) -> ExternalStructureCandidate: ...


def _json_object(body: bytes) -> Mapping[str, Any]:
    try:
        decoded = body.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalReferenceError(
            "schema_drift", "provider response is not a JSON object") from exc
    if not isinstance(value, Mapping):
        raise ExternalReferenceError(
            "schema_drift", "provider response root is not an object")
    return value


def _credential_pattern(provider_id: str) -> re.Pattern[str] | None:
    return _CREDENTIAL_PATTERNS.get(str(provider_id))


def _valid_credential(provider_id: str, value: Any) -> str | None:
    """Return one provider-shaped key without coercing arbitrary input."""
    if not isinstance(value, str):
        return None
    pattern = _credential_pattern(provider_id)
    return value if pattern is not None and pattern.fullmatch(value) else None


def _json_strings(value: Any) -> Iterator[str]:
    """Yield every decoded JSON key and value string, recursively."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _json_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _json_strings(item)


def _credential_echoed(api_key: str | None, response: TransportResponse,
                       decoded: Mapping[str, Any] | None = None, *extra: Any) -> bool:
    """Detect a key in raw bytes, headers, decoded JSON, DTOs and manifests."""
    if not api_key:
        return False
    encoded = api_key.encode("utf-8")
    if encoded in response.body:
        return True
    if any(api_key in str(key) or api_key in str(value)
           for key, value in response.headers.items()):
        return True
    values: list[Any] = []
    if decoded is not None:
        values.append(decoded)
    values.extend(extra)
    return any(api_key in text for value in values for text in _json_strings(value))


def _scrub_credentialed_cache_json(value: Any) -> Any:
    """Build a cache-safe JSON snapshot; credentialed bytes are never stored raw."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if (_SECRET_RE.search(value) or _LOCAL_PATH_RE.search(value)
                or len(value) > 16_384 or _CONTROL_RE.search(value)):
            return "[redacted-external-text]"
        return value
    if isinstance(value, Mapping):
        result = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if (_SECRET_RE.search(key) or _LOCAL_PATH_RE.search(key)
                    or _CONTROL_RE.search(key) or len(key) > 256):
                key = "redacted_field_" + hashlib.sha256(
                    key.encode("utf-8", errors="replace")).hexdigest()[:12]
            if re.search(
                    r"(?i)(?:authorization|api[_-]?key|credential|password|secret|token)",
                    key):
                result[key] = "[redacted-external-value]"
            else:
                result[key] = _scrub_credentialed_cache_json(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_scrub_credentialed_cache_json(item) for item in value]
    return "[redacted-external-value]"


def _plain_formula_composition(formula: Any) -> dict[str, int] | None:
    if not isinstance(formula, str):
        return None
    value = formula.strip()
    parts = re.findall(r"([A-Z][a-z]?)([0-9]*)", value)
    if not parts or "".join(element + count for element, count in parts) != value:
        return None
    result: dict[str, int] = {}
    for element, raw_count in parts:
        if element not in _ELEMENTS:
            return None
        count = int(raw_count or 1)
        if count < 1:
            return None
        result[element] = result.get(element, 0) + count
    return result


def _reduced_composition(composition: Mapping[str, int]) -> dict[str, int]:
    values = {str(element): int(count) for element, count in composition.items()
              if str(element) in _ELEMENTS and int(count) > 0}
    if not values or len(values) != len(composition):
        raise ExternalReferenceError(
            "partial_data", "external structure composition is invalid")
    divisor = math.gcd(*values.values())
    return {element: count // divisor for element, count in values.items()}


def _canonical_formula(composition: Mapping[str, int]) -> str:
    reduced = _reduced_composition(composition)
    if "C" in reduced:
        order = ["C"] + (["H"] if "H" in reduced else [])
        order.extend(sorted(
            element for element in reduced if element not in {"C", "H"}))
    else:
        order = sorted(reduced)
    return "".join(
        element + (str(reduced[element]) if reduced[element] != 1 else "")
        for element in order)


def _cross_validate_formula(provider_formula: Any,
                            structure_composition: Mapping[str, int]) -> str:
    declared = _plain_formula_composition(provider_formula)
    actual = _reduced_composition(structure_composition)
    if declared is None or _reduced_composition(declared) != actual:
        raise ExternalReferenceError(
            "partial_data", "provider formula does not match structure sites")
    return _canonical_formula(actual)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_external_text(value: Any, *, maximum: int = 240) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _CONTROL_RE.sub(" ", text)
    if _SECRET_RE.search(text) or _LOCAL_PATH_RE.search(text):
        return "[redacted-external-text]"
    return text[:maximum]


def _safe_doi(value: Any) -> str:
    text = _safe_external_text(value, maximum=160)
    text = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", text).strip()
    return text if re.fullmatch(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", text) else ""


def _item_identity(provider_id: str, source_id: str) -> str:
    digest = hashlib.sha256(str(source_id).encode("utf-8")).hexdigest()[:24]
    return f"{provider_id}:{digest}"


def _structure_content_sha256(formula: str, structure_format: str,
                              payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json({
        "formula": str(formula),
        "structure_format": str(structure_format),
        "structure": payload,
    })).hexdigest()


def _sanitize_structure_payload(value: Any, *, depth: int = 0) -> Any:
    """Validate provider structure JSON before it may enter candidate state."""
    if depth > 10:
        raise ExternalReferenceError(
            "structure_unavailable", "external structure nesting is too deep")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExternalReferenceError(
                "structure_unavailable", "external structure contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > 4096 or _CONTROL_RE.search(value):
            raise ExternalReferenceError(
                "structure_unavailable", "external structure contains invalid text")
        if _SECRET_RE.search(value) or _LOCAL_PATH_RE.search(value):
            raise ExternalReferenceError(
                "structure_unavailable", "external structure contains sensitive text")
        return value
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ExternalReferenceError(
                "structure_unavailable", "external structure object is too large")
        result = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if (not re.fullmatch(r"[A-Za-z0-9_@.:+-]{1,96}", key)
                    or key.lower() in _FORBIDDEN_QUERY_KEYS):
                raise ExternalReferenceError(
                    "structure_unavailable", "external structure contains an invalid field")
            result[key] = _sanitize_structure_payload(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > 100_000:
            raise ExternalReferenceError(
                "structure_unavailable", "external structure array is too large")
        return [_sanitize_structure_payload(item, depth=depth + 1) for item in value]
    raise ExternalReferenceError(
        "structure_unavailable", "external structure contains an unsupported value")


def _vector3(value: Any, *, field_name: str) -> list[float]:
    if (not isinstance(value, list) or len(value) != 3
            or any(_finite_number(item) is None for item in value)):
        raise ExternalReferenceError(
            "partial_data", f"{field_name} is not a finite 3-vector")
    return [float(item) for item in value]


def _matrix3(value: Any, *, field_name: str) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != 3:
        raise ExternalReferenceError(
            "partial_data", f"{field_name} is not a finite 3x3 matrix")
    return [_vector3(row, field_name=field_name) for row in value]


def _materials_project_structure(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExternalReferenceError(
            "partial_data", "Materials Project structure is missing")
    lattice = value.get("lattice")
    if not isinstance(lattice, Mapping):
        raise ExternalReferenceError(
            "partial_data", "Materials Project lattice is missing")
    _matrix3(lattice.get("matrix"), field_name="Materials Project lattice")
    sites = value.get("sites")
    if not isinstance(sites, list) or not sites or len(sites) > 100_000:
        raise ExternalReferenceError(
            "partial_data", "Materials Project sites are incomplete")
    for site in sites:
        if not isinstance(site, Mapping):
            raise ExternalReferenceError(
                "partial_data", "Materials Project site is not an object")
        coordinates = site.get("xyz") if isinstance(site.get("xyz"), list) else site.get("abc")
        _vector3(coordinates, field_name="Materials Project site coordinates")
        if not site.get("species") and not _safe_external_text(site.get("label"), maximum=40):
            raise ExternalReferenceError(
                "partial_data", "Materials Project site species is missing")
    try:
        return _sanitize_structure_payload(copy.deepcopy(value))
    except ExternalReferenceError as exc:
        raise ExternalReferenceError(
            "partial_data", "Materials Project structure contains unsafe data") from exc


def _materials_project_composition(value: Mapping[str, Any]) -> dict[str, int]:
    composition: dict[str, int] = {}
    for site in value.get("sites") or []:
        if not isinstance(site, Mapping):
            raise ExternalReferenceError(
                "partial_data", "Materials Project site is incomplete")
        species = site.get("species")
        element = ""
        if isinstance(species, list):
            if len(species) != 1 or not isinstance(species[0], Mapping):
                raise ExternalReferenceError(
                    "partial_data", "Materials Project structure is disordered")
            element = species[0].get("element")
            occupancy = _finite_number(species[0].get("occu"))
            if (not isinstance(element, str) or element not in _ELEMENTS
                    or occupancy is None or abs(occupancy - 1.0) > 1e-9):
                raise ExternalReferenceError(
                    "partial_data", "Materials Project site occupancy is invalid")
        else:
            label = site.get("label")
            element = label if isinstance(label, str) and label in _ELEMENTS else ""
        if element not in _ELEMENTS:
            raise ExternalReferenceError(
                "partial_data", "Materials Project site species is ambiguous")
        composition[element] = composition.get(element, 0) + 1
    return composition


def _optimade_structure(attrs: Mapping[str, Any]) -> Mapping[str, Any]:
    nsites = attrs.get("nsites")
    nelements = attrs.get("nelements")
    if (isinstance(nsites, bool) or not isinstance(nsites, int)
            or nsites < 1 or nsites > 100_000):
        raise ExternalReferenceError("partial_data", "OPTIMADE nsites is invalid")
    if (isinstance(nelements, bool) or not isinstance(nelements, int)
            or nelements < 1 or nelements > len(_ELEMENTS)):
        raise ExternalReferenceError("partial_data", "OPTIMADE nelements is invalid")
    elements = attrs.get("elements")
    if (not isinstance(elements, list) or len(elements) != nelements
            or any(not isinstance(item, str) or item not in _ELEMENTS for item in elements)):
        raise ExternalReferenceError("partial_data", "OPTIMADE elements are invalid")
    lattice = _matrix3(attrs.get("lattice_vectors"), field_name="OPTIMADE lattice")
    positions = attrs.get("cartesian_site_positions")
    species_at_sites = attrs.get("species_at_sites")
    if (not isinstance(positions, list) or not isinstance(species_at_sites, list)
            or len(positions) != nsites or len(species_at_sites) != nsites):
        raise ExternalReferenceError(
            "partial_data", "OPTIMADE site arrays do not match nsites")
    clean_positions = [
        _vector3(position, field_name="OPTIMADE site coordinates")
        for position in positions]
    clean_species = []
    for raw in species_at_sites:
        if not isinstance(raw, str):
            raise ExternalReferenceError(
                "partial_data", "OPTIMADE species_at_sites is invalid")
        name = _safe_external_text(raw, maximum=80)
        if not name or name == "[redacted-external-text]":
            raise ExternalReferenceError(
                "partial_data", "OPTIMADE species_at_sites is invalid")
        clean_species.append(name)
    payload = {
        key: copy.deepcopy(attrs.get(key)) for key in (
            "chemical_formula_reduced", "chemical_formula_hill", "elements",
            "nelements", "nsites", "species", "dimension_types")
    }
    payload["lattice_vectors"] = lattice
    payload["cartesian_site_positions"] = clean_positions
    payload["species_at_sites"] = clean_species
    try:
        return _sanitize_structure_payload(payload)
    except ExternalReferenceError as exc:
        raise ExternalReferenceError(
            "partial_data", "OPTIMADE structure contains unsafe data") from exc


def _optimade_composition(attrs: Mapping[str, Any]) -> dict[str, int]:
    definitions: dict[str, str] = {}
    raw_definitions = attrs.get("species")
    if raw_definitions is not None and not isinstance(raw_definitions, list):
        raise ExternalReferenceError("partial_data", "OPTIMADE species are invalid")
    for definition in raw_definitions or []:
        if not isinstance(definition, Mapping):
            raise ExternalReferenceError("partial_data", "OPTIMADE species are invalid")
        name = definition.get("name")
        symbols = definition.get("chemical_symbols")
        concentrations = definition.get("concentration")
        if (not isinstance(name, str) or not name
                or not isinstance(symbols, list) or len(symbols) != 1
                or symbols[0] not in _ELEMENTS
                or not isinstance(concentrations, list) or len(concentrations) != 1
                or _finite_number(concentrations[0]) is None
                or abs(float(concentrations[0]) - 1.0) > 1e-9):
            raise ExternalReferenceError(
                "partial_data", "OPTIMADE disordered species are unsupported")
        definitions[name] = str(symbols[0])
    composition: dict[str, int] = {}
    for raw in attrs.get("species_at_sites") or []:
        if not isinstance(raw, str):
            raise ExternalReferenceError(
                "partial_data", "OPTIMADE site species is invalid")
        element = raw if raw in _ELEMENTS else definitions.get(raw, "")
        if element not in _ELEMENTS:
            raise ExternalReferenceError(
                "partial_data", "OPTIMADE site species is ambiguous")
        composition[element] = composition.get(element, 0) + 1
    return composition


def _evidence_policy() -> dict[str, Any]:
    return {
        "role": "external_reference",
        "method_compatibility": "not_assessed",
        "aggregate_eligible": False,
        "validation_result_eligible": False,
        "final_claim_eligible": False,
        "promotion_status": "candidate_only",
    }


def _property(key: str, value: Any, *, unit: str | None = None,
              quantity: str | None = None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "quantity": quantity or key,
        "evidence_role": "external_reference",
        "aggregate_eligible": False,
    }


class ProviderAdapter:
    def build_request(self, spec: ProviderSpec, query: Mapping[str, Any],
                      api_key: str | None) -> OutboundRequest:
        raise NotImplementedError

    def parse_response(self, spec: ProviderSpec, query: Mapping[str, Any],
                       response: TransportResponse) -> AdapterResult:
        raise NotImplementedError


class MaterialsProjectAdapter(ProviderAdapter):
    _FIELDS = (
        "material_id", "formula_pretty", "chemsys", "elements", "nelements",
        "density", "volume", "energy_above_hull", "formation_energy_per_atom",
        "band_gap", "is_stable", "structure", "origins", "last_updated",
    )

    def build_request(self, spec: ProviderSpec, query: Mapping[str, Any],
                      api_key: str | None) -> OutboundRequest:
        if _valid_credential(spec.provider_id, api_key) is None:
            raise ExternalReferenceError(
                "invalid_credential", "provider API key is invalid")
        params: dict[str, Any] = {
            "_fields": ",".join(self._FIELDS),
            "_limit": int(query["limit"]),
            "_skip": (int(query["page"]) - 1) * int(query["limit"]),
        }
        for key in ("formula", "chemsys"):
            if key in query:
                params[key] = query[key]
        if "elements" in query:
            params["elements"] = ",".join(query["elements"])
        if "material_ids" in query:
            params["material_ids"] = ",".join(query["material_ids"])
        url = spec.endpoint + "?" + urllib_parse.urlencode(params)
        return OutboundRequest(
            "GET", url,
            {"Accept": "application/json", "X-API-KEY": str(api_key)})

    def parse_response(self, spec: ProviderSpec, query: Mapping[str, Any],
                       response: TransportResponse) -> AdapterResult:
        root = _json_object(response.body)
        raw_rows = root.get("data")
        if not isinstance(raw_rows, list):
            raise ExternalReferenceError(
                "schema_drift", "Materials Project response has no data array")
        if len(raw_rows) > int(query["limit"]):
            raise ExternalReferenceError(
                "partial_data", "Materials Project exceeded the requested page limit")
        items = []
        source_ids = set()
        for row in raw_rows:
            if not isinstance(row, Mapping):
                raise ExternalReferenceError(
                    "partial_data", "Materials Project returned a non-object item")
            source_id = _safe_external_text(row.get("material_id"), maximum=80)
            declared_formula = row.get("formula_pretty")
            if not source_id or not isinstance(declared_formula, str):
                raise ExternalReferenceError(
                    "partial_data", "Materials Project item lacks identity fields")
            if source_id in source_ids:
                raise ExternalReferenceError(
                    "partial_data", "Materials Project returned duplicate identities")
            source_ids.add(source_id)
            structure_payload = _materials_project_structure(row.get("structure"))
            formula = _cross_validate_formula(
                declared_formula, _materials_project_composition(structure_payload))
            content_sha256 = _structure_content_sha256(
                formula, "materials-project-structure-json", structure_payload)
            properties = []
            for record in (
                    _property("energy_above_hull", _finite_number(row.get("energy_above_hull")),
                              unit="eV/atom", quantity="energy_above_hull"),
                    _property("formation_energy_per_atom",
                              _finite_number(row.get("formation_energy_per_atom")),
                              unit="eV/atom", quantity="formation_energy_per_atom"),
                    _property("band_gap", _finite_number(row.get("band_gap")),
                              unit="eV", quantity="band_gap"),
                    _property("density", _finite_number(row.get("density")),
                              unit="g/cm3", quantity="density"),
                    _property("volume", _finite_number(row.get("volume")),
                              unit="angstrom3", quantity="volume"),
                    _property("is_stable", row.get("is_stable")
                              if isinstance(row.get("is_stable"), bool) else None,
                              quantity="stability_flag")):
                if record:
                    properties.append(record)
            origins = row.get("origins") if isinstance(row.get("origins"), list) else []
            method_refs = []
            for origin in origins[:20]:
                if not isinstance(origin, Mapping):
                    continue
                name = _safe_external_text(origin.get("name"), maximum=80)
                task_id = _safe_external_text(origin.get("task_id"), maximum=80)
                if name or task_id:
                    method_refs.append({"property": name, "task_id": task_id})
            item_id = _item_identity(spec.provider_id, source_id)
            items.append({
                "item_id": item_id,
                "kind": "material_structure",
                "source_id": source_id,
                "title": f"{formula} · {source_id}",
                "formula": formula,
                "chemical_system": _safe_external_text(row.get("chemsys"), maximum=80),
                "properties": properties,
                "method": {
                    "status": "provider_reported" if method_refs else "unknown",
                    "label": "Materials Project summary",
                    "references": method_refs,
                },
                "structure_available": True,
                "structure_format": "materials-project-structure-json",
                "_structure_content_sha256": content_sha256,
                "citation": {
                    "dois": [], "url": spec.citation_url,
                    "attribution": spec.attribution,
                },
                "license": {"id": spec.license_id, "url": spec.license_url},
                "evidence_policy": _evidence_policy(),
                "_structure_payload": structure_payload,
            })
        meta = root.get("meta") if isinstance(root.get("meta"), Mapping) else {}
        version = _safe_external_text(
            meta.get("db_version") or response.headers.get("x-api-version")
            or spec.adapter_version, maximum=80)
        total = meta.get("total_doc")
        total_count = int(total) if isinstance(total, int) and total >= 0 else None
        more = bool(total_count is not None and (
            int(query["page"]) * int(query["limit"]) < total_count))
        return AdapterResult(
            items=tuple(items), provider_version=version,
            total_count=total_count, more_available=more,
            response_metadata={"database_version": version})


class OptimadeAdapter(ProviderAdapter):
    _FIELDS = (
        "chemical_formula_reduced", "chemical_formula_hill", "elements",
        "nelements", "nsites", "lattice_vectors", "cartesian_site_positions",
        "species_at_sites", "species", "dimension_types", "last_modified",
    )

    def build_request(self, spec: ProviderSpec, query: Mapping[str, Any],
                      api_key: str | None) -> OutboundRequest:
        clauses = []
        if "formula" in query:
            clauses.append(f'chemical_formula_reduced = "{query["formula"]}"')
        for element in query.get("elements") or []:
            clauses.append(f'elements HAS "{element}"')
        if "nelements_min" in query:
            clauses.append(f'nelements >= {int(query["nelements_min"])}')
        if "nelements_max" in query:
            clauses.append(f'nelements <= {int(query["nelements_max"])}')
        params = {
            "filter": " AND ".join(clauses),
            "page_limit": int(query["limit"]),
            "page_offset": (int(query["page"]) - 1) * int(query["limit"]),
            "response_fields": ",".join(self._FIELDS),
        }
        return OutboundRequest(
            "GET", spec.endpoint + "?" + urllib_parse.urlencode(params),
            {"Accept": "application/vnd.api+json, application/json"})

    def parse_response(self, spec: ProviderSpec, query: Mapping[str, Any],
                       response: TransportResponse) -> AdapterResult:
        root = _json_object(response.body)
        if root.get("errors"):
            raise ExternalReferenceError(
                "provider_unavailable", "OPTIMADE provider returned errors")
        raw_rows = root.get("data")
        if not isinstance(raw_rows, list):
            raise ExternalReferenceError(
                "schema_drift", "OPTIMADE response has no data array")
        if len(raw_rows) > int(query["limit"]):
            raise ExternalReferenceError(
                "partial_data", "OPTIMADE exceeded the requested page limit")
        items = []
        source_ids = set()
        for row in raw_rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("attributes"), Mapping):
                raise ExternalReferenceError(
                    "partial_data", "OPTIMADE returned an incomplete resource")
            attrs = row["attributes"]
            source_id = _safe_external_text(row.get("id"), maximum=160)
            declared_formula = (
                attrs.get("chemical_formula_reduced")
                or attrs.get("chemical_formula_hill"))
            if not source_id or not isinstance(declared_formula, str):
                raise ExternalReferenceError(
                    "partial_data", "OPTIMADE structure lacks mandatory fields")
            if source_id in source_ids:
                raise ExternalReferenceError(
                    "partial_data", "OPTIMADE returned duplicate identities")
            source_ids.add(source_id)
            structure_payload = _optimade_structure(attrs)
            formula = _cross_validate_formula(
                declared_formula, _optimade_composition(structure_payload))
            content_sha256 = _structure_content_sha256(
                formula, "optimade-structure-json", structure_payload)
            items.append({
                "item_id": _item_identity(spec.provider_id, source_id),
                "kind": "material_structure",
                "source_id": source_id,
                "title": f"{formula} · {source_id}",
                "formula": formula,
                "chemical_system": "-".join(
                    _safe_external_text(item, maximum=4)
                    for item in (attrs.get("elements") or [])[:8]),
                "properties": [record for record in (
                    _property("nelements", attrs.get("nelements"), quantity="nelements"),
                    _property("nsites", attrs.get("nsites"), quantity="nsites"),
                ) if record],
                "method": {"status": "unknown", "label": "OPTIMADE structure record",
                           "references": []},
                "structure_available": True,
                "structure_format": "optimade-structure-json",
                "_structure_content_sha256": content_sha256,
                "citation": {"dois": [], "url": spec.citation_url,
                             "attribution": spec.attribution},
                "license": {"id": spec.license_id, "url": spec.license_url},
                "evidence_policy": _evidence_policy(),
                "_structure_payload": structure_payload,
            })
        meta = root.get("meta") if isinstance(root.get("meta"), Mapping) else {}
        provider_meta = meta.get("provider") if isinstance(meta.get("provider"), Mapping) else {}
        version = _safe_external_text(
            meta.get("api_version") or spec.adapter_version, maximum=80)
        data_available = meta.get("data_available")
        total_count = (int(data_available)
                       if isinstance(data_available, int) and data_available >= 0 else None)
        more = bool(meta.get("more_data_available"))
        return AdapterResult(
            items=tuple(items), provider_version=version,
            total_count=total_count, more_available=more,
            response_metadata={
                "api_version": version,
                "provider_name": _safe_external_text(provider_meta.get("name"), maximum=120),
                "provider_prefix": _safe_external_text(provider_meta.get("prefix"), maximum=32),
            })


class CatalysisHubAdapter(ProviderAdapter):
    _QUERY = """query ExternalReferenceSearch(
  $first: Int!, $reactants: String, $products: String,
  $chemicalComposition: String, $surfaceComposition: String, $facet: String) {
  reactions(first: $first, reactants: $reactants, products: $products,
    chemicalComposition: $chemicalComposition,
    surfaceComposition: $surfaceComposition, facet: $facet) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        chemicalComposition
        surfaceComposition
        facet
        reactants
        products
        reactionEnergy
        activationEnergy
        dftCode
        dftFunctional
        pubId
        publication { title authors year doi publisher }
      }
    }
  }
}"""

    def build_request(self, spec: ProviderSpec, query: Mapping[str, Any],
                      api_key: str | None) -> OutboundRequest:
        if _valid_credential(spec.provider_id, api_key) is None:
            raise ExternalReferenceError(
                "invalid_credential", "provider API key is invalid")
        variables: dict[str, Any] = {"first": int(query["limit"]) * int(query["page"])}
        mapping = {
            "reactants": "reactants", "products": "products",
            "chemical_composition": "chemicalComposition",
            "surface": "surfaceComposition", "facet": "facet",
        }
        for public_key, variable in mapping.items():
            variables[variable] = query.get(public_key)
        body = json.dumps(
            {"query": self._QUERY, "variables": variables},
            ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return OutboundRequest(
            "POST", spec.endpoint,
            {"Accept": "application/json", "Content-Type": "application/json",
             "X-API-Key": api_key},
            body)

    def parse_response(self, spec: ProviderSpec, query: Mapping[str, Any],
                       response: TransportResponse) -> AdapterResult:
        root = _json_object(response.body)
        if root.get("errors"):
            raise ExternalReferenceError(
                "schema_drift", "Catalysis-Hub GraphQL response contains errors")
        data = root.get("data")
        if not isinstance(data, Mapping):
            raise ExternalReferenceError(
                "schema_drift", "Catalysis-Hub response has no data object")
        connection = data.get("reactions")
        if not isinstance(connection, Mapping):
            raise ExternalReferenceError(
                "schema_drift", "Catalysis-Hub response has no reactions connection")
        all_edges = connection.get("edges")
        total = connection.get("totalCount")
        page_info = connection.get("pageInfo")
        if (not isinstance(all_edges, list)
                or isinstance(total, bool) or not isinstance(total, int) or total < 0
                or not isinstance(page_info, Mapping)
                or not isinstance(page_info.get("hasNextPage"), bool)
                or (page_info.get("endCursor") is not None
                    and not isinstance(page_info.get("endCursor"), str))):
            raise ExternalReferenceError(
                "schema_drift", "Catalysis-Hub connection schema is invalid")
        limit = int(query["limit"])
        offset = (int(query["page"]) - 1) * limit
        if len(all_edges) > offset + limit:
            raise ExternalReferenceError(
                "partial_data", "Catalysis-Hub exceeded the bounded query size")
        selected = all_edges[offset:offset + limit]
        items = []
        source_ids = set()
        for edge in selected:
            row = edge.get("node") if isinstance(edge, Mapping) else None
            if not isinstance(row, Mapping):
                raise ExternalReferenceError(
                    "partial_data", "Catalysis-Hub returned an incomplete edge")
            string_fields = (
                "id", "chemicalComposition", "surfaceComposition", "facet",
                "reactants", "products", "dftCode", "dftFunctional", "pubId")
            if any(row.get(key) is not None and not isinstance(row.get(key), str)
                   for key in string_fields):
                raise ExternalReferenceError(
                    "partial_data", "Catalysis-Hub returned invalid text fields")
            if any(row.get(key) is not None and _finite_number(row.get(key)) is None
                   for key in ("reactionEnergy", "activationEnergy")):
                raise ExternalReferenceError(
                    "partial_data", "Catalysis-Hub returned invalid energy fields")
            source_id = _safe_external_text(row.get("id"), maximum=80)
            reactants = _safe_external_text(row.get("reactants"), maximum=160)
            products = _safe_external_text(row.get("products"), maximum=160)
            if not source_id or (not reactants and not products):
                raise ExternalReferenceError(
                    "partial_data", "Catalysis-Hub reaction lacks identity fields")
            if source_id in source_ids:
                raise ExternalReferenceError(
                    "partial_data", "Catalysis-Hub returned duplicate identities")
            source_ids.add(source_id)
            publication_raw = row.get("publication")
            if publication_raw is not None and not isinstance(publication_raw, Mapping):
                raise ExternalReferenceError(
                    "partial_data", "Catalysis-Hub publication schema is invalid")
            publication = publication_raw or {}
            if (any(publication.get(key) is not None
                    and not isinstance(publication.get(key), str)
                    for key in ("title", "authors", "doi", "publisher"))
                    or (publication.get("year") is not None
                        and (isinstance(publication.get("year"), bool)
                             or not isinstance(publication.get("year"), int)))):
                raise ExternalReferenceError(
                    "partial_data", "Catalysis-Hub publication schema is invalid")
            doi = _safe_doi(publication.get("doi") or row.get("pubId"))
            properties = []
            for record in (
                    _property("reaction_energy", _finite_number(row.get("reactionEnergy")),
                              unit="eV", quantity="reaction_energy"),
                    _property("activation_energy", _finite_number(row.get("activationEnergy")),
                              unit="eV", quantity="activation_energy")):
                if record:
                    properties.append(record)
            method_details = []
            for key, raw in (("code", row.get("dftCode")),
                             ("functional", row.get("dftFunctional"))):
                value = _safe_external_text(raw, maximum=120)
                if value:
                    method_details.append({"key": key, "value": value})
            title = f"{reactants or '?'} -> {products or '?'}"
            items.append({
                "item_id": _item_identity(spec.provider_id, source_id),
                "kind": "catalytic_reaction",
                "source_id": source_id,
                "title": title,
                "formula": _safe_external_text(row.get("chemicalComposition"), maximum=80),
                "chemical_system": _safe_external_text(row.get("surfaceComposition"), maximum=80),
                "surface": _safe_external_text(row.get("surfaceComposition"), maximum=80),
                "facet": _safe_external_text(row.get("facet"), maximum=80),
                "reactants": reactants,
                "products": products,
                "properties": properties,
                "method": {
                    "status": "provider_reported" if method_details else "unknown",
                    "label": "Catalysis-Hub reaction record",
                    "references": method_details,
                },
                "structure_available": False,
                "structure_format": None,
                "publication": {
                    "title": _safe_external_text(publication.get("title"), maximum=240),
                    "authors": _safe_external_text(publication.get("authors"), maximum=240),
                    "year": publication.get("year") if isinstance(publication.get("year"), int) else None,
                    "publisher": _safe_external_text(publication.get("publisher"), maximum=120),
                    "doi": doi or None,
                },
                "citation": {
                    "dois": [doi] if doi else [], "url": spec.citation_url,
                    "attribution": spec.attribution,
                },
                "license": {"id": spec.license_id, "url": spec.license_url},
                "evidence_policy": _evidence_policy(),
                "_structure_payload": None,
            })
        total_count = total
        more = bool(total_count is not None and offset + len(selected) < total_count)
        if not more:
            more = bool(page_info.get("hasNextPage"))
        return AdapterResult(
            items=tuple(items), provider_version=spec.adapter_version,
            total_count=total_count, more_available=more,
            response_metadata={"graphql_document_id": "external-reference-search-v1"})


def _utc_now(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


def _default_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "vcstudio" / "external_reference_cache"
    return Path.home() / ".vcstudio" / "external_reference_cache"


class ExternalReferenceCache:
    """Bounded response-snapshot cache with detached provenance manifests."""

    _ENTRY_RE = re.compile(r"^external-[a-f0-9]{32}$")

    def __init__(self, root: str | os.PathLike[str] | None = None, *,
                 ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
                 capacity_bytes: int = DEFAULT_CACHE_CAPACITY_BYTES,
                 max_entries: int = DEFAULT_CACHE_ENTRIES,
                 clock=time.time):
        self.root = Path(root) if root is not None else _default_cache_root()
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.capacity_bytes = max(MAX_RESPONSE_BYTES, int(capacity_bytes))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.RLock()
        self.cleanup()

    def _paths(self, entry_id: str) -> tuple[Path, Path]:
        if not self._ENTRY_RE.fullmatch(str(entry_id)):
            raise ValueError("invalid cache entry identity")
        return self.root / f"{entry_id}.raw", self.root / f"{entry_id}.json"

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        temp = path.with_name(
            f".{path.name}.{token_secrets.token_hex(8)}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def write(self, raw_response: bytes, metadata: Mapping[str, Any]) -> dict[str, Any]:
        raw = bytes(raw_response)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("raw response exceeds cache limit")
        now = float(self._clock())
        cached_sha256 = hashlib.sha256(raw).hexdigest()
        source_sha256 = str(
            metadata.get("source_response_sha256") or cached_sha256)
        source_size = metadata.get("source_response_size")
        if (not re.fullmatch(r"[a-f0-9]{64}", source_sha256)
                or isinstance(source_size, bool)
                or (source_size is not None and not isinstance(source_size, int))):
            raise ValueError("invalid source-response metadata")
        entry_id = "external-" + hashlib.sha256(
            token_secrets.token_bytes(24) + cached_sha256.encode("ascii")
        ).hexdigest()[:32]
        manifest = {
            "schema": "vcstudio.external-reference-cache-entry/v1",
            "entry_id": entry_id,
            "retrieved_at": _utc_now(now),
            "retrieved_at_epoch": now,
            "expires_at": _utc_now(now + self.ttl_seconds),
            "expires_at_epoch": now + self.ttl_seconds,
            **copy.deepcopy(dict(metadata)),
            "raw_response_sha256": source_sha256,
            "raw_response_size": int(source_size) if source_size is not None else len(raw),
            "cached_response_sha256": cached_sha256,
            "cached_response_size": len(raw),
        }
        encoded = _canonical_json(manifest)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            raw_path, manifest_path = self._paths(entry_id)
            self._atomic_write(raw_path, raw)
            try:
                self._atomic_write(manifest_path, encoded)
            except Exception:
                try:
                    raw_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            self.cleanup()
        return copy.deepcopy(manifest)

    def _entries(self) -> list[tuple[float, str, int]]:
        records = []
        if not self.root.is_dir():
            return records
        for manifest_path in self.root.glob("external-*.json"):
            entry_id = manifest_path.stem
            if (not self._ENTRY_RE.fullmatch(entry_id)
                    or manifest_path.is_symlink() or not manifest_path.is_file()):
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(data, Mapping) or data.get("entry_id") != entry_id:
                    continue
                raw_path, _ = self._paths(entry_id)
                size = manifest_path.stat().st_size
                if raw_path.is_file() and not raw_path.is_symlink():
                    size += raw_path.stat().st_size
                records.append((float(data.get("retrieved_at_epoch") or 0), entry_id, size))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return records

    def _remove_entry(self, entry_id: str) -> None:
        raw_path, manifest_path = self._paths(entry_id)
        for path in (raw_path, manifest_path):
            if path.is_symlink():
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup(self) -> dict[str, int]:
        with self._lock:
            orphan_removed = 0
            now = float(self._clock())
            if self.root.is_dir():
                for manifest_path in self.root.glob("external-*.json"):
                    entry_id = manifest_path.stem
                    if (not self._ENTRY_RE.fullmatch(entry_id)
                            or manifest_path.is_symlink()):
                        continue
                    raw_path, _ = self._paths(entry_id)
                    try:
                        data = json.loads(manifest_path.read_text(encoding="utf-8"))
                        valid = (
                            isinstance(data, Mapping)
                            and data.get("entry_id") == entry_id
                            and raw_path.is_file() and not raw_path.is_symlink())
                    except (OSError, json.JSONDecodeError):
                        valid = False
                    if not valid:
                        self._remove_entry(entry_id)
                        orphan_removed += 1
                for raw_path in self.root.glob("external-*.raw"):
                    entry_id = raw_path.stem
                    if (not self._ENTRY_RE.fullmatch(entry_id)
                            or raw_path.is_symlink()):
                        continue
                    _raw, manifest_path = self._paths(entry_id)
                    if not manifest_path.is_file() or manifest_path.is_symlink():
                        self._remove_entry(entry_id)
                        orphan_removed += 1
                for temp_path in self.root.glob(".external-*.tmp"):
                    if temp_path.is_symlink() or not temp_path.is_file():
                        continue
                    try:
                        if temp_path.stat().st_mtime + self.ttl_seconds <= now:
                            temp_path.unlink(missing_ok=True)
                            orphan_removed += 1
                    except OSError:
                        pass
            entries = self._entries()
            expired = 0
            retained = []
            for retrieved, entry_id, size in entries:
                if retrieved + self.ttl_seconds <= now:
                    self._remove_entry(entry_id)
                    expired += 1
                else:
                    retained.append((retrieved, entry_id, size))
            retained.sort(key=lambda item: (item[0], item[1]))
            total = sum(item[2] for item in retained)
            capacity_removed = 0
            while (len(retained) > self.max_entries
                   or total > self.capacity_bytes):
                _retrieved, entry_id, size = retained.pop(0)
                self._remove_entry(entry_id)
                total -= size
                capacity_removed += 1
            return {
                "expired_removed": expired,
                "orphan_removed": orphan_removed,
                "capacity_removed": capacity_removed,
                "retained": len(retained),
                "retained_bytes": max(0, total),
            }


@dataclass
class _TokenRecord:
    provider_id: str
    cache_entry_id: str
    issued_at_epoch: float
    expires_at_epoch: float
    query: Mapping[str, Any]
    cache_metadata: Mapping[str, Any]
    items: tuple[dict[str, Any], ...]
    size_bytes: int
    import_state: str = "available"
    import_reservation: str | None = None


class _ResultTokenStore:
    def __init__(self, *, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
                 max_records: int = DEFAULT_TOKEN_RECORDS,
                 capacity_bytes: int = DEFAULT_TOKEN_CAPACITY_BYTES,
                 clock=time.time):
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.max_records = max(1, int(max_records))
        self.capacity_bytes = max(1024, int(capacity_bytes))
        self._clock = clock
        self._records: dict[str, _TokenRecord] = {}
        self._lock = threading.RLock()

    def _cleanup(self) -> None:
        now = float(self._clock())
        expired = [token for token, record in self._records.items()
                   if record.expires_at_epoch <= now]
        for token in expired:
            self._records.pop(token, None)

    def issue(self, *, provider_id: str, cache_entry_id: str,
              query: Mapping[str, Any], cache_metadata: Mapping[str, Any],
              items: Sequence[dict[str, Any]]) -> tuple[str, float]:
        with self._lock:
            self._cleanup()
            copied_query = copy.deepcopy(dict(query))
            copied_metadata = copy.deepcopy(dict(cache_metadata))
            copied_items = tuple(copy.deepcopy(list(items)))
            size_bytes = len(_canonical_json({
                "provider": provider_id,
                "cache_entry_id": cache_entry_id,
                "query": copied_query,
                "cache_metadata": copied_metadata,
                "items": copied_items,
            }))
            if size_bytes > self.capacity_bytes:
                raise ExternalReferenceError(
                    "result_too_large", "external result exceeds the token capacity")
            ordered = sorted(
                self._records.items(),
                key=lambda item: (item[1].issued_at_epoch, item[0]))
            retained_bytes = sum(record.size_bytes for record in self._records.values())
            while (ordered and (
                    len(self._records) >= self.max_records
                    or retained_bytes + size_bytes > self.capacity_bytes)):
                old_token, old_record = ordered.pop(0)
                self._records.pop(old_token, None)
                retained_bytes -= old_record.size_bytes
            token = "external-reference." + token_secrets.token_urlsafe(32)
            now = float(self._clock())
            self._records[token] = _TokenRecord(
                provider_id=provider_id,
                cache_entry_id=cache_entry_id,
                issued_at_epoch=now,
                expires_at_epoch=now + self.ttl_seconds,
                query=copied_query,
                cache_metadata=copied_metadata,
                items=copied_items,
                size_bytes=size_bytes,
            )
            return token, now + self.ttl_seconds

    def resolve(self, token: Any, *, for_import: bool = False) -> _TokenRecord:
        text = str(token or "")
        if not _TOKEN_RE.fullmatch(text):
            raise ExternalReferenceError("invalid_token", "result token is invalid")
        with self._lock:
            record = self._records.get(text)
            if record is None:
                raise ExternalReferenceError("invalid_token", "result token is invalid")
            if record.expires_at_epoch <= float(self._clock()):
                self._records.pop(text, None)
                raise ExternalReferenceError("expired_token", "result token has expired")
            if for_import and record.import_state != "available":
                code = ("token_replayed" if record.import_state == "committed"
                        else "token_reserved")
                message = ("result token was already consumed for import"
                           if record.import_state == "committed"
                           else "result token is reserved by another import")
                raise ExternalReferenceError(code, message)
            return record

    def reserve_import(self, token: Any) -> str:
        text = str(token or "")
        with self._lock:
            record = self.resolve(text, for_import=True)
            reservation = token_secrets.token_urlsafe(24)
            record.import_state = "reserved"
            record.import_reservation = reservation
            return reservation

    def release_import(self, token: Any, reservation: str) -> None:
        text = str(token or "")
        with self._lock:
            record = self._records.get(text)
            if (record is not None and record.import_state == "reserved"
                    and token_secrets.compare_digest(
                        str(record.import_reservation or ""), str(reservation))):
                record.import_state = "available"
                record.import_reservation = None

    def commit_import(self, token: Any, reservation: str) -> None:
        text = str(token or "")
        with self._lock:
            record = self._records.get(text)
            if (record is None or record.expires_at_epoch <= float(self._clock())):
                self._records.pop(text, None)
                raise ExternalReferenceError("expired_token", "result token has expired")
            if (record.import_state != "reserved"
                    or not token_secrets.compare_digest(
                        str(record.import_reservation or ""), str(reservation))):
                raise ExternalReferenceError(
                    "invalid_token", "result token reservation is invalid")
            record.import_state = "committed"
            record.import_reservation = None

    def consume_import(self, token: Any) -> None:
        reservation = self.reserve_import(token)
        self.commit_import(token, reservation)


@dataclass(frozen=True)
class _ImportPreviewRecord:
    project_id: str
    result_token: str
    item_id: str
    content_sha256: str
    expires_at_epoch: float


class _ImportPreviewTokenStore:
    """Short-lived opaque binding of project/result/item/structure content."""

    def __init__(self, *, ttl_seconds: int, max_records: int = 64,
                 clock=time.time):
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.max_records = max(1, min(int(max_records), 128))
        self._clock = clock
        self._records: dict[str, _ImportPreviewRecord] = {}
        self._consumed: dict[str, float] = {}
        self._lock = threading.RLock()

    def _cleanup(self) -> None:
        now = float(self._clock())
        for token, record in list(self._records.items()):
            if record.expires_at_epoch <= now:
                self._records.pop(token, None)
        for token, expires in list(self._consumed.items()):
            if expires <= now:
                self._consumed.pop(token, None)

    def issue(self, *, project_id: str, result_token: str, item_id: str,
              content_sha256: str) -> tuple[str, float]:
        with self._lock:
            self._cleanup()
            while len(self._records) >= self.max_records:
                oldest = min(
                    self._records,
                    key=lambda key: (self._records[key].expires_at_epoch, key))
                self._records.pop(oldest, None)
            token = "external-import-preview." + token_secrets.token_urlsafe(32)
            expires = float(self._clock()) + self.ttl_seconds
            self._records[token] = _ImportPreviewRecord(
                project_id=project_id, result_token=result_token,
                item_id=item_id, content_sha256=content_sha256,
                expires_at_epoch=expires)
            return token, expires

    def resolve(self, token: Any, *, project_id: str) -> _ImportPreviewRecord:
        text = str(token or "")
        if not _IMPORT_PREVIEW_TOKEN_RE.fullmatch(text):
            raise ExternalReferenceError(
                "invalid_token", "import preview token is invalid")
        with self._lock:
            self._cleanup()
            record = self._records.get(text)
            if record is None:
                if text in self._consumed:
                    raise ExternalReferenceError(
                        "token_replayed", "import preview token was already consumed")
                raise ExternalReferenceError(
                    "expired_token", "import preview token is invalid or expired")
            if not token_secrets.compare_digest(record.project_id, project_id):
                raise ExternalReferenceError(
                    "invalid_token", "import preview token project binding is invalid")
            return record

    def consume(self, token: Any) -> None:
        with self._lock:
            text = str(token or "")
            record = self._records.pop(text, None)
            if record is not None:
                self._consumed[text] = record.expires_at_epoch
                while len(self._consumed) > self.max_records:
                    oldest = min(
                        self._consumed,
                        key=lambda key: (self._consumed[key], key))
                    self._consumed.pop(oldest, None)


def _public_item(item: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "item_id", "kind", "source_id", "title", "formula", "chemical_system",
        "surface", "facet", "reactants", "products", "properties", "method",
        "structure_available", "structure_format", "publication", "citation",
        "license", "evidence_policy",
    )
    return {key: copy.deepcopy(item[key]) for key in allowed if key in item}


_PROJECT_IMPORT_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_IMPORT_LOCKS_GUARD = threading.Lock()


@contextmanager
def _project_import_lock(project_root: Path, *, timeout_seconds: float = 10.0):
    """Serialize candidate commits across threads and cooperating processes."""
    canonical_root = os.path.normcase(os.path.normpath(str(project_root)))
    key = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()
    with _PROJECT_IMPORT_LOCKS_GUARD:
        local_lock = _PROJECT_IMPORT_LOCKS.setdefault(key, threading.Lock())
    if not local_lock.acquire(timeout=max(0.1, float(timeout_seconds))):
        raise ExternalReferenceError(
            "candidate_import_busy", "candidate import lock is busy")
    lock_root = Path(tempfile.gettempdir()) / "vcstudio-external-reference-locks"
    handle = None
    locked = False
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
        handle = (lock_root / f"{key}.lock").open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised on non-Windows CI only
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ExternalReferenceError(
                        "candidate_import_busy", "candidate import lock is busy") from exc
                time.sleep(0.05)
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - exercised on non-Windows CI only
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        local_lock.release()


class ExternalReferenceGateway:
    """Read-only provider gateway and candidate-provenance import boundary."""

    def __init__(self, *, registry: ProviderRegistry | None = None,
                 transport: BoundedHttpTransport | None = None,
                 cache: ExternalReferenceCache | None = None,
                 secrets_store=None, network_enabled: bool = False,
                 token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
                 token_max_records: int = DEFAULT_TOKEN_RECORDS,
                 token_capacity_bytes: int = DEFAULT_TOKEN_CAPACITY_BYTES,
                 clock=time.time):
        from vcstudio.shared import secrets as shared_secrets

        self.registry = registry or ProviderRegistry()
        self.transport = transport or BoundedHttpTransport()
        self.cache = cache or ExternalReferenceCache(clock=clock)
        self.secrets_store = secrets_store or shared_secrets
        self._network_enabled = bool(network_enabled)
        self._clock = clock
        self._tokens = _ResultTokenStore(
            ttl_seconds=token_ttl_seconds,
            max_records=token_max_records,
            capacity_bytes=token_capacity_bytes,
            clock=clock)
        self._lock = threading.RLock()
        self._network_condition = threading.Condition(self._lock)
        self._network_epoch = 1
        self._network_in_flight = 0
        self._import_previews = _ImportPreviewTokenStore(
            ttl_seconds=token_ttl_seconds, max_records=token_max_records,
            clock=clock)
        self._adapters: dict[str, ProviderAdapter] = {
            "materials-project-rest": MaterialsProjectAdapter(),
            "optimade": OptimadeAdapter(),
            "catalysis-hub-graphql": CatalysisHubAdapter(),
        }

    @property
    def network_enabled(self) -> bool:
        with self._lock:
            return self._network_enabled

    def _credential(self, spec: ProviderSpec) -> str | None:
        if not spec.requires_api_key:
            return None
        try:
            value = self.secrets_store.get_external_reference_api_key(
                spec.provider_id)
        except Exception:
            return None
        return _valid_credential(spec.provider_id, value)

    def _acquire_network_lease(self) -> int:
        """Atomically check the switch and register one outbound operation."""
        with self._network_condition:
            if not self._network_enabled:
                raise ExternalReferenceError(
                    "network_disabled", "external reference networking is disabled")
            epoch = self._network_epoch
            self._network_in_flight += 1
            return epoch

    def _release_network_lease(self) -> None:
        with self._network_condition:
            self._network_in_flight = max(0, self._network_in_flight - 1)
            self._network_condition.notify_all()

    def _assert_network_lease_current(self, epoch: int) -> None:
        if not self._network_enabled or int(epoch) != self._network_epoch:
            raise ExternalReferenceError(
                "network_session_changed",
                "external network session changed while the request was in flight")

    def catalog(self) -> dict[str, Any]:
        with self._network_condition:
            network_enabled = self._network_enabled
            network_in_flight = self._network_in_flight
        providers = []
        for spec in self.registry.providers():
            providers.append(spec.public(
                network_enabled=network_enabled,
                credential_available=bool(self._credential(spec))))
        return {
            "schema": CATALOG_SCHEMA,
            "ok": True,
            "network_enabled": network_enabled,
            "network_default": "disabled",
            "network_state": (
                "enabled" if network_enabled else
                ("draining" if network_in_flight else "disabled")),
            "draining_requests": network_in_flight if not network_enabled else 0,
            "providers": providers,
            "request_contract": {
                "schema": SEARCH_SCHEMA,
                "browser_fields": ["provider", "filters"],
                "max_page_size": MAX_PAGE_SIZE,
                "max_page_number": MAX_PAGE_NUMBER,
                "timeout_seconds": self.transport.timeout_seconds,
                "max_response_bytes": self.transport.max_response_bytes,
                "forbidden": sorted(_FORBIDDEN_QUERY_KEYS),
            },
            "evidence_policy": _evidence_policy(),
        }

    def set_network_enabled(self, enabled: Any, confirmation: Any = None) -> dict[str, Any]:
        wanted = enabled is True
        if wanted and str(confirmation or "") != "enable-external-reference-network":
            return self._unavailable(
                "network_confirmation_required", "explicit network confirmation is required",
                schema=NETWORK_SCHEMA)
        with self._network_condition:
            if self._network_enabled != wanted:
                self._network_epoch += 1
            self._network_enabled = wanted
            draining = self._network_in_flight if not wanted else 0
        return {
            "schema": NETWORK_SCHEMA,
            "ok": True,
            "network_enabled": wanted,
            "persisted": False,
            "scope": "current_process",
            "network_state": (
                "enabled" if wanted else ("draining" if draining else "disabled")),
            "draining_requests": draining,
        }

    def store_api_key(self, provider_id: Any, api_key: Any) -> dict[str, Any]:
        try:
            spec = self.registry.get(provider_id)
            if not spec.requires_api_key:
                raise ExternalReferenceError(
                    "credential_not_supported", "provider does not use an API key")
            key = _valid_credential(spec.provider_id, api_key)
            if key is None:
                raise ExternalReferenceError(
                    "invalid_credential", "API key does not satisfy the key contract")
            saved = bool(self.secrets_store.set_external_reference_api_key(
                spec.provider_id, key))
            loaded = self.secrets_store.get_external_reference_api_key(
                spec.provider_id) if saved else None
            if not saved or not isinstance(loaded, str) or not token_secrets.compare_digest(loaded, key):
                return self._unavailable(
                    "keyring_unavailable", "system credential storage is unavailable",
                    provider_id=spec.provider_id, schema=CREDENTIAL_SCHEMA)
            return {
                "schema": CREDENTIAL_SCHEMA,
                "ok": True, "provider": spec.provider_id,
                "credential_available": True,
            }
        except ExternalReferenceError as exc:
            return self._unavailable(exc.code, str(exc), schema=CREDENTIAL_SCHEMA)
        except Exception:
            return self._unavailable(
                "keyring_unavailable", "system credential storage is unavailable",
                schema=CREDENTIAL_SCHEMA)

    def delete_api_key(self, provider_id: Any) -> dict[str, Any]:
        try:
            spec = self.registry.get(provider_id)
            self.secrets_store.delete_external_reference_api_key(spec.provider_id)
            return {
                "schema": CREDENTIAL_SCHEMA,
                "ok": True, "provider": spec.provider_id,
                "credential_available": False,
            }
        except ExternalReferenceError as exc:
            return self._unavailable(exc.code, str(exc), schema=CREDENTIAL_SCHEMA)
        except Exception:
            return self._unavailable(
                "keyring_unavailable", "system credential storage is unavailable",
                schema=CREDENTIAL_SCHEMA)

    @staticmethod
    def _unavailable(code: str, message: str, *, provider_id: str | None = None,
                     query: Mapping[str, Any] | None = None,
                     retryable: bool = False,
                     schema: str = RESULT_SCHEMA) -> dict[str, Any]:
        return {
            "schema": schema,
            "ok": False,
            "status": "unavailable",
            "provider": provider_id,
            "query": copy.deepcopy(dict(query)) if query is not None else None,
            "items": [],
            "result_token": None,
            "expires_at": None,
            "error": {
                "code": str(code),
                "message": _safe_external_text(message, maximum=200),
                "retryable": bool(retryable),
            },
        }

    @staticmethod
    def _http_failure(response: TransportResponse) -> tuple[str, str, bool] | None:
        if 200 <= response.status < 300:
            return None
        if response.status == 429:
            return "rate_limited", "external provider rate limit reached", True
        if response.status in {401, 403}:
            return "credential_rejected", "external provider rejected credentials", False
        if response.status in {408, 425} or response.status >= 500:
            return "provider_unavailable", "external provider is unavailable", True
        return "provider_error", "external provider rejected the bounded request", False

    @staticmethod
    def _cache_metadata(spec: ProviderSpec, query: Mapping[str, Any], *,
                        status: str, provider_version: str,
                        items: Sequence[Mapping[str, Any]],
                        response_metadata: Mapping[str, Any] | None = None,
                        error_code: str | None = None) -> dict[str, Any]:
        dois = sorted({doi for item in items for doi in
                       ((item.get("citation") or {}).get("dois") or []) if doi})
        methods = [copy.deepcopy(item.get("method")) for item in items
                   if isinstance(item.get("method"), Mapping)]
        return {
            "provider": spec.provider_id,
            "provider_protocol": spec.protocol,
            "provider_version": provider_version,
            "endpoint_identity": spec.endpoint_identity,
            "adapter_version": spec.adapter_version,
            "query": copy.deepcopy(dict(query)),
            "query_sha256": hashlib.sha256(_canonical_json(query)).hexdigest(),
            "status": status,
            "error_code": error_code,
            "license": {"id": spec.license_id, "url": spec.license_url},
            "attribution": spec.attribution,
            "citation_url": spec.citation_url,
            "dois": dois,
            "method_metadata": methods,
            "response_metadata": copy.deepcopy(dict(response_metadata or {})),
        }

    def search(self, provider_id: Any, filters: Any) -> dict[str, Any]:
        try:
            spec = self.registry.get(provider_id)
            query = normalize_query(spec, filters)
        except ExternalReferenceError as exc:
            return self._unavailable(exc.code, str(exc))
        adapter = self._adapters[spec.protocol]
        try:
            lease_epoch = self._acquire_network_lease()
        except ExternalReferenceError as exc:
            return self._unavailable(
                exc.code, str(exc), provider_id=spec.provider_id, query=query)
        try:
            api_key = self._credential(spec)
            if spec.requires_api_key and not api_key:
                return self._unavailable(
                    "credential_required", "provider API key is not available",
                    provider_id=spec.provider_id, query=query)
            outbound = adapter.build_request(spec, query, api_key)
            response = self.transport.request(outbound)
            try:
                decoded = _json_object(response.body)
            except ExternalReferenceError:
                decoded = None
            if _credential_echoed(api_key, response, decoded):
                return self._unavailable(
                    "credential_echo_detected",
                    "external provider response echoed a credential",
                    provider_id=spec.provider_id, query=query)
            http_failure = self._http_failure(response)
            if http_failure:
                code, message, retryable = http_failure
                return self._unavailable(
                    code, message, provider_id=spec.provider_id,
                    query=query, retryable=retryable)
            source_metadata = {
                "source_response_sha256": hashlib.sha256(response.body).hexdigest(),
                "source_response_size": len(response.body),
                "cache_representation": (
                    "credential-scrubbed-json" if api_key else "provider-response-bytes"),
            }
            cache_payload = (
                _canonical_json(_scrub_credentialed_cache_json(decoded))
                if api_key and decoded is not None else
                (None if api_key else response.body))
            try:
                parsed = adapter.parse_response(spec, query, response)
            except ExternalReferenceError as exc:
                metadata = self._cache_metadata(
                    spec, query, status="unavailable",
                    provider_version=spec.adapter_version, items=(),
                    error_code=exc.code)
                metadata.update(source_metadata)
                try:
                    if (cache_payload is not None
                            and not _credential_echoed(
                                api_key, response, decoded, metadata)):
                        with self._network_condition:
                            self._assert_network_lease_current(lease_epoch)
                            self.cache.write(cache_payload, metadata)
                except Exception:
                    pass
                return self._unavailable(
                    exc.code, str(exc), provider_id=spec.provider_id, query=query)
            metadata = self._cache_metadata(
                spec, query, status="available",
                provider_version=parsed.provider_version,
                items=parsed.items,
                response_metadata=parsed.response_metadata)
            metadata.update(source_metadata)
            if _credential_echoed(
                    api_key, response, decoded, parsed.items,
                    parsed.response_metadata, metadata):
                return self._unavailable(
                    "credential_echo_detected",
                    "external provider response echoed a credential",
                    provider_id=spec.provider_id, query=query)
            with self._network_condition:
                self._assert_network_lease_current(lease_epoch)
                cache_manifest = self.cache.write(cache_payload, metadata)
                if _credential_echoed(api_key, response, None, cache_manifest):
                    self.cache._remove_entry(str(cache_manifest["entry_id"]))
                    raise ExternalReferenceError(
                        "credential_echo_detected",
                        "external provider response echoed a credential")
                token, expires_epoch = self._tokens.issue(
                    provider_id=spec.provider_id,
                    cache_entry_id=str(cache_manifest["entry_id"]),
                    query=query, cache_metadata=cache_manifest,
                    items=parsed.items)
            return {
                "schema": RESULT_SCHEMA,
                "ok": True,
                "status": "available",
                "provider": spec.provider_id,
                "provider_version": parsed.provider_version,
                "endpoint_identity": spec.endpoint_identity,
                "query": copy.deepcopy(query),
                "page": query["page"],
                "limit": query["limit"],
                "total_count": parsed.total_count,
                "more_available": parsed.more_available,
                "items": [_public_item(item) for item in parsed.items],
                "result_token": token,
                "expires_at": _utc_now(expires_epoch),
                "retrieved_at": cache_manifest["retrieved_at"],
                "license": copy.deepcopy(metadata["license"]),
                "attribution": spec.attribution,
                "citation_url": spec.citation_url,
                "evidence_policy": _evidence_policy(),
                "error": None,
            }
        except ExternalReferenceError as exc:
            return self._unavailable(
                exc.code, str(exc), provider_id=spec.provider_id, query=query)
        except TransportFailure as exc:
            return self._unavailable(
                exc.code, str(exc), provider_id=spec.provider_id,
                query=query, retryable=exc.retryable)
        except Exception:
            return self._unavailable(
                "provider_unavailable", "external provider operation failed",
                provider_id=spec.provider_id, query=query, retryable=True)
        finally:
            self._release_network_lease()

    @staticmethod
    def _select_item(record: _TokenRecord, item_id: Any) -> dict[str, Any]:
        identifier = str(item_id or "").strip()
        if not _ITEM_ID_RE.fullmatch(identifier):
            raise ExternalReferenceError("invalid_item", "result item id is invalid")
        matches = [item for item in record.items if item.get("item_id") == identifier]
        if len(matches) != 1:
            raise ExternalReferenceError("invalid_item", "result item is missing or ambiguous")
        return copy.deepcopy(matches[0])

    def preview_structure_candidate(
            self, result_token: Any,
            item_id: Any) -> ExternalStructureCandidate:
        """Resolve one path-free structure without consuming import authority."""
        record = self._tokens.resolve(result_token)
        item = self._select_item(record, item_id)
        if not item.get("structure_available") or item.get("_structure_payload") is None:
            raise ExternalReferenceError(
                "structure_unavailable", "selected external result has no importable structure")
        formula = str(item.get("formula") or "")
        structure_format = str(item.get("structure_format") or "external-json")
        structure_payload = _sanitize_structure_payload(
            copy.deepcopy(item["_structure_payload"]))
        content_sha256 = _structure_content_sha256(
            formula, structure_format, structure_payload)
        if not token_secrets.compare_digest(
                content_sha256, str(item.get("_structure_content_sha256") or "")):
            raise ExternalReferenceError(
                "structure_changed", "external structure content binding is invalid")
        preview_id = "external-preview-" + hashlib.sha256(
            f"{result_token}:{item.get('item_id')}:{content_sha256}".encode("utf-8")
        ).hexdigest()[:24]
        cache_meta = record.cache_metadata
        provenance = {
            "schema": CANDIDATE_SCHEMA,
            "candidate_id": preview_id,
            "provider": record.provider_id,
            "source_id": item.get("source_id"),
            "item_id": item.get("item_id"),
            "formula": formula,
            "structure_content_sha256": content_sha256,
            "query": copy.deepcopy(dict(record.query)),
            "endpoint_identity": cache_meta.get("endpoint_identity"),
            "provider_version": cache_meta.get("provider_version"),
            "adapter_version": cache_meta.get("adapter_version"),
            "retrieved_at": cache_meta.get("retrieved_at"),
            "raw_response_sha256": cache_meta.get("raw_response_sha256"),
            "license": copy.deepcopy(item.get("license")),
            "citation": copy.deepcopy(item.get("citation")),
            "method": copy.deepcopy(item.get("method")),
            "attribution": cache_meta.get("attribution"),
        }
        return ExternalStructureCandidate(
            candidate_id=preview_id,
            provider_id=record.provider_id,
            source_id=str(item.get("source_id") or ""),
            structure_format=structure_format,
            structure_payload=structure_payload,
            provenance=provenance,
            evidence_policy=_evidence_policy(),
        )

    def claim_structure_candidate(
            self, result_token: Any, item_id: Any, *,
            confirmed: bool) -> ExternalStructureCandidate:
        if confirmed is not True:
            raise ExternalReferenceError(
                "confirmation_required", "candidate import requires explicit confirmation")
        reservation = self._tokens.reserve_import(result_token)
        try:
            candidate = self._candidate_from_preview(
                self.preview_structure_candidate(result_token, item_id))
            self._tokens.commit_import(result_token, reservation)
            return candidate
        except Exception:
            self._tokens.release_import(result_token, reservation)
            raise

    @staticmethod
    def _candidate_from_preview(
            preview: ExternalStructureCandidate) -> ExternalStructureCandidate:
        candidate_id = "external-candidate-" + token_secrets.token_hex(12)
        provenance = copy.deepcopy(dict(preview.provenance))
        provenance["candidate_id"] = candidate_id
        return ExternalStructureCandidate(
            candidate_id=candidate_id,
            provider_id=preview.provider_id,
            source_id=preview.source_id,
            structure_format=preview.structure_format,
            structure_payload=copy.deepcopy(preview.structure_payload),
            provenance=provenance,
            evidence_policy=copy.deepcopy(dict(preview.evidence_policy)),
        )

    def prepare_candidate_import(self, result_token: Any, item_id: Any, *,
                                 project_id: Any) -> dict[str, Any]:
        """Issue a path-free, project/content-bound preview before confirmation."""
        try:
            identifier = str(project_id or "").strip().lower()
            if not _PROJECT_ID_RE.fullmatch(identifier):
                raise ExternalReferenceError(
                    "invalid_project_identity", "project identity is invalid")
            self._tokens.resolve(result_token, for_import=True)
            preview = self.preview_structure_candidate(result_token, item_id)
            content_sha256 = str(
                preview.provenance.get("structure_content_sha256") or "")
            if not re.fullmatch(r"[a-f0-9]{64}", content_sha256):
                raise ExternalReferenceError(
                    "structure_changed", "external structure content binding is invalid")
            token, expires = self._import_previews.issue(
                project_id=identifier, result_token=str(result_token),
                item_id=str(preview.provenance.get("item_id") or ""),
                content_sha256=content_sha256)
            sites = 0
            if isinstance(preview.structure_payload, Mapping):
                if preview.provider_id == "materials_project":
                    raw_sites = preview.structure_payload.get("sites")
                else:
                    raw_sites = preview.structure_payload.get("species_at_sites")
                sites = len(raw_sites) if isinstance(raw_sites, list) else 0
            return {
                "schema": IMPORT_PREVIEW_SCHEMA,
                "ok": True,
                "status": "preview",
                "project_id": identifier,
                "preview_token": token,
                "expires_at": _utc_now(expires),
                "provider": preview.provider_id,
                "source_id": preview.source_id,
                "formula": preview.provenance.get("formula"),
                "structure_format": preview.structure_format,
                "site_count": sites,
                "content_sha256": content_sha256,
                "evidence_policy": copy.deepcopy(dict(preview.evidence_policy)),
                "error": None,
            }
        except ExternalReferenceError as exc:
            return self._unavailable(
                exc.code, str(exc), schema=IMPORT_PREVIEW_SCHEMA)

    @staticmethod
    def _validate_import_identity(
            validator: Callable[[], bool] | None) -> None:
        if validator is None:
            return
        try:
            valid = validator()
        except Exception as exc:
            raise ExternalReferenceError(
                "identity_mismatch", "project identity changed during import") from exc
        if valid is not True:
            raise ExternalReferenceError(
                "identity_mismatch", "project identity changed during import")

    @staticmethod
    def _preflight_import_paths(root: Path) -> tuple[Path, Path]:
        state_root = root / ".vcstudio"
        destination = state_root / "external_reference_candidates"
        for path in (state_root, destination):
            if path.is_symlink():
                raise ExternalReferenceError(
                    "candidate_import_failed",
                    "candidate provenance destination is not trusted")
            if path.exists() and not path.is_dir():
                raise ExternalReferenceError(
                    "candidate_import_failed",
                    "candidate provenance destination is not a directory")
        for path in (state_root, destination):
            if not path.resolve().is_relative_to(root):
                raise ExternalReferenceError(
                    "candidate_import_failed",
                    "candidate provenance destination is not trusted")
        return state_root, destination

    def import_candidate(self, preview_token: Any, *,
                         project_root: str | os.PathLike[str], project_id: str,
                         confirmed: bool,
                         identity_validator: Callable[[], bool] | None = None) -> dict[str, Any]:
        try:
            if confirmed is not True:
                raise ExternalReferenceError(
                    "confirmation_required", "candidate import requires explicit confirmation")
            identifier = str(project_id or "").strip().lower()
            if not _PROJECT_ID_RE.fullmatch(identifier):
                raise ExternalReferenceError(
                    "invalid_project_identity", "project identity is invalid")
            root = Path(project_root).expanduser().resolve()
            if root.parent == root or not root.is_dir():
                raise ExternalReferenceError(
                    "project_unavailable", "project root is unavailable")
            self._validate_import_identity(identity_validator)
            self._preflight_import_paths(root)
            binding = self._import_previews.resolve(
                preview_token, project_id=identifier)
            reservation = None
            temp: Path | None = None
            target: Path | None = None
            created_state = False
            created_destination = False
            committed_file = False
            token_committed = False
            try:
                with _project_import_lock(root):
                    self._validate_import_identity(identity_validator)
                    state_root, destination = self._preflight_import_paths(root)
                    preview = self.preview_structure_candidate(
                        binding.result_token, binding.item_id)
                    content_sha256 = str(
                        preview.provenance.get("structure_content_sha256") or "")
                    if not token_secrets.compare_digest(
                            binding.content_sha256, content_sha256):
                        raise ExternalReferenceError(
                            "structure_changed", "external structure changed after preview")
                    reservation = self._tokens.reserve_import(binding.result_token)
                    candidate = self._candidate_from_preview(preview)
                    self._validate_import_identity(identity_validator)
                    self._preflight_import_paths(root)
                    if not state_root.exists():
                        state_root.mkdir()
                        created_state = True
                    if not destination.exists():
                        destination.mkdir()
                        created_destination = True
                    self._preflight_import_paths(root)
                    target = destination / f"{candidate.candidate_id}.json"
                    temp = destination / (
                        f".{candidate.candidate_id}.{token_secrets.token_hex(8)}.tmp")
                    if target.exists() or target.is_symlink():
                        raise ExternalReferenceError(
                            "candidate_import_failed", "candidate identity already exists")
                    payload = {
                        "schema": CANDIDATE_SCHEMA,
                        "candidate_id": candidate.candidate_id,
                        "project_id": identifier,
                        "status": "candidate_only",
                        "source": {
                            "provider": candidate.provider_id,
                            "source_id": candidate.source_id,
                        },
                        "structure": {
                            "format": candidate.structure_format,
                            "payload": candidate.structure_payload,
                        },
                        "provenance": copy.deepcopy(dict(candidate.provenance)),
                        "evidence_policy": copy.deepcopy(dict(candidate.evidence_policy)),
                        "imported_at": _utc_now(float(self._clock())),
                    }
                    with temp.open("xb") as handle:
                        handle.write(_canonical_json(payload))
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._validate_import_identity(identity_validator)
                    self._preflight_import_paths(root)
                    if target.exists() or target.is_symlink():
                        raise ExternalReferenceError(
                            "candidate_import_failed", "candidate identity already exists")
                    os.replace(temp, target)
                    temp = None
                    committed_file = True
                    self._validate_import_identity(identity_validator)
                    self._tokens.commit_import(binding.result_token, reservation)
                    token_committed = True
                    self._import_previews.consume(preview_token)
            except Exception:
                if committed_file and target is not None and not token_committed:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass
                if reservation is not None and not token_committed:
                    self._tokens.release_import(binding.result_token, reservation)
                raise
            finally:
                if temp is not None:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
                if not token_committed:
                    if created_destination:
                        try:
                            destination.rmdir()
                        except OSError:
                            pass
                    if created_state:
                        try:
                            state_root.rmdir()
                        except OSError:
                            pass
            return {
                "schema": CANDIDATE_SCHEMA,
                "ok": True,
                "status": "candidate_only",
                "candidate_id": candidate.candidate_id,
                "project_id": identifier,
                "provider": candidate.provider_id,
                "source_id": candidate.source_id,
                "structure_format": candidate.structure_format,
                "evidence_policy": copy.deepcopy(dict(candidate.evidence_policy)),
                "error": None,
            }
        except ExternalReferenceError as exc:
            return self._unavailable(exc.code, str(exc), schema=CANDIDATE_SCHEMA)
        except Exception:
            return self._unavailable(
                "candidate_import_failed", "candidate provenance could not be persisted",
                schema=CANDIDATE_SCHEMA)

    def compare(self, result_token: Any, item_ids: Any) -> dict[str, Any]:
        try:
            if isinstance(item_ids, (str, bytes)) or not isinstance(item_ids, Sequence):
                raise ExternalReferenceError(
                    "invalid_item", "item_ids must be an array")
            identifiers = [str(item or "").strip() for item in item_ids]
            if (not identifiers or len(identifiers) > 20
                    or len(identifiers) != len(set(identifiers))):
                raise ExternalReferenceError(
                    "invalid_item", "item_ids must contain 1 to 20 unique ids")
            record = self._tokens.resolve(result_token)
            items = [self._select_item(record, identifier) for identifier in identifiers]
            return {
                "schema": COMPARE_SCHEMA,
                "ok": True,
                "status": "side_by_side_only",
                "provider": record.provider_id,
                "external_items": [_public_item(item) for item in items],
                "aggregate": None,
                "evidence_policy": _evidence_policy(),
                "limitations": [
                    "External values are not method-compatible by default.",
                    "External values cannot enter ValidationResult or final claims.",
                ],
                "error": None,
            }
        except ExternalReferenceError as exc:
            return self._unavailable(exc.code, str(exc), schema=COMPARE_SCHEMA)


def _source_query_filters(provider_id: str, query: Any) -> dict[str, Any]:
    """Map Structure Source Hub's text box to a tiny scientific query grammar."""
    if provider_id not in {"materials_project", "optimade"}:
        raise ExternalReferenceError(
            "unknown_provider", "provider does not expose importable structures")
    if not isinstance(query, str):
        raise ExternalReferenceError("invalid_query", "structure query must be text")
    text = query.strip()
    if (not text or len(text) > 64 or _CONTROL_RE.search(text)
            or _LOCAL_PATH_RE.search(text) or _SECRET_RE.search(text)
            or not re.fullmatch(r"[A-Za-z0-9*().,+\-\s]{1,64}", text)):
        raise ExternalReferenceError(
            "invalid_query", "structure query contains unsupported characters")
    filters: dict[str, Any] = {"page": 1, "limit": 20}
    if provider_id == "materials_project" and _MATERIAL_ID_RE.fullmatch(text.lower()):
        filters["material_ids"] = [text.lower()]
        return filters
    comma_parts = [part.strip() for part in re.split(r"[,\s]+", text) if part.strip()]
    if len(comma_parts) > 1 and all(part in _ELEMENTS for part in comma_parts):
        filters["elements"] = comma_parts
        return filters
    hyphen_parts = text.split("-")
    if len(hyphen_parts) > 1 and all(part in _ELEMENTS for part in hyphen_parts):
        if provider_id == "materials_project":
            filters["chemsys"] = text
        else:
            filters["elements"] = hyphen_parts
        return filters
    if not _FORMULA_RE.fullmatch(text):
        raise ExternalReferenceError(
            "invalid_query", "structure query is not a formula, element set, or material id")
    filters["formula"] = text
    return filters


def _poscar_number(value: Any) -> str:
    number = _finite_number(value)
    if number is None:
        raise ExternalReferenceError(
            "structure_unavailable", "structure contains a non-finite coordinate")
    return f"{number:.16g}"


def _group_structure_sites(elements: Sequence[str], coordinates: Sequence[Sequence[Any]]) -> tuple[list[str], list[int], list[list[Any]]]:
    if len(elements) != len(coordinates) or not elements:
        raise ExternalReferenceError(
            "structure_unavailable", "structure site arrays do not match")
    order: list[str] = []
    grouped: dict[str, list[list[Any]]] = {}
    for element, coordinate in zip(elements, coordinates):
        if element not in _ELEMENTS:
            raise ExternalReferenceError(
                "structure_unavailable", "structure contains an unsupported species")
        vector = _vector3(list(coordinate), field_name="structure coordinate")
        if element not in grouped:
            order.append(element)
            grouped[element] = []
        grouped[element].append(vector)
    counts = [len(grouped[element]) for element in order]
    flattened = [vector for element in order for vector in grouped[element]]
    return order, counts, flattened


def _render_poscar(comment: str, lattice: Sequence[Sequence[Any]],
                   elements: Sequence[str], counts: Sequence[int],
                   coordinates: Sequence[Sequence[Any]], *, direct: bool) -> str:
    clean_lattice = _matrix3(list(lattice), field_name="structure lattice")
    lines = [
        _safe_external_text(comment, maximum=120) or "External reference structure",
        "1.0",
    ]
    lines.extend("  ".join(_poscar_number(value) for value in vector)
                 for vector in clean_lattice)
    lines.append("  ".join(elements))
    lines.append("  ".join(str(int(count)) for count in counts))
    lines.append("Direct" if direct else "Cartesian")
    lines.extend("  ".join(_poscar_number(value) for value in vector)
                 for vector in coordinates)
    return "\n".join(lines) + "\n"


def _materials_project_poscar(candidate: ExternalStructureCandidate) -> str:
    payload = candidate.structure_payload
    if not isinstance(payload, Mapping):
        raise ExternalReferenceError(
            "structure_unavailable", "Materials Project structure is unavailable")
    lattice = payload.get("lattice")
    sites = payload.get("sites")
    if not isinstance(lattice, Mapping) or not isinstance(sites, list):
        raise ExternalReferenceError(
            "structure_unavailable", "Materials Project structure is incomplete")
    elements = []
    fractional = []
    cartesian = []
    has_fractional = True
    has_cartesian = True
    for site in sites:
        if not isinstance(site, Mapping):
            raise ExternalReferenceError(
                "structure_unavailable", "Materials Project site is incomplete")
        element = ""
        species = site.get("species")
        if isinstance(species, list):
            if len(species) != 1 or not isinstance(species[0], Mapping):
                raise ExternalReferenceError(
                    "structure_unavailable", "disordered structures require explicit review")
            element = str(species[0].get("element") or "")
            occupancy = _finite_number(species[0].get("occu"))
            if occupancy is None or abs(occupancy - 1.0) > 1e-9:
                raise ExternalReferenceError(
                    "structure_unavailable", "partial occupancies require explicit review")
        if not element:
            label = str(site.get("label") or "")
            element = label if label in _ELEMENTS else ""
        if element not in _ELEMENTS:
            raise ExternalReferenceError(
                "structure_unavailable", "Materials Project site species is ambiguous")
        elements.append(element)
        if isinstance(site.get("abc"), list):
            fractional.append(site["abc"])
        else:
            has_fractional = False
        if isinstance(site.get("xyz"), list):
            cartesian.append(site["xyz"])
        else:
            has_cartesian = False
    if has_fractional:
        order, counts, coords = _group_structure_sites(elements, fractional)
        direct = True
    elif has_cartesian:
        order, counts, coords = _group_structure_sites(elements, cartesian)
        direct = False
    else:
        raise ExternalReferenceError(
            "structure_unavailable", "Materials Project coordinates are incomplete")
    return _render_poscar(
        f"Materials Project {candidate.source_id}", lattice.get("matrix"),
        order, counts, coords, direct=direct)


def _optimade_poscar(candidate: ExternalStructureCandidate) -> str:
    payload = candidate.structure_payload
    if not isinstance(payload, Mapping):
        raise ExternalReferenceError(
            "structure_unavailable", "OPTIMADE structure is unavailable")
    definitions = {}
    for definition in payload.get("species") or []:
        if not isinstance(definition, Mapping):
            raise ExternalReferenceError(
                "structure_unavailable", "OPTIMADE species definition is invalid")
        name = str(definition.get("name") or "")
        symbols = definition.get("chemical_symbols")
        concentrations = definition.get("concentration")
        if (not name or not isinstance(symbols, list) or len(symbols) != 1
                or not isinstance(concentrations, list) or len(concentrations) != 1
                or symbols[0] not in _ELEMENTS
                or _finite_number(concentrations[0]) is None
                or abs(float(concentrations[0]) - 1.0) > 1e-9):
            raise ExternalReferenceError(
                "structure_unavailable", "disordered OPTIMADE species require explicit review")
        definitions[name] = str(symbols[0])
    elements = []
    for raw in payload.get("species_at_sites") or []:
        name = str(raw or "")
        element = name if name in _ELEMENTS else definitions.get(name, "")
        if element not in _ELEMENTS:
            raise ExternalReferenceError(
                "structure_unavailable", "OPTIMADE site species is ambiguous")
        elements.append(element)
    order, counts, coords = _group_structure_sites(
        elements, payload.get("cartesian_site_positions") or [])
    return _render_poscar(
        f"OPTIMADE {candidate.source_id}", payload.get("lattice_vectors"),
        order, counts, coords, direct=False)


@dataclass
class _StructureSourceAdapterRecord:
    created_at: float
    expires_at: float
    preview: Mapping[str, Any]


class StructureSourceGatewayAdapter:
    """Compatibility adapter for Structure Source Hub's frozen narrow Protocol.

    The adapter intentionally exposes only ``capabilities/search/preview``.
    Search text is mapped to formula/element/material-ID filters; it is never
    treated as a URL, OPTIMADE filter expression or GraphQL document.
    """

    _TOKEN_RE = re.compile(r"^external-structure\.[A-Za-z0-9_-]{32,96}$")

    def __init__(self, gateway: ExternalReferenceGateway, *,
                 ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
                 max_records: int = 100, clock=time.time):
        if not isinstance(gateway, ExternalReferenceGateway):
            raise TypeError("gateway must be an ExternalReferenceGateway")
        self.gateway = gateway
        self.ttl_seconds = max(30, min(
            int(ttl_seconds), int(gateway._tokens.ttl_seconds)))
        self.max_records = max(1, min(int(max_records), 100))
        self._clock = clock
        self._records: dict[str, _StructureSourceAdapterRecord] = {}
        self._lock = threading.RLock()

    def _prune(self) -> None:
        now = float(self._clock())
        for token, record in list(self._records.items()):
            if record.expires_at <= now:
                self._records.pop(token, None)

    def capabilities(self) -> list[dict[str, Any]]:
        catalog = self.gateway.catalog()
        capabilities = []
        for provider in catalog.get("providers") or []:
            if provider.get("id") not in {"materials_project", "optimade"}:
                continue
            capabilities.append({
                "provider": provider["id"],
                "label": {
                    "zh": str(provider.get("label_zh") or provider["id"]),
                    "en": str(provider.get("label_en") or provider["id"]),
                },
                "modes": ["formula", "elements", "material_id"],
                "formats": ["poscar"],
                "network": True,
                "enabled": provider.get("status") == "available",
            })
        return capabilities

    @staticmethod
    def _result_metadata(provider_id: str, search: Mapping[str, Any],
                         candidate: ExternalStructureCandidate,
                         poscar: str) -> dict[str, Any]:
        provenance = candidate.provenance
        license_record = provenance.get("license") if isinstance(
            provenance.get("license"), Mapping) else {}
        citation_record = provenance.get("citation") if isinstance(
            provenance.get("citation"), Mapping) else {}
        license_result = {
            "name": str(license_record.get("id") or "unknown"),
        }
        if license_record.get("id") == "CC-BY-4.0":
            license_result["spdx_id"] = "CC-BY-4.0"
        if license_record.get("url"):
            license_result["url"] = str(license_record["url"])
        citation_result = {
            "text": str(
                citation_record.get("attribution")
                or provenance.get("attribution") or provider_id),
        }
        dois = citation_record.get("dois") if isinstance(
            citation_record.get("dois"), list) else []
        if dois:
            citation_result["doi"] = str(dois[0])
        if citation_record.get("url"):
            citation_result["url"] = str(citation_record["url"])
        formula = str(provenance.get("formula") or "")
        if not _FORMULA_RE.fullmatch(formula):
            raise ExternalReferenceError(
                "structure_unavailable", "external structure formula is invalid")
        return {
            "source_id": str(provenance.get("item_id") or candidate.source_id),
            "formula": formula,
            "license": license_result,
            "citation": citation_result,
            "method": {
                "provider": provider_id,
                "endpoint_identity": str(search.get("endpoint_identity") or "unknown"),
                "provider_version": str(search.get("provider_version") or "unknown"),
                "format": "poscar",
                "evidence_role": "external_reference",
            },
            "raw_structure_sha256": hashlib.sha256(
                poscar.encode("utf-8")).hexdigest(),
        }

    def search(self, provider: str, query: str) -> list[dict[str, Any]]:
        provider_id = str(provider or "").strip().lower()
        filters = _source_query_filters(provider_id, query)
        search = self.gateway.search(provider_id, filters)
        if not search.get("ok"):
            error = search.get("error") if isinstance(search.get("error"), Mapping) else {}
            raise ExternalReferenceError(
                str(error.get("code") or "provider_unavailable"),
                str(error.get("message") or "external structure provider is unavailable"))
        prepared = []
        for item in search.get("items") or []:
            if not isinstance(item, Mapping) or item.get("structure_available") is not True:
                continue
            candidate = self.gateway.preview_structure_candidate(
                search["result_token"], item.get("item_id"))
            if provider_id == "materials_project":
                poscar = _materials_project_poscar(candidate)
            else:
                poscar = _optimade_poscar(candidate)
            metadata = self._result_metadata(
                provider_id, search, candidate, poscar)
            token = "external-structure." + token_secrets.token_urlsafe(32)
            prepared.append((token, metadata, {
                "token": token,
                "source_id": metadata["source_id"],
                "formula": metadata["formula"],
                "raw_structure_sha256": metadata["raw_structure_sha256"],
                "poscar": poscar,
            }))
        with self._lock:
            self._prune()
            now = float(self._clock())
            while self._records and len(self._records) + len(prepared) > self.max_records:
                oldest = min(
                    self._records,
                    key=lambda key: (self._records[key].created_at, key))
                self._records.pop(oldest, None)
            if len(prepared) > self.max_records:
                raise ExternalReferenceError(
                    "result_too_large", "structure source result exceeds token capacity")
            results = []
            for token, metadata, preview in prepared:
                self._records[token] = _StructureSourceAdapterRecord(
                    created_at=now, expires_at=now + self.ttl_seconds,
                    preview=copy.deepcopy(preview))
                results.append({"token": token, **copy.deepcopy(metadata)})
        return results

    def preview(self, token: str) -> dict[str, Any]:
        text = str(token or "")
        if not self._TOKEN_RE.fullmatch(text):
            raise ExternalReferenceError(
                "invalid_token", "structure source token is invalid or expired")
        with self._lock:
            self._prune()
            record = self._records.get(text)
            if record is None:
                raise ExternalReferenceError(
                    "expired_token", "structure source token is invalid or expired")
            return copy.deepcopy(dict(record.preview))


__all__ = [
    "APPLICATION_USER_AGENT", "AdapterResult", "BoundedHttpTransport", "CATALOG_SCHEMA",
    "CANDIDATE_SCHEMA", "COMPARE_SCHEMA", "CatalysisHubAdapter",
    "ExternalReferenceCache", "ExternalReferenceError",
    "ExternalReferenceGateway", "ExternalReferenceProtocol",
    "ExternalStructureCandidate", "MaterialsProjectAdapter",
    "OptimadeAdapter", "OutboundRequest", "ProviderRegistry",
    "IMPORT_PREVIEW_SCHEMA", "ProviderSpec", "RESULT_SCHEMA", "SEARCH_SCHEMA",
    "StructureSourceGatewayAdapter", "TransportFailure",
    "TransportResponse", "normalize_query",
]
