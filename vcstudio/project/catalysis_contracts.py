"""Versioned, path-free catalysis domain contracts.

These DTOs describe scientific intent and logical provenance.  They do not
replace ``job.yaml`` (the authoritative record for an executed job), inspect
the filesystem, hold credentials, or grant execution authority.  Every model
uses opaque identifiers, typed evidence references, and a method fingerprint;
``observed``, ``imported``, and ``inferred`` provenance are never implicit.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, ClassVar

from vcstudio.shared.credential_classifier import is_sensitive_key, looks_like_credential


MODEL_VERSION = "1.0.0"
DOMAIN_ENVELOPE_SCHEMA = "vcstudio.catalysis-domain-envelope/v2"
WORKFLOW_RUN_SCHEMA = "vcstudio.workflow-run-snapshot/v1"

PROVENANCE_KINDS = ("observed", "imported", "inferred")
EVIDENCE_TYPES = (
    "job_manifest",
    "structure_record",
    "calculation_result",
    "experimental_record",
    "publication_record",
    "imported_record",
    "human_review",
    "method_record",
)
PARAMETER_SOURCES = ("recipe_default", "user_override")
PARTICIPANT_PHASES = (
    "gas", "liquid", "aqueous", "solid", "adsorbed", "surface", "electron",
)

_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]{0,159}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SEMVER_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_PARAMETER_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_FORMULA_RE = re.compile(r"[A-Za-z0-9()+\-.*]{1,120}\Z")
_ABS_WINDOWS_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[a-z]:[\\/]|\\\\[^\\/\s]+[\\/])")
_ABS_POSIX_RE = re.compile(r"(?<![\w:/])/(?!/)[^\s,;，；]+")
_FILE_URI_RE = re.compile(r"(?i)\bfile:(?:/{0,3}|\\)")
_PRIVATE_KEYS = frozenset({
    "path", "paths", "root", "roots", "dir", "dirs", "directory",
    "directories", "locator", "locators", "destination", "destinations",
    "browser_path", "project_path", "job_path", "output_path", "out_dir",
    "password", "passwd", "secret", "token", "credential", "credentials",
    "authorization", "cookie", "cookies", "private_key", "api_key",
    "access_key", "command", "remote_host",
    "apikey", "privatekey", "accesskey", "projectpath", "jobpath",
    "outputpath", "browserpath", "remotehost",
})
_PRIVATE_KEY_SUFFIXES = (
    "_path", "_paths", "_dir", "_dirs", "_directory", "_directories",
    "_root", "_roots", "_locator", "_locators", "_destination", "_destinations",
    "_secret", "_secrets", "_password", "_passwd", "_token", "_tokens",
    "_credential", "_credentials", "_cookie", "_cookies", "_private_key",
    "_api_key", "_access_key", "_authorization", "_command", "_remote_host",
)
_PRIVATE_COLLAPSED_SUFFIXES = (
    "browserpath", "projectpath", "jobpath", "outputpath", "remotepath",
    "browserdirectory", "projectroot", "jobroot", "outputroot", "remoteroot",
    "apikey", "privatekey", "accesskey", "clientsecret", "remotetoken",
    "password", "passwd", "credential", "authorization", "remotehost",
)
_PUBLIC_BOUNDARY_KEYS = frozenset({
    "creates_directories", "creates_jobs", "remote_side_effects",
    "job_source_of_truth", "official_reference_urls",
})


class CatalysisContractError(ValueError):
    """A catalysis DTO is malformed or crosses the public-data boundary."""


class _FrozenDict(Mapping[str, Any]):
    """Minimal immutable mapping for caller-owned nested contract values."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, Any]):
        self._data = dict(value)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __repr__(self) -> str:
        return f"_FrozenDict({self._data!r})"


def _json_value(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CatalysisContractError("JSON object keys must be strings")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise CatalysisContractError("value is not canonical JSON")


def _freeze_json(value: Any) -> Any:
    normalized = _json_value(value)
    if isinstance(normalized, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in normalized.items()})
    if isinstance(normalized, list):
        return tuple(_freeze_json(item) for item in normalized)
    return normalized


def _normalised_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _is_private_key(value: Any) -> bool:
    key = _normalised_key(value)
    if key in _PUBLIC_BOUNDARY_KEYS:
        return False
    collapsed = key.replace("_", "")
    return bool(is_sensitive_key(value) or
        key in _PRIVATE_KEYS
        or key.endswith(_PRIVATE_KEY_SUFFIXES)
        or collapsed.endswith(_PRIVATE_COLLAPSED_SUFFIXES)
    )


def _contains_path_or_secret(value: str) -> bool:
    return bool(
        _ABS_WINDOWS_RE.search(value)
        or _ABS_POSIX_RE.search(value)
        or _FILE_URI_RE.search(value)
        or looks_like_credential(value)
        or "../" in value
        or "..\\" in value
    )


