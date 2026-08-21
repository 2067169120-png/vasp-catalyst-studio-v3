"""Strict, revisioned microkinetic modelling decisions and project-local CAS.

Reaction-domain DTOs remain authoritative for reaction facts.  A
``KineticsModelSpec`` records only the explicit modelling decisions needed to
turn one frozen reaction projection into a solver input.  The store is fixed
below ``.vcstudio/kinetics/model-spec`` and never accepts a caller-selected
filename.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, runtime_checkable


SCHEMA = "vcstudio.kinetics-model-spec/v1"
STORE_SCHEMA = "vcstudio.kinetics-model-spec-store/v1"
STORE_DIRECTORY = (".vcstudio", "kinetics", "model-spec")
STORE_FILENAME = "store.json"
LOCK_FILENAME = ".store.lock"
ANCHOR_FILENAME = ".store-anchor.wal"
PENDING_FILENAME = ".store-pending.json"
ANCHOR_RECORD_SCHEMA = "vcstudio.kinetics-model-spec-anchor-record/v1"
PENDING_SCHEMA = "vcstudio.kinetics-model-spec-pending-store/v1"

_MAX_STORE_BYTES = 8 * 1024 * 1024
_MAX_PENDING_BYTES = _MAX_STORE_BYTES + 64 * 1024
_MAX_ANCHOR_BYTES = 32 * 1024 * 1024
_MAX_RECORDS = 4096
_MAX_ANCHOR_RECORDS = 2 * (_MAX_RECORDS + 1)
_MAX_COLLECTION = 512
MAX_EVIDENCE_BINDINGS = 256
SOURCE_SNAPSHOT_SCHEMA = "vcstudio.kinetics-authoritative-source-snapshot/v1"
STORE_SNAPSHOT_SCHEMA = "vcstudio.kinetics-model-spec-store-snapshot/v1"
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_RE = re.compile(r"[0-9a-f]{32}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
_UNC_PATH_RE = re.compile(r"(?<![:A-Za-z0-9])(?:\\\\|//)[^\\/\s]+[\\/]")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9._~%+\-/])/(?!/)[^\s]*")
_TILDE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])~[\\/]")
_FILE_URI_RE = re.compile(r"(?i)file:(?:/{0,3}|\\)")
_URL_USERINFO_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/@\s]+@[^/\s]+")
_PROTOCOL_RELATIVE_USERINFO_RE = re.compile(
    r"(?i)(?<!:)(?:/{2}|\\{2})[^/\\\s@]+@[^/\\\s]+")
_SECRET_RE = re.compile(
    r"(?i)(?:github_pat_|gh[opusr]_|sk-|bearer\s+|private key|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[:=])"
)
_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "credential", "credentials",
    "authorization", "cookie", "api_key", "apikey", "private_key",
})
_PROCESS_LOCK = threading.RLock()
_MAX_LOCK_BYTES = 4096
_ZERO_SHA256 = "0" * 64

_TOP_LEVEL_FIELDS = frozenset({
    "schema", "project_id", "spec_id", "revision", "parent_revision",
    "expected_current_hash", "source_binding", "rate_law_policy",
    "assumptions", "feed_reservoirs", "target_products", "steps",
    "site_population_totals", "saddle_selector",
})
_LEGACY_SOURCE_BINDING_FIELDS = frozenset({
    "domain_authority_id", "domain_generation", "network_revision",
    "source_projection_sha256",
})
_SOURCE_BINDING_FIELDS = frozenset({
    *_LEGACY_SOURCE_BINDING_FIELDS, "network_id",
})
_RATE_POLICY_FIELDS = frozenset({
    "activity", "reversibility", "detailed_balance", "prefactor",
    "electrochemical", "reactor",
})
_ASSUMPTION_FIELDS = frozenset({
    "mean_field", "steady_state", "site_uniformity", "lateral_interactions",
    "mechanism_completeness", "evidence",
})
_EVIDENCE_FIELDS = frozenset({"kind", "reference", "evidence_sha256"})
_RESERVOIR_FIELDS = frozenset({
    "species_id", "activity", "unit", "source", "evidence_sha256",
})
_STEP_FIELDS = frozenset({
    "step_id", "prefactors", "bep", "scaling", "uncertainty_eV", "evidence",
})
_PREFACTORS_FIELDS = frozenset({"forward", "reverse"})
_PREFACTOR_FIELDS = frozenset({"value", "unit", "source"})
_EMPIRICAL_FIELDS = frozenset({"used", "source", "parameters_sha256"})
_SITE_TOTAL_FIELDS = frozenset({
    "site_type", "value", "unit", "basis", "evidence",
})
_SADDLE_FIELDS = frozenset({"mode", "by_step", "evidence"})


class KineticsModelSpecError(ValueError):
    """A spec, write intent, or persisted store violates the contract."""


class KineticsModelSpecConflict(KineticsModelSpecError):
    """An immutable revision or current-head compare-and-swap conflicted."""

    def __init__(self, reason: str, spec: KineticsModelSpec):
        self.reason = reason
        self.project_id = spec.project_id
        self.spec_id = spec.spec_id
        self.revision = spec.revision
        super().__init__(
            f"kinetics model spec conflict ({reason}) for "
            f"{spec.project_id}/{spec.spec_id}/{spec.revision}"
        )


@dataclass(frozen=True)
class AuthoritativeKineticsSourceSnapshot:
    """Hash-sealed path-free identity returned by a trusted source authority."""

    project_id: str
    domain_authority_id: str
    domain_generation: int
    network_revision: str
    source_projection_sha256: str
    network_id: str | None = None
    snapshot_sha256: str = ""
    schema: str = SOURCE_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SOURCE_SNAPSHOT_SCHEMA:
            raise KineticsModelSpecError("authoritative source snapshot schema is invalid")
        project_id = _safe_text(self.project_id, "source_snapshot.project_id",
                                identifier=True)
        if (not isinstance(self.domain_authority_id, str)
                or not _AUTHORITY_RE.fullmatch(self.domain_authority_id)):
            raise KineticsModelSpecError(
                "source_snapshot.domain_authority_id is invalid")
        if (isinstance(self.domain_generation, bool)
                or not isinstance(self.domain_generation, int)
                or not 0 <= self.domain_generation <= 2**53 - 1):
            raise KineticsModelSpecError(
                "source_snapshot.domain_generation is invalid")
        network_revision = _safe_text(
            self.network_revision, "source_snapshot.network_revision", identifier=True)
        projection_hash = _sha(
            self.source_projection_sha256,
            "source_snapshot.source_projection_sha256")
        network_id = self.network_id
        if network_id is not None:
            network_id = _safe_text(
                network_id, "source_snapshot.network_id", identifier=True)
        material = {
            "schema": SOURCE_SNAPSHOT_SCHEMA,
            "project_id": project_id,
            "domain_authority_id": self.domain_authority_id,
            "domain_generation": self.domain_generation,
            "network_revision": network_revision,
            "source_projection_sha256": projection_hash,
            "network_id": network_id,
        }
        digest = hashlib.sha256(_canonical_bytes(material)).hexdigest()
        if self.snapshot_sha256 not in {"", digest}:
            raise KineticsModelSpecError(
                "authoritative source snapshot seal is invalid")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "network_revision", network_revision)
        object.__setattr__(self, "source_projection_sha256", projection_hash)
        object.__setattr__(self, "network_id", network_id)
        object.__setattr__(self, "snapshot_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "domain_authority_id": self.domain_authority_id,
            "domain_generation": self.domain_generation,
            "network_revision": self.network_revision,
            "source_projection_sha256": self.source_projection_sha256,
            "network_id": self.network_id,
            "snapshot_sha256": self.snapshot_sha256,
        }


@runtime_checkable
class KineticsSourceAuthority(Protocol):
    """Trusted callback seam re-read around one model-spec store commit."""

    def authoritative_kinetics_source_snapshot(
            self) -> AuthoritativeKineticsSourceSnapshot:
        """Return the current immutable source identity."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KineticsModelSpecError(
            "kinetics model spec must be canonical finite JSON") from exc


