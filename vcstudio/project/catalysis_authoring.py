"""Path-free authoring core for explicitly selecting one ReactionNetwork head.

The production projection authority does not currently expose a public method
that enumerates every ReactionNetwork head and its closure while holding one
binding/domain lock.  This module therefore consumes a narrow injected snapshot
protocol.  It never assembles an authority view by issuing per-candidate reads.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from vcstudio.project.catalysis_contracts import CatalysisContractError, reject_sensitive
from vcstudio.project.catalysis_projection import (
    ActiveBindingAuthoritySnapshot,
    BindingWriteResult,
    CatalysisProjectionError,
    NetworkHeadCAS,
)


BOOTSTRAP_SCHEMA = "vcstudio.catalysis-authoring-bootstrap/v1"
CANDIDATE_SCHEMA = "vcstudio.catalysis-authoring-network-candidate/v1"
CANDIDATE_SNAPSHOT_SCHEMA = "vcstudio.catalysis-authoring-candidate-snapshot/v1"
PREVIEW_REQUEST_SCHEMA = "vcstudio.catalysis-active-network-preview-request/v1"
PREVIEW_SCHEMA = "vcstudio.catalysis-active-network-preview/v1"
CONFIRMATION_SCHEMA = "vcstudio.catalysis-active-network-confirmation/v1"
CONFIRM_RESULT_SCHEMA = "vcstudio.catalysis-active-network-confirm-result/v1"
CONFLICT_SCHEMA = "vcstudio.catalysis-active-network-conflict/v1"

MAX_CANDIDATES = 512
MAX_GAPS = 64
MAX_GAP_CODES = 256
MAX_PUBLIC_BYTES = 1024 * 1024
DEFAULT_PREVIEW_TTL_SECONDS = 300
MAX_PREVIEW_TTL_SECONDS = 900

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:+-]{0,159}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
_RAW_FACT_KEY_PARTS = frozenset(
    {
        "actor",
        "charge",
        "composition",
        "credential",
        "energy",
        "envelope",
        "formula",
        "path",
        "payload",
        "phase",
        "pressure",
        "raw",
        "secret",
        "temperature",
        "token",
    }
)


class CatalysisAuthoringError(ValueError):
    """An authoring DTO, adapter or signed confirmation is invalid."""


class CatalysisAuthoringValidationError(CatalysisAuthoringError):
    """A caller-owned request violates the strict public contract."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalysisAuthoringError("authoring value must be canonical finite JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _detached(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _detached(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_detached(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            raise CatalysisAuthoringError("authoring value contains a non-finite number")
        return value
    raise CatalysisAuthoringError("authoring value is not JSON")


def _strict(value: Any, fields: set[str] | frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise CatalysisAuthoringValidationError(f"{label} has unknown or missing fields")
    try:
        reject_sensitive(value, field=label)
    except CatalysisContractError as exc:
        raise CatalysisAuthoringValidationError(
            f"{label} contains a private field or unsafe value"
        ) from exc
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or value in {".", ".."}:
        raise CatalysisAuthoringValidationError(f"{label} must be an opaque identifier")
    try:
        reject_sensitive(value, field=label)
    except CatalysisContractError as exc:
        raise CatalysisAuthoringValidationError(
            f"{label} must be an opaque identifier"
        ) from exc
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise CatalysisAuthoringValidationError(f"{label} must be a lowercase SHA-256")
    return value


def _bounded_integer(value: Any, label: str, *, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CatalysisAuthoringValidationError(f"{label} is outside its integer bounds")
    return value


def _reject_raw_fact_keys(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if set(normalized.split("_")) & _RAW_FACT_KEY_PARTS:
                raise CatalysisAuthoringValidationError(f"{label} contains raw or private facts")
            _reject_raw_fact_keys(item, label)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_raw_fact_keys(item, label)


@dataclass(frozen=True)
class CatalysisAuthoringGap:
    """Bounded diagnostic without envelopes, formulas, energies, actors or paths."""

    status: str
    code: str
    object_type: str
    object_id: str | None
    context: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.status not in {"missing", "unavailable", "blocked"}:
            raise CatalysisAuthoringValidationError("gap.status is invalid")
        object.__setattr__(self, "code", _identifier(self.code, "gap.code"))
        object.__setattr__(self, "object_type", _identifier(self.object_type, "gap.object_type"))
        if self.object_id is not None:
            object.__setattr__(self, "object_id", _identifier(self.object_id, "gap.object_id"))
        if not isinstance(self.context, Mapping) or len(self.context) > 32:
            raise CatalysisAuthoringValidationError("gap.context is invalid")
        context = _detached(self.context)
        try:
            reject_sensitive(context, field="gap.context")
        except CatalysisContractError as exc:
            raise CatalysisAuthoringValidationError("gap.context is not public") from exc
        _reject_raw_fact_keys(context, "gap.context")
        if len(_canonical_bytes(context)) > 16 * 1024:
            raise CatalysisAuthoringValidationError("gap.context exceeds its byte limit")
        object.__setattr__(self, "context", context)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "context": _detached(self.context),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringGap":
        _strict(value, {"status", "code", "object_type", "object_id", "context"}, "gap")
        return cls(**dict(value))


@dataclass(frozen=True)
class CatalysisAuthoringGapSummary:
    total: int
    returned: int
    omitted: int
    by_code: Mapping[str, int]

    def __post_init__(self) -> None:
        total = _bounded_integer(self.total, "gap_summary.total", maximum=100_000)
        returned = _bounded_integer(self.returned, "gap_summary.returned", maximum=MAX_GAPS)
        omitted = _bounded_integer(self.omitted, "gap_summary.omitted", maximum=100_000)
        if total != returned + omitted:
            raise CatalysisAuthoringValidationError("gap summary totals are inconsistent")
        if not isinstance(self.by_code, Mapping) or len(self.by_code) > MAX_GAP_CODES:
            raise CatalysisAuthoringValidationError("gap_summary.by_code is invalid")
        by_code = {
            _identifier(code, "gap_summary.by_code key"): _bounded_integer(
                count, f"gap_summary.by_code.{code}", minimum=1, maximum=100_000
            )
            for code, count in sorted(self.by_code.items())
        }
        if sum(by_code.values()) != total:
            raise CatalysisAuthoringValidationError("gap summary code counts are inconsistent")
        object.__setattr__(self, "by_code", by_code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "returned": self.returned,
            "omitted": self.omitted,
            "by_code": dict(self.by_code),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringGapSummary":
        _strict(value, {"total", "returned", "omitted", "by_code"}, "gap_summary")
        return cls(**dict(value))


def _network_head(value: NetworkHeadCAS | Mapping[str, Any]) -> NetworkHeadCAS:
    try:
        source = value.to_dict() if isinstance(value, NetworkHeadCAS) else value
        return NetworkHeadCAS.from_dict(source)
    except (CatalysisContractError, TypeError, ValueError) as exc:
        raise CatalysisAuthoringValidationError("network head CAS is invalid") from exc


def _binding_snapshot(
    value: ActiveBindingAuthoritySnapshot | Mapping[str, Any],
) -> ActiveBindingAuthoritySnapshot:
    try:
        source = value.to_dict() if isinstance(value, ActiveBindingAuthoritySnapshot) else value
        return ActiveBindingAuthoritySnapshot.from_dict(source)
    except (CatalysisContractError, TypeError, ValueError) as exc:
        raise CatalysisAuthoringValidationError("binding snapshot is invalid") from exc


@dataclass(frozen=True)
class CatalysisAuthoringNetworkCandidate:
    """Bootstrap-safe summary of one ReactionNetwork head and closure status."""

    network_head: NetworkHeadCAS | Mapping[str, Any]
    projection_status: str
    gap_summary: CatalysisAuthoringGapSummary | Mapping[str, Any]
    closure_sha256: str
    schema: str = CANDIDATE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_SCHEMA:
            raise CatalysisAuthoringValidationError("candidate schema is unsupported")
        head = _network_head(self.network_head)
        if head.network_status != "available":
            raise CatalysisAuthoringValidationError(
                "bootstrap candidates must be available ReactionNetwork heads"
            )
        if self.projection_status not in {"available", "unavailable"}:
            raise CatalysisAuthoringValidationError("candidate projection status is invalid")
        summary = (
            self.gap_summary
            if isinstance(self.gap_summary, CatalysisAuthoringGapSummary)
            else CatalysisAuthoringGapSummary.from_dict(self.gap_summary)
        )
        if (self.projection_status == "available") != (summary.total == 0):
            raise CatalysisAuthoringValidationError(
                "candidate projection status disagrees with its gaps"
            )
        object.__setattr__(self, "network_head", head)
        object.__setattr__(self, "gap_summary", summary)
        object.__setattr__(self, "closure_sha256", _sha(self.closure_sha256, "candidate.closure_sha256"))

    @property
    def network_id(self) -> str:
        return self.network_head.network_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "network_head": self.network_head.to_dict(),
            "projection_status": self.projection_status,
            "gap_summary": self.gap_summary.to_dict(),
            "closure_sha256": self.closure_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringNetworkCandidate":
        _strict(
            value,
            {"schema", "network_head", "projection_status", "gap_summary", "closure_sha256"},
            "network candidate",
        )
        return cls(**dict(value))


@dataclass(frozen=True)
class CatalysisAuthoringCandidateSnapshot:
    """One single-lock binding/domain/closure view used by preview and confirm."""

    project_id: str
    binding: ActiveBindingAuthoritySnapshot | Mapping[str, Any]
    network_head: NetworkHeadCAS | Mapping[str, Any]
    projection_status: str
    gaps: Sequence[CatalysisAuthoringGap | Mapping[str, Any]]
    gap_summary: CatalysisAuthoringGapSummary | Mapping[str, Any]
    closure_sha256: str = ""
    snapshot_sha256: str = ""
    schema: str = CANDIDATE_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_SNAPSHOT_SCHEMA:
            raise CatalysisAuthoringValidationError("candidate snapshot schema is unsupported")
        project_id = _identifier(self.project_id, "candidate_snapshot.project_id")
        binding = _binding_snapshot(self.binding)
        head = _network_head(self.network_head)
        if self.projection_status not in {"available", "unavailable"}:
            raise CatalysisAuthoringValidationError("candidate snapshot status is invalid")
        if (
            isinstance(self.gaps, (str, bytes))
            or not isinstance(self.gaps, Sequence)
            or len(self.gaps) > MAX_GAPS
        ):
            raise CatalysisAuthoringValidationError("candidate snapshot gaps are invalid")
        gaps = tuple(
            item if isinstance(item, CatalysisAuthoringGap) else CatalysisAuthoringGap.from_dict(item)
            for item in self.gaps
        )
        ordered = tuple(sorted(gaps, key=lambda item: _canonical_bytes(item.to_dict())))
        if gaps != ordered:
            raise CatalysisAuthoringValidationError("candidate snapshot gaps must be canonical")
        summary = (
            self.gap_summary
            if isinstance(self.gap_summary, CatalysisAuthoringGapSummary)
            else CatalysisAuthoringGapSummary.from_dict(self.gap_summary)
        )
        if summary.returned != len(gaps):
            raise CatalysisAuthoringValidationError("candidate returned gap count is invalid")
        if (self.projection_status == "available") != (summary.total == 0):
            raise CatalysisAuthoringValidationError(
                "candidate snapshot status disagrees with its gaps"
            )
        closure_material = {
            "network_head": head.to_dict(),
            "projection_status": self.projection_status,
            "gaps": [item.to_dict() for item in gaps],
            "gap_summary": summary.to_dict(),
        }
        closure_sha = _digest(closure_material)
        if self.closure_sha256 not in {"", closure_sha}:
            raise CatalysisAuthoringValidationError("candidate closure seal is invalid")
        material = {
            "schema": CANDIDATE_SNAPSHOT_SCHEMA,
            "project_id": project_id,
            "binding": binding.to_dict(),
            **closure_material,
            "closure_sha256": closure_sha,
        }
        snapshot_sha = _digest(material)
        if self.snapshot_sha256 not in {"", snapshot_sha}:
            raise CatalysisAuthoringValidationError("candidate snapshot seal is invalid")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "network_head", head)
        object.__setattr__(self, "gaps", gaps)
        object.__setattr__(self, "gap_summary", summary)
        object.__setattr__(self, "closure_sha256", closure_sha)
        object.__setattr__(self, "snapshot_sha256", snapshot_sha)
        if len(_canonical_bytes(self.to_dict())) > MAX_PUBLIC_BYTES:
            raise CatalysisAuthoringValidationError("candidate snapshot exceeds its byte limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "binding": self.binding.to_dict(),
            "network_head": self.network_head.to_dict(),
            "projection_status": self.projection_status,
            "gaps": [item.to_dict() for item in self.gaps],
            "gap_summary": self.gap_summary.to_dict(),
            "closure_sha256": self.closure_sha256,
            "snapshot_sha256": self.snapshot_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringCandidateSnapshot":
        _strict(
            value,
            {
                "schema",
                "project_id",
                "binding",
                "network_head",
                "projection_status",
                "gaps",
                "gap_summary",
                "closure_sha256",
                "snapshot_sha256",
            },
            "candidate snapshot",
        )
        return cls(**dict(value))

    def to_candidate(self) -> CatalysisAuthoringNetworkCandidate:
        return CatalysisAuthoringNetworkCandidate(
            network_head=self.network_head,
            projection_status=self.projection_status,
            gap_summary=self.gap_summary,
            closure_sha256=self.closure_sha256,
        )


@dataclass(frozen=True)
class CatalysisAuthoringDomainCAS:
    authority_id: str
    generation: int
    snapshot_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority_id, str) or not _AUTHORITY_RE.fullmatch(
            self.authority_id
        ):
            raise CatalysisAuthoringValidationError("domain CAS authority is invalid")
        _bounded_integer(self.generation, "domain_cas.generation", minimum=1)
        object.__setattr__(
            self, "snapshot_sha256", _sha(self.snapshot_sha256, "domain_cas.snapshot_sha256")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "generation": self.generation,
            "snapshot_sha256": self.snapshot_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringDomainCAS":
        _strict(value, {"authority_id", "generation", "snapshot_sha256"}, "domain CAS")
        return cls(**dict(value))


@dataclass(frozen=True)
class CatalysisAuthoringBootstrap:
    """Single-lock, summary-only bootstrap snapshot for an authoring client."""

    project_id: str
    status: str
    reason: str | None
    domain_cas: CatalysisAuthoringDomainCAS | Mapping[str, Any] | None
    binding_cas: ActiveBindingAuthoritySnapshot | Mapping[str, Any]
    candidates: Sequence[CatalysisAuthoringNetworkCandidate | Mapping[str, Any]]
    active_network_id: str | None = None
    active_status: str = "none"
    snapshot_sha256: str = ""
    schema: str = BOOTSTRAP_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != BOOTSTRAP_SCHEMA:
            raise CatalysisAuthoringValidationError("bootstrap schema is unsupported")
        project_id = _identifier(self.project_id, "bootstrap.project_id")
        if self.status not in {"available", "missing", "unavailable"}:
            raise CatalysisAuthoringValidationError("bootstrap status is invalid")
        reason = None if self.reason is None else _identifier(self.reason, "bootstrap.reason")
        domain = self.domain_cas
        if domain is not None and not isinstance(domain, CatalysisAuthoringDomainCAS):
            domain = CatalysisAuthoringDomainCAS.from_dict(domain)
        binding = _binding_snapshot(self.binding_cas)
        if (
            isinstance(self.candidates, (str, bytes))
            or not isinstance(self.candidates, Sequence)
            or len(self.candidates) > MAX_CANDIDATES
        ):
            raise CatalysisAuthoringValidationError("bootstrap candidates are invalid")
        candidates = tuple(
            item
            if isinstance(item, CatalysisAuthoringNetworkCandidate)
            else CatalysisAuthoringNetworkCandidate.from_dict(item)
            for item in self.candidates
        )
        ordered = tuple(sorted(candidates, key=lambda item: item.network_id))
        if candidates != ordered or len({item.network_id for item in candidates}) != len(candidates):
            raise CatalysisAuthoringValidationError(
                "bootstrap candidates must be sorted unique networks"
            )
        if self.status == "available":
            if reason is not None or domain is None:
                raise CatalysisAuthoringValidationError("available bootstrap authority is incomplete")
            for candidate in candidates:
                head = candidate.network_head
                if (
                    head.domain_authority_id != domain.authority_id
                    or head.domain_generation != domain.generation
                    or head.domain_snapshot_sha256 != domain.snapshot_sha256
                ):
                    raise CatalysisAuthoringValidationError(
                        "bootstrap candidates cross domain snapshot generations"
                    )
        elif reason is None:
            raise CatalysisAuthoringValidationError("unavailable bootstrap requires a reason")
        binding_record = binding.binding
        if binding_record is None:
            derived_active_id = None
            derived_active_status = "none"
        else:
            derived_active_id = binding_record.network_id
            match = next(
                (item.network_head for item in candidates if item.network_id == derived_active_id),
                None,
            )
            if match is None:
                derived_active_status = "missing"
            elif (
                domain is not None
                and binding_record.domain_authority_id == domain.authority_id
                and binding_record.domain_generation == domain.generation
                and binding_record.domain_snapshot_sha256 == domain.snapshot_sha256
                and binding_record.network_revision_id == match.network_revision_id
                and binding_record.network_semantic_sha256 == match.network_semantic_sha256
            ):
                derived_active_status = "available"
            else:
                derived_active_status = "stale"
        if self.active_network_id not in {None, derived_active_id}:
            raise CatalysisAuthoringValidationError("bootstrap active network id is invalid")
        if self.active_status not in {"none", derived_active_status}:
            raise CatalysisAuthoringValidationError("bootstrap active status is invalid")
        material = {
            "schema": BOOTSTRAP_SCHEMA,
            "project_id": project_id,
            "status": self.status,
            "reason": reason,
            "domain_cas": None if domain is None else domain.to_dict(),
            "binding_cas": binding.to_dict(),
            "candidates": [item.to_dict() for item in candidates],
            "active_network_id": derived_active_id,
            "active_status": derived_active_status,
        }
        digest = _digest(material)
        if self.snapshot_sha256 not in {"", digest}:
            raise CatalysisAuthoringValidationError("bootstrap snapshot seal is invalid")
        if len(_canonical_bytes(material)) > MAX_PUBLIC_BYTES:
            raise CatalysisAuthoringValidationError("bootstrap snapshot exceeds its byte limit")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "domain_cas", domain)
        object.__setattr__(self, "binding_cas", binding)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "active_network_id", derived_active_id)
        object.__setattr__(self, "active_status", derived_active_status)
        object.__setattr__(self, "snapshot_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "status": self.status,
            "reason": self.reason,
            "domain_cas": None if self.domain_cas is None else self.domain_cas.to_dict(),
            "binding_cas": self.binding_cas.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "active_network_id": self.active_network_id,
            "active_status": self.active_status,
            "snapshot_sha256": self.snapshot_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringBootstrap":
        _strict(
            value,
            {
                "schema",
                "project_id",
                "status",
                "reason",
                "domain_cas",
                "binding_cas",
                "candidates",
                "active_network_id",
                "active_status",
                "snapshot_sha256",
            },
            "authoring bootstrap",
        )
        result = cls(**dict(value))
        if (
            value["active_network_id"] != result.active_network_id
            or value["active_status"] != result.active_status
        ):
            raise CatalysisAuthoringValidationError(
                "bootstrap active network fields are not authoritative"
            )
        return result


@dataclass(frozen=True)
class CatalysisActiveNetworkPreviewRequest:
    """Strict browser request containing only an explicit target and echoed CAS."""

    network_id: str
    intent_id: str
    expected_binding: ActiveBindingAuthoritySnapshot | Mapping[str, Any]
    expected_network_head: NetworkHeadCAS | Mapping[str, Any]
    schema: str = PREVIEW_REQUEST_SCHEMA
    _canonical: bytes = field(init=False, repr=False, compare=False)
    _semantic_sha256: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema != PREVIEW_REQUEST_SCHEMA:
            raise CatalysisAuthoringValidationError("preview request schema is unsupported")
        network_id = _identifier(self.network_id, "preview_request.network_id")
        intent_id = _identifier(self.intent_id, "preview_request.intent_id")
        binding = _binding_snapshot(self.expected_binding)
        head = _network_head(self.expected_network_head)
        if head.network_id != network_id:
            raise CatalysisAuthoringValidationError(
                "preview request network id does not match its network CAS"
            )
        material = {
            "schema": PREVIEW_REQUEST_SCHEMA,
            "network_id": network_id,
            "intent_id": intent_id,
            "expected_binding": binding.to_dict(),
            "expected_network_head": head.to_dict(),
        }
        canonical = _canonical_bytes(material)
        object.__setattr__(self, "network_id", network_id)
        object.__setattr__(self, "intent_id", intent_id)
        object.__setattr__(self, "expected_binding", binding)
        object.__setattr__(self, "expected_network_head", head)
        object.__setattr__(self, "_canonical", canonical)
        object.__setattr__(
            self, "_semantic_sha256", hashlib.sha256(canonical).hexdigest()
        )

    @property
    def semantic_sha256(self) -> str:
        return self._semantic_sha256

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical.decode("utf-8"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisActiveNetworkPreviewRequest":
        _strict(
            value,
            {
                "schema",
                "network_id",
                "intent_id",
                "expected_binding",
                "expected_network_head",
            },
            "active network preview request",
        )
        return cls(**dict(value))


@dataclass(frozen=True)
class CatalysisActiveNetworkPreview:
    project_id: str
    network_id: str
    intent_id: str
    request_sha256: str
    status: str
    reason: str | None
    action: str | None
    binding_cas: Mapping[str, Any]
    network_cas: Mapping[str, Any]
    projection_status: str
    gaps: Sequence[Mapping[str, Any]]
    gap_summary: Mapping[str, Any]
    closure_sha256: str
    issued_at_ms: int
    expires_at_ms: int
    preview_sha256: str
    can_confirm: bool
    confirmed: bool = False
    authorizes_execution: bool = False
    schema: str = PREVIEW_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PREVIEW_SCHEMA:
            raise CatalysisAuthoringError("preview schema is unsupported")
        object.__setattr__(self, "project_id", _identifier(self.project_id, "preview.project_id"))
        object.__setattr__(self, "network_id", _identifier(self.network_id, "preview.network_id"))
        object.__setattr__(self, "intent_id", _identifier(self.intent_id, "preview.intent_id"))
        object.__setattr__(
            self, "request_sha256", _sha(self.request_sha256, "preview.request_sha256")
        )
        if self.status not in {"ready", "conflict", "missing", "unavailable"}:
            raise CatalysisAuthoringError("preview status is invalid")
        if self.action not in {None, "create", "advance"}:
            raise CatalysisAuthoringError("preview action is invalid")
        if self.reason is not None:
            _identifier(self.reason, "preview.reason")
        elif self.status != "ready":
            raise CatalysisAuthoringError("non-ready preview requires a stable reason")
        binding = _binding_snapshot(self.binding_cas)
        network = _network_head(self.network_cas)
        if network.network_id != self.network_id:
            raise CatalysisAuthoringError("preview network CAS is on another axis")
        if self.projection_status not in {"available", "unavailable"}:
            raise CatalysisAuthoringError("preview projection status is invalid")
        if (
            isinstance(self.gaps, (str, bytes))
            or not isinstance(self.gaps, Sequence)
            or len(self.gaps) > MAX_GAPS
        ):
            raise CatalysisAuthoringError("preview gaps are invalid")
        gaps = tuple(CatalysisAuthoringGap.from_dict(item) for item in self.gaps)
        summary = CatalysisAuthoringGapSummary.from_dict(self.gap_summary)
        if summary.returned != len(gaps):
            raise CatalysisAuthoringError("preview gap summary is invalid")
        if (self.projection_status == "available") != (summary.total == 0):
            raise CatalysisAuthoringError("preview projection status disagrees with gaps")
        object.__setattr__(self, "binding_cas", binding.to_dict())
        object.__setattr__(self, "network_cas", network.to_dict())
        object.__setattr__(self, "gaps", tuple(item.to_dict() for item in gaps))
        object.__setattr__(self, "gap_summary", summary.to_dict())
        object.__setattr__(
            self, "closure_sha256", _sha(self.closure_sha256, "preview.closure_sha256")
        )
        issued = _bounded_integer(self.issued_at_ms, "preview.issued_at_ms")
        expires = _bounded_integer(self.expires_at_ms, "preview.expires_at_ms")
        if expires <= issued or expires - issued > MAX_PREVIEW_TTL_SECONDS * 1000:
            raise CatalysisAuthoringError("preview TTL is invalid")
        object.__setattr__(
            self, "preview_sha256", _sha(self.preview_sha256, "preview.preview_sha256")
        )
        if self.confirmed is not False or self.authorizes_execution is not False:
            raise CatalysisAuthoringError("preview must not grant confirmation or execution")
        if self.can_confirm != (self.status == "ready"):
            raise CatalysisAuthoringError("preview confirmation status is invalid")
        if self.can_confirm and self.action is None:
            raise CatalysisAuthoringError("confirmable preview is missing its action")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "project_id": self.project_id,
            "network_id": self.network_id,
            "intent_id": self.intent_id,
            "request_sha256": self.request_sha256,
            "status": self.status,
            "reason": self.reason,
            "action": self.action,
            "binding_cas": _detached(self.binding_cas),
            "network_cas": _detached(self.network_cas),
            "projection_status": self.projection_status,
            "gaps": _detached(self.gaps),
            "gap_summary": _detached(self.gap_summary),
            "closure_sha256": self.closure_sha256,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "preview_sha256": self.preview_sha256,
            "can_confirm": self.can_confirm,
            "confirmed": self.confirmed,
            "authorizes_execution": self.authorizes_execution,
        }
        if len(_canonical_bytes(value)) > MAX_PUBLIC_BYTES:
            raise CatalysisAuthoringError("preview exceeds its byte limit")
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisActiveNetworkPreview":
        _strict(
            value,
            {
                "schema",
                "project_id",
                "network_id",
                "intent_id",
                "request_sha256",
                "status",
                "reason",
                "action",
                "binding_cas",
                "network_cas",
                "projection_status",
                "gaps",
                "gap_summary",
                "closure_sha256",
                "issued_at_ms",
                "expires_at_ms",
                "preview_sha256",
                "can_confirm",
                "confirmed",
                "authorizes_execution",
            },
            "active network preview",
        )
        return cls(**dict(value))


@dataclass(frozen=True)
class CatalysisActiveNetworkConfirmation:
    request_sha256: str
    preview_sha256: str
    issued_at_ms: int
    expires_at_ms: int
    confirmed: bool
    schema: str = CONFIRMATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONFIRMATION_SCHEMA:
            raise CatalysisAuthoringValidationError("confirmation schema is unsupported")
        object.__setattr__(
            self, "request_sha256", _sha(self.request_sha256, "confirmation.request_sha256")
        )
        object.__setattr__(
            self, "preview_sha256", _sha(self.preview_sha256, "confirmation.preview_sha256")
        )
        issued = _bounded_integer(self.issued_at_ms, "confirmation.issued_at_ms")
        expires = _bounded_integer(self.expires_at_ms, "confirmation.expires_at_ms")
        if expires <= issued or expires - issued > MAX_PREVIEW_TTL_SECONDS * 1000:
            raise CatalysisAuthoringValidationError("confirmation TTL is invalid")
        if self.confirmed is not True:
            raise CatalysisAuthoringValidationError("explicit confirmation is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request_sha256": self.request_sha256,
            "preview_sha256": self.preview_sha256,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisActiveNetworkConfirmation":
        _strict(
            value,
            {
                "schema",
                "request_sha256",
                "preview_sha256",
                "issued_at_ms",
                "expires_at_ms",
                "confirmed",
            },
            "active network confirmation",
        )
        return cls(**dict(value))


@dataclass(frozen=True)
class CatalysisAuthoringConflict:
    reason: str
    latest_binding_cas: Mapping[str, Any]
    latest_network_cas: Mapping[str, Any] | None
    retry_automatically: bool = False
    schema: str = CONFLICT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONFLICT_SCHEMA or self.retry_automatically is not False:
            raise CatalysisAuthoringError("conflict authority boundary is invalid")
        object.__setattr__(self, "reason", _identifier(self.reason, "conflict.reason"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "reason": self.reason,
            "latest_binding_cas": _detached(self.latest_binding_cas),
            "latest_network_cas": _detached(self.latest_network_cas),
            "retry_automatically": self.retry_automatically,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringConflict":
        _strict(
            value,
            {
                "schema",
                "reason",
                "latest_binding_cas",
                "latest_network_cas",
                "retry_automatically",
            },
            "authoring conflict",
        )
        return cls(**dict(value))


@dataclass(frozen=True)
class CatalysisAuthoringConfirmResult:
    action: str
    binding_cas: Mapping[str, Any] | None = None
    network_cas: Mapping[str, Any] | None = None
    conflict: CatalysisAuthoringConflict | None = None
    reason: str | None = None
    schema: str = CONFIRM_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONFIRM_RESULT_SCHEMA:
            raise CatalysisAuthoringError("confirm result schema is unsupported")
        if self.action not in {"created", "advanced", "replayed", "conflict", "unavailable"}:
            raise CatalysisAuthoringError("confirm result action is invalid")
        if self.action == "conflict" and self.conflict is None:
            raise CatalysisAuthoringError("conflict result is incomplete")
        if self.reason is not None:
            object.__setattr__(self, "reason", _identifier(self.reason, "confirm_result.reason"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "action": self.action,
            "binding_cas": _detached(self.binding_cas),
            "network_cas": _detached(self.network_cas),
            "conflict": None if self.conflict is None else self.conflict.to_dict(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalysisAuthoringConfirmResult":
        _strict(
            value,
            {
                "schema",
                "action",
                "binding_cas",
                "network_cas",
                "conflict",
                "reason",
            },
            "authoring confirm result",
        )
        conflict = value["conflict"]
        return cls(
            schema=value["schema"],
            action=value["action"],
            binding_cas=value["binding_cas"],
            network_cas=value["network_cas"],
            conflict=(
                None if conflict is None else CatalysisAuthoringConflict.from_dict(conflict)
            ),
            reason=value["reason"],
        )


@runtime_checkable
class CatalysisAuthoringSnapshotAuthority(Protocol):
    """Production seam that must snapshot under one binding/domain lock."""

    def authoring_bootstrap_snapshot(self) -> CatalysisAuthoringBootstrap:
        """Return every ReactionNetwork head from one generation, summary-only."""

    def authoring_candidate_snapshot(
        self, network_id: str
    ) -> CatalysisAuthoringCandidateSnapshot | None:
        """Return one current closure and binding CAS from the same locked view."""


@runtime_checkable
class CatalysisBindingAuthority(Protocol):
    """The existing mutation seam implemented by CatalysisProjectionAuthority."""

    def bind_network(self, **kwargs: Any) -> BindingWriteResult:
        """Advance the fixed active-network binding through strict CAS."""


class CatalysisActiveNetworkAuthoringService:
    """Stateless signed-preview coordinator for active ReactionNetwork selection."""

    def __init__(
        self,
        *,
        project_id: str,
        owner_id: str,
        snapshot_authority: CatalysisAuthoringSnapshotAuthority | Any | None,
        binding_authority: CatalysisBindingAuthority | Any | None,
        preview_seal_key: bytes,
        preview_ttl_seconds: int = DEFAULT_PREVIEW_TTL_SECONDS,
        time_source: Callable[[], float] = time.time,
    ):
        self._project_id = _identifier(project_id, "service.project_id")
        self._owner_id = _identifier(owner_id, "service.owner_id")
        if not isinstance(preview_seal_key, (bytes, bytearray, memoryview)):
            raise CatalysisAuthoringError("preview seal key must be server-owned bytes")
        self._seal_key = bytes(preview_seal_key)
        if len(self._seal_key) < 32:
            raise CatalysisAuthoringError("preview seal key must contain at least 256 bits")
        self._ttl_seconds = _bounded_integer(
            preview_ttl_seconds,
            "preview_ttl_seconds",
            minimum=1,
            maximum=MAX_PREVIEW_TTL_SECONDS,
        )
        if not callable(time_source):
            raise CatalysisAuthoringError("time_source is unavailable")
        self._time_source = time_source
        self._snapshot_authority = snapshot_authority
        self._binding_authority = binding_authority

    @staticmethod
    def _empty_binding() -> ActiveBindingAuthoritySnapshot:
        return ActiveBindingAuthoritySnapshot(
            authority_id=None, revision=0, current_hash=None, binding=None
        )

    def _unavailable_bootstrap(self, reason: str, *, status: str = "unavailable") -> CatalysisAuthoringBootstrap:
        return CatalysisAuthoringBootstrap(
            project_id=self._project_id,
            status=status,
            reason=reason,
            domain_cas=None,
            binding_cas=self._empty_binding(),
            candidates=(),
        )

    def bootstrap(self) -> CatalysisAuthoringBootstrap:
        """Return exactly one injected, single-lock authority snapshot."""

        loader = getattr(self._snapshot_authority, "authoring_bootstrap_snapshot", None)
        if not callable(loader):
            return self._unavailable_bootstrap("snapshot_authority_unavailable")
        try:
            value = loader()
            if value is None:
                return self._unavailable_bootstrap("domain_authority_missing", status="missing")
            if not isinstance(value, CatalysisAuthoringBootstrap):
                raise CatalysisAuthoringError("snapshot adapter returned an unsealed bootstrap")
            snapshot = CatalysisAuthoringBootstrap.from_dict(value.to_dict())
            if snapshot.project_id != self._project_id:
                raise CatalysisAuthoringError("bootstrap belongs to another project")
            return snapshot
        except CatalysisAuthoringError:
            return self._unavailable_bootstrap("snapshot_authority_unavailable")
        except Exception:  # noqa: BLE001 - adapter errors never cross the public boundary
            return self._unavailable_bootstrap("snapshot_authority_unavailable")

    def _candidate(self, network_id: str) -> CatalysisAuthoringCandidateSnapshot | None:
        loader = getattr(self._snapshot_authority, "authoring_candidate_snapshot", None)
        if not callable(loader):
            raise CatalysisAuthoringError("candidate snapshot adapter is unavailable")
        try:
            value = loader(network_id)
        except Exception as exc:  # noqa: BLE001 - opaque authority boundary
            raise CatalysisAuthoringError("candidate snapshot is unavailable") from exc
        if value is None:
            return None
        if not isinstance(value, CatalysisAuthoringCandidateSnapshot):
            raise CatalysisAuthoringError("candidate adapter returned an unsealed snapshot")
        snapshot = CatalysisAuthoringCandidateSnapshot.from_dict(value.to_dict())
        if snapshot.project_id != self._project_id or snapshot.network_head.network_id != network_id:
            raise CatalysisAuthoringError("candidate snapshot is on another project/network axis")
        return snapshot

    @staticmethod
    def _action(request: CatalysisActiveNetworkPreviewRequest) -> str:
        return "create" if request.expected_binding.revision == 0 else "advance"

    @staticmethod
    def _resource_blocked(candidate: CatalysisAuthoringCandidateSnapshot) -> bool:
        return any(
            gap.code in {"network_closure_resource_limit"}
            for gap in candidate.gaps
        )

    @staticmethod
    def _domain_matches(
        expected: NetworkHeadCAS, current: NetworkHeadCAS
    ) -> bool:
        return (
            expected.domain_authority_id == current.domain_authority_id
            and expected.domain_generation == current.domain_generation
            and expected.domain_snapshot_sha256 == current.domain_snapshot_sha256
        )

    @staticmethod
    def _network_matches(
        expected: NetworkHeadCAS, current: NetworkHeadCAS
    ) -> bool:
        return (
            expected.network_id == current.network_id
            and expected.network_status == current.network_status
            and expected.network_revision_id == current.network_revision_id
            and expected.network_semantic_sha256 == current.network_semantic_sha256
        )

    def _seal_material(
        self,
        request: CatalysisActiveNetworkPreviewRequest,
        candidate: CatalysisAuthoringCandidateSnapshot,
        *,
        issued_at_ms: int,
        expires_at_ms: int,
        status: str,
        reason: str | None,
    ) -> dict[str, Any]:
        return {
            "schema": "vcstudio.catalysis-active-network-preview-seal/v1",
            "project_id": self._project_id,
            "owner_id": self._owner_id,
            "request_sha256": request.semantic_sha256,
            "network_id": request.network_id,
            "intent_id": request.intent_id,
            "expected_binding_snapshot_sha256": request.expected_binding.snapshot_sha256,
            "expected_network_snapshot_sha256": request.expected_network_head.snapshot_sha256,
            "action": self._action(request),
            "projection_status": candidate.projection_status,
            "closure_sha256": candidate.closure_sha256,
            "status": status,
            "reason": reason,
            "issued_at_ms": issued_at_ms,
            "expires_at_ms": expires_at_ms,
        }

    def _sign(self, material: Mapping[str, Any]) -> str:
        return hmac.new(self._seal_key, _canonical_bytes(material), hashlib.sha256).hexdigest()

    def _preview_result(
        self,
        request: CatalysisActiveNetworkPreviewRequest,
        candidate: CatalysisAuthoringCandidateSnapshot,
        *,
        issued_at_ms: int,
        expires_at_ms: int,
        status: str,
        reason: str | None,
    ) -> CatalysisActiveNetworkPreview:
        material = self._seal_material(
            request,
            candidate,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            status=status,
            reason=reason,
        )
        return CatalysisActiveNetworkPreview(
            project_id=self._project_id,
            network_id=request.network_id,
            intent_id=request.intent_id,
            request_sha256=request.semantic_sha256,
            status=status,
            reason=reason,
            action=self._action(request),
            binding_cas=candidate.binding.to_dict(),
            network_cas=candidate.network_head.to_dict(),
            projection_status=candidate.projection_status,
            gaps=tuple(gap.to_dict() for gap in candidate.gaps),
            gap_summary=candidate.gap_summary.to_dict(),
            closure_sha256=candidate.closure_sha256,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            preview_sha256=self._sign(material),
            can_confirm=status == "ready",
        )

    def _fallback_candidate(
        self,
        request: CatalysisActiveNetworkPreviewRequest,
        reason: str,
    ) -> CatalysisAuthoringCandidateSnapshot:
        gap = CatalysisAuthoringGap(
            status="unavailable",
            code=reason,
            object_type="ReactionNetwork",
            object_id=request.network_id,
            context={},
        )
        return CatalysisAuthoringCandidateSnapshot(
            project_id=self._project_id,
            binding=request.expected_binding,
            network_head=request.expected_network_head,
            projection_status="unavailable",
            gaps=(gap,),
            gap_summary=CatalysisAuthoringGapSummary(
                total=1, returned=1, omitted=0, by_code={reason: 1}
            ),
        )

    def preview(
        self,
        request: CatalysisActiveNetworkPreviewRequest | Mapping[str, Any],
    ) -> CatalysisActiveNetworkPreview:
        """Build and sign a read-only preview; never mutate the active binding."""

        try:
            request = (
                request
                if isinstance(request, CatalysisActiveNetworkPreviewRequest)
                else CatalysisActiveNetworkPreviewRequest.from_dict(request)
            )
        except CatalysisAuthoringError:
            placeholder = CatalysisActiveNetworkPreviewRequest(
                network_id="invalid-network",
                intent_id="invalid-intent",
                expected_binding=self._empty_binding(),
                expected_network_head=NetworkHeadCAS(
                    domain_authority_id="0" * 32,
                    domain_generation=1,
                    domain_snapshot_sha256="0" * 64,
                    network_id="invalid-network",
                    network_status="missing",
                    network_revision_id=None,
                    network_semantic_sha256=None,
                ),
            )
            candidate = self._fallback_candidate(placeholder, "invalid_preview_request")
            now = int(self._time_source() * 1000)
            return self._preview_result(
                placeholder,
                candidate,
                issued_at_ms=now,
                expires_at_ms=now + self._ttl_seconds * 1000,
                status="unavailable",
                reason="invalid_preview_request",
            )
        now = int(self._time_source() * 1000)
        expires = now + self._ttl_seconds * 1000
        try:
            candidate = self._candidate(request.network_id)
        except CatalysisAuthoringError:
            candidate = self._fallback_candidate(request, "candidate_authority_unavailable")
            return self._preview_result(
                request,
                candidate,
                issued_at_ms=now,
                expires_at_ms=expires,
                status="unavailable",
                reason="candidate_authority_unavailable",
            )
        if candidate is None:
            candidate = self._fallback_candidate(request, "network_missing")
            return self._preview_result(
                request,
                candidate,
                issued_at_ms=now,
                expires_at_ms=expires,
                status="missing",
                reason="network_missing",
            )
        if candidate.binding.snapshot_sha256 != request.expected_binding.snapshot_sha256:
            status, reason = "conflict", "stale_binding"
        elif not self._domain_matches(request.expected_network_head, candidate.network_head):
            status, reason = "conflict", "stale_domain_snapshot"
        elif not self._network_matches(request.expected_network_head, candidate.network_head):
            status, reason = "conflict", "stale_network_head"
        elif candidate.network_head.network_status != "available":
            status, reason = "missing", "network_missing"
        elif self._resource_blocked(candidate):
            status, reason = "unavailable", "network_resource_limit"
        else:
            status, reason = "ready", None
        return self._preview_result(
            request,
            candidate,
            issued_at_ms=now,
            expires_at_ms=expires,
            status=status,
            reason=reason,
        )

    @staticmethod
    def confirmation_from_preview(
        preview: CatalysisActiveNetworkPreview,
    ) -> CatalysisActiveNetworkConfirmation:
        if not preview.can_confirm:
            raise CatalysisAuthoringError("only a ready preview can be confirmed")
        return CatalysisActiveNetworkConfirmation(
            request_sha256=preview.request_sha256,
            preview_sha256=preview.preview_sha256,
            issued_at_ms=preview.issued_at_ms,
            expires_at_ms=preview.expires_at_ms,
            confirmed=True,
        )

    def _conflict(
        self,
        reason: str,
        *,
        binding: ActiveBindingAuthoritySnapshot,
        network: NetworkHeadCAS | None,
    ) -> CatalysisAuthoringConfirmResult:
        return CatalysisAuthoringConfirmResult(
            action="conflict",
            binding_cas=binding.to_dict(),
            network_cas=None if network is None else network.to_dict(),
            conflict=CatalysisAuthoringConflict(
                reason=reason,
                latest_binding_cas=binding.to_dict(),
                latest_network_cas=None if network is None else network.to_dict(),
            ),
        )

    def confirm(
        self,
        request: CatalysisActiveNetworkPreviewRequest | Mapping[str, Any],
        confirmation: CatalysisActiveNetworkConfirmation | Mapping[str, Any],
    ) -> CatalysisAuthoringConfirmResult:
        """Verify the stateless seal, re-read closure, then invoke bind_network."""

        try:
            request = (
                request
                if isinstance(request, CatalysisActiveNetworkPreviewRequest)
                else CatalysisActiveNetworkPreviewRequest.from_dict(request)
            )
            confirmation = (
                confirmation
                if isinstance(confirmation, CatalysisActiveNetworkConfirmation)
                else CatalysisActiveNetworkConfirmation.from_dict(confirmation)
            )
        except CatalysisAuthoringError:
            return CatalysisAuthoringConfirmResult(
                action="unavailable", reason="invalid_confirmation"
            )
        if confirmation.request_sha256 != request.semantic_sha256:
            return self._conflict(
                "request_changed",
                binding=request.expected_binding,
                network=request.expected_network_head,
            )
        now = int(self._time_source() * 1000)
        if now < confirmation.issued_at_ms or now > confirmation.expires_at_ms:
            return self._conflict(
                "preview_expired",
                binding=request.expected_binding,
                network=request.expected_network_head,
            )
        try:
            candidate = self._candidate(request.network_id)
        except CatalysisAuthoringError:
            return CatalysisAuthoringConfirmResult(
                action="unavailable", reason="candidate_authority_unavailable"
            )
        if candidate is None:
            return self._conflict(
                "network_missing",
                binding=request.expected_binding,
                network=request.expected_network_head,
            )
        if not self._domain_matches(request.expected_network_head, candidate.network_head):
            return self._conflict(
                "stale_domain_snapshot",
                binding=candidate.binding,
                network=candidate.network_head,
            )
        if not self._network_matches(request.expected_network_head, candidate.network_head):
            return self._conflict(
                "stale_network_head",
                binding=candidate.binding,
                network=candidate.network_head,
            )
        if candidate.network_head.network_status != "available":
            return self._conflict(
                "network_missing",
                binding=candidate.binding,
                network=candidate.network_head,
            )
        if self._resource_blocked(candidate):
            return self._conflict(
                "network_resource_limit",
                binding=candidate.binding,
                network=candidate.network_head,
            )
        material = self._seal_material(
            request,
            candidate,
            issued_at_ms=confirmation.issued_at_ms,
            expires_at_ms=confirmation.expires_at_ms,
            status="ready",
            reason=None,
        )
        expected_seal = self._sign(material)
        if not hmac.compare_digest(expected_seal, confirmation.preview_sha256):
            return self._conflict(
                "invalid_preview_seal",
                binding=candidate.binding,
                network=candidate.network_head,
            )
        binder = getattr(self._binding_authority, "bind_network", None)
        if not callable(binder):
            return CatalysisAuthoringConfirmResult(
                action="unavailable", reason="binding_authority_unavailable"
            )
        binding = request.expected_binding
        head = request.expected_network_head
        try:
            result = binder(
                network_id=request.network_id,
                intent_id=request.intent_id,
                confirmed=True,
                expected_authority_id=binding.authority_id,
                expected_revision=binding.revision,
                expected_current_hash=binding.current_hash,
                expected_domain_authority_id=head.domain_authority_id,
                expected_domain_generation=head.domain_generation,
                expected_domain_snapshot_sha256=head.domain_snapshot_sha256,
                expected_network_revision_id=head.network_revision_id,
                expected_network_semantic_sha256=head.network_semantic_sha256,
            )
        except (CatalysisContractError, CatalysisProjectionError):
            return CatalysisAuthoringConfirmResult(
                action="unavailable", reason="binding_authority_unavailable"
            )
        except Exception:  # noqa: BLE001 - raw adapter exceptions never escape
            return CatalysisAuthoringConfirmResult(
                action="unavailable", reason="binding_authority_unavailable"
            )
        if not isinstance(result, BindingWriteResult):
            return CatalysisAuthoringConfirmResult(
                action="unavailable", reason="binding_authority_unavailable"
            )
        if result.action == "conflict":
            return self._conflict(
                result.reason or "binding_conflict",
                binding=result.snapshot,
                network=result.network_cas,
            )
        return CatalysisAuthoringConfirmResult(
            action=result.action,
            binding_cas=result.snapshot.to_dict(),
            network_cas=None if result.network_cas is None else result.network_cas.to_dict(),
        )


# Short integration name used by API composition.
CatalysisAuthoringService = CatalysisActiveNetworkAuthoringService


__all__ = [
    "BOOTSTRAP_SCHEMA",
    "CANDIDATE_SCHEMA",
    "CANDIDATE_SNAPSHOT_SCHEMA",
    "CONFIRMATION_SCHEMA",
    "CONFIRM_RESULT_SCHEMA",
    "CONFLICT_SCHEMA",
    "CatalysisActiveNetworkAuthoringService",
    "CatalysisActiveNetworkConfirmation",
    "CatalysisActiveNetworkPreview",
    "CatalysisActiveNetworkPreviewRequest",
    "CatalysisAuthoringBootstrap",
    "CatalysisAuthoringCandidateSnapshot",
    "CatalysisAuthoringConfirmResult",
    "CatalysisAuthoringConflict",
    "CatalysisAuthoringDomainCAS",
    "CatalysisAuthoringError",
    "CatalysisAuthoringGap",
    "CatalysisAuthoringGapSummary",
    "CatalysisAuthoringNetworkCandidate",
    "CatalysisAuthoringService",
    "CatalysisAuthoringSnapshotAuthority",
    "CatalysisAuthoringValidationError",
    "CatalysisBindingAuthority",
    "DEFAULT_PREVIEW_TTL_SECONDS",
    "MAX_CANDIDATES",
    "MAX_GAPS",
    "MAX_PREVIEW_TTL_SECONDS",
    "PREVIEW_REQUEST_SCHEMA",
    "PREVIEW_SCHEMA",
]
