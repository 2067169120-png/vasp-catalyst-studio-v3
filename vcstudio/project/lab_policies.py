"""Auditable laboratory method/resource recommendation templates.

Policies are *recommendations*, never scientific evidence and never implicit mutation.  A caller
must resolve a known template, inspect the effective values, and explicitly confirm before using
them to prepare a new job.  Existing manifests are never rewritten by this module.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vcstudio.shared.config import user_config_dir


SCHEMA = "vcstudio.lab-policy/v1"
SELECTION_SCHEMA = "vcstudio.lab-policy-selection/v1"
STORE_SCHEMA = "vcstudio.lab-policy-selection-store/v1"
SELECTION_FILENAME = "lab-policy-selection.json"
LOCK_FILENAME = ".lab-policy-selection.lock"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_WALLTIME = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
_SECRET_KEY = re.compile(
    r"(?i)(?:password|passwd|secret|token|api.?key|private.?key|credential)"
)
_PATH_KEY = re.compile(r"(?i)(?:path|dir|root|file|executable|command|host|user)")
_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}$")
_METHOD_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]{0,79}$")
_VALUE_SECRET = re.compile(
    r"(?i)(?:gh[opusr]_[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}|bearer\s+|password|passwd|secret|token|api.?key)"
)
_PROCESS_LOCK = threading.RLock()
_MAX_REVISION = 2**53 - 1
_MAX_FILE_BYTES = 128 * 1024


_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "id": "reproducible-periodic-baseline",
        "version": 1,
        "label_zh": "周期体系可复现基线",
        "label_en": "Reproducible periodic baseline",
        "description_zh": "以相对 ENMAX、显式 k 点与严格收敛检查作为新周期任务的起点。",
        "description_en": (
            "A starting point for new periodic jobs using relative ENMAX, explicit k-point "
            "evidence, and strict convergence checks."
        ),
        "applicability": ["bulk", "slab", "adsorption"],
        "method": {
            "functional": "PBE",
            "encut_enmax_multiplier": 1.30,
            "energy_tolerance_eV": 1e-5,
            "force_tolerance_eV_A": 0.02,
            "kpoint_policy": "explicit-converged-mesh",
            "dispersion_policy": "declare-and-match-across-comparisons",
        },
        "resources": {"cores": 32, "walltime": "24:00:00"},
        "required_checks": [
            "potcar_identity", "encut_vs_enmax", "kpoint_convergence",
            "method_consistency", "electronic_convergence",
        ],
    },
    {
        "id": "surface-production",
        "version": 1,
        "label_zh": "表面与吸附生产计算",
        "label_en": "Surface and adsorption production",
        "description_zh": "面向 slab/吸附比较，强调真空、层厚、固定层和参考态方法一致。",
        "description_en": (
            "For slab and adsorption comparisons, with explicit vacuum, thickness, fixed-layer, "
            "and reference-state consistency checks."
        ),
        "applicability": ["slab", "adsorption"],
        "method": {
            "functional": "PBE",
            "encut_enmax_multiplier": 1.30,
            "energy_tolerance_eV": 1e-5,
            "force_tolerance_eV_A": 0.02,
            "kpoint_policy": "surface-converged-mesh",
            "dispersion_policy": "declare-and-match-across-references",
        },
        "resources": {"cores": 32, "walltime": "36:00:00"},
        "required_checks": [
            "vacuum_convergence", "slab_thickness_convergence", "fixed_layer_policy",
            "reference_state", "method_consistency", "electronic_convergence",
        ],
    },
    {
        "id": "screening-pilot",
        "version": 1,
        "label_zh": "筛选单点先行",
        "label_en": "Screening pilot first",
        "description_zh": "先以一个代表性任务验证输入、队列与解析链，再扩展批量；不降低最终验证门。",
        "description_en": (
            "Validate inputs, scheduler, and parser flow with one representative job before "
            "expanding the batch; final validation gates remain unchanged."
        ),
        "applicability": ["bulk", "slab", "adsorption", "molecule"],
        "method": {
            "functional": "PBE",
            "encut_enmax_multiplier": 1.20,
            "energy_tolerance_eV": 1e-5,
            "force_tolerance_eV_A": 0.03,
            "kpoint_policy": "pilot-then-convergence",
            "dispersion_policy": "declare-before-comparison",
        },
        "resources": {"cores": 16, "walltime": "08:00:00"},
        "required_checks": [
            "pilot_completed", "potcar_identity", "method_consistency",
            "electronic_convergence",
        ],
    },
)
_BY_ID = {item["id"]: item for item in _POLICIES}

_OVERRIDE_FIELDS = {
    "functional", "encut_enmax_multiplier", "energy_tolerance_eV",
    "force_tolerance_eV_A", "kpoint_policy", "dispersion_policy",
    "cores", "walltime",
}


class LabPolicyError(ValueError):
    """A policy request is invalid or attempts to cross the recommendation boundary."""


def _hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identifier(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise LabPolicyError(f"{field} is invalid")
    return text


def _finite(value: Any, *, field: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LabPolicyError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise LabPolicyError(f"{field} must be between {low:g} and {high:g}")
    return number


def normalize_overrides(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise LabPolicyError("overrides must be an object")
    out: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if (_SECRET_KEY.search(key) or _PATH_KEY.search(key)
                or key not in _OVERRIDE_FIELDS):
            raise LabPolicyError(f"override field is not allowed: {key or '<empty>'}")
        if key == "encut_enmax_multiplier":
            out[key] = _finite(raw_value, field=key, low=1.0, high=3.0)
        elif key == "energy_tolerance_eV":
            out[key] = _finite(raw_value, field=key, low=1e-9, high=1e-2)
        elif key == "force_tolerance_eV_A":
            out[key] = _finite(raw_value, field=key, low=1e-5, high=1.0)
        elif key == "cores":
            cores = _finite(raw_value, field=key, low=1, high=4096)
            if not cores.is_integer():
                raise LabPolicyError("cores must be an integer")
            out[key] = int(cores)
        elif key == "walltime":
            walltime = str(raw_value or "").strip()
            match = _WALLTIME.fullmatch(walltime)
            if not match or int(match.group(1)) > 999:
                raise LabPolicyError("walltime must use HHH:MM:SS")
            out[key] = walltime
        else:
            text = str(raw_value or "").strip()
            # Method overrides are identifiers, not free text.  A bounded slug
            # excludes drive/UNC/file-URI/traversal paths and credential-like
            # payloads while still allowing common names such as PBE+U.
            if (not _METHOD_VALUE.fullmatch(text) or _VALUE_SECRET.search(text)
                    or ".." in text):
                raise LabPolicyError(f"{key} is invalid")
            out[key] = text
    return dict(sorted(out.items()))


def catalog() -> dict[str, Any]:
    records = []
    for source in _POLICIES:
        record = copy.deepcopy(source)
        semantic = {"schema": SCHEMA, **record, "recommendation_only": True}
        record["semantic_sha256"] = _hash(semantic)
        record["recommendation_only"] = True
        records.append(record)
    return {"schema": SCHEMA, "policies": records}


def resolve(policy_id: Any, overrides: Any = None, *, applicability: Any = None) -> dict[str, Any]:
    identifier = _identifier(policy_id, field="policy_id")
    source = _BY_ID.get(identifier)
    if source is None:
        raise LabPolicyError("unknown policy_id")
    normalized = normalize_overrides(overrides)
    requested_applicability = str(applicability or "").strip().lower()
    if requested_applicability and requested_applicability not in source["applicability"]:
        raise LabPolicyError(
            f"policy {identifier} does not declare applicability for {requested_applicability}"
        )
    method = copy.deepcopy(source["method"])
    resources = copy.deepcopy(source["resources"])
    for key, value in normalized.items():
        target = resources if key in {"cores", "walltime"} else method
        target[key] = value
    semantic = {
        "schema": SELECTION_SCHEMA,
        "policy_id": identifier,
        "policy_version": int(source["version"]),
        "applicability": requested_applicability or None,
        "method": method,
        "resources": resources,
        "required_checks": list(source["required_checks"]),
        "overrides": normalized,
        "recommendation_only": True,
        "requires_user_confirmation": True,
    }
    return {**semantic, "semantic_sha256": _hash(semantic)}


class LabPolicyBusy(LabPolicyError):
    """Another process holds the selection writer lock."""


def default_selection_path() -> Path:
    return user_config_dir() / SELECTION_FILENAME


def _default_state() -> dict[str, Any]:
    return {"schema": STORE_SCHEMA, "revision": 0, "selection": None}


def _expected_revision(value: Any) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= _MAX_REVISION):
        raise LabPolicyError("expected_revision must be a bounded non-negative integer")
    return value


def _actor(value: Any) -> str:
    text = str(value or "").strip()
    if (not _ACTOR.fullmatch(text) or _SECRET_KEY.search(text)
            or _VALUE_SECRET.search(text)
            or re.search(r"(?i)(?:[A-Z]:[\\/]|/[^\s]+|\\\\)", text)):
        raise LabPolicyError("actor must be a safe opaque identity")
    return text


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LabPolicyError("selection store must contain an object")
    if set(value) != {"schema", "revision", "selection"}:
        raise LabPolicyError("selection store contains unknown fields")
    if value.get("schema") != STORE_SCHEMA:
        raise LabPolicyError("unsupported selection store schema")
    revision = _expected_revision(value.get("revision"))
    selection = value.get("selection")
    if selection is None:
        return {"schema": STORE_SCHEMA, "revision": revision, "selection": None}
    if not isinstance(selection, Mapping):
        raise LabPolicyError("selection must be an object or null")
    allowed = {
        "schema", "policy_id", "policy_version", "overrides", "applicability",
        "semantic_sha256", "actor", "confirmed_at", "recommendation_only",
        "requires_user_confirmation", "authorizes_submission",
    }
    if set(selection) != allowed:
        raise LabPolicyError("stored selection contains unknown fields")
    resolved = resolve(
        selection.get("policy_id"), selection.get("overrides"),
        applicability=selection.get("applicability"))
    if selection.get("schema") != SELECTION_SCHEMA:
        raise LabPolicyError("stored selection schema is invalid")
    if selection.get("policy_version") != resolved["policy_version"]:
        raise LabPolicyError("stored policy version is unavailable")
    if selection.get("semantic_sha256") != resolved["semantic_sha256"]:
        raise LabPolicyError("stored selection semantic hash mismatch")
    actor = _actor(selection.get("actor"))
    confirmed_at = str(selection.get("confirmed_at") or "").strip()
    try:
        parsed = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LabPolicyError("stored confirmed_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LabPolicyError("stored confirmed_at must be timezone-aware")
    if (selection.get("recommendation_only") is not True
            or selection.get("requires_user_confirmation") is not True
            or selection.get("authorizes_submission") is not False):
        raise LabPolicyError("stored recommendation boundary is invalid")
    return {
        "schema": STORE_SCHEMA, "revision": revision,
        "selection": {
            "schema": SELECTION_SCHEMA,
            "policy_id": resolved["policy_id"],
            "policy_version": resolved["policy_version"],
            "overrides": copy.deepcopy(resolved["overrides"]),
            "applicability": resolved["applicability"],
            "semantic_sha256": resolved["semantic_sha256"],
            "actor": actor,
            "confirmed_at": parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "recommendation_only": True,
            "requires_user_confirmation": True,
            "authorizes_submission": False,
        },
    }


@contextmanager
def _advisory_lock(path: Path, *, timeout: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        if os.name == "nt":
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise LabPolicyBusy("lab policy selection is busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise LabPolicyBusy("lab policy selection is busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class LabPolicySelectionStore:
    """Atomic, CAS-protected user-level confirmed policy selection."""

    def __init__(self, path: str | os.PathLike | None = None, *,
                 lock_timeout: float = 5.0, clock=None):
        self.path = Path(path) if path is not None else default_selection_path()
        self.lock_path = self.path.parent / LOCK_FILENAME
        if (isinstance(lock_timeout, bool)
                or not isinstance(lock_timeout, (int, float))
                or not math.isfinite(float(lock_timeout))
                or not 0 <= float(lock_timeout) <= 60):
            raise LabPolicyError("lock_timeout must be between 0 and 60 seconds")
        self.lock_timeout = float(lock_timeout)
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _default_state()
        if self.path.stat().st_size > _MAX_FILE_BYTES:
            raise LabPolicyError("selection store is too large")
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return _validate_state(json.load(handle))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LabPolicyError(f"cannot read policy selection: {exc}") from exc

    @staticmethod
    def _result(state: Mapping[str, Any], *, ok: bool,
                conflict: bool = False, error: str | None = None) -> dict[str, Any]:
        return {
            "ok": bool(ok), "conflict": bool(conflict),
            "revision": state["revision"],
            "selection": copy.deepcopy(state["selection"]), "error": error,
        }

    def read(self) -> dict[str, Any]:
        with _PROCESS_LOCK:
            return self._result(self._read_unlocked(), ok=True)

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        validated = _validate_state(state)
        raw = (json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def confirm(self, policy_id: Any, overrides: Any = None, *, applicability: Any = None,
                actor: Any, confirmed: Any, expected_revision: Any) -> dict[str, Any]:
        if confirmed is not True:
            raise LabPolicyError("confirmed=true is required")
        expected = _expected_revision(expected_revision)
        actor_id = _actor(actor)
        resolved = resolve(policy_id, overrides, applicability=applicability)
        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current = self._read_unlocked()
                if current["revision"] != expected:
                    return self._result(
                        current, ok=False, conflict=True,
                        error="policy selection revision conflict")
                if current["revision"] >= _MAX_REVISION:
                    raise LabPolicyError("selection revision is exhausted")
                selection = {
                    "schema": SELECTION_SCHEMA,
                    "policy_id": resolved["policy_id"],
                    "policy_version": resolved["policy_version"],
                    "overrides": copy.deepcopy(resolved["overrides"]),
                    "applicability": resolved["applicability"],
                    "semantic_sha256": resolved["semantic_sha256"],
                    "actor": actor_id,
                    "confirmed_at": str(self._clock()),
                    "recommendation_only": True,
                    "requires_user_confirmation": True,
                    "authorizes_submission": False,
                }
                updated = _validate_state({
                    "schema": STORE_SCHEMA,
                    "revision": current["revision"] + 1,
                    "selection": selection,
                })
                self._write_unlocked(updated)
                return self._result(updated, ok=True)


__all__ = [
    "LOCK_FILENAME", "SCHEMA", "SELECTION_FILENAME", "SELECTION_SCHEMA",
    "STORE_SCHEMA", "LabPolicyBusy", "LabPolicyError", "LabPolicySelectionStore",
    "catalog", "default_selection_path", "normalize_overrides", "resolve",
]
