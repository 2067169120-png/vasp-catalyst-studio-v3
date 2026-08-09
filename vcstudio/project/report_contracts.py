"""Immutable, JSON-serializable contracts for scientific report generation.

The rendering layer deliberately accepts plain mappings, but a report that may be
reproduced or audited needs stronger boundaries than an ad-hoc ``dict``.  This
module provides those boundaries without adding a runtime dependency: all public
models are frozen dataclasses, nested JSON values are copied into immutable
containers, and every contract has a deterministic semantic digest.

Semantic digests describe intent, scientific inputs, and validation decisions.
Administrative timestamps and source ``locator`` values are retained for display
and navigation but excluded from those digests.  This lets a project move between
directories without changing its scientific identity.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any


REPORT_SPEC_SCHEMA = "vcstudio.report-spec/v1"
REPORT_SNAPSHOT_SCHEMA = "vcstudio.report-snapshot/v1"
VALIDATION_RESULT_SCHEMA = "vcstudio.report-validation/v1"

SUPPORTED_FORMATS = ("html", "docx", "pdf")
REPORT_KINDS = ("diagnostic", "final", "draft")
QUALIFICATION_LEVELS = (
    "diagnostic",
    "adsorption_result_verified",
    "thermodynamic_path_verified",
    "kinetic_evidence_verified",
    "publication_package_verified",
    "human_scientific_reviewed",
)
CHECK_STATUSES = ("pass", "warn", "fail", "unknown", "not_applicable")
CHECK_SEVERITIES = ("blocking", "warning", "informational")
VALIDATION_STATUSES = ("passed", "passed_with_warnings", "blocked", "unknown")
CLAIM_STATUSES = ("supported", "limited", "blocked", "unknown")

DEFAULT_OUTLINE = (
    "executive_summary",
    "key_findings",
    "candidate_evaluations",
    "adsorption_table",
    "comparison_table",
    "figures",
    "methods",
    "limitations",
    "recommendations",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ADMINISTRATIVE_TIME_KEYS = frozenset({
    "created_at",
    "created_at_utc",
    "generated_at",
    "generated_at_utc",
    "updated_at",
    "updated_at_utc",
    "validated_at",
    "validated_at_utc",
})


class _FrozenDict(Mapping[str, Any]):
    """Small immutable mapping used to freeze caller-owned nested dictionaries."""

    __slots__ = ("_data", "_hash")

    def __init__(self, data: Mapping[str, Any]):
        self._data = dict(data)
        self._hash: int | None = None

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"_FrozenDict({self._data!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(tuple(sorted(self._data.items())))
        return self._hash


def _json_value(value: Any) -> Any:
    """Return a detached JSON-compatible value or raise for unsupported input."""

    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and Infinity are not valid report contract values")
        return value
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize *value* as deterministic UTF-8 JSON.

    Object keys are sorted, array order is preserved, insignificant whitespace is
    removed, and non-finite floating-point values are rejected.
    """

    normalized = _json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    """Return the SHA-256 hex digest of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _freeze_json(value: Any, field_name: str) -> Any:
    """Copy a JSON value recursively into immutable containers."""

    try:
        normalized = _json_value(value)
    except (TypeError, ValueError) as exc:
        raise type(exc)(f"{field_name}: {exc}") from exc
    return _freeze_normalized(normalized)


def _freeze_normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_normalized(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_normalized(item) for item in value)
    return value


def _freeze_mapping(value: Any, field_name: str) -> _FrozenDict:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen = _freeze_json(value, field_name)
    assert isinstance(frozen, _FrozenDict)
    return frozen


def _thaw(value: Any) -> Any:
    """Return a mutable JSON value detached from an immutable contract."""

    return _json_value(value)


def _nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or None")
    return value


def _enum(value: Any, field_name: str, allowed: tuple[str, ...]) -> str:
    text = _nonempty_string(value, field_name)
    if text not in allowed:
        raise ValueError(f"unsupported {field_name}: {text!r}; expected one of {allowed!r}")
    return text


def _schema(value: Any, expected: str) -> str:
    text = _nonempty_string(value, "schema")
    if text != expected:
        raise ValueError(f"unsupported schema: {text!r}; expected {expected!r}")
    return text


def _sha256(value: Any, field_name: str) -> str:
    text = _nonempty_string(value, field_name).lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a 64-character SHA-256 hex digest")
    return text


def _normalize_utc(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    text = _nonempty_string(value, field_name)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat()


def _string_tuple(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
    unique: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a list or tuple of strings")
    result = tuple(_nonempty_string(item, field_name) for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{field_name} must not be empty")
    if unique and len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _normalize_formats(value: Any) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise TypeError("formats must be a string, list, or tuple")
    requested: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise TypeError("formats entries must be strings")
        fmt = item.strip().lower()
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"unsupported report format: {item!r}; expected html, docx, or pdf"
            )
        requested.add(fmt)
    normalized = tuple(fmt for fmt in SUPPORTED_FORMATS if fmt in requested)
    if not normalized:
        raise ValueError("formats must contain at least one supported format")
    return normalized


def _mapping_sequence(value: Any, field_name: str) -> tuple[_FrozenDict, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a list or tuple of mappings")
    return tuple(_freeze_mapping(item, field_name) for item in value)


def _prepare_mapping(
    value: Any,
    allowed: set[str],
    *,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("contract input must be a mapping")
    data: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("contract keys must be strings")
        data[key] = item
    for alias, canonical in (aliases or {}).items():
        if alias not in data:
            continue
        if canonical in data and data[canonical] != data[alias]:
            raise ValueError(f"conflicting values for {canonical!r} and alias {alias!r}")
        data.setdefault(canonical, data[alias])
        del data[alias]
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown contract fields: {', '.join(unknown)}")
    return data


def _without_reference_locator(value: Any) -> Any:
    """Remove only locator fields on a known reference record.

    This is deliberately non-recursive. A scientific payload may legitimately
    use names such as ``adsorption_site.locator`` or ``trajectory.created_at``;
    deleting those by spelling would create hash collisions.
    """
    if value is None:
        return None
    result = _json_value(value)
    if not isinstance(result, dict):
        raise TypeError("reference value must be a mapping")
    for key in tuple(result):
        lowered = key.lower()
        if lowered == "locator" or lowered.endswith("_locator"):
            result.pop(key, None)
    return result


def _contract_hash(value: Any, expected_type: type, field_name: str) -> str:
    if isinstance(value, expected_type):
        return value.semantic_sha256
    if isinstance(value, str):
        return _sha256(value, field_name)
    raise TypeError(f"{field_name} binding must be a SHA-256 string or {expected_type.__name__}")


class _SemanticContract:
    """Shared digest conveniences for the frozen public contracts."""

    def semantic_payload(self) -> dict[str, Any]:  # pragma: no cover - abstract contract
        raise NotImplementedError

    def semantic_hash(self) -> str:
        return sha256_json(self.semantic_payload())

    @property
    def semantic_sha256(self) -> str:
        return self.semantic_hash()

    @property
    def sha256(self) -> str:
        return self.semantic_hash()


@dataclass(frozen=True)
class ReportSpec(_SemanticContract):
    """The normalized request describing what report the user asked for."""

    schema: str = REPORT_SPEC_SCHEMA
    preset_id: str = "scientific-review"
    requested_kind: str = "diagnostic"
    audience: str = "researcher"
    locale: str = "zh-CN"
    formats: tuple[str, ...] = SUPPORTED_FORMATS
    scope: Mapping[str, Any] = field(default_factory=dict)
    outline: tuple[str, ...] = DEFAULT_OUTLINE
    theme_id: str = "default"
    template_ref: Mapping[str, Any] | None = None
    policy_refs: tuple[Mapping[str, Any], ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)
    created_at_utc: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _schema(self.schema, REPORT_SPEC_SCHEMA))
        object.__setattr__(self, "preset_id", _nonempty_string(self.preset_id, "preset_id"))
        object.__setattr__(
            self, "requested_kind", _enum(self.requested_kind, "report_kind", REPORT_KINDS)
        )
        object.__setattr__(self, "audience", _nonempty_string(self.audience, "audience"))
        object.__setattr__(self, "locale", _nonempty_string(self.locale, "locale"))
        object.__setattr__(self, "formats", _normalize_formats(self.formats))
        object.__setattr__(self, "scope", _freeze_mapping(self.scope, "scope"))
        object.__setattr__(
            self,
            "outline",
            _string_tuple(self.outline, "outline", allow_empty=False, unique=True),
        )
        object.__setattr__(self, "theme_id", _nonempty_string(self.theme_id, "theme_id"))
        template_ref = (
            None if self.template_ref is None
            else _freeze_mapping(self.template_ref, "template_ref")
        )
        object.__setattr__(self, "template_ref", template_ref)
        object.__setattr__(
            self, "policy_refs", _mapping_sequence(self.policy_refs, "policy_refs")
        )
        object.__setattr__(self, "options", _freeze_mapping(self.options, "options"))
        object.__setattr__(
            self, "extensions", _freeze_mapping(self.extensions, "extensions")
        )
        object.__setattr__(
            self, "created_at_utc", _normalize_utc(self.created_at_utc, "created_at_utc")
        )
        canonical_json_bytes(self.to_dict())

    @property
    def report_kind(self) -> str:
        """Alias used by report model and manifest layers."""

        return self.requested_kind

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReportSpec":
        allowed = {item.name for item in fields(cls)}
        data = _prepare_mapping(value, allowed, aliases={"report_kind": "requested_kind"})
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "preset_id": self.preset_id,
            "requested_kind": self.requested_kind,
            "audience": self.audience,
            "locale": self.locale,
            "formats": list(self.formats),
            "scope": _thaw(self.scope),
            "outline": list(self.outline),
            "theme_id": self.theme_id,
            "template_ref": None if self.template_ref is None else _thaw(self.template_ref),
            "policy_refs": [_thaw(item) for item in self.policy_refs],
            "options": _thaw(self.options),
            "extensions": _thaw(self.extensions),
            "created_at_utc": self.created_at_utc,
        }

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("created_at_utc", None)
        payload["template_ref"] = _without_reference_locator(payload["template_ref"])
        payload["policy_refs"] = [
            _without_reference_locator(item) for item in payload["policy_refs"]
        ]
        return payload


def _normalize_sources(value: Any) -> tuple[_FrozenDict, ...]:
    sources = _mapping_sequence(value, "sources")
    normalized: list[_FrozenDict] = []
    seen: set[str] = set()
    for frozen in sources:
        source = _thaw(frozen)
        source_id = _nonempty_string(source.get("source_id"), "sources.source_id")
        if source_id in seen:
            raise ValueError(f"duplicate source_id: {source_id!r}")
        seen.add(source_id)
        source["source_id"] = source_id
        if "sha256" in source:
            source["sha256"] = _sha256(source["sha256"], "sources.sha256")
        if "size" in source:
            size = source["size"]
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("sources.size must be a non-negative integer")
        normalized.append(_freeze_mapping(source, "sources"))
    normalized.sort(key=lambda item: item["source_id"])
    return tuple(normalized)


@dataclass(frozen=True)
class ReportSnapshot(_SemanticContract):
    """A frozen scientific input snapshot bound to one :class:`ReportSpec`."""

    spec_sha256: str
    input_fingerprint: str
    schema: str = REPORT_SNAPSHOT_SCHEMA
    created_at_utc: str | None = None
    resolved_scope: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[Mapping[str, Any], ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _schema(self.schema, REPORT_SNAPSHOT_SCHEMA))
        object.__setattr__(self, "spec_sha256", _sha256(self.spec_sha256, "spec_sha256"))
        object.__setattr__(
            self,
            "input_fingerprint",
            _nonempty_string(self.input_fingerprint, "input_fingerprint"),
        )
        object.__setattr__(
            self, "created_at_utc", _normalize_utc(self.created_at_utc, "created_at_utc")
        )
        object.__setattr__(
            self,
            "resolved_scope",
            _freeze_mapping(self.resolved_scope, "resolved_scope"),
        )
        object.__setattr__(self, "sources", _normalize_sources(self.sources))
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))
        object.__setattr__(self, "evidence", _freeze_mapping(self.evidence, "evidence"))
        object.__setattr__(
            self, "extensions", _freeze_mapping(self.extensions, "extensions")
        )
        canonical_json_bytes(self.to_dict())

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        spec: ReportSpec | str | None = None,
    ) -> "ReportSnapshot":
        data = _prepare_mapping(value, {item.name for item in fields(cls)})
        snapshot = cls(**data)
        if spec is not None:
            snapshot.assert_bound_to(spec)
        return snapshot

    def assert_bound_to(self, spec: ReportSpec | str) -> None:
        expected = _contract_hash(spec, ReportSpec, "spec_sha256")
        if self.spec_sha256 != expected:
            raise ValueError(
                f"snapshot spec binding mismatch: {self.spec_sha256} != {expected}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "created_at_utc": self.created_at_utc,
            "spec_sha256": self.spec_sha256,
            "input_fingerprint": self.input_fingerprint,
            "resolved_scope": _thaw(self.resolved_scope),
            "sources": [_thaw(item) for item in self.sources],
            "payload": _thaw(self.payload),
            "evidence": _thaw(self.evidence),
            "extensions": _thaw(self.extensions),
        }

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("created_at_utc", None)
        payload["sources"] = [
            _without_reference_locator(item) for item in payload["sources"]
        ]
        return payload


@dataclass(frozen=True)
class ValidationCheck:
    """One auditable validation check contributing to the report gate."""

    id: str
    status: str = "unknown"
    severity: str = "blocking"
    required: bool = True
    message: str = ""
    evidence_refs: tuple[str, ...] = ()
    remediation: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _nonempty_string(self.id, "checks.id"))
        object.__setattr__(
            self, "status", _enum(self.status, "checks.status", CHECK_STATUSES)
        )
        object.__setattr__(
            self, "severity", _enum(self.severity, "checks.severity", CHECK_SEVERITIES)
        )
        if not isinstance(self.required, bool):
            raise TypeError("checks.required must be a bool")
        if not isinstance(self.message, str):
            raise TypeError("checks.message must be a string")
        object.__setattr__(
            self,
            "evidence_refs",
            _string_tuple(self.evidence_refs, "checks.evidence_refs", unique=True),
        )
        object.__setattr__(
            self, "remediation", _optional_string(self.remediation, "checks.remediation")
        )
        object.__setattr__(
            self, "extensions", _freeze_mapping(self.extensions, "checks.extensions")
        )
        canonical_json_bytes(self.to_dict())

    @property
    def blocks_final(self) -> bool:
        return (self.required or self.severity == "blocking") and self.status != "pass"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ValidationCheck":
        data = _prepare_mapping(value, {item.name for item in fields(cls)})
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "severity": self.severity,
            "required": self.required,
            "message": self.message,
            "evidence_refs": list(self.evidence_refs),
            "remediation": self.remediation,
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True)
class ClaimRecord:
    """A bounded scientific claim and the evidence level supporting it."""

    id: str
    text: str
    qualification: str = "diagnostic"
    status: str = "unknown"
    evidence_refs: tuple[str, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _nonempty_string(self.id, "claims.id"))
        object.__setattr__(self, "text", _nonempty_string(self.text, "claims.text"))
        object.__setattr__(
            self,
            "qualification",
            _enum(self.qualification, "claims.qualification", QUALIFICATION_LEVELS),
        )
        object.__setattr__(
            self, "status", _enum(self.status, "claims.status", CLAIM_STATUSES)
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _string_tuple(self.evidence_refs, "claims.evidence_refs", unique=True),
        )
        object.__setattr__(
            self, "extensions", _freeze_mapping(self.extensions, "claims.extensions")
        )
        canonical_json_bytes(self.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClaimRecord":
        data = _prepare_mapping(
            value, {item.name for item in fields(cls)}, aliases={"claim_id": "id"}
        )
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "qualification": self.qualification,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "extensions": _thaw(self.extensions),
        }


def _normalize_checks(value: Any) -> tuple[ValidationCheck, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("checks must be a list or tuple")
    result = tuple(
        item if isinstance(item, ValidationCheck) else ValidationCheck.from_mapping(item)
        for item in value
    )
    ids = [item.id for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("checks must have unique ids")
    return result


def _normalize_claims(value: Any) -> tuple[ClaimRecord, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("claims must be a list or tuple")
    result = tuple(
        item if isinstance(item, ClaimRecord) else ClaimRecord.from_mapping(item)
        for item in value
    )
    ids = [item.id for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("claims must have unique ids")
    return result


def _normalize_human_review(value: Any) -> _FrozenDict:
    if not isinstance(value, Mapping):
        raise TypeError("human_review must be a mapping")
    review = _json_value(value)
    if not review:
        return _FrozenDict({})
    if "reviewed_at_utc" in review:
        review["reviewed_at_utc"] = _normalize_utc(
            review["reviewed_at_utc"], "human_review.reviewed_at_utc"
        )
    if "evidence_refs" in review:
        review["evidence_refs"] = list(_string_tuple(
            review["evidence_refs"],
            "human_review.evidence_refs",
            allow_empty=False,
            unique=True,
        ))
    return _freeze_mapping(review, "human_review")


@dataclass(frozen=True)
class ValidationResult(_SemanticContract):
    """Fail-closed scientific validation bound to an exact spec and snapshot."""

    spec_sha256: str
    snapshot_sha256: str
    schema: str = VALIDATION_RESULT_SCHEMA
    validated_at_utc: str | None = None
    validator: Mapping[str, Any] = field(default_factory=dict)
    status: str = "unknown"
    effective_kind: str = "diagnostic"
    final_allowed: bool = False
    scientific_qualification: str = "diagnostic"
    claim_ceiling: str = ""
    report_model_sha256: str = ""
    checks: tuple[ValidationCheck, ...] = ()
    claims: tuple[ClaimRecord, ...] = ()
    human_review: Mapping[str, Any] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", _schema(self.schema, VALIDATION_RESULT_SCHEMA))
        object.__setattr__(self, "spec_sha256", _sha256(self.spec_sha256, "spec_sha256"))
        object.__setattr__(
            self, "snapshot_sha256", _sha256(self.snapshot_sha256, "snapshot_sha256")
        )
        object.__setattr__(
            self,
            "validated_at_utc",
            _normalize_utc(self.validated_at_utc, "validated_at_utc"),
        )
        object.__setattr__(self, "validator", _freeze_mapping(self.validator, "validator"))
        object.__setattr__(
            self, "status", _enum(self.status, "validation status", VALIDATION_STATUSES)
        )
        object.__setattr__(
            self, "effective_kind", _enum(self.effective_kind, "report_kind", REPORT_KINDS)
        )
        if not isinstance(self.final_allowed, bool):
            raise TypeError("final_allowed must be a bool")
        object.__setattr__(
            self,
            "scientific_qualification",
            _enum(
                self.scientific_qualification,
                "scientific_qualification",
                QUALIFICATION_LEVELS,
            ),
        )
        if not isinstance(self.claim_ceiling, str):
            raise TypeError("claim_ceiling must be a string")
        if not isinstance(self.report_model_sha256, str):
            raise TypeError("report_model_sha256 must be a string")
        if self.report_model_sha256:
            object.__setattr__(
                self,
                "report_model_sha256",
                _sha256(self.report_model_sha256, "report_model_sha256"),
            )
        object.__setattr__(self, "checks", _normalize_checks(self.checks))
        object.__setattr__(self, "claims", _normalize_claims(self.claims))
        object.__setattr__(self, "human_review", _normalize_human_review(self.human_review))
        object.__setattr__(
            self, "extensions", _freeze_mapping(self.extensions, "extensions")
        )

        qualification_rank = QUALIFICATION_LEVELS.index(self.scientific_qualification)
        excessive_claims = [
            claim.id for claim in self.claims
            if (claim.status == "supported"
                and QUALIFICATION_LEVELS.index(claim.qualification) > qualification_rank)
        ]
        if excessive_claims:
            raise ValueError(
                "supported claims cannot exceed the validation scientific_qualification: "
                + ", ".join(excessive_claims)
            )

        blocking = any(check.blocks_final for check in self.checks)
        warning_checks = [
            check for check in self.checks
            if not check.blocks_final
            and check.status not in {"pass", "not_applicable"}
        ]
        if self.status == "passed" and blocking:
            raise ValueError(
                "passed validation cannot contain a non-passing required or "
                "blocking check"
            )
        if self.status == "passed" and warning_checks:
            raise ValueError(
                "passed validation requires every applicable check to pass"
            )
        if self.status == "passed_with_warnings" and blocking:
            raise ValueError(
                "passed_with_warnings validation cannot contain a non-passing "
                "required or blocking check"
            )
        if self.status == "passed_with_warnings" and not warning_checks:
            raise ValueError(
                "passed_with_warnings validation requires at least one non-blocking warning"
            )
        if self.status == "blocked" and not blocking:
            raise ValueError("blocked validation requires a non-passing required or blocking check")
        if (self.status == "unknown" and self.checks
                and not any(check.status == "unknown" for check in self.checks)):
            raise ValueError("unknown validation status requires an unknown check")

        validator_id = self.validator.get("id")
        validator_version = self.validator.get("version")
        verified_qualification = self.scientific_qualification != "diagnostic"
        if self.final_allowed:
            if not isinstance(validator_id, str) or not validator_id.strip():
                raise ValueError("final_allowed requires validator.id")
            if not isinstance(validator_version, str) or not validator_version.strip():
                raise ValueError("final_allowed requires validator.version")
            if not self.checks or not any(
                    check.required or check.severity == "blocking"
                    for check in self.checks):
                raise ValueError(
                    "final_allowed requires at least one required or blocking check"
                )
            if self.status not in {"passed", "passed_with_warnings"}:
                raise ValueError("final_allowed requires a passed validation status")
            if blocking:
                raise ValueError(
                    "final_allowed requires every required or blocking check to pass"
                )
            if self.scientific_qualification == "diagnostic":
                raise ValueError("final_allowed requires a verified scientific qualification")
        if verified_qualification:
            if not self.report_model_sha256:
                raise ValueError(
                    "verified scientific qualification requires report_model_sha256"
                )
            if not isinstance(validator_id, str) or not validator_id.strip():
                raise ValueError("verified scientific qualification requires validator.id")
            if not isinstance(validator_version, str) or not validator_version.strip():
                raise ValueError("verified scientific qualification requires validator.version")
            if self.status == "unknown":
                raise ValueError(
                    "unknown validation status cannot carry a verified scientific qualification"
                )
            if not any(
                    check.status == "pass"
                    and (check.required or check.severity == "blocking")
                    for check in self.checks):
                raise ValueError(
                    "verified scientific qualification requires at least one passing "
                    "required or blocking check"
                )
        if self.scientific_qualification == "human_scientific_reviewed":
            review = self.human_review
            if review.get("reviewer_type") != "human":
                raise ValueError(
                    "human_scientific_reviewed requires human_review.reviewer_type='human'"
                )
            if not isinstance(review.get("reviewed_by"), str) or not str(
                    review.get("reviewed_by")).strip():
                raise ValueError(
                    "human_scientific_reviewed requires human_review.reviewed_by"
                )
            if review.get("decision") != "approved":
                raise ValueError(
                    "human_scientific_reviewed requires human_review.decision='approved'"
                )
            if not review.get("reviewed_at_utc"):
                raise ValueError(
                    "human_scientific_reviewed requires human_review.reviewed_at_utc"
                )
            if not review.get("evidence_refs"):
                raise ValueError(
                    "human_scientific_reviewed requires human_review.evidence_refs"
                )
        if self.effective_kind == "final" and not self.final_allowed:
            raise ValueError("effective_kind='final' requires final_allowed=True")
        if self.effective_kind == "final" and self.scientific_qualification == "diagnostic":
            raise ValueError("a final report cannot have diagnostic qualification")
        canonical_json_bytes(self.to_dict())

    @property
    def report_kind(self) -> str:
        return self.effective_kind

    @property
    def qualification(self) -> str:
        return self.scientific_qualification

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        spec: ReportSpec | str | None = None,
        snapshot: ReportSnapshot | str | None = None,
    ) -> "ValidationResult":
        data = _prepare_mapping(
            value,
            {item.name for item in fields(cls)},
            aliases={
                "report_kind": "effective_kind",
                "qualification": "scientific_qualification",
            },
        )
        result = cls(**data)
        if spec is not None or snapshot is not None:
            result.assert_bound_to(spec=spec, snapshot=snapshot)
        return result

    def assert_bound_to(
        self,
        spec: ReportSpec | str | None = None,
        snapshot: ReportSnapshot | str | None = None,
    ) -> None:
        expected_spec: str | None = None
        if spec is not None:
            expected_spec = _contract_hash(spec, ReportSpec, "spec_sha256")
            if self.spec_sha256 != expected_spec:
                raise ValueError(
                    f"validation spec binding mismatch: {self.spec_sha256} != {expected_spec}"
                )
            if (isinstance(spec, ReportSpec)
                    and self.effective_kind == "final"
                    and spec.requested_kind != "final"):
                raise ValueError(
                    "validation cannot upgrade a non-final ReportSpec to final"
                )
        if snapshot is not None:
            expected_snapshot = _contract_hash(
                snapshot, ReportSnapshot, "snapshot_sha256"
            )
            if self.snapshot_sha256 != expected_snapshot:
                raise ValueError(
                    "validation snapshot binding mismatch: "
                    f"{self.snapshot_sha256} != {expected_snapshot}"
                )
            if isinstance(snapshot, ReportSnapshot):
                if self.spec_sha256 != snapshot.spec_sha256:
                    raise ValueError(
                        "validation and snapshot spec bindings differ: "
                        f"{self.spec_sha256} != {snapshot.spec_sha256}"
                    )
                if expected_spec is not None:
                    snapshot.assert_bound_to(expected_spec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "validated_at_utc": self.validated_at_utc,
            "spec_sha256": self.spec_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "validator": _thaw(self.validator),
            "status": self.status,
            "effective_kind": self.effective_kind,
            "final_allowed": self.final_allowed,
            "scientific_qualification": self.scientific_qualification,
            "claim_ceiling": self.claim_ceiling,
            "report_model_sha256": self.report_model_sha256,
            "checks": [item.to_dict() for item in self.checks],
            "claims": [item.to_dict() for item in self.claims],
            "human_review": _thaw(self.human_review),
            "extensions": _thaw(self.extensions),
        }

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("validated_at_utc", None)
        payload["validator"] = _without_reference_locator(payload["validator"])
        return payload


def validate_bindings(
    spec: ReportSpec,
    snapshot: ReportSnapshot,
    validation: ValidationResult,
) -> None:
    """Raise :class:`ValueError` unless all three contracts form one exact chain."""

    if not isinstance(spec, ReportSpec):
        raise TypeError("spec must be a ReportSpec")
    if not isinstance(snapshot, ReportSnapshot):
        raise TypeError("snapshot must be a ReportSnapshot")
    if not isinstance(validation, ValidationResult):
        raise TypeError("validation must be a ValidationResult")
    snapshot.assert_bound_to(spec)
    validation.assert_bound_to(spec=spec, snapshot=snapshot)


__all__ = [
    "CHECK_SEVERITIES",
    "CHECK_STATUSES",
    "CLAIM_STATUSES",
    "DEFAULT_OUTLINE",
    "QUALIFICATION_LEVELS",
    "REPORT_KINDS",
    "REPORT_SNAPSHOT_SCHEMA",
    "REPORT_SPEC_SCHEMA",
    "SUPPORTED_FORMATS",
    "VALIDATION_RESULT_SCHEMA",
    "VALIDATION_STATUSES",
    "ClaimRecord",
    "ReportSnapshot",
    "ReportSpec",
    "ValidationCheck",
    "ValidationResult",
    "canonical_json_bytes",
    "sha256_json",
    "validate_bindings",
]