def _strict(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise KineticsModelSpecError(f"{label} has unknown or missing fields")
    return value


def _sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or bool(
        set(normalized.split("_")) & _SENSITIVE_KEYS)


def classify_sensitive_text(value: Any) -> str | None:
    """Classify embedded filesystem paths or credentials without echoing them."""
    if not isinstance(value, str):
        return "invalid"
    if (_SECRET_RE.search(value) or _URL_USERINFO_RE.search(value)
            or _PROTOCOL_RELATIVE_USERINFO_RE.search(value)):
        return "credential"
    if (_FILE_URI_RE.search(value) or _WINDOWS_PATH_RE.search(value)
            or _UNC_PATH_RE.search(value) or _POSIX_PATH_RE.search(value)
            or _TILDE_PATH_RE.search(value)
            or any(part == ".." for part in value.replace("\\", "/").split("/"))):
        return "path"
    return None


def _safe_text(value: Any, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise KineticsModelSpecError(f"{label} must be bounded non-empty text")
    if _CONTROL_RE.search(value) or classify_sensitive_text(value) is not None:
        raise KineticsModelSpecError(f"{label} contains a path or secret")
    if identifier and not _SAFE_ID_RE.fullmatch(value):
        raise KineticsModelSpecError(f"{label} must be a safe opaque identifier")
    return value


class _FrozenMapping(Mapping):
    """Read-only Mapping that cannot be bypassed through ``dict`` methods."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]):
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __setitem__(self, _key, _value) -> None:
        raise TypeError("frozen kinetics model spec values are immutable")

    def __delitem__(self, _key) -> None:
        raise TypeError("frozen kinetics model spec values are immutable")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __repr__(self) -> str:
        return f"_FrozenMapping({self._values!r})"

    def __deepcopy__(self, memo):
        detached: dict[str, Any] = {}
        memo[id(self)] = detached
        detached.update({
            copy.deepcopy(key, memo): copy.deepcopy(value, memo)
            for key, value in self.items()
        })
        return detached


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenMapping({
            key: _freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _collect_evidence_bindings(value: Any) -> tuple[tuple[str, str], ...]:
    records: dict[str, str] = {}

    def add(reference: Any, digest: Any) -> None:
        if not isinstance(reference, str) or not isinstance(digest, str):
            raise KineticsModelSpecError("evidence binding is invalid")
        prior = records.get(reference)
        if prior is not None and prior != digest:
            raise KineticsModelSpecError(
                "one evidence reference cannot name multiple artifact hashes")
        records[reference] = digest
        if len(records) > MAX_EVIDENCE_BINDINGS:
            raise KineticsModelSpecError(
                f"kinetics model spec exceeds {MAX_EVIDENCE_BINDINGS} evidence bindings")

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            if set(node) == _EVIDENCE_FIELDS:
                add(node.get("reference"), node.get("evidence_sha256"))
            elif ({"source", "evidence_sha256"} <= set(node)
                  and isinstance(node.get("source"), str)):
                add(node.get("source"), node.get("evidence_sha256"))
            for item in node.values():
                visit(item)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for item in node:
                visit(item)

    visit(value)
    return tuple(sorted(records.items()))


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise KineticsModelSpecError(f"{label} must be a lowercase SHA-256")
    return value


def _number(value: Any, label: str, *, minimum: float = 0.0,
            maximum: float = 1.0e100, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KineticsModelSpecError(f"{label} must be a finite number")
    result = float(value)
    if (not math.isfinite(result) or result < minimum or result > maximum
            or (positive and result <= 0.0)):
        raise KineticsModelSpecError(f"{label} must be a bounded finite number")
    return result


def _validate_json_tree(value: Any, label: str, *, depth: int = 0) -> None:
    """Defence in depth against paths/secrets hidden in otherwise known fields."""
    if depth > 12:
        raise KineticsModelSpecError(f"{label} is nested too deeply")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 2**53 - 1:
            raise KineticsModelSpecError(f"{label} integer is out of range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise KineticsModelSpecError(f"{label} contains a non-finite number")
        return
    if isinstance(value, str):
        _safe_text(value, label)
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION:
            raise KineticsModelSpecError(f"{label} contains too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or _sensitive_key(key):
                raise KineticsModelSpecError(f"{label} contains a secret-bearing key")
            _safe_text(key, f"{label} key")
            _validate_json_tree(item, f"{label}.{key}", depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > _MAX_COLLECTION:
            raise KineticsModelSpecError(f"{label} contains too many items")
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{label}[{index}]", depth=depth + 1)
        return
    raise KineticsModelSpecError(f"{label} is not JSON")


def _evidence(value: Any, label: str) -> dict[str, Any]:
    record = _strict(value, _EVIDENCE_FIELDS, label)
    return {
        "kind": _safe_text(record["kind"], f"{label}.kind", identifier=True),
        "reference": _safe_text(record["reference"], f"{label}.reference"),
        "evidence_sha256": _sha(
            record["evidence_sha256"], f"{label}.evidence_sha256"),
    }


def _evidence_list(value: Any, label: str, *, required: bool = True) -> list[dict[str, Any]]:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or len(value) > MAX_EVIDENCE_BINDINGS or (required and not value)):
        raise KineticsModelSpecError(f"{label} must be a bounded evidence array")
    records = [_evidence(item, f"{label}[{index}]") for index, item in enumerate(value)]
    identities = [(item["reference"], item["evidence_sha256"]) for item in records]
    if len(identities) != len(set(identities)):
        raise KineticsModelSpecError(f"{label} contains duplicate evidence")
    return records


def _source(value: Any, label: str) -> dict[str, Any]:
    return _evidence(value, label)


def _empirical(value: Any, label: str) -> dict[str, Any]:
    record = _strict(value, _EMPIRICAL_FIELDS, label)
    used = record["used"]
    if not isinstance(used, bool):
        raise KineticsModelSpecError(f"{label}.used must be boolean")
    if used:
        source = _source(record["source"], f"{label}.source")
        parameters = _sha(record["parameters_sha256"], f"{label}.parameters_sha256")
    else:
        if record["source"] is not None or record["parameters_sha256"] is not None:
            raise KineticsModelSpecError(
                f"{label} must not carry parameters when used is false")
        source = None
        parameters = None
    return {"used": used, "source": source, "parameters_sha256": parameters}


def _prefactor(value: Any, label: str) -> dict[str, Any]:
    record = _strict(value, _PREFACTOR_FIELDS, label)
    unit = record["unit"]
    if unit not in {"s^-1", "bar^-1 s^-1", "mol^-1 L s^-1"}:
        raise KineticsModelSpecError(f"{label}.unit is unsupported")
    return {
        "value": _number(record["value"], f"{label}.value", positive=True),
        "unit": unit,
        "source": _source(record["source"], f"{label}.source"),
    }


@dataclass(frozen=True)
class KineticsModelSpec:
    """Detached strict DTO for one immutable modelling-spec revision."""

    schema: ClassVar[str] = SCHEMA
    project_id: str
    spec_id: str
    revision: str
    parent_revision: str | None
    expected_current_hash: str | None
    source_binding: Mapping[str, Any]
    rate_law_policy: Mapping[str, Any]
    assumptions: Mapping[str, Any]
    feed_reservoirs: Sequence[Mapping[str, Any]]
    target_products: Sequence[str]
    steps: Sequence[Mapping[str, Any]]
    site_population_totals: Sequence[Mapping[str, Any]]
    saddle_selector: Mapping[str, Any] | None = None
    _canonical_bytes_cache: bytes = field(init=False, repr=False, compare=False)
    _semantic_sha256_cache: str = field(init=False, repr=False, compare=False)
    _evidence_bindings_cache: tuple[tuple[str, str], ...] = field(
        init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        project_id = _safe_text(self.project_id, "project_id", identifier=True)
        spec_id = _safe_text(self.spec_id, "spec_id", identifier=True)
        revision = _safe_text(self.revision, "revision", identifier=True)
        parent = self.parent_revision
        if parent is not None:
            parent = _safe_text(parent, "parent_revision", identifier=True)
        expected = self.expected_current_hash
        if expected is not None:
            expected = _sha(expected, "expected_current_hash")
        if (parent is None) != (expected is None):
            raise KineticsModelSpecError(
                "parent_revision and expected_current_hash must be provided together")
        if revision == parent:
            raise KineticsModelSpecError("a spec revision cannot be its own parent")

        if (not isinstance(self.source_binding, Mapping)
                or frozenset(self.source_binding) not in {
                    _LEGACY_SOURCE_BINDING_FIELDS, _SOURCE_BINDING_FIELDS}):
            raise KineticsModelSpecError(
                "source_binding has missing or unknown fields")
        binding = self.source_binding
        generation = binding["domain_generation"]
        if (isinstance(generation, bool) or not isinstance(generation, int)
                or not 0 <= generation <= 2**53 - 1):
            raise KineticsModelSpecError(
                "source_binding.domain_generation must be a bounded integer")
        domain_authority = binding["domain_authority_id"]
        if not isinstance(domain_authority, str) or not _AUTHORITY_RE.fullmatch(
                domain_authority):
            raise KineticsModelSpecError(
                "source_binding.domain_authority_id must be a 128-bit authority id")
        normalized_binding = {
            "domain_authority_id": domain_authority,
            "domain_generation": generation,
            "network_revision": _safe_text(
                binding["network_revision"],
                "source_binding.network_revision", identifier=True),
            "source_projection_sha256": _sha(
                binding["source_projection_sha256"],
                "source_binding.source_projection_sha256"),
        }
        if "network_id" in binding:
            normalized_binding["network_id"] = _safe_text(
                binding["network_id"], "source_binding.network_id",
                identifier=True)

        policy = _strict(self.rate_law_policy, _RATE_POLICY_FIELDS, "rate_law_policy")
        normalized_policy = {}
        for key in sorted(_RATE_POLICY_FIELDS):
            normalized_policy[key] = _safe_text(
                policy[key], f"rate_law_policy.{key}", identifier=True)

        assumptions = _strict(self.assumptions, _ASSUMPTION_FIELDS, "assumptions")
        if not isinstance(assumptions["mean_field"], bool) \
                or not isinstance(assumptions["steady_state"], bool):
            raise KineticsModelSpecError("assumption booleans must be explicit")
        normalized_assumptions = {
            "mean_field": assumptions["mean_field"],
            "steady_state": assumptions["steady_state"],
            "site_uniformity": _safe_text(
                assumptions["site_uniformity"], "assumptions.site_uniformity",
                identifier=True),
            "lateral_interactions": _safe_text(
                assumptions["lateral_interactions"],
                "assumptions.lateral_interactions", identifier=True),
            "mechanism_completeness": _safe_text(
                assumptions["mechanism_completeness"],
                "assumptions.mechanism_completeness", identifier=True),
            "evidence": _evidence_list(assumptions["evidence"], "assumptions.evidence"),
        }

        if (isinstance(self.feed_reservoirs, (str, bytes))
                or not isinstance(self.feed_reservoirs, Sequence)
                or not self.feed_reservoirs
                or len(self.feed_reservoirs) > _MAX_COLLECTION):
            raise KineticsModelSpecError("feed_reservoirs must be a non-empty bounded array")
        reservoirs = []
        reservoir_ids = set()
        for index, raw in enumerate(self.feed_reservoirs):
            label = f"feed_reservoirs[{index}]"
            record = _strict(raw, _RESERVOIR_FIELDS, label)
            species_id = _safe_text(record["species_id"], f"{label}.species_id",
                                    identifier=True)
            if species_id in reservoir_ids:
                raise KineticsModelSpecError("feed_reservoirs contain duplicate species")
            reservoir_ids.add(species_id)
            unit = record["unit"]
            if unit not in {"bar", "mol/L", "dimensionless"}:
                raise KineticsModelSpecError(f"{label}.unit is unsupported")
            reservoirs.append({
                "species_id": species_id,
                "activity": _number(record["activity"], f"{label}.activity"),
                "unit": unit,
                "source": _safe_text(record["source"], f"{label}.source"),
                "evidence_sha256": _sha(
                    record["evidence_sha256"], f"{label}.evidence_sha256"),
            })

        if (isinstance(self.target_products, (str, bytes))
                or not isinstance(self.target_products, Sequence)
                or not self.target_products or len(self.target_products) > _MAX_COLLECTION):
            raise KineticsModelSpecError("target_products must be a non-empty bounded array")
        products = [
            _safe_text(item, f"target_products[{index}]", identifier=True)
            for index, item in enumerate(self.target_products)
        ]
        if len(products) != len(set(products)):
            raise KineticsModelSpecError("target_products contain duplicates")

        if (isinstance(self.steps, (str, bytes)) or not isinstance(self.steps, Sequence)
                or not self.steps or len(self.steps) > _MAX_COLLECTION):
            raise KineticsModelSpecError("steps must be a non-empty bounded array")
        steps = []
        step_ids = set()
        for index, raw in enumerate(self.steps):
            label = f"steps[{index}]"
            record = _strict(raw, _STEP_FIELDS, label)
            step_id = _safe_text(record["step_id"], f"{label}.step_id", identifier=True)
            if step_id in step_ids:
                raise KineticsModelSpecError("steps contain duplicate step ids")
            step_ids.add(step_id)
            prefactors = _strict(record["prefactors"], _PREFACTORS_FIELDS,
                                 f"{label}.prefactors")
            steps.append({
                "step_id": step_id,
                "prefactors": {
                    "forward": _prefactor(
                        prefactors["forward"], f"{label}.prefactors.forward"),
                    "reverse": _prefactor(
                        prefactors["reverse"], f"{label}.prefactors.reverse"),
                },
                "bep": _empirical(record["bep"], f"{label}.bep"),
                "scaling": _empirical(record["scaling"], f"{label}.scaling"),
                "uncertainty_eV": _number(
                    record["uncertainty_eV"], f"{label}.uncertainty_eV",
                    maximum=1.0e4),
                "evidence": _evidence_list(record["evidence"], f"{label}.evidence"),
            })

        if (isinstance(self.site_population_totals, (str, bytes))
                or not isinstance(self.site_population_totals, Sequence)
                or not self.site_population_totals
                or len(self.site_population_totals) > _MAX_COLLECTION):
            raise KineticsModelSpecError(
                "site_population_totals must be a non-empty bounded array")
        totals = []
        site_ids = set()
        for index, raw in enumerate(self.site_population_totals):
            label = f"site_population_totals[{index}]"
            record = _strict(raw, _SITE_TOTAL_FIELDS, label)
            site_type = _safe_text(record["site_type"], f"{label}.site_type",
                                   identifier=True)
            if site_type in site_ids:
                raise KineticsModelSpecError(
                    "site_population_totals contain duplicate site types")
            site_ids.add(site_type)
            unit = record["unit"]
            basis = record["basis"]
            if unit not in {"sites", "dimensionless"}:
                raise KineticsModelSpecError(f"{label}.unit is unsupported")
            if basis not in {"surface_unit_cell", "normalized_site_population"}:
                raise KineticsModelSpecError(f"{label}.basis is unsupported")
            totals.append({
                "site_type": site_type,
                "value": _number(record["value"], f"{label}.value", positive=True),
                "unit": unit,
                "basis": basis,
                "evidence": _evidence_list(record["evidence"], f"{label}.evidence"),
            })

        saddle = self.saddle_selector
        normalized_saddle = None
        if saddle is not None:
            saddle = _strict(saddle, _SADDLE_FIELDS, "saddle_selector")
            mode = _safe_text(saddle["mode"], "saddle_selector.mode", identifier=True)
            if mode != "explicit_species_by_step":
                raise KineticsModelSpecError("saddle_selector.mode is unsupported")
            by_step = saddle["by_step"]
            if not isinstance(by_step, Mapping) or set(by_step) != step_ids:
                raise KineticsModelSpecError(
                    "saddle_selector.by_step must select every spec step exactly once")
            normalized_saddle = {
                "mode": mode,
                "by_step": {
                    _safe_text(key, "saddle_selector.by_step key", identifier=True):
                    _safe_text(value, f"saddle_selector.by_step.{key}", identifier=True)
                    for key, value in sorted(by_step.items())
                },
                "evidence": _evidence_list(
                    saddle["evidence"], "saddle_selector.evidence"),
            }

        normalized_values = dict((
            ("project_id", project_id), ("spec_id", spec_id),
            ("revision", revision), ("source_binding", normalized_binding),
            ("rate_law_policy", normalized_policy),
            ("assumptions", normalized_assumptions),
            ("feed_reservoirs", reservoirs), ("target_products", products),
            ("steps", steps), ("site_population_totals", totals),
            ("saddle_selector", normalized_saddle),
        ))
        for name, value in normalized_values.items():
            object.__setattr__(self, name, _freeze_json(value))
        object.__setattr__(self, "parent_revision", parent)
        object.__setattr__(self, "expected_current_hash", expected)
        payload = {
            "schema": SCHEMA,
            "project_id": project_id,
            "spec_id": spec_id,
            "revision": revision,
            "parent_revision": parent,
            "expected_current_hash": expected,
            "source_binding": normalized_binding,
            "rate_law_policy": normalized_policy,
            "assumptions": normalized_assumptions,
            "feed_reservoirs": reservoirs,
            "target_products": products,
            "steps": steps,
            "site_population_totals": totals,
            "saddle_selector": normalized_saddle,
        }
        _validate_json_tree(payload, "kinetics_model_spec")
        evidence_bindings = _collect_evidence_bindings(payload)
        canonical = _canonical_bytes(payload)
        object.__setattr__(self, "_canonical_bytes_cache", canonical)
        object.__setattr__(self, "_semantic_sha256_cache",
                           hashlib.sha256(canonical).hexdigest())
        object.__setattr__(self, "_evidence_bindings_cache", evidence_bindings)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_bytes_cache.decode("utf-8"))

    def semantic_hash(self) -> str:
        return self._semantic_sha256_cache

    @property
    def semantic_sha256(self) -> str:
        return self.semantic_hash()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> KineticsModelSpec:
        record = _strict(value, _TOP_LEVEL_FIELDS, "kinetics_model_spec")
        if record["schema"] != SCHEMA:
            raise KineticsModelSpecError("kinetics model spec schema is unsupported")
        return cls(
            project_id=record["project_id"], spec_id=record["spec_id"],
            revision=record["revision"], parent_revision=record["parent_revision"],
            expected_current_hash=record["expected_current_hash"],
            source_binding=record["source_binding"],
            rate_law_policy=record["rate_law_policy"],
            assumptions=record["assumptions"],
            feed_reservoirs=record["feed_reservoirs"],
            target_products=record["target_products"], steps=record["steps"],
            site_population_totals=record["site_population_totals"],
            saddle_selector=record["saddle_selector"],
        )

    def evidence_bindings(self) -> list[dict[str, str]]:
        """Return every unique opaque reference/digest pair in stable order."""
        return [
            {"reference": reference, "artifact_sha256": digest}
            for reference, digest in self._evidence_bindings_cache
        ]


@dataclass(frozen=True)
class KineticsModelSpecWriteResult:
    action: str
    generation: int
    authority_id: str
    spec: KineticsModelSpec

    def __post_init__(self) -> None:
        if self.action not in {"created", "advanced", "replayed"}:
            raise KineticsModelSpecError("spec write action is invalid")

    @property
    def current_hash(self) -> str:
        return self.spec.semantic_sha256

    @property
    def spec_sha256(self) -> str:
        return self.current_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "generation": self.generation,
            "authority_id": self.authority_id,
            "current_hash": self.current_hash,
            "spec": self.spec.to_dict(),
        }


@dataclass(frozen=True)
class KineticsModelSpecStoreSnapshot:
    """Complete current-head and immutable-chain seal for caller-side CAS."""

    authority_id: str
    generation: int
    heads: Sequence[Mapping[str, Any]]
    chain_sha256: str
    store_sha256: str
    anchor_chain_sha256: str
    snapshot_sha256: str = ""
    schema: str = STORE_SNAPSHOT_SCHEMA
    _canonical_bytes_cache: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema != STORE_SNAPSHOT_SCHEMA:
            raise KineticsModelSpecError("model-spec store snapshot schema is invalid")
        if (not isinstance(self.authority_id, str)
                or not _AUTHORITY_RE.fullmatch(self.authority_id)):
            raise KineticsModelSpecError("model-spec store snapshot authority is invalid")
        if (isinstance(self.generation, bool) or not isinstance(self.generation, int)
                or self.generation < 0):
            raise KineticsModelSpecError("model-spec store snapshot generation is invalid")
        if isinstance(self.heads, (str, bytes)) or not isinstance(self.heads, Sequence):
            raise KineticsModelSpecError("model-spec store snapshot heads are invalid")
        normalized = []
        for raw in self.heads:
            if not isinstance(raw, Mapping) or set(raw) != {
                    "project_id", "spec_id", "revision", "current_hash"}:
                raise KineticsModelSpecError(
                    "model-spec store snapshot head is invalid")
            normalized.append({
                "project_id": _safe_text(
                    raw["project_id"], "store_snapshot.project_id", identifier=True),
                "spec_id": _safe_text(
                    raw["spec_id"], "store_snapshot.spec_id", identifier=True),
                "revision": _safe_text(
                    raw["revision"], "store_snapshot.revision", identifier=True),
                "current_hash": _sha(
                    raw["current_hash"], "store_snapshot.current_hash"),
            })
        ordered = sorted(normalized, key=lambda item: (
            item["project_id"], item["spec_id"], item["revision"],
        ))
        if normalized != ordered or len({
                (item["project_id"], item["spec_id"]) for item in ordered
        }) != len(ordered):
            raise KineticsModelSpecError(
                "model-spec store snapshot heads must be sorted unique identities")
        chain_sha = _sha(self.chain_sha256, "store_snapshot.chain_sha256")
        store_sha = _sha(self.store_sha256, "store_snapshot.store_sha256")
        anchor_chain_sha = _sha(
            self.anchor_chain_sha256,
            "store_snapshot.anchor_chain_sha256")
        material = {
            "schema": STORE_SNAPSHOT_SCHEMA,
            "authority_id": self.authority_id,
            "generation": self.generation,
            "heads": ordered,
            "chain_sha256": chain_sha,
            "store_sha256": store_sha,
            "anchor_chain_sha256": anchor_chain_sha,
        }
        canonical = _canonical_bytes(material)
        digest = hashlib.sha256(canonical).hexdigest()
        if self.snapshot_sha256 not in {"", digest}:
            raise KineticsModelSpecError("model-spec store snapshot seal is invalid")
        object.__setattr__(self, "heads", _freeze_json(ordered))
        object.__setattr__(self, "chain_sha256", chain_sha)
        object.__setattr__(self, "store_sha256", store_sha)
        object.__setattr__(self, "anchor_chain_sha256", anchor_chain_sha)
        object.__setattr__(self, "snapshot_sha256", digest)
        object.__setattr__(self, "_canonical_bytes_cache", canonical)

    def to_dict(self) -> dict[str, Any]:
        value = json.loads(self._canonical_bytes_cache.decode("utf-8"))
        value["snapshot_sha256"] = self.snapshot_sha256
        return value


def _identity_key(project_id: str, spec_id: str) -> str:
    return hashlib.sha256(_canonical_bytes([project_id, spec_id])).hexdigest()


def _revision_key(spec: KineticsModelSpec) -> str:
    return hashlib.sha256(
        _canonical_bytes([spec.project_id, spec.spec_id, spec.revision])
    ).hexdigest()


def _empty_store() -> dict[str, Any]:
    return {
        "schema": STORE_SCHEMA,
        "authority_id": secrets.token_hex(16),
        "generation": 0,
        "heads": {},
        "revisions": {},
    }


def _validated_store(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
            "schema", "authority_id", "generation", "heads", "revisions"}:
        raise KineticsModelSpecError("kinetics model spec store is invalid")
    if value["schema"] != STORE_SCHEMA or not _AUTHORITY_RE.fullmatch(
            str(value["authority_id"])):
        raise KineticsModelSpecError("kinetics model spec store authority is invalid")
    generation = value["generation"]
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0):
        raise KineticsModelSpecError("kinetics model spec store generation is invalid")
    heads = value["heads"]
    revisions = value["revisions"]
    if not isinstance(heads, Mapping) or not isinstance(revisions, Mapping) \
            or len(revisions) > _MAX_RECORDS or generation != len(revisions):
        raise KineticsModelSpecError("kinetics model spec store index is invalid")
    parsed: dict[str, KineticsModelSpec] = {}
    normalized_revisions = {}
    for key, raw in revisions.items():
        if not _SHA_RE.fullmatch(str(key)) or not isinstance(raw, Mapping) \
                or set(raw) != {"spec_sha256", "spec"}:
            raise KineticsModelSpecError("kinetics model spec revision entry is invalid")
        spec = KineticsModelSpec.from_dict(raw["spec"])
        if (_revision_key(spec) != key
                or _sha(raw["spec_sha256"], "spec_sha256") != spec.semantic_sha256):
            raise KineticsModelSpecError("kinetics model spec revision hash is invalid")
        parsed[str(key)] = spec
        normalized_revisions[str(key)] = {
            "spec_sha256": spec.semantic_sha256, "spec": spec.to_dict(),
        }
    normalized_heads = {}
    for identity, revision_key in heads.items():
        if not _SHA_RE.fullmatch(str(identity)) or revision_key not in parsed:
            raise KineticsModelSpecError("kinetics model spec head is invalid")
        spec = parsed[str(revision_key)]
        if _identity_key(spec.project_id, spec.spec_id) != identity:
            raise KineticsModelSpecError("kinetics model spec head identity is invalid")
        normalized_heads[str(identity)] = str(revision_key)
    groups: dict[str, set[str]] = {}
    for key, spec in parsed.items():
        groups.setdefault(_identity_key(spec.project_id, spec.spec_id), set()).add(key)
    if set(groups) != set(normalized_heads):
        raise KineticsModelSpecError("each kinetics model spec requires one head")
    for identity, keys in groups.items():
        by_revision = {parsed[key].revision: key for key in keys}
        if len(by_revision) != len(keys):
            raise KineticsModelSpecError("kinetics model spec revisions are duplicated")
        roots = []
        children = {}
        for key in keys:
            spec = parsed[key]
            if spec.parent_revision is None:
                roots.append(key)
                continue
            parent_key = by_revision.get(spec.parent_revision)
            if parent_key is None:
                raise KineticsModelSpecError("kinetics model spec parent is missing")
            if spec.expected_current_hash != parsed[parent_key].semantic_sha256:
                raise KineticsModelSpecError("kinetics model spec parent hash is invalid")
            if parent_key in children:
                raise KineticsModelSpecError("kinetics model spec history contains a fork")
            children[parent_key] = key
        if len(roots) != 1:
            raise KineticsModelSpecError("kinetics model spec history requires one root")
        visited = set()
        cursor = roots[0]
        while cursor is not None:
            if cursor in visited:
                raise KineticsModelSpecError("kinetics model spec history contains a cycle")
            visited.add(cursor)
            cursor = children.get(cursor)
        if visited != keys:
            raise KineticsModelSpecError("kinetics model spec history is disconnected")
        leaf = next(key for key in keys if key not in children)
        if normalized_heads[identity] != leaf:
            raise KineticsModelSpecError("kinetics model spec head is not the chain leaf")
    return {
        "schema": STORE_SCHEMA, "authority_id": str(value["authority_id"]),
        "generation": generation, "heads": normalized_heads,
        "revisions": normalized_revisions,
    }


_ANCHOR_DESCRIPTOR_FIELDS = frozenset({
    "authority_id", "generation", "store_sha256",
    "revision_chain_sha256", "chain_sha256",
})
_ANCHOR_PREPARE_FIELDS = frozenset({
    "schema", "kind", "sequence", "previous_record_sha256",
    "transaction_id", "base", "target", "pending_sha256", "record_sha256",
})
_ANCHOR_COMMIT_FIELDS = frozenset({
    "schema", "kind", "sequence", "previous_record_sha256",
    "transaction_id", "prepare_record_sha256", "target", "record_sha256",
})


def _revision_chain_sha256(store: Mapping[str, Any]) -> str:
    material = [
        {
            "revision_key": key,
            "spec_sha256": store["revisions"][key]["spec_sha256"],
        }
        for key in sorted(store["revisions"])
    ]
    return hashlib.sha256(_canonical_bytes(material)).hexdigest()


def _store_sha256(store: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(store)).hexdigest()


def _anchor_descriptor(
        store: Mapping[str, Any], previous_chain_sha256: str) -> dict[str, Any]:
    store = _validated_store(store)
    previous = _sha(previous_chain_sha256, "anchor.previous_chain_sha256")
    material = {
        "previous_chain_sha256": previous,
        "authority_id": store["authority_id"],
        "generation": store["generation"],
        "store_sha256": _store_sha256(store),
        "revision_chain_sha256": _revision_chain_sha256(store),
    }
    return {
        **{key: value for key, value in material.items()
           if key != "previous_chain_sha256"},
        "chain_sha256": hashlib.sha256(_canonical_bytes(material)).hexdigest(),
    }


def _validated_anchor_descriptor(value: Any) -> dict[str, Any]:
    record = _strict(value, _ANCHOR_DESCRIPTOR_FIELDS, "anchor.descriptor")
    authority = record["authority_id"]
    if not isinstance(authority, str) or not _AUTHORITY_RE.fullmatch(authority):
        raise KineticsModelSpecError("model-spec anchor authority is invalid")
    generation = record["generation"]
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0 or generation > _MAX_RECORDS):
        raise KineticsModelSpecError("model-spec anchor generation is invalid")
    return {
        "authority_id": authority,
        "generation": generation,
        "store_sha256": _sha(record["store_sha256"], "anchor.store_sha256"),
        "revision_chain_sha256": _sha(
            record["revision_chain_sha256"],
            "anchor.revision_chain_sha256"),
        "chain_sha256": _sha(record["chain_sha256"], "anchor.chain_sha256"),
    }


def _record_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes({
        key: item for key, item in value.items() if key != "record_sha256"
    })).hexdigest()


def _sealed_anchor_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(value)
    record["record_sha256"] = _record_sha256(record)
    return record


def _store_matches_descriptor(
        store: Mapping[str, Any], descriptor: Mapping[str, Any]) -> bool:
    normalized = _validated_store(store)
    return (
        normalized["authority_id"] == descriptor["authority_id"]
        and normalized["generation"] == descriptor["generation"]
        and _store_sha256(normalized) == descriptor["store_sha256"]
        and _revision_chain_sha256(normalized)
        == descriptor["revision_chain_sha256"]
    )


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & flag)


def _path_is_linklike(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KineticsModelSpecError("model-spec filesystem boundary is unavailable") from exc
    junction = getattr(path, "is_junction", None)
    return bool(
        stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)
        or (callable(junction) and junction())
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (
        int(metadata.st_dev), int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
    )


def _safe_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise KineticsModelSpecError(f"{label} is unavailable") from exc
    if (_path_is_linklike(path) or not stat.S_ISDIR(metadata.st_mode)
            or os.path.normcase(os.path.abspath(path))
            != os.path.normcase(os.path.realpath(path))):
        raise KineticsModelSpecError(
            f"{label} must not use a link, junction, or reparse point")
    return metadata


class _ProjectBoundary:
    """Pin the authored project/store directory entities for this store object."""

    def __init__(self, project_root: str | os.PathLike[str]):
        raw = Path(os.path.abspath(os.fspath(project_root)))
        if not Path(project_root).is_absolute():
            raise KineticsModelSpecError(
                "project_root must be an existing absolute directory")
        root_metadata = _safe_directory(raw, label="project_root")
        self.root = raw
        pins = [(raw, _directory_identity(root_metadata))]
        current = raw
        for component in STORE_DIRECTORY:
            candidate = current / component
            try:
                os.mkdir(candidate)
            except FileExistsError:
                pass
            except OSError as exc:
                raise KineticsModelSpecError(
                    "kinetics model spec directory is unavailable") from exc
            metadata = _safe_directory(
                candidate, label="kinetics model spec directory")
            try:
                if os.path.commonpath((str(raw), str(candidate))) != str(raw):
                    raise ValueError
            except ValueError as exc:
                raise KineticsModelSpecError(
                    "kinetics model spec directory escaped project") from exc
            pins.append((candidate, _directory_identity(metadata)))
            current = candidate
        self.directory = current
        self._pins = tuple(pins)
        self.verify()

    def verify(self) -> None:
        for path, identity in self._pins:
            metadata = _safe_directory(path, label="model-spec directory boundary")
            if _directory_identity(metadata) != identity:
                raise KineticsModelSpecError(
                    "model-spec project/store directory entity changed")


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_size),
        int(metadata.st_mtime_ns), int(metadata.st_ctime_ns),
        int(stat.S_IFMT(metadata.st_mode)),
    )


def _read_bounded_regular(path: Path, *, maximum: int) -> bytes | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KineticsModelSpecError(
            "kinetics model spec store is unreadable; refusing overwrite") from exc
    if (_path_is_linklike(path) or not stat.S_ISREG(before.st_mode)
            or before.st_size < 0 or before.st_size > maximum):
        raise KineticsModelSpecError("kinetics model spec store is unsafe")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KineticsModelSpecError(
            "kinetics model spec store is unreadable; refusing overwrite") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size < 0
                or opened.st_size > maximum
                or _directory_identity(opened) != _directory_identity(before)):
            raise KineticsModelSpecError("kinetics model spec store is unsafe")
        chunks = []
        remaining = int(opened.st_size)
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise KineticsModelSpecError(
                "kinetics model spec store changed while it was read") from exc
        if (len(payload) != opened.st_size
                or _file_identity(opened) != _file_identity(after)
                or _directory_identity(after) != _directory_identity(current)
                or int(after.st_size) != int(current.st_size)
                or int(after.st_mtime_ns) != int(current.st_mtime_ns)):
            raise KineticsModelSpecError(
                "kinetics model spec store changed while it was read")
        return payload
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _exclusive_lock(path: Path, boundary: _ProjectBoundary) -> Iterator[None]:
    with _PROCESS_LOCK:
        boundary.verify()
        if _path_is_linklike(path):
            raise KineticsModelSpecError(
                "kinetics model spec lock must not be a link or reparse point")
        flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise KineticsModelSpecError(
                "kinetics model spec lock is unavailable") from exc
        with os.fdopen(descriptor, "a+b") as handle:
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_LOCK_BYTES:
                raise KineticsModelSpecError("kinetics model spec lock is unsafe")
            try:
                current = os.lstat(path)
            except OSError as exc:
                raise KineticsModelSpecError(
                    "kinetics model spec lock changed") from exc
            if _directory_identity(current) != _directory_identity(details):
                raise KineticsModelSpecError("kinetics model spec lock changed")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    boundary.verify()
                    yield
                    boundary.verify()
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    boundary.verify()
                    yield
                    boundary.verify()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        boundary.verify()


def _store_from_payload(payload: bytes) -> dict[str, Any]:
    try:
        return _validated_store(json.loads(payload.decode("utf-8")))
    except KineticsModelSpecError:
        raise
    except Exception as exc:
        raise KineticsModelSpecError(
            "kinetics model spec store is unreadable; refusing overwrite") from exc


def _read_store(path: Path) -> dict[str, Any] | None:
    payload = _read_bounded_regular(path, maximum=_MAX_STORE_BYTES)
    return None if payload is None else _store_from_payload(payload)


def _write_json_file(
        path: Path, value: Mapping[str, Any], *, maximum: int,
        label: str, boundary: _ProjectBoundary) -> None:
    boundary.verify()
    if _path_is_linklike(path):
        raise KineticsModelSpecError(
            f"{label} must not be a link or reparse point")
    payload = _canonical_bytes(value) + b"\n"
    if len(payload) > maximum:
        raise KineticsModelSpecError(f"{label} is too large")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        boundary.verify()
        os.replace(temporary, path)
        boundary.verify()
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _write_store(path: Path, value: Mapping[str, Any],
                 boundary: _ProjectBoundary) -> None:
    _write_json_file(
        path, _validated_store(value), maximum=_MAX_STORE_BYTES,
        label="kinetics model spec store", boundary=boundary)


def _unlink_regular_file(
        path: Path, *, label: str, boundary: _ProjectBoundary) -> None:
    if not os.path.lexists(path):
        return
    metadata = os.lstat(path)
    if (_path_is_linklike(path) or not stat.S_ISREG(metadata.st_mode)):
        raise KineticsModelSpecError(f"{label} is unsafe")
    boundary.verify()
    try:
        os.unlink(path)
    except OSError as exc:
        raise KineticsModelSpecError(f"{label} could not be removed") from exc
    boundary.verify()


def _truncate_anchor_tail(
        path: Path, length: int, boundary: _ProjectBoundary) -> None:
    boundary.verify()
    before = os.lstat(path)
    if (_path_is_linklike(path) or not stat.S_ISREG(before.st_mode)
            or not 0 <= length <= before.st_size):
        raise KineticsModelSpecError("model-spec anchor WAL is unsafe")
    flags = os.O_RDWR | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KineticsModelSpecError("model-spec anchor WAL is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (_directory_identity(opened) != _directory_identity(before)
                or not stat.S_ISREG(opened.st_mode)
                or int(opened.st_size) != int(before.st_size)):
            raise KineticsModelSpecError("model-spec anchor WAL changed")
        os.ftruncate(descriptor, length)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise KineticsModelSpecError(
            "model-spec anchor WAL could not be recovered") from exc
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    if (_directory_identity(after) != _directory_identity(current)
            or int(current.st_size) != length):
        raise KineticsModelSpecError("model-spec anchor WAL changed")
    boundary.verify()


def _append_anchor_record(
        path: Path, record: Mapping[str, Any],
        boundary: _ProjectBoundary) -> None:
    payload = _canonical_bytes(record) + b"\n"
    boundary.verify()
    if _path_is_linklike(path):
        raise KineticsModelSpecError(
            "model-spec anchor WAL must not be a link or reparse point")
    before = None
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise KineticsModelSpecError("model-spec anchor WAL is unavailable") from exc
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise KineticsModelSpecError("model-spec anchor WAL is unsafe")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise KineticsModelSpecError("model-spec anchor WAL is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode)
                or (before is not None
                    and _directory_identity(opened) != _directory_identity(before))
                or opened.st_size + len(payload) > _MAX_ANCHOR_BYTES):
            raise KineticsModelSpecError("model-spec anchor WAL is unsafe")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise KineticsModelSpecError(
                    "model-spec anchor WAL append made no progress")
            offset += written
        os.fsync(descriptor)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise KineticsModelSpecError("model-spec anchor WAL append failed") from exc
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    if (_directory_identity(after) != _directory_identity(current)
            or int(current.st_size) != int(after.st_size)):
        raise KineticsModelSpecError("model-spec anchor WAL changed")
    boundary.verify()


@dataclass(frozen=True)
class _AnchorLogState:
    records: tuple[Mapping[str, Any], ...]
    committed: Mapping[str, Any] | None
    pending: Mapping[str, Any] | None
    last_record_sha256: str


def _expected_anchor_chain(
        descriptor: Mapping[str, Any], previous_chain: str) -> str:
    return hashlib.sha256(_canonical_bytes({
        "previous_chain_sha256": previous_chain,
        "authority_id": descriptor["authority_id"],
        "generation": descriptor["generation"],
        "store_sha256": descriptor["store_sha256"],
        "revision_chain_sha256": descriptor["revision_chain_sha256"],
    })).hexdigest()


def _validated_anchor_log(payload: bytes) -> _AnchorLogState:
    if payload and not payload.endswith(b"\n"):
        raise KineticsModelSpecError("model-spec anchor WAL has an incomplete tail")
    raw_lines = payload.splitlines()
    if len(raw_lines) > _MAX_ANCHOR_RECORDS:
        raise KineticsModelSpecError("model-spec anchor WAL exceeds its record limit")
    records = []
    committed: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    previous_record_sha = _ZERO_SHA256
    for index, raw_line in enumerate(raw_lines, start=1):
        try:
            raw = json.loads(raw_line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise KineticsModelSpecError("model-spec anchor WAL is invalid") from exc
        if not isinstance(raw, Mapping):
            raise KineticsModelSpecError("model-spec anchor WAL record is invalid")
        kind = raw.get("kind")
        fields = (
            _ANCHOR_PREPARE_FIELDS if kind == "prepare"
            else _ANCHOR_COMMIT_FIELDS if kind == "commit"
            else frozenset()
        )
        if not fields or set(raw) != fields:
            raise KineticsModelSpecError("model-spec anchor WAL record is invalid")
        if (raw.get("schema") != ANCHOR_RECORD_SCHEMA
                or raw.get("sequence") != index
                or raw.get("previous_record_sha256") != previous_record_sha):
            raise KineticsModelSpecError("model-spec anchor WAL chain is invalid")
        transaction_id = raw.get("transaction_id")
        if (not isinstance(transaction_id, str)
                or not _AUTHORITY_RE.fullmatch(transaction_id)):
            raise KineticsModelSpecError(
                "model-spec anchor transaction is invalid")
        declared_record_sha = _sha(
            raw.get("record_sha256"), "anchor.record_sha256")
        if _record_sha256(raw) != declared_record_sha:
            raise KineticsModelSpecError("model-spec anchor WAL seal is invalid")
        target = _validated_anchor_descriptor(raw.get("target"))
        if kind == "prepare":
            if pending is not None:
                raise KineticsModelSpecError(
                    "model-spec anchor contains nested transactions")
            raw_base = raw.get("base")
            if committed is None:
                if raw_base is not None or target["generation"] != 0:
                    raise KineticsModelSpecError(
                        "model-spec anchor initialization is invalid")
                prior_chain = _ZERO_SHA256
            else:
                base = _validated_anchor_descriptor(raw_base)
                if (base != committed
                        or target["authority_id"] != committed["authority_id"]
                        or target["generation"] != committed["generation"] + 1):
                    raise KineticsModelSpecError(
                        "model-spec anchor transition is invalid")
                prior_chain = committed["chain_sha256"]
            if target["chain_sha256"] != _expected_anchor_chain(
                    target, prior_chain):
                raise KineticsModelSpecError(
                    "model-spec anchor transition seal is invalid")
            pending_sha = _sha(
                raw.get("pending_sha256"), "anchor.pending_sha256")
            pending = {
                "record": dict(raw),
                "transaction_id": transaction_id,
                "target": target,
                "pending_sha256": pending_sha,
            }
        else:
            if (pending is None
                    or raw.get("prepare_record_sha256")
                    != pending["record"]["record_sha256"]
                    or transaction_id != pending["transaction_id"]
                    or target != pending["target"]):
                raise KineticsModelSpecError(
                    "model-spec anchor commit is invalid")
            committed = target
            pending = None
        record = dict(raw)
        record["target"] = target
        if record.get("base") is not None:
            record["base"] = _validated_anchor_descriptor(record["base"])
        records.append(record)
        previous_record_sha = declared_record_sha
    return _AnchorLogState(
        records=tuple(records), committed=committed, pending=pending,
        last_record_sha256=previous_record_sha)


def _read_anchor_state(
        path: Path, boundary: _ProjectBoundary) -> _AnchorLogState:
    payload = _read_bounded_regular(path, maximum=_MAX_ANCHOR_BYTES)
    if payload is None:
        return _AnchorLogState((), None, None, _ZERO_SHA256)
    if payload and not payload.endswith(b"\n"):
        complete_length = payload.rfind(b"\n") + 1
        complete = payload[:complete_length]
        # A torn final append is not an authority record.  Truncate only that
        # suffix under the already-held store lock; complete records stay append-only.
        state = _validated_anchor_log(complete)
        _truncate_anchor_tail(path, complete_length, boundary)
        return state
    return _validated_anchor_log(payload)


def _pending_store_value(
        transaction_id: str, store: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": PENDING_SCHEMA,
        "transaction_id": transaction_id,
        "store": _validated_store(store),
    }


def _pending_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_pending_store(path: Path) -> dict[str, Any] | None:
    payload = _read_bounded_regular(path, maximum=_MAX_PENDING_BYTES)
    if payload is None:
        return None
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise KineticsModelSpecError(
            "model-spec pending store is invalid") from exc
    record = _strict(
        raw, frozenset({"schema", "transaction_id", "store"}),
        "pending_store")
    transaction_id = record.get("transaction_id")
    if (record.get("schema") != PENDING_SCHEMA
            or not isinstance(transaction_id, str)
            or not _AUTHORITY_RE.fullmatch(transaction_id)):
        raise KineticsModelSpecError("model-spec pending store is invalid")
    return _pending_store_value(transaction_id, record.get("store"))


def _prepare_anchor_record(
        state: _AnchorLogState, pending_value: Mapping[str, Any],
        target: Mapping[str, Any]) -> dict[str, Any]:
    return _sealed_anchor_record({
        "schema": ANCHOR_RECORD_SCHEMA,
        "kind": "prepare",
        "sequence": len(state.records) + 1,
        "previous_record_sha256": state.last_record_sha256,
        "transaction_id": pending_value["transaction_id"],
        "base": None if state.committed is None else dict(state.committed),
        "target": dict(target),
        "pending_sha256": _pending_sha256(pending_value),
    })


def _commit_anchor_record(
        state: _AnchorLogState, prepare: Mapping[str, Any]) -> dict[str, Any]:
    return _sealed_anchor_record({
        "schema": ANCHOR_RECORD_SCHEMA,
        "kind": "commit",
        "sequence": len(state.records) + 1,
        "previous_record_sha256": state.last_record_sha256,
        "transaction_id": prepare["transaction_id"],
        "prepare_record_sha256": prepare["record_sha256"],
        "target": copy.deepcopy(prepare["target"]),
    })


def _commit_store_transaction(
        *, store_path: Path, anchor_path: Path, pending_path: Path,
        candidate: Mapping[str, Any], state: _AnchorLogState,
        boundary: _ProjectBoundary) -> Mapping[str, Any]:
    if state.pending is not None or os.path.lexists(pending_path):
        raise KineticsModelSpecError(
            "model-spec store has an unresolved transaction")
    candidate = _validated_store(candidate)
    previous_chain = (
        _ZERO_SHA256 if state.committed is None
        else state.committed["chain_sha256"])
    target = _anchor_descriptor(candidate, previous_chain)
    transaction_id = secrets.token_hex(16)
    pending_value = _pending_store_value(transaction_id, candidate)
    _write_json_file(
        pending_path, pending_value, maximum=_MAX_PENDING_BYTES,
        label="model-spec pending store", boundary=boundary)
    prepare = _prepare_anchor_record(state, pending_value, target)
    _append_anchor_record(anchor_path, prepare, boundary)
    prepared_state = _AnchorLogState(
        records=(*state.records, prepare), committed=state.committed,
        pending={
            "record": prepare,
            "transaction_id": transaction_id,
            "target": target,
            "pending_sha256": prepare["pending_sha256"],
        },
        last_record_sha256=prepare["record_sha256"],
    )
    _write_store(store_path, candidate, boundary)
    commit = _commit_anchor_record(prepared_state, prepare)
    _append_anchor_record(anchor_path, commit, boundary)
    _unlink_regular_file(
        pending_path, label="model-spec pending store", boundary=boundary)
    return target


def _read_consistent_store(
        *, store_path: Path, anchor_path: Path, pending_path: Path,
        boundary: _ProjectBoundary,
        ) -> tuple[dict[str, Any] | None, _AnchorLogState]:
    state = _read_anchor_state(anchor_path, boundary)
    store = _read_store(store_path)
    pending_value = _read_pending_store(pending_path)
    if state.pending is not None:
        if pending_value is None:
            raise KineticsModelSpecError(
                "model-spec anchor pending payload is unavailable")
        pending = state.pending
        candidate = pending_value["store"]
        if (pending_value["transaction_id"] != pending["transaction_id"]
                or _pending_sha256(pending_value) != pending["pending_sha256"]
                or not _store_matches_descriptor(candidate, pending["target"])):
            raise KineticsModelSpecError(
                "model-spec anchor pending payload does not match")
        base_matches = (
            store is None and state.committed is None
        ) or (
            store is not None and state.committed is not None
            and _store_matches_descriptor(store, state.committed)
        )
        target_matches = (
            store is not None
            and _store_matches_descriptor(store, pending["target"])
        )
        if base_matches:
            _write_store(store_path, candidate, boundary)
            store = candidate
        elif not target_matches:
            raise KineticsModelSpecError(
                "model-spec store does not match its pending anchor transition")
        prepare = pending["record"]
        commit = _commit_anchor_record(state, prepare)
        _append_anchor_record(anchor_path, commit, boundary)
        _unlink_regular_file(
            pending_path, label="model-spec pending store", boundary=boundary)
        state = _read_anchor_state(anchor_path, boundary)
    if state.committed is None:
        if store is not None:
            raise KineticsModelSpecError(
                "model-spec store exists without an independent anchor")
        if pending_value is not None and os.path.lexists(pending_path):
            _unlink_regular_file(
                pending_path, label="unreferenced model-spec pending store",
                boundary=boundary)
        return None, state
    if store is None:
        raise KineticsModelSpecError(
            "model-spec store is missing below its independent anchor")
    if not _store_matches_descriptor(store, state.committed):
        if (store["authority_id"] == state.committed["authority_id"]
                and store["generation"] < state.committed["generation"]):
            raise KineticsModelSpecError(
                "model-spec store is behind its independent anchor")
        raise KineticsModelSpecError(
            "model-spec store does not match its independent anchor")
    if pending_value is not None and os.path.lexists(pending_path):
        _unlink_regular_file(
            pending_path, label="unreferenced model-spec pending store",
            boundary=boundary)
    return store, state


def _store_snapshot(
        store: Mapping[str, Any],
        anchor: Mapping[str, Any]) -> KineticsModelSpecStoreSnapshot:
    if not _store_matches_descriptor(store, anchor):
        raise KineticsModelSpecError(
            "model-spec snapshot does not match its independent anchor")
    heads = []
    for revision_key in store["heads"].values():
        raw = store["revisions"][revision_key]
        spec = KineticsModelSpec.from_dict(raw["spec"])
        heads.append({
            "project_id": spec.project_id,
            "spec_id": spec.spec_id,
            "revision": spec.revision,
            "current_hash": spec.semantic_sha256,
        })
    heads.sort(key=lambda item: (item["project_id"], item["spec_id"], item["revision"]))
    chain_material = [
        {
            "revision_key": key,
            "spec_sha256": store["revisions"][key]["spec_sha256"],
        }
        for key in sorted(store["revisions"])
    ]
    return KineticsModelSpecStoreSnapshot(
        authority_id=store["authority_id"], generation=store["generation"],
        heads=heads,
        chain_sha256=hashlib.sha256(_canonical_bytes(chain_material)).hexdigest(),
        store_sha256=_store_sha256(store),
        anchor_chain_sha256=anchor["chain_sha256"],
    )


class KineticsModelSpecStore:
    """Project-local immutable revisions with a process-safe current-head CAS."""

    def __init__(self, project_root: str | os.PathLike[str]):
        self._boundary = _ProjectBoundary(project_root)
        self.directory = self._boundary.directory
        self.path = self.directory / STORE_FILENAME
        self.lock_path = self.directory / LOCK_FILENAME
        self.anchor_path = self.directory / ANCHOR_FILENAME
        self.pending_path = self.directory / PENDING_FILENAME
        self._anchor_identity: tuple[int, int, int] | None = None

    def _verify_anchor_entity(self) -> None:
        if self._anchor_identity is None:
            return
        try:
            metadata = os.lstat(self.anchor_path)
        except OSError as exc:
            raise KineticsModelSpecError(
                "model-spec anchor WAL entity changed") from exc
        if (_path_is_linklike(self.anchor_path)
                or not stat.S_ISREG(metadata.st_mode)
                or _directory_identity(metadata) != self._anchor_identity):
            raise KineticsModelSpecError(
                "model-spec anchor WAL entity changed")

    def _pin_anchor_entity(self) -> None:
        if not os.path.lexists(self.anchor_path):
            return
        metadata = os.lstat(self.anchor_path)
        if (_path_is_linklike(self.anchor_path)
                or not stat.S_ISREG(metadata.st_mode)):
            raise KineticsModelSpecError("model-spec anchor WAL is unsafe")
        identity = _directory_identity(metadata)
        if self._anchor_identity is None:
            self._anchor_identity = identity
        elif self._anchor_identity != identity:
            raise KineticsModelSpecError(
                "model-spec anchor WAL entity changed")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._boundary.verify()
        self._verify_anchor_entity()
        try:
            with _exclusive_lock(self.lock_path, self._boundary):
                self._boundary.verify()
                self._verify_anchor_entity()
                yield
                self._verify_anchor_entity()
                self._boundary.verify()
        finally:
            self._verify_anchor_entity()
            self._boundary.verify()

    def _consistent_store(
            self) -> tuple[dict[str, Any] | None, _AnchorLogState]:
        self._verify_anchor_entity()
        result = _read_consistent_store(
            store_path=self.path, anchor_path=self.anchor_path,
            pending_path=self.pending_path, boundary=self._boundary)
        self._pin_anchor_entity()
        self._verify_anchor_entity()
        return result

    @staticmethod
    def _source_snapshot(source_authority: Any) -> AuthoritativeKineticsSourceSnapshot:
        loader = getattr(
            source_authority, "authoritative_kinetics_source_snapshot", None)
        if not callable(loader):
            raise KineticsModelSpecError(
                "a trusted authoritative kinetics source validator is required")
        try:
            snapshot = loader()
        except Exception as exc:  # noqa: BLE001 authority boundary fails closed
            raise KineticsModelSpecError(
                "authoritative kinetics source could not be revalidated") from exc
        if not isinstance(snapshot, AuthoritativeKineticsSourceSnapshot):
            raise KineticsModelSpecError(
                "authoritative kinetics source must return a sealed snapshot")
        # Reconstruct the seal so a forged subclass cannot bypass validation.
        return AuthoritativeKineticsSourceSnapshot(**{
            key: value for key, value in snapshot.to_dict().items()
            if key != "snapshot_sha256"
        }, snapshot_sha256=snapshot.snapshot_sha256)

    @staticmethod
    def _validate_source(
            spec: KineticsModelSpec,
            snapshot: AuthoritativeKineticsSourceSnapshot, *,
            expected_domain_authority_id: str,
            expected_domain_generation: int,
            expected_source_projection_sha256: str,
            expected_network_id: str | None) -> None:
        checks = {
            "stale_project": (snapshot.project_id, spec.project_id),
            "stale_domain_authority": (
                snapshot.domain_authority_id,
                spec.source_binding["domain_authority_id"]),
            "stale_domain_generation": (
                snapshot.domain_generation,
                spec.source_binding["domain_generation"]),
            "stale_network_revision": (
                snapshot.network_revision,
                spec.source_binding["network_revision"]),
            "stale_source_projection": (
                snapshot.source_projection_sha256,
                spec.source_binding["source_projection_sha256"]),
            "stale_expected_domain_authority": (
                snapshot.domain_authority_id, expected_domain_authority_id),
            "stale_expected_domain_generation": (
                snapshot.domain_generation, expected_domain_generation),
            "stale_expected_source_projection": (
                snapshot.source_projection_sha256,
                expected_source_projection_sha256),
        }
        if expected_network_id is not None:
            checks["stale_network_id"] = (snapshot.network_id, expected_network_id)
        if "network_id" in spec.source_binding:
            checks["stale_spec_network_id"] = (
                snapshot.network_id, spec.source_binding["network_id"])
        for reason, (actual, expected) in checks.items():
            if actual != expected:
                raise KineticsModelSpecConflict(reason, spec)

    def put(
        self, spec: KineticsModelSpec | Mapping[str, Any], *, confirmed: bool,
        intent: str, expected_domain_authority_id: str,
        expected_source_projection_sha256: str,
        expected_domain_generation: int | None = None,
        expected_store_snapshot_sha256: str | None = None,
        source_authority: KineticsSourceAuthority | None = None,
        expected_network_id: str | None = None,
    ) -> KineticsModelSpecWriteResult:
        if confirmed is not True:
            raise KineticsModelSpecError("explicit confirmation is required")
        spec = KineticsModelSpec.from_dict(
            spec.to_dict() if isinstance(spec, KineticsModelSpec) else spec)
        expected_intent = "create" if spec.parent_revision is None else "advance"
        if intent != expected_intent:
            raise KineticsModelSpecError(
                f"intent must be {expected_intent!r} for this revision")
        if (not isinstance(expected_domain_authority_id, str)
                or not _AUTHORITY_RE.fullmatch(expected_domain_authority_id)):
            raise KineticsModelSpecError(
                "expected_domain_authority_id must be a 128-bit authority id")
        authority = expected_domain_authority_id
        source_hash = _sha(
            expected_source_projection_sha256,
            "expected_source_projection_sha256")
        if (isinstance(expected_domain_generation, bool)
                or not isinstance(expected_domain_generation, int)
                or expected_domain_generation < 0):
            raise KineticsModelSpecError(
                "expected_domain_generation is required and must be non-negative")
        store_snapshot_hash = _sha(
            expected_store_snapshot_sha256, "expected_store_snapshot_sha256")
        if expected_network_id is not None:
            expected_network_id = _safe_text(
                expected_network_id, "expected_network_id", identifier=True)
        if source_authority is None:
            raise KineticsModelSpecError(
                "a trusted authoritative kinetics source validator is required")

        with self._locked():
            before_source = self._source_snapshot(source_authority)
            self._validate_source(
                spec, before_source,
                expected_domain_authority_id=authority,
                expected_domain_generation=expected_domain_generation,
                expected_source_projection_sha256=source_hash,
                expected_network_id=expected_network_id)
            store, anchor_state = self._consistent_store()
            if store is None:
                raise KineticsModelSpecError(
                    "model-spec store authority must be snapshotted before writing")
            if anchor_state.committed is None:
                raise KineticsModelSpecError(
                    "model-spec store independent anchor is unavailable")
            current_snapshot = _store_snapshot(store, anchor_state.committed)
            if current_snapshot.snapshot_sha256 != store_snapshot_hash:
                raise KineticsModelSpecConflict("stale_store_snapshot", spec)
            identity = _identity_key(spec.project_id, spec.spec_id)
            revision_key = _revision_key(spec)
            existing = store["revisions"].get(revision_key)
            if existing is not None:
                existing_spec = KineticsModelSpec.from_dict(existing["spec"])
                if existing_spec.semantic_sha256 != spec.semantic_sha256:
                    raise KineticsModelSpecConflict("immutable_revision_reused", spec)
                after_source = self._source_snapshot(source_authority)
                if after_source.snapshot_sha256 != before_source.snapshot_sha256:
                    raise KineticsModelSpecConflict(
                        "authoritative_source_changed", spec)
                self._validate_source(
                    spec, after_source,
                    expected_domain_authority_id=authority,
                    expected_domain_generation=expected_domain_generation,
                    expected_source_projection_sha256=source_hash,
                    expected_network_id=expected_network_id)
                return KineticsModelSpecWriteResult(
                    "replayed", store["generation"], store["authority_id"],
                    KineticsModelSpec.from_dict(existing_spec.to_dict()))
            head_key = store["heads"].get(identity)
            if head_key is None:
                if spec.parent_revision is not None or spec.expected_current_hash is not None:
                    raise KineticsModelSpecConflict("unexpected_parent_for_create", spec)
                action = "created"
            else:
                current = KineticsModelSpec.from_dict(
                    store["revisions"][head_key]["spec"])
                if spec.parent_revision != current.revision:
                    raise KineticsModelSpecConflict("stale_parent_revision", spec)
                if spec.expected_current_hash != current.semantic_sha256:
                    raise KineticsModelSpecConflict("stale_current_hash", spec)
                action = "advanced"
            candidate = {
                **store,
                "generation": store["generation"] + 1,
                "heads": {**store["heads"], identity: revision_key},
                "revisions": {
                    **store["revisions"],
                    revision_key: {
                        "spec_sha256": spec.semantic_sha256,
                        "spec": spec.to_dict(),
                    },
                },
            }
            validated = _validated_store(candidate)
            after_source = self._source_snapshot(source_authority)
            if after_source.snapshot_sha256 != before_source.snapshot_sha256:
                raise KineticsModelSpecConflict("authoritative_source_changed", spec)
            self._validate_source(
                spec, after_source,
                expected_domain_authority_id=authority,
                expected_domain_generation=expected_domain_generation,
                expected_source_projection_sha256=source_hash,
                expected_network_id=expected_network_id)
            self._boundary.verify()
            _commit_store_transaction(
                store_path=self.path, anchor_path=self.anchor_path,
                pending_path=self.pending_path, candidate=validated,
                state=anchor_state, boundary=self._boundary)
            committed, committed_anchor = self._consistent_store()
            if committed is None:
                raise KineticsModelSpecError("model-spec commit could not be revalidated")
            if committed_anchor.committed is None:
                raise KineticsModelSpecError(
                    "model-spec commit anchor could not be revalidated")
            committed_raw = committed["revisions"].get(revision_key)
            if committed_raw is None:
                raise KineticsModelSpecError("model-spec commit is missing")
            committed_spec = KineticsModelSpec.from_dict(committed_raw["spec"])
            return KineticsModelSpecWriteResult(
                action, committed["generation"], committed["authority_id"],
                committed_spec)

    def head(self, project_id: str, spec_id: str) -> KineticsModelSpec | None:
        project_id = _safe_text(project_id, "project_id", identifier=True)
        spec_id = _safe_text(spec_id, "spec_id", identifier=True)
        with self._locked():
            store, _anchor = self._consistent_store()
            if store is None:
                return None
            key = store["heads"].get(_identity_key(project_id, spec_id))
            return None if key is None else KineticsModelSpec.from_dict(
                store["revisions"][key]["spec"])

    def get(self, project_id: str, spec_id: str,
            revision: str) -> KineticsModelSpec | None:
        # Revision keys do not depend on any untrusted persisted fields.
        key = hashlib.sha256(_canonical_bytes([
            _safe_text(project_id, "project_id", identifier=True),
            _safe_text(spec_id, "spec_id", identifier=True),
            _safe_text(revision, "revision", identifier=True),
        ])).hexdigest()
        with self._locked():
            store, _anchor = self._consistent_store()
            raw = None if store is None else store["revisions"].get(key)
            return None if raw is None else KineticsModelSpec.from_dict(raw["spec"])

    def snapshot(self) -> KineticsModelSpecStoreSnapshot:
        """Persist even an empty authority and seal every current head and revision."""
        with self._locked():
            store, anchor_state = self._consistent_store()
            if store is None:
                store = _empty_store()
                _commit_store_transaction(
                    store_path=self.path, anchor_path=self.anchor_path,
                    pending_path=self.pending_path, candidate=store,
                    state=anchor_state, boundary=self._boundary)
                committed, anchor_state = self._consistent_store()
                if committed is None:
                    raise KineticsModelSpecError(
                        "empty model-spec authority could not be persisted")
                store = committed
            if anchor_state.committed is None:
                raise KineticsModelSpecError(
                    "model-spec store independent anchor is unavailable")
            return _store_snapshot(store, anchor_state.committed)

    def authority_snapshot(self) -> dict[str, Any]:
        return self.snapshot().to_dict()


__all__ = [
    "ANCHOR_FILENAME", "ANCHOR_RECORD_SCHEMA",
    "AuthoritativeKineticsSourceSnapshot", "KineticsModelSpec",
    "KineticsModelSpecConflict", "KineticsModelSpecError",
    "KineticsModelSpecStore", "KineticsModelSpecStoreSnapshot",
    "KineticsModelSpecWriteResult", "KineticsSourceAuthority", "LOCK_FILENAME",
    "MAX_EVIDENCE_BINDINGS", "PENDING_FILENAME", "PENDING_SCHEMA", "SCHEMA",
    "SOURCE_SNAPSHOT_SCHEMA",
    "STORE_DIRECTORY", "STORE_FILENAME", "STORE_SCHEMA",
    "STORE_SNAPSHOT_SCHEMA", "classify_sensitive_text",
]