def reject_sensitive(value: Any, *, field: str = "value") -> None:
    """Reject paths, traversal fragments, secret-shaped values, and private keys.

    The check is recursive and is used for every caller-owned mapping before it
    can become part of canonical JSON.  Public HTTPS documentation URLs are
    permitted; local paths and credential-bearing URLs are not.
    """

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise CatalysisContractError(f"{field} keys must be strings")
            if _is_private_key(raw_key) or _contains_path_or_secret(raw_key):
                raise CatalysisContractError(f"{field} contains a private field")
            reject_sensitive(item, field=f"{field}.{raw_key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            reject_sensitive(item, field=f"{field}[{index}]")
        return
    if isinstance(value, str):
        if _contains_path_or_secret(value):
            raise CatalysisContractError(f"{field} contains a path or secret")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CatalysisContractError(f"{field} must be finite")
        return
    raise CatalysisContractError(f"{field} is not JSON-compatible")


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """Return a deterministic JSON-safe projection with recursive redaction."""

    if _is_private_key(key):
        return "[redacted-sensitive-field]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_sensitive(item, key=str(item_key))
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not (_is_private_key(item_key)
                    or _contains_path_or_secret(str(item_key)))
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return "[redacted-sensitive-value]" if _contains_path_or_secret(value) else value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return redact_sensitive(value.to_dict(), key=key)
    return "[redacted-sensitive-value]"


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for a public contract value."""

    value = _json_value(value)
    reject_sensitive(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalysisContractError("value is not canonical JSON") from exc


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _strict_fields(value: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise CatalysisContractError(f"{label} must be an object")
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown:
        raise CatalysisContractError(f"{label} contains unknown fields")
    if missing:
        raise CatalysisContractError(f"{label} is missing required fields")
    reject_sensitive(value, field=label)


def _opaque_id(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    text = str(value or "").strip()
    if (not _OPAQUE_ID_RE.fullmatch(text) or text in {".", ".."}
            or _contains_path_or_secret(text)):
        raise CatalysisContractError(f"{field} must be an opaque identifier")
    return text


def _opaque_ids(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CatalysisContractError(f"{field} must be an array of opaque identifiers")
    result = tuple(_opaque_id(item, f"{field}[]") for item in value)
    if not allow_empty and not result:
        raise CatalysisContractError(f"{field} must not be empty")
    if len(set(result)) != len(result):
        raise CatalysisContractError(f"{field} must not contain duplicates")
    return result


def _enum(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise CatalysisContractError(f"{field} has an unsupported value")
    return text


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise CatalysisContractError(f"{field} must be a SHA-256 digest")
    return text


def _version(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SEMVER_RE.fullmatch(text):
        raise CatalysisContractError(f"{field} must be a semantic version")
    return text


def _finite_optional(value: Any, field: str, *, low: float | None = None,
                     high: float | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CatalysisContractError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CatalysisContractError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or (low is not None and number < low) \
            or (high is not None and number > high):
        raise CatalysisContractError(f"{field} is outside the supported range")
    return number


def _text(value: Any, field: str, *, limit: int = 600) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise CatalysisContractError(f"{field} must be non-empty bounded text")
    text = value.strip()
    reject_sensitive(text, field=field)
    return text


@dataclass(frozen=True)
class EvidenceRef:
    """Typed pointer to evidence without a filesystem or browser locator."""

    ref_type: str
    opaque_id: str
    origin: str
    revision_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref_type", _enum(
            self.ref_type, "evidence.ref_type", EVIDENCE_TYPES))
        object.__setattr__(self, "opaque_id", _opaque_id(
            self.opaque_id, "evidence.opaque_id"))
        object.__setattr__(self, "origin", _enum(
            self.origin, "evidence.origin", PROVENANCE_KINDS))
        if (self.ref_type in {"publication_record", "imported_record"}
                and self.origin != "imported"):
            raise CatalysisContractError(
                "publication/imported evidence must declare imported origin")
        object.__setattr__(self, "revision_id", _opaque_id(
            self.revision_id, "evidence.revision_id", optional=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_type": self.ref_type,
            "opaque_id": self.opaque_id,
            "origin": self.origin,
            "revision_id": self.revision_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceRef:
        _strict_fields(value, {"ref_type", "opaque_id", "origin", "revision_id"},
                       label="evidence_ref")
        return cls(**dict(value))


def _evidence_refs(value: Any, field: str) -> tuple[EvidenceRef, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CatalysisContractError(f"{field} must be an array")
    refs = tuple(item if isinstance(item, EvidenceRef) else EvidenceRef.from_dict(item)
                 for item in value)
    if not refs:
        raise CatalysisContractError(f"{field} must not be empty")
    keys = [(item.ref_type, item.opaque_id, item.revision_id) for item in refs]
    if len(keys) != len(set(keys)):
        raise CatalysisContractError(f"{field} contains duplicate evidence")
    return refs


def _validate_provenance(provenance: str, refs: tuple[EvidenceRef, ...]) -> str:
    result = _enum(provenance, "provenance", PROVENANCE_KINDS)
    origins = {item.origin for item in refs}
    if result == "observed" and origins != {"observed"}:
        raise CatalysisContractError(
            "observed objects may only cite observed evidence")
    if result == "imported" and "imported" not in origins:
        raise CatalysisContractError(
            "imported objects require an imported evidence reference")
    if result == "inferred" and "inferred" not in origins:
        raise CatalysisContractError(
            "inferred objects require an inferred evidence reference")
    return result


@dataclass(frozen=True)
class MethodFingerprint:
    """Opaque binding to a normalized computational or experimental method."""

    method_id: str
    scope: str
    sha256: str
    evidence_refs: tuple[EvidenceRef, ...]
    schema: str = "vcstudio.method-fingerprint/v1"

    def __post_init__(self) -> None:
        if self.schema != "vcstudio.method-fingerprint/v1":
            raise CatalysisContractError("method fingerprint schema is unsupported")
        object.__setattr__(self, "method_id", _opaque_id(
            self.method_id, "method_fingerprint.method_id"))
        object.__setattr__(self, "scope", _opaque_id(
            self.scope, "method_fingerprint.scope"))
        object.__setattr__(self, "sha256", _sha256(
            self.sha256, "method_fingerprint.sha256"))
        object.__setattr__(self, "evidence_refs", _evidence_refs(
            self.evidence_refs, "method_fingerprint.evidence_refs"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "method_id": self.method_id,
            "scope": self.scope,
            "sha256": self.sha256,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MethodFingerprint:
        allowed = {"schema", "method_id", "scope", "sha256", "evidence_refs"}
        _strict_fields(value, allowed, label="method_fingerprint")
        return cls(
            schema=value["schema"], method_id=value["method_id"], scope=value["scope"],
            sha256=value["sha256"], evidence_refs=_evidence_refs(
                value["evidence_refs"], "method_fingerprint.evidence_refs"),
        )


class _DomainDTO:
    schema: ClassVar[str]
    provenance: str
    evidence_refs: tuple[EvidenceRef, ...]
    method_fingerprint: MethodFingerprint

    def semantic_hash(self) -> str:
        return semantic_hash(self.to_dict())

    def _validate_common(self) -> None:
        refs = _evidence_refs(self.evidence_refs, "evidence_refs")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "provenance", _validate_provenance(
            self.provenance, refs))
        fingerprint = self.method_fingerprint
        if not isinstance(fingerprint, MethodFingerprint):
            fingerprint = MethodFingerprint.from_dict(fingerprint)
        if (self.provenance == "observed"
                and any(ref.origin != "observed" for ref in fingerprint.evidence_refs)):
            raise CatalysisContractError(
                "observed objects require an observed method fingerprint")
        object.__setattr__(self, "method_fingerprint", fingerprint)

    def _common_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "provenance": self.provenance,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "method_fingerprint": self.method_fingerprint.to_dict(),
        }


class _RevisionedDomainDTO(_DomainDTO):
    dto_schema_version: ClassVar[str]
    schema_version: str
    object_revision_id: str
    parent_revision: str | None
    expected_current_hash: str | None

    def _validate_common(self) -> None:
        if self.schema_version != self.dto_schema_version:
            raise CatalysisContractError("domain DTO schema version is unsupported")
        object.__setattr__(self, "object_revision_id", _opaque_id(
            self.object_revision_id, "object_revision_id"))
        object.__setattr__(self, "parent_revision", _opaque_id(
            self.parent_revision, "parent_revision", optional=True))
        expected = self.expected_current_hash
        if expected is not None:
            expected = _sha256(expected, "expected_current_hash")
        if (self.parent_revision is None) != (expected is None):
            raise CatalysisContractError(
                "parent_revision and expected_current_hash must be provided together")
        if self.parent_revision == self.object_revision_id:
            raise CatalysisContractError("a revision cannot be its own parent")
        object.__setattr__(self, "expected_current_hash", expected)
        super()._validate_common()

    def _common_dict(self) -> dict[str, Any]:
        return {
            **super()._common_dict(),
            "schema_version": self.schema_version,
            "object_revision_id": self.object_revision_id,
            "parent_revision": self.parent_revision,
            "expected_current_hash": self.expected_current_hash,
        }


@dataclass(frozen=True)
class CatalystSurface(_RevisionedDomainDTO):
    schema: ClassVar[str] = "vcstudio.catalyst-surface/v1"
    dto_schema_version: ClassVar[str] = "1.0.0"
    surface_id: str
    composition: str
    miller_indices: tuple[int, int, int]
    termination_id: str | None
    geometric_site_ids: tuple[str, ...]
    provenance: str
    evidence_refs: tuple[EvidenceRef, ...]
    method_fingerprint: MethodFingerprint
    object_revision_id: str
    parent_revision: str | None = None
    expected_current_hash: str | None = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        self._validate_common()
        object.__setattr__(self, "surface_id", _opaque_id(self.surface_id, "surface_id"))
        composition = str(self.composition or "").strip()
        if not _FORMULA_RE.fullmatch(composition):
            raise CatalysisContractError("composition is invalid")
        reject_sensitive(composition, field="composition")
        object.__setattr__(self, "composition", composition)
        miller = tuple(self.miller_indices)
        if len(miller) != 3 or any(isinstance(item, bool) or not isinstance(item, int)
                                  or abs(item) > 99 for item in miller) or miller == (0, 0, 0):
            raise CatalysisContractError("miller_indices must be three bounded integers")
        object.__setattr__(self, "miller_indices", miller)
        object.__setattr__(self, "termination_id", _opaque_id(
            self.termination_id, "termination_id", optional=True))
        object.__setattr__(self, "geometric_site_ids", _opaque_ids(
            self.geometric_site_ids, "geometric_site_ids", allow_empty=True))

    def to_dict(self) -> dict[str, Any]:
        return {**self._common_dict(), "surface_id": self.surface_id,
                "composition": self.composition,
                "miller_indices": list(self.miller_indices),
                "termination_id": self.termination_id,
                "geometric_site_ids": list(self.geometric_site_ids)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CatalystSurface:
        allowed = {"schema", "schema_version", "object_revision_id",
                   "parent_revision", "expected_current_hash", "surface_id", "composition",
                   "miller_indices", "termination_id", "geometric_site_ids",
                   "provenance", "evidence_refs", "method_fingerprint"}
        _strict_fields(value, allowed, label="catalyst_surface")
        if value["schema"] != cls.schema:
            raise CatalysisContractError("catalyst surface schema is unsupported")
        return cls(
            surface_id=value["surface_id"], composition=value["composition"],
            miller_indices=tuple(value["miller_indices"]),
            termination_id=value["termination_id"],
            geometric_site_ids=tuple(value["geometric_site_ids"]),
            provenance=value["provenance"],
            evidence_refs=_evidence_refs(value["evidence_refs"], "evidence_refs"),
            method_fingerprint=MethodFingerprint.from_dict(value["method_fingerprint"]),
            object_revision_id=value["object_revision_id"],
            parent_revision=value["parent_revision"],
            expected_current_hash=value["expected_current_hash"],
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True)
class AdsorbateState(_RevisionedDomainDTO):
    schema: ClassVar[str] = "vcstudio.adsorbate-state/v1"
    dto_schema_version: ClassVar[str] = "1.0.0"
    state_id: str
    surface_id: str
    adsorbate_id: str
    chemical_formula: str
    geometric_site_id: str | None
    charge: int | None
    multiplicity: int | None
    provenance: str
    evidence_refs: tuple[EvidenceRef, ...]
    method_fingerprint: MethodFingerprint
    object_revision_id: str
    parent_revision: str | None = None
    expected_current_hash: str | None = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        self._validate_common()
        for name in ("state_id", "surface_id", "adsorbate_id"):
            object.__setattr__(self, name, _opaque_id(getattr(self, name), name))
        formula = str(self.chemical_formula or "").strip()
        if not _FORMULA_RE.fullmatch(formula):
            raise CatalysisContractError("chemical_formula is invalid")
        reject_sensitive(formula, field="chemical_formula")
        object.__setattr__(self, "chemical_formula", formula)
        object.__setattr__(self, "geometric_site_id", _opaque_id(
            self.geometric_site_id, "geometric_site_id", optional=True))
        if self.charge is not None and (isinstance(self.charge, bool)
                                        or not isinstance(self.charge, int)
                                        or not -20 <= self.charge <= 20):
            raise CatalysisContractError("charge is invalid")
        if self.multiplicity is not None and (isinstance(self.multiplicity, bool)
                                              or not isinstance(self.multiplicity, int)
                                              or not 1 <= self.multiplicity <= 50):
            raise CatalysisContractError("multiplicity is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {**self._common_dict(), "state_id": self.state_id,
                "surface_id": self.surface_id, "adsorbate_id": self.adsorbate_id,
                "chemical_formula": self.chemical_formula,
                "geometric_site_id": self.geometric_site_id,
                "charge": self.charge, "multiplicity": self.multiplicity}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdsorbateState:
        allowed = {"schema", "schema_version", "object_revision_id",
                   "parent_revision", "expected_current_hash", "state_id", "surface_id",
                   "adsorbate_id", "chemical_formula", "geometric_site_id",
                   "charge", "multiplicity", "provenance", "evidence_refs",
                   "method_fingerprint"}
        _strict_fields(value, allowed, label="adsorbate_state")
        if value["schema"] != cls.schema:
            raise CatalysisContractError("adsorbate state schema is unsupported")
        return cls(
            state_id=value["state_id"], surface_id=value["surface_id"],
            adsorbate_id=value["adsorbate_id"], chemical_formula=value["chemical_formula"],
            geometric_site_id=value["geometric_site_id"], charge=value["charge"],
            multiplicity=value["multiplicity"], provenance=value["provenance"],
            evidence_refs=_evidence_refs(value["evidence_refs"], "evidence_refs"),
            method_fingerprint=MethodFingerprint.from_dict(value["method_fingerprint"]),
            object_revision_id=value["object_revision_id"],
            parent_revision=value["parent_revision"],
            expected_current_hash=value["expected_current_hash"],
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True)
class ExactRational:
    """A bounded canonical non-negative rational encoded as numerator/denominator."""

    numerator: int
    denominator: int = 1

    def __post_init__(self) -> None:
        if (isinstance(self.numerator, bool) or not isinstance(self.numerator, int)
                or isinstance(self.denominator, bool)
                or not isinstance(self.denominator, int)):
            raise CatalysisContractError("rational numerator and denominator must be integers")
        if self.numerator < 0 or self.denominator <= 0:
            raise CatalysisContractError(
                "rational numerator must be non-negative and denominator positive")
        if abs(self.numerator) > 1_000_000_000 or self.denominator > 1_000_000_000:
            raise CatalysisContractError("rational value is outside the supported range")
        normalized = Fraction(self.numerator, self.denominator)
        object.__setattr__(self, "numerator", normalized.numerator)
        object.__setattr__(self, "denominator", normalized.denominator)

    def to_fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def to_dict(self) -> dict[str, int]:
        return {"numerator": self.numerator, "denominator": self.denominator}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExactRational:
        _strict_fields(value, {"numerator", "denominator"}, label="exact_rational")
        return cls(numerator=value["numerator"], denominator=value["denominator"])


def _exact_rational(value: Any, field: str, *, allow_zero: bool) -> ExactRational:
    if isinstance(value, ExactRational):
        result = value
    elif isinstance(value, int) and not isinstance(value, bool):
        result = ExactRational(value, 1)
    elif isinstance(value, Mapping):
        result = ExactRational.from_dict(value)
    else:
        raise CatalysisContractError(
            f"{field} must be an exact rational, never a floating-point number")
    if not allow_zero and result.numerator == 0:
        raise CatalysisContractError(f"{field} must be positive")
    return result


def _site_stoichiometry(value: Any, field: str) -> Mapping[str, ExactRational]:
    if not isinstance(value, Mapping) or len(value) > 64:
        raise CatalysisContractError(f"{field} must be a bounded site-type mapping")
    result: dict[str, ExactRational] = {}
    for raw_site_type, raw_amount in value.items():
        if _is_private_key(raw_site_type):
            raise CatalysisContractError(f"{field} contains a sensitive site type")
        site_type = _opaque_id(raw_site_type, f"{field} site type")
        if site_type in result:
            raise CatalysisContractError(f"{field} contains duplicate normalized site types")
        result[site_type] = _exact_rational(
            raw_amount, f"{field}.{site_type}", allow_zero=False)
    return _FrozenDict(result)


def _site_stoichiometry_from_dict(
        value: Any, field: str) -> Mapping[str, ExactRational]:
    if not isinstance(value, Mapping):
        raise CatalysisContractError(f"{field} must be a site-type mapping")
    parsed: dict[str, ExactRational] = {}
    for site_type, amount in value.items():
        if not isinstance(amount, Mapping):
            raise CatalysisContractError(
                f"{field}.{site_type} must use numerator/denominator wire form")
        parsed[site_type] = ExactRational.from_dict(amount)
    return parsed


@dataclass(frozen=True)
class ReactionParticipant:
    """One exact stoichiometric participant resolved outside the DTO."""

    state_id: str
    coefficient: ExactRational
    phase: str
    charge: int
    site_stoichiometry: Mapping[str, ExactRational]

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_id", _opaque_id(self.state_id, "participant.state_id"))
        object.__setattr__(self, "coefficient", _exact_rational(
            self.coefficient, "participant.coefficient", allow_zero=False))
        object.__setattr__(self, "phase", _enum(
            self.phase, "participant.phase", PARTICIPANT_PHASES))
        if (isinstance(self.charge, bool) or not isinstance(self.charge, int)
                or not -1000 <= self.charge <= 1000):
            raise CatalysisContractError("participant.charge must be a bounded integer")
        object.__setattr__(self, "site_stoichiometry", _site_stoichiometry(
            self.site_stoichiometry, "participant.site_stoichiometry"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "coefficient": self.coefficient.to_dict(),
            "phase": self.phase,
            "charge": self.charge,
            "site_stoichiometry": {
                key: amount.to_dict()
                for key, amount in sorted(self.site_stoichiometry.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReactionParticipant:
        _strict_fields(
            value, {"state_id", "coefficient", "phase", "charge",
                    "site_stoichiometry"},
            label="reaction_participant",
        )
        if not isinstance(value["coefficient"], Mapping):
            raise CatalysisContractError(
                "participant.coefficient must use numerator/denominator wire form")
        return cls(
            state_id=value["state_id"],
            coefficient=ExactRational.from_dict(value["coefficient"]),
            phase=value["phase"], charge=value["charge"],
            site_stoichiometry=_site_stoichiometry_from_dict(
                value["site_stoichiometry"], "participant.site_stoichiometry"),
        )


def _participants(value: Any, field: str) -> tuple[ReactionParticipant, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CatalysisContractError(f"{field} must be an array of participants")
    result = tuple(
        item if isinstance(item, ReactionParticipant) else ReactionParticipant.from_dict(item)
        for item in value
    )
    if not result:
        raise CatalysisContractError(f"{field} must not be empty")
    if len({item.state_id for item in result}) != len(result):
        raise CatalysisContractError(f"{field} must combine duplicate states into coefficients")
    return result


@dataclass(frozen=True)
class ElementaryStep(_RevisionedDomainDTO):
    """A v3 step with exact coefficients and a complete transition-state side."""

    schema: ClassVar[str] = "vcstudio.elementary-step/v3"
    dto_schema_version: ClassVar[str] = "3.0.0"
    step_id: str
    reactants: tuple[ReactionParticipant, ...]
    transition_state: tuple[ReactionParticipant, ...]
    products: tuple[ReactionParticipant, ...]
    condition_set_id: str | None
    reversible: bool
    provenance: str
    evidence_refs: tuple[EvidenceRef, ...]
    method_fingerprint: MethodFingerprint
    object_revision_id: str
    parent_revision: str | None = None
    expected_current_hash: str | None = None
    schema_version: str = "3.0.0"

    def __post_init__(self) -> None:
        self._validate_common()
        object.__setattr__(self, "step_id", _opaque_id(self.step_id, "step_id"))
        object.__setattr__(self, "reactants", _participants(self.reactants, "reactants"))
        object.__setattr__(self, "transition_state", _participants(
            self.transition_state, "transition_state"))
        object.__setattr__(self, "products", _participants(self.products, "products"))
        object.__setattr__(self, "condition_set_id", _opaque_id(
            self.condition_set_id, "condition_set_id", optional=True))
        if not isinstance(self.reversible, bool):
            raise CatalysisContractError("reversible must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {**self._common_dict(), "step_id": self.step_id,
                "reactants": [item.to_dict() for item in self.reactants],
                "transition_state": [item.to_dict() for item in self.transition_state],
                "products": [item.to_dict() for item in self.products],
                "condition_set_id": self.condition_set_id,
                "reversible": self.reversible}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ElementaryStep:
        if isinstance(value, Mapping) and value.get("schema") in {
                "vcstudio.elementary-step/v1", "vcstudio.elementary-step/v2"}:
            raise CatalysisContractError(
                "elementary step v1/v2 must be migrated to the v3 participant contract")
        allowed = {"schema", "schema_version", "object_revision_id",
                   "parent_revision", "expected_current_hash", "step_id", "reactants",
                   "transition_state", "products", "condition_set_id", "reversible",
                   "provenance", "evidence_refs", "method_fingerprint"}
        _strict_fields(value, allowed, label="elementary_step")
        if value["schema"] != cls.schema:
            raise CatalysisContractError("elementary step schema is unsupported")
        return cls(
            step_id=value["step_id"],
            reactants=tuple(ReactionParticipant.from_dict(item) for item in value["reactants"]),
            transition_state=tuple(
                ReactionParticipant.from_dict(item) for item in value["transition_state"]),
            products=tuple(ReactionParticipant.from_dict(item) for item in value["products"]),
            condition_set_id=value["condition_set_id"], reversible=value["reversible"],
            provenance=value["provenance"],
            evidence_refs=_evidence_refs(value["evidence_refs"], "evidence_refs"),
            method_fingerprint=MethodFingerprint.from_dict(value["method_fingerprint"]),
            object_revision_id=value["object_revision_id"],
            parent_revision=value["parent_revision"],
            expected_current_hash=value["expected_current_hash"],
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True)
class AuthoritativeParticipantState:
    """Resolver-owned state used to verify, never infer, step conservation."""

    state_id: str
    chemical_formula: str
    phase: str
    charge: int
    site_stoichiometry: Mapping[str, ExactRational]

    def __post_init__(self) -> None:
        participant = ReactionParticipant(
            state_id=self.state_id, coefficient=1, phase=self.phase,
            charge=self.charge, site_stoichiometry=self.site_stoichiometry,
        )
        object.__setattr__(self, "state_id", participant.state_id)
        object.__setattr__(self, "phase", participant.phase)
        object.__setattr__(self, "charge", participant.charge)
        object.__setattr__(self, "site_stoichiometry", participant.site_stoichiometry)
        formula = str(self.chemical_formula or "").strip()
        if not formula or not _FORMULA_RE.fullmatch(formula):
            raise CatalysisContractError("authoritative chemical_formula is invalid")
        reject_sensitive(formula, field="authoritative chemical_formula")
        object.__setattr__(self, "chemical_formula", formula)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "chemical_formula": self.chemical_formula,
            "phase": self.phase,
            "charge": self.charge,
            "site_stoichiometry": {
                key: amount.to_dict()
                for key, amount in sorted(self.site_stoichiometry.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AuthoritativeParticipantState:
        _strict_fields(
            value, {"state_id", "chemical_formula", "phase", "charge",
                    "site_stoichiometry"},
            label="authoritative_participant_state",
        )
        return cls(
            state_id=value["state_id"], chemical_formula=value["chemical_formula"],
            phase=value["phase"], charge=value["charge"],
            site_stoichiometry=_site_stoichiometry_from_dict(
                value["site_stoichiometry"], "authoritative.site_stoichiometry"),
        )


_ELEMENT_TOKEN_RE = re.compile(r"([A-Z][a-z]?)([1-9]\d*)?")


def _element_counts(formula: str) -> dict[str, int]:
    material = formula.replace("*", "")
    if not material:
        return {}
    tokens = list(_ELEMENT_TOKEN_RE.finditer(material))
    if not tokens or "".join(match.group(0) for match in tokens) != material:
        raise CatalysisContractError(
            "authoritative chemical_formula is not an exact elemental formula")
    result: dict[str, int] = {}
    for match in tokens:
        element = match.group(1)
        result[element] = result.get(element, 0) + int(match.group(2) or "1")
    return result


def validate_elementary_step_conservation(
    step: ElementaryStep,
    resolver: Mapping[str, AuthoritativeParticipantState | Mapping[str, Any]]
    | Callable[[str], AuthoritativeParticipantState | Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Resolve every state authoritatively and verify element/charge/site balance.

    Participant declarations are checked against the resolved record before any
    balance is computed.  The function is validation-only and never authorizes
    or instantiates a workflow.
    """

    if not isinstance(step, ElementaryStep):
        raise CatalysisContractError("step must be an ElementaryStep")

    resolved_cache: dict[str, AuthoritativeParticipantState] = {}

    def resolved(state_id: str) -> AuthoritativeParticipantState:
        if state_id in resolved_cache:
            return resolved_cache[state_id]
        try:
            value = resolver(state_id) if callable(resolver) else resolver[state_id]
        except (KeyError, LookupError) as exc:
            raise CatalysisContractError(
                f"authoritative resolver has no state {state_id}") from exc
        if isinstance(value, AuthoritativeParticipantState):
            record = value
        elif isinstance(value, Mapping):
            record = AuthoritativeParticipantState.from_dict(value)
        else:
            raise CatalysisContractError("authoritative resolver returned an invalid state")
        if record.state_id != state_id:
            raise CatalysisContractError("authoritative resolver identity mismatch")
        resolved_cache[state_id] = record
        return record

    def totals(side: tuple[ReactionParticipant, ...]) -> tuple[
            dict[str, Fraction], Fraction, dict[str, Fraction]]:
        elements: dict[str, Fraction] = {}
        charge = Fraction(0)
        sites: dict[str, Fraction] = {}
        for participant in side:
            state = resolved(participant.state_id)
            if (state.phase != participant.phase or state.charge != participant.charge
                    or state.site_stoichiometry != participant.site_stoichiometry):
                raise CatalysisContractError(
                    "participant declaration disagrees with authoritative state")
            coefficient = participant.coefficient.to_fraction()
            for element, count in _element_counts(state.chemical_formula).items():
                elements[element] = (
                    elements.get(element, Fraction(0)) + coefficient * count)
            charge += coefficient * participant.charge
            for site_type, amount in participant.site_stoichiometry.items():
                sites[site_type] = (
                    sites.get(site_type, Fraction(0))
                    + coefficient * amount.to_fraction())
        return elements, charge, sites

    reactant_elements, reactant_charge, reactant_sites = totals(step.reactants)
    transition_elements, transition_charge, transition_sites = totals(
        step.transition_state)
    product_elements, product_charge, product_sites = totals(step.products)

    for left_name, left, right_name, right in (
        ("reactants", reactant_elements, "transition_state", transition_elements),
        ("transition_state", transition_elements, "products", product_elements),
    ):
        if left != right:
            raise CatalysisContractError(
                f"{left_name}/{right_name} violates elemental conservation")
    for left_name, left, right_name, right in (
        ("reactants", reactant_charge, "transition_state", transition_charge),
        ("transition_state", transition_charge, "products", product_charge),
    ):
        if left != right:
            raise CatalysisContractError(
                f"{left_name}/{right_name} violates charge conservation")
    for left_name, left, right_name, right in (
        ("reactants", reactant_sites, "transition_state", transition_sites),
        ("transition_state", transition_sites, "products", product_sites),
    ):
        if left != right:
            raise CatalysisContractError(
                f"{left_name}/{right_name} violates site-type conservation")

    def rational_wire(value: Fraction) -> dict[str, int]:
        return {"numerator": value.numerator, "denominator": value.denominator}

    return _FrozenDict({
        "schema": "vcstudio.elementary-step-conservation/v2",
        "step_id": step.step_id,
        "elements": _FrozenDict({
            key: _FrozenDict(rational_wire(amount))
            for key, amount in sorted(reactant_elements.items())
        }),
        "charge": _FrozenDict(rational_wire(reactant_charge)),
        "site_stoichiometry": _FrozenDict({
            key: _FrozenDict(rational_wire(amount))
            for key, amount in sorted(reactant_sites.items())
        }),
        "authorizes_execution": False,
    })


@dataclass(frozen=True)
class ConditionSet(_RevisionedDomainDTO):
    schema: ClassVar[str] = "vcstudio.condition-set/v1"
    dto_schema_version: ClassVar[str] = "1.0.0"
    condition_set_id: str
    temperature_k: float | None
    pressure_pa: float | None
    ph: float | None
    electrode_potential_v: float | None
    provenance: str
    evidence_refs: tuple[EvidenceRef, ...]
    method_fingerprint: MethodFingerprint
    object_revision_id: str
    parent_revision: str | None = None
    expected_current_hash: str | None = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        self._validate_common()
        object.__setattr__(self, "condition_set_id", _opaque_id(
            self.condition_set_id, "condition_set_id"))
        object.__setattr__(self, "temperature_k", _finite_optional(
            self.temperature_k, "temperature_k", low=0.0, high=10000.0))
        object.__setattr__(self, "pressure_pa", _finite_optional(
            self.pressure_pa, "pressure_pa", low=0.0, high=1e12))
        object.__setattr__(self, "ph", _finite_optional(
            self.ph, "ph", low=-5.0, high=20.0))
        object.__setattr__(self, "electrode_potential_v", _finite_optional(
            self.electrode_potential_v, "electrode_potential_v", low=-20.0, high=20.0))
        if all(value is None for value in (
                self.temperature_k, self.pressure_pa, self.ph,
                self.electrode_potential_v)):
            raise CatalysisContractError("condition set must define at least one condition")

    def to_dict(self) -> dict[str, Any]:
        return {**self._common_dict(), "condition_set_id": self.condition_set_id,
                "temperature_k": self.temperature_k, "pressure_pa": self.pressure_pa,
                "ph": self.ph, "electrode_potential_v": self.electrode_potential_v}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConditionSet:
        allowed = {"schema", "schema_version", "object_revision_id",
                   "parent_revision", "expected_current_hash", "condition_set_id", "temperature_k",
                   "pressure_pa", "ph", "electrode_potential_v", "provenance",
                   "evidence_refs", "method_fingerprint"}
        _strict_fields(value, allowed, label="condition_set")
        if value["schema"] != cls.schema:
            raise CatalysisContractError("condition set schema is unsupported")
        return cls(
            condition_set_id=value["condition_set_id"],
            temperature_k=value["temperature_k"], pressure_pa=value["pressure_pa"],
            ph=value["ph"], electrode_potential_v=value["electrode_potential_v"],
            provenance=value["provenance"],
            evidence_refs=_evidence_refs(value["evidence_refs"], "evidence_refs"),
            method_fingerprint=MethodFingerprint.from_dict(value["method_fingerprint"]),
            object_revision_id=value["object_revision_id"],
            parent_revision=value["parent_revision"],
            expected_current_hash=value["expected_current_hash"],
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True)
class ReactionNetwork(_RevisionedDomainDTO):
    schema: ClassVar[str] = "vcstudio.reaction-network/v1"
    dto_schema_version: ClassVar[str] = "1.0.0"
    network_id: str
    surface_ids: tuple[str, ...]
    state_ids: tuple[str, ...]
    step_ids: tuple[str, ...]
    condition_set_ids: tuple[str, ...]
    provenance: str
    evidence_refs: tuple[EvidenceRef, ...]
    method_fingerprint: MethodFingerprint
    object_revision_id: str
    parent_revision: str | None = None
    expected_current_hash: str | None = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        self._validate_common()
        object.__setattr__(self, "network_id", _opaque_id(self.network_id, "network_id"))
        for field in ("surface_ids", "state_ids", "step_ids"):
            object.__setattr__(self, field, _opaque_ids(getattr(self, field), field))
        object.__setattr__(self, "condition_set_ids", _opaque_ids(
            self.condition_set_ids, "condition_set_ids", allow_empty=True))

    def to_dict(self) -> dict[str, Any]:
        return {**self._common_dict(), "network_id": self.network_id,
                "surface_ids": list(self.surface_ids), "state_ids": list(self.state_ids),
                "step_ids": list(self.step_ids),
                "condition_set_ids": list(self.condition_set_ids)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReactionNetwork:
        allowed = {"schema", "schema_version", "object_revision_id",
                   "parent_revision", "expected_current_hash", "network_id", "surface_ids", "state_ids",
                   "step_ids", "condition_set_ids", "provenance", "evidence_refs",
                   "method_fingerprint"}
        _strict_fields(value, allowed, label="reaction_network")
        if value["schema"] != cls.schema:
            raise CatalysisContractError("reaction network schema is unsupported")
        return cls(
            network_id=value["network_id"], surface_ids=tuple(value["surface_ids"]),
            state_ids=tuple(value["state_ids"]), step_ids=tuple(value["step_ids"]),
            condition_set_ids=tuple(value["condition_set_ids"]),
            provenance=value["provenance"],
            evidence_refs=_evidence_refs(value["evidence_refs"], "evidence_refs"),
            method_fingerprint=MethodFingerprint.from_dict(value["method_fingerprint"]),
            object_revision_id=value["object_revision_id"],
            parent_revision=value["parent_revision"],
            expected_current_hash=value["expected_current_hash"],
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True)
class RecipeInput:
    input_id: str
    evidence_type: str
    label_zh: str
    label_en: str
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_id", _opaque_id(self.input_id, "recipe_input.input_id"))
        object.__setattr__(self, "evidence_type", _enum(
            self.evidence_type, "recipe_input.evidence_type", EVIDENCE_TYPES))
        object.__setattr__(self, "label_zh", _text(self.label_zh, "recipe_input.label_zh", limit=160))
        object.__setattr__(self, "label_en", _text(self.label_en, "recipe_input.label_en", limit=160))
        if not isinstance(self.required, bool):
            raise CatalysisContractError("recipe_input.required must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {"input_id": self.input_id, "evidence_type": self.evidence_type,
                "label_zh": self.label_zh, "label_en": self.label_en,
                "required": self.required}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RecipeInput:
        allowed = {"input_id", "evidence_type", "label_zh", "label_en", "required"}
        _strict_fields(value, allowed, label="recipe_input")
        return cls(**dict(value))


@dataclass(frozen=True)
class RecipeParameter:
    parameter_id: str
    value_kind: str
    default: Any
    label_zh: str
    label_en: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        parameter_id = str(self.parameter_id or "").strip()
        if not _PARAMETER_ID_RE.fullmatch(parameter_id):
            raise CatalysisContractError("recipe parameter ID is invalid")
        object.__setattr__(self, "parameter_id", parameter_id)
        kind = _enum(self.value_kind, "recipe_parameter.value_kind",
                     ("integer", "number", "boolean", "string", "number_list"))
        object.__setattr__(self, "value_kind", kind)
        object.__setattr__(self, "label_zh", _text(self.label_zh, "parameter.label_zh", limit=160))
        object.__setattr__(self, "label_en", _text(self.label_en, "parameter.label_en", limit=160))
        if self.unit is not None:
            object.__setattr__(self, "unit", _opaque_id(self.unit, "parameter.unit"))
        low = _finite_optional(self.minimum, "parameter.minimum")
        high = _finite_optional(self.maximum, "parameter.maximum")
        if low is not None and high is not None and low > high:
            raise CatalysisContractError("parameter minimum exceeds maximum")
        object.__setattr__(self, "minimum", low)
        object.__setattr__(self, "maximum", high)
        object.__setattr__(self, "default", self.normalize(self.default))

    def normalize(self, value: Any) -> Any:
        if self.value_kind == "boolean":
            if not isinstance(value, bool):
                raise CatalysisContractError(f"{self.parameter_id} must be boolean")
            return value
        if self.value_kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise CatalysisContractError(f"{self.parameter_id} must be an integer")
            number: int | float = value
        elif self.value_kind == "number":
            if isinstance(value, bool):
                raise CatalysisContractError(f"{self.parameter_id} must be numeric")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise CatalysisContractError(f"{self.parameter_id} must be numeric") from exc
            if not math.isfinite(number):
                raise CatalysisContractError(f"{self.parameter_id} must be finite")
        elif self.value_kind == "string":
            return _opaque_id(value, self.parameter_id)
        else:
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
                raise CatalysisContractError(f"{self.parameter_id} must be a number array")
            numbers = []
            for item in value:
                if isinstance(item, bool):
                    raise CatalysisContractError(f"{self.parameter_id} must contain numbers")
                try:
                    number_item = float(item)
                except (TypeError, ValueError) as exc:
                    raise CatalysisContractError(
                        f"{self.parameter_id} must contain numbers") from exc
                if not math.isfinite(number_item):
                    raise CatalysisContractError(f"{self.parameter_id} must be finite")
                if self.minimum is not None and number_item < self.minimum:
                    raise CatalysisContractError(
                        f"{self.parameter_id} contains a value below its minimum")
                if self.maximum is not None and number_item > self.maximum:
                    raise CatalysisContractError(
                        f"{self.parameter_id} contains a value above its maximum")
                numbers.append(number_item)
            return tuple(numbers)
        if self.minimum is not None and number < self.minimum:
            raise CatalysisContractError(f"{self.parameter_id} is below its minimum")
        if self.maximum is not None and number > self.maximum:
            raise CatalysisContractError(f"{self.parameter_id} exceeds its maximum")
        return number

    def to_dict(self) -> dict[str, Any]:
        default = list(self.default) if isinstance(self.default, tuple) else self.default
        return {"parameter_id": self.parameter_id, "value_kind": self.value_kind,
                "default": default, "label_zh": self.label_zh, "label_en": self.label_en,
                "unit": self.unit, "minimum": self.minimum, "maximum": self.maximum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RecipeParameter:
        allowed = {"parameter_id", "value_kind", "default", "label_zh", "label_en",
                   "unit", "minimum", "maximum"}
        _strict_fields(value, allowed, label="recipe_parameter")
        return cls(**dict(value))


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    task_kind: str
    depends_on: tuple[str, ...]
    input_ids: tuple[str, ...]
    parameter_ids: tuple[str, ...]
    outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _opaque_id(self.node_id, "workflow_node.node_id"))
        object.__setattr__(self, "task_kind", _opaque_id(
            self.task_kind, "workflow_node.task_kind"))
        object.__setattr__(self, "depends_on", _opaque_ids(
            self.depends_on, "workflow_node.depends_on", allow_empty=True))
        object.__setattr__(self, "input_ids", _opaque_ids(
            self.input_ids, "workflow_node.input_ids", allow_empty=True))
        params = tuple(str(item or "").strip() for item in self.parameter_ids)
        if any(not _PARAMETER_ID_RE.fullmatch(item) for item in params) \
                or len(set(params)) != len(params):
            raise CatalysisContractError("workflow node parameter IDs are invalid")
        object.__setattr__(self, "parameter_ids", params)
        object.__setattr__(self, "outputs", _opaque_ids(self.outputs, "workflow_node.outputs"))

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "task_kind": self.task_kind,
                "depends_on": list(self.depends_on), "input_ids": list(self.input_ids),
                "parameter_ids": list(self.parameter_ids), "outputs": list(self.outputs)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkflowNode:
        allowed = {"node_id", "task_kind", "depends_on", "input_ids",
                   "parameter_ids", "outputs"}
        _strict_fields(value, allowed, label="workflow_node")
        return cls(**{**dict(value), "depends_on": tuple(value["depends_on"]),
                      "input_ids": tuple(value["input_ids"]),
                      "parameter_ids": tuple(value["parameter_ids"]),
                      "outputs": tuple(value["outputs"])})


@dataclass(frozen=True)
class ScientificLimit:
    code: str
    text_zh: str
    text_en: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _opaque_id(self.code, "scientific_limit.code"))
        object.__setattr__(self, "text_zh", _text(self.text_zh, "scientific_limit.text_zh"))
        object.__setattr__(self, "text_en", _text(self.text_en, "scientific_limit.text_en"))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "text_zh": self.text_zh, "text_en": self.text_en}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ScientificLimit:
        _strict_fields(value, {"code", "text_zh", "text_en"}, label="scientific_limit")
        return cls(**dict(value))


@dataclass(frozen=True)
class WorkflowRecipe(_DomainDTO):
    schema: ClassVar[str] = "vcstudio.workflow-recipe/v1"
    recipe_id: str
    recipe_version: str
    label_zh: str
    label_en: str
    summary_zh: str
    summary_en: str
    inputs: tuple[RecipeInput, ...]
    parameters: tuple[RecipeParameter, ...]
    nodes: tuple[WorkflowNode, ...]
    scientific_limits: tuple[ScientificLimit, ...]
    official_reference_urls: tuple[str, ...]
    provenance: str
    evidence_refs: tuple[EvidenceRef, ...]
    method_fingerprint: MethodFingerprint
    model_version: str = MODEL_VERSION

    def __post_init__(self) -> None:
        if self.model_version != MODEL_VERSION:
            raise CatalysisContractError("workflow recipe model version is unsupported")
        self._validate_common()
        object.__setattr__(self, "recipe_id", _opaque_id(self.recipe_id, "recipe_id"))
        object.__setattr__(self, "recipe_version", _version(
            self.recipe_version, "recipe_version"))
        for field in ("label_zh", "label_en", "summary_zh", "summary_en"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        inputs = tuple(item if isinstance(item, RecipeInput) else RecipeInput.from_dict(item)
                       for item in self.inputs)
        parameters = tuple(
            item if isinstance(item, RecipeParameter) else RecipeParameter.from_dict(item)
            for item in self.parameters)
        nodes = tuple(item if isinstance(item, WorkflowNode) else WorkflowNode.from_dict(item)
                      for item in self.nodes)
        limits = tuple(
            item if isinstance(item, ScientificLimit) else ScientificLimit.from_dict(item)
            for item in self.scientific_limits)
        if not inputs or not parameters or not nodes or not limits:
            raise CatalysisContractError("workflow recipe collections must not be empty")
        for name, collection, key in (
            ("inputs", inputs, lambda item: item.input_id),
            ("parameters", parameters, lambda item: item.parameter_id),
            ("nodes", nodes, lambda item: item.node_id),
            ("scientific_limits", limits, lambda item: item.code),
        ):
            keys = [key(item) for item in collection]
            if len(keys) != len(set(keys)):
                raise CatalysisContractError(f"workflow recipe {name} contain duplicates")
        input_ids = {item.input_id for item in inputs}
        parameter_ids = {item.parameter_id for item in parameters}
        node_ids = {item.node_id for item in nodes}
        prior: set[str] = set()
        for node in nodes:
            if any(dep not in node_ids for dep in node.depends_on):
                raise CatalysisContractError("workflow node dependency is unknown")
            if any(dep not in prior for dep in node.depends_on):
                raise CatalysisContractError("workflow nodes must be topologically ordered")
            if any(item not in input_ids for item in node.input_ids):
                raise CatalysisContractError("workflow node input is unknown")
            if any(item not in parameter_ids for item in node.parameter_ids):
                raise CatalysisContractError("workflow node parameter is unknown")
            prior.add(node.node_id)
        urls = tuple(str(item or "").strip() for item in self.official_reference_urls)
        if not urls or len(set(urls)) != len(urls) or any(
                not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s]*)?", item)
                or looks_like_credential(item) for item in urls):
            raise CatalysisContractError("official references must be unique public HTTPS URLs")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "scientific_limits", limits)
        object.__setattr__(self, "official_reference_urls", urls)

    def to_dict(self) -> dict[str, Any]:
        return {**self._common_dict(), "model_version": self.model_version,
                "recipe_id": self.recipe_id,
                "recipe_version": self.recipe_version, "label_zh": self.label_zh,
                "label_en": self.label_en, "summary_zh": self.summary_zh,
                "summary_en": self.summary_en,
                "inputs": [item.to_dict() for item in self.inputs],
                "parameters": [item.to_dict() for item in self.parameters],
                "nodes": [item.to_dict() for item in self.nodes],
                "scientific_limits": [item.to_dict() for item in self.scientific_limits],
                "official_reference_urls": list(self.official_reference_urls)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkflowRecipe:
        allowed = {"schema", "model_version", "recipe_id", "recipe_version", "label_zh",
                   "label_en", "summary_zh", "summary_en", "inputs", "parameters", "nodes",
                   "scientific_limits", "official_reference_urls", "provenance",
                   "evidence_refs", "method_fingerprint"}
        _strict_fields(value, allowed, label="workflow_recipe")
        if value["schema"] != cls.schema:
            raise CatalysisContractError("workflow recipe schema is unsupported")
        return cls(
            recipe_id=value["recipe_id"], recipe_version=value["recipe_version"],
            label_zh=value["label_zh"], label_en=value["label_en"],
            summary_zh=value["summary_zh"], summary_en=value["summary_en"],
            inputs=tuple(RecipeInput.from_dict(item) for item in value["inputs"]),
            parameters=tuple(RecipeParameter.from_dict(item) for item in value["parameters"]),
            nodes=tuple(WorkflowNode.from_dict(item) for item in value["nodes"]),
            scientific_limits=tuple(
                ScientificLimit.from_dict(item) for item in value["scientific_limits"]),
            official_reference_urls=tuple(value["official_reference_urls"]),
            provenance=value["provenance"],
            evidence_refs=_evidence_refs(value["evidence_refs"], "evidence_refs"),
            method_fingerprint=MethodFingerprint.from_dict(value["method_fingerprint"]),
            model_version=value["model_version"],
        )


DOMAIN_TYPES: dict[str, type[_DomainDTO]] = {
    "CatalystSurface": CatalystSurface,
    "AdsorbateState": AdsorbateState,
    "ElementaryStep": ElementaryStep,
    "ConditionSet": ConditionSet,
    "ReactionNetwork": ReactionNetwork,
    "WorkflowRecipe": WorkflowRecipe,
}


def _object_identity(value: _DomainDTO) -> str:
    for field in ("surface_id", "state_id", "step_id", "condition_set_id",
                  "network_id", "recipe_id"):
        if hasattr(value, field):
            return str(getattr(value, field))
    raise CatalysisContractError("domain object has no identity")


def _object_schema_version(value: _DomainDTO) -> str:
    if isinstance(value, WorkflowRecipe):
        return value.model_version
    return value.schema_version


def _object_revision(value: _DomainDTO) -> tuple[str, str | None, str | None]:
    if isinstance(value, WorkflowRecipe):
        return value.recipe_version, None, None
    return (
        value.object_revision_id,
        value.parent_revision,
        value.expected_current_hash,
    )


@dataclass(frozen=True)
class DomainEnvelope:
    """Canonical storage envelope for one immutable domain DTO revision."""

    object_type: str
    object_id: str
    schema_version: str
    object_revision_id: str
    parent_revision: str | None
    expected_current_hash: str | None
    payload: Mapping[str, Any]
    semantic_sha256: str
    schema: str = DOMAIN_ENVELOPE_SCHEMA
    job_source_of_truth: str = "job.yaml"
    authorizes_execution: bool = False

    def __post_init__(self) -> None:
        if self.schema != DOMAIN_ENVELOPE_SCHEMA:
            raise CatalysisContractError("domain envelope schema is unsupported")
        if self.object_type not in DOMAIN_TYPES:
            raise CatalysisContractError("domain envelope object type is unsupported")
        object.__setattr__(self, "object_id", _opaque_id(self.object_id, "object_id"))
        object.__setattr__(self, "schema_version", _version(
            self.schema_version, "schema_version"))
        object.__setattr__(self, "object_revision_id", _opaque_id(
            self.object_revision_id, "object_revision_id"))
        object.__setattr__(self, "parent_revision", _opaque_id(
            self.parent_revision, "parent_revision", optional=True))
        expected = self.expected_current_hash
        if expected is not None:
            expected = _sha256(expected, "expected_current_hash")
        if (self.parent_revision is None) != (expected is None):
            raise CatalysisContractError(
                "envelope parent revision and expected hash must be provided together")
        object.__setattr__(self, "expected_current_hash", expected)
        if self.job_source_of_truth != "job.yaml" or self.authorizes_execution is not False:
            raise CatalysisContractError("domain envelope authority boundary is invalid")
        parsed = DOMAIN_TYPES[self.object_type].from_dict(self.payload)
        if _object_identity(parsed) != self.object_id:
            raise CatalysisContractError("domain envelope identity mismatch")
        if _object_schema_version(parsed) != self.schema_version:
            raise CatalysisContractError("domain envelope schema version mismatch")
        revision_id, parent_id, parsed_expected = _object_revision(parsed)
        if (revision_id != self.object_revision_id or parent_id != self.parent_revision
                or parsed_expected != self.expected_current_hash):
            raise CatalysisContractError("domain envelope revision identity mismatch")
        digest = _sha256(self.semantic_sha256, "semantic_sha256")
        if parsed.semantic_hash() != digest:
            raise CatalysisContractError("domain envelope semantic hash mismatch")
        object.__setattr__(self, "payload", _freeze_json(parsed.to_dict()))
        object.__setattr__(self, "semantic_sha256", digest)

    @classmethod
    def wrap(cls, value: _DomainDTO) -> DomainEnvelope:
        if not isinstance(value, tuple(DOMAIN_TYPES.values())):
            raise CatalysisContractError("unsupported domain object")
        revision_id, parent_id, expected = _object_revision(value)
        return cls(
            object_type=type(value).__name__, object_id=_object_identity(value),
            schema_version=_object_schema_version(value),
            object_revision_id=revision_id, parent_revision=parent_id,
            expected_current_hash=expected, payload=value.to_dict(),
            semantic_sha256=value.semantic_hash(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "object_type": self.object_type,
                "object_id": self.object_id, "schema_version": self.schema_version,
                "object_revision_id": self.object_revision_id,
                "parent_revision": self.parent_revision,
                "expected_current_hash": self.expected_current_hash,
                "payload": _json_value(self.payload), "semantic_sha256": self.semantic_sha256,
                "job_source_of_truth": self.job_source_of_truth,
                "authorizes_execution": self.authorizes_execution}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DomainEnvelope:
        allowed = {"schema", "object_type", "object_id", "schema_version",
                   "object_revision_id", "parent_revision", "expected_current_hash",
                   "payload", "semantic_sha256", "job_source_of_truth",
                   "authorizes_execution"}
        _strict_fields(value, allowed, label="domain_envelope")
        return cls(**dict(value))


@dataclass(frozen=True)
class WorkflowRunSnapshot:
    """Pinned plan snapshot for a possible run; never an execution record."""

    run_id: str
    recipe_id: str
    recipe_version: str
    recipe_semantic_sha256: str
    resolved_parameters: Mapping[str, Any]
    parameter_sources: Mapping[str, str]
    input_evidence: Mapping[str, EvidenceRef]
    preview_semantic_sha256: str
    plan_status: str
    schema: str = WORKFLOW_RUN_SCHEMA
    scientific_status: str = "not_validated"
    job_source_of_truth: str = "job.yaml"
    authorizes_execution: bool = False

    def __post_init__(self) -> None:
        if self.schema != WORKFLOW_RUN_SCHEMA:
            raise CatalysisContractError("workflow run schema is unsupported")
        for field in ("run_id", "recipe_id"):
            object.__setattr__(self, field, _opaque_id(getattr(self, field), field))
        object.__setattr__(self, "recipe_version", _version(
            self.recipe_version, "recipe_version"))
        object.__setattr__(self, "recipe_semantic_sha256", _sha256(
            self.recipe_semantic_sha256, "recipe_semantic_sha256"))
        object.__setattr__(self, "preview_semantic_sha256", _sha256(
            self.preview_semantic_sha256, "preview_semantic_sha256"))
        if self.plan_status not in {"preview_ready", "blocked"}:
            raise CatalysisContractError("workflow run plan status is invalid")
        if (self.scientific_status != "not_validated"
                or self.job_source_of_truth != "job.yaml"
                or self.authorizes_execution is not False):
            raise CatalysisContractError("workflow run authority boundary is invalid")
        parameters = dict(self.resolved_parameters)
        sources = dict(self.parameter_sources)
        evidence = dict(self.input_evidence)
        if not parameters or set(parameters) != set(sources):
            raise CatalysisContractError("workflow run parameters are not fully pinned")
        for key, source in sources.items():
            if not _PARAMETER_ID_RE.fullmatch(str(key)) or source not in PARAMETER_SOURCES:
                raise CatalysisContractError("workflow run parameter source is invalid")
        reject_sensitive(parameters, field="resolved_parameters")
        parsed_evidence = {}
        for key, ref in evidence.items():
            input_id = _opaque_id(key, "input_evidence key")
            parsed_evidence[input_id] = (
                ref if isinstance(ref, EvidenceRef) else EvidenceRef.from_dict(ref))
        # A stored run snapshot is valid only if the pinned built-in recipe can
        # reproduce its exact parameters, evidence binding, status, and preview
        # hash.  This prevents callers from bypassing ``pin_preview`` by directly
        # constructing a superficially well-shaped DTO.
        try:
            from vcstudio.project import research_recipes

            recipe = research_recipes.get(self.recipe_id, self.recipe_version)
            recomputed = research_recipes.preview(
                self.recipe_id,
                self.recipe_version,
                {
                    "overrides": {
                        key: parameters[key]
                        for key, source in sources.items() if source == "user_override"
                    },
                    "evidence": {
                        key: ref.to_dict() for key, ref in parsed_evidence.items()
                    },
                },
            )
        except CatalysisContractError:
            raise
        except Exception as exc:
            raise CatalysisContractError(
                "workflow run recipe binding is unavailable") from exc
        if (recipe.semantic_hash() != self.recipe_semantic_sha256
                or recomputed["resolved_parameters"] != parameters
                or recomputed["parameter_sources"] != sources
                or recomputed["input_evidence"] != {
                    key: ref.to_dict() for key, ref in parsed_evidence.items()}
                or recomputed["status"] != self.plan_status
                or recomputed["preview_semantic_sha256"] != self.preview_semantic_sha256):
            raise CatalysisContractError("workflow run snapshot recipe binding mismatch")
        object.__setattr__(self, "resolved_parameters", _freeze_json(parameters))
        object.__setattr__(self, "parameter_sources", _freeze_json(sources))
        object.__setattr__(self, "input_evidence", _FrozenDict(parsed_evidence))

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "run_id": self.run_id,
                "recipe_id": self.recipe_id, "recipe_version": self.recipe_version,
                "recipe_semantic_sha256": self.recipe_semantic_sha256,
                "resolved_parameters": _json_value(self.resolved_parameters),
                "parameter_sources": _json_value(self.parameter_sources),
                "input_evidence": {key: ref.to_dict() for key, ref in self.input_evidence.items()},
                "preview_semantic_sha256": self.preview_semantic_sha256,
                "plan_status": self.plan_status, "scientific_status": self.scientific_status,
                "job_source_of_truth": self.job_source_of_truth,
                "authorizes_execution": self.authorizes_execution}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkflowRunSnapshot:
        allowed = {"schema", "run_id", "recipe_id", "recipe_version",
                   "recipe_semantic_sha256", "resolved_parameters", "parameter_sources",
                   "input_evidence", "preview_semantic_sha256", "plan_status",
                   "scientific_status", "job_source_of_truth", "authorizes_execution"}
        _strict_fields(value, allowed, label="workflow_run_snapshot")
        return cls(**dict(value))


__all__ = [
    "AdsorbateState", "AuthoritativeParticipantState", "CatalystSurface",
    "CatalysisContractError", "ConditionSet",
    "DOMAIN_ENVELOPE_SCHEMA", "DomainEnvelope", "EVIDENCE_TYPES", "ElementaryStep",
    "EvidenceRef", "ExactRational", "MODEL_VERSION", "MethodFingerprint",
    "PARTICIPANT_PHASES",
    "PROVENANCE_KINDS", "ReactionNetwork", "ReactionParticipant", "RecipeInput",
    "RecipeParameter", "ScientificLimit",
    "WORKFLOW_RUN_SCHEMA", "WorkflowNode", "WorkflowRecipe", "WorkflowRunSnapshot",
    "canonical_json_bytes", "redact_sensitive", "reject_sensitive", "semantic_hash",
    "validate_elementary_step_conservation",
]
