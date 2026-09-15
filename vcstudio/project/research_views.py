"""CAS-governed saved views for the cross-project research explorer.

Saved views are deliberately presentation-only.  The authority stores a
bounded name plus validated filters/sort/axes; it never stores project paths,
credentials, result rows, scientific values supplied by the browser, or free
form document text.  Scientific facts remain in project.yaml/job.yaml and the
manifest/validation chain.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from vcstudio.shared.config import user_config_dir


SCHEMA = "vcstudio.research-views/v1"
FILENAME = "research-views.json"
LOCK_FILENAME = "research-views.lock"
MAX_FILE_BYTES = 256 * 1024
MAX_VIEWS = 64
MAX_REVISION = 2**53 - 1

_PROCESS_LOCK = threading.RLock()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_AUTHORITY_RE = re.compile(r"^[a-f0-9]{32}$")
_ELEMENT_RE = re.compile(r"^[A-Z][a-z]?$")
_PATH_RE = re.compile(
    r"(?i)(?:^[A-Z]:[\\/]|^\\\\|^//|^/|^~[\\/]|\bfile:|[\\/].*[\\/])")
_SECRET_RE = re.compile(
    r"(?i)(?:github_pat_|gh[opusr]_|sk-|Bearer\s+|PRIVATE KEY|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[:=])")

_LIST_FILTERS = {
    "elements", "task_types", "states", "method_fingerprints",
    "evidence_levels", "project_ids",
}
_TEXT_FILTERS = {"formula", "facet", "adsorbate"}
_RANGE_FILTERS = {
    "energy_min_eV", "energy_max_eV", "barrier_min_eV", "barrier_max_eV",
}
_FILTER_KEYS = _LIST_FILTERS | _TEXT_FILTERS | _RANGE_FILTERS | {
    "method_compatible",
}
_SORT_KEYS = {
    "project", "job", "formula", "facet", "adsorbate", "task", "state",
    "method", "evidence", "energy_eV", "barrier_eV",
}
_AXES = {"energy_eV", "barrier_eV"}


class ResearchViewError(ValueError):
    """A saved-view request or authority file violates its contract."""


class ResearchViewBusy(ResearchViewError):
    """The saved-view authority lock could not be acquired in time."""


def default_views_path() -> Path:
    return user_config_dir() / FILENAME


def _safe_string(value: Any, *, field: str, maximum: int = 128,
                 allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise ResearchViewError(f"{field} must be a string")
    text = value.strip()
    if (not allow_blank and not text) or len(text) > maximum:
        raise ResearchViewError(f"{field} has an invalid length")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ResearchViewError(f"{field} contains control characters")
    if _PATH_RE.search(text):
        raise ResearchViewError(f"{field} must not contain a path")
    if _SECRET_RE.search(text):
        raise ResearchViewError(f"{field} must not contain a credential")
    return text


def _safe_id(value: Any, *, field: str) -> str:
    text = _safe_string(value, field=field)
    if not _ID_RE.fullmatch(text):
        raise ResearchViewError(f"{field} must be a safe opaque identifier")
    return text


def _finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchViewError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or not -1_000_000.0 <= number <= 1_000_000.0:
        raise ResearchViewError(f"{field} must be a bounded finite number")
    return number


def _identifier_list(value: Any, *, field: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ResearchViewError(f"{field} must be an array")
    if len(value) > 32:
        raise ResearchViewError(f"{field} contains too many identifiers")
    values: list[str] = []
    for index, raw in enumerate(value):
        if field == "elements":
            item = _safe_string(raw, field=f"{field}[{index}]", maximum=3)
            if not _ELEMENT_RE.fullmatch(item):
                raise ResearchViewError(f"{field}[{index}] is not an element symbol")
        else:
            item = _safe_id(raw, field=f"{field}[{index}]")
        if item not in values:
            values.append(item)
    return values


def normalize_filters(value: Any) -> dict[str, Any]:
    if value is None:
        return {"method_compatible": True}
    if not isinstance(value, Mapping):
        raise ResearchViewError("filters must be an object")
    unknown = set(value) - _FILTER_KEYS
    if unknown:
        raise ResearchViewError(
            "filters contain unknown fields: " + ", ".join(sorted(unknown)))
    result: dict[str, Any] = {}
    for key in sorted(_LIST_FILTERS):
        if key in value:
            result[key] = _identifier_list(value[key], field=key)
    for key in sorted(_TEXT_FILTERS):
        if key in value:
            result[key] = _safe_string(
                value[key], field=key, maximum=64, allow_blank=True)
    for key in sorted(_RANGE_FILTERS):
        if key in value and value[key] is not None:
            result[key] = _finite(value[key], field=key)
    compatible = value.get("method_compatible", True)
    if not isinstance(compatible, bool):
        raise ResearchViewError("method_compatible must be boolean")
    result["method_compatible"] = compatible
    for prefix in ("energy", "barrier"):
        low = result.get(f"{prefix}_min_eV")
        high = result.get(f"{prefix}_max_eV")
        if low is not None and high is not None and low > high:
            raise ResearchViewError(f"{prefix} minimum must not exceed maximum")
    return result


def normalize_sort(value: Any) -> dict[str, str]:
    if value is None:
        return {"key": "project", "direction": "asc"}
    if not isinstance(value, Mapping) or set(value) != {"key", "direction"}:
        raise ResearchViewError("sort requires exactly key and direction")
    key = str(value.get("key") or "")
    direction = str(value.get("direction") or "")
    if key not in _SORT_KEYS:
        raise ResearchViewError("sort.key is unsupported")
    if direction not in {"asc", "desc"}:
        raise ResearchViewError("sort.direction must be asc or desc")
    return {"key": key, "direction": direction}


def normalize_axes(value: Any) -> dict[str, str]:
    if value is None:
        return {"x": "energy_eV", "y": "barrier_eV"}
    if not isinstance(value, Mapping) or set(value) != {"x", "y"}:
        raise ResearchViewError("axes requires exactly x and y")
    x_axis = str(value.get("x") or "")
    y_axis = str(value.get("y") or "")
    if x_axis not in _AXES or y_axis not in _AXES:
        raise ResearchViewError("axes contain an unsupported scientific field")
    return {"x": x_axis, "y": y_axis}


def normalize_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchViewError("view must be an object")
    required = {"id", "name", "filters", "sort", "axes"}
    if set(value) != required:
        raise ResearchViewError("view requires exactly id/name/filters/sort/axes")
    return {
        "id": _safe_id(value.get("id"), field="view.id"),
        "name": _safe_string(value.get("name"), field="view.name", maximum=96),
        "filters": normalize_filters(value.get("filters")),
        "sort": normalize_sort(value.get("sort")),
        "axes": normalize_axes(value.get("axes")),
    }


def _validate_revision(value: Any, *, field: str) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value <= MAX_REVISION):
        raise ResearchViewError(f"{field} must be a bounded non-negative integer")
    return value


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
            "schema", "authority_id", "revision", "views"}:
        raise ResearchViewError("research-view authority has an invalid shape")
    if value.get("schema") != SCHEMA:
        raise ResearchViewError("unsupported research-view authority schema")
    authority_id = str(value.get("authority_id") or "")
    if not _AUTHORITY_RE.fullmatch(authority_id):
        raise ResearchViewError("research-view authority_id is invalid")
    revision = _validate_revision(value.get("revision"), field="revision")
    raw_views = value.get("views")
    if not isinstance(raw_views, list) or len(raw_views) > MAX_VIEWS:
        raise ResearchViewError(f"views must contain at most {MAX_VIEWS} entries")
    views = [normalize_view(view) for view in raw_views]
    identifiers = [view["id"] for view in views]
    if len(identifiers) != len(set(identifiers)):
        raise ResearchViewError("views contain duplicate ids")
    return {
        "schema": SCHEMA,
        "authority_id": authority_id,
        "revision": revision,
        "views": sorted(views, key=lambda item: item["id"]),
    }


@contextmanager
def _advisory_lock(path: Path, *, timeout: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.seek(0)
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise ResearchViewBusy("research-view authority is busy") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


class ResearchViewStore:
    """Small file-backed authority with authority_id + revision CAS."""

    def __init__(self, path: str | os.PathLike | None = None, *,
                 lock_timeout: float = 5.0, authority_id: str | None = None):
        self.path = Path(path) if path is not None else default_views_path()
        self.lock_path = self.path.parent / LOCK_FILENAME
        self.lock_timeout = float(lock_timeout)
        bootstrap = authority_id or uuid.uuid4().hex
        if not _AUTHORITY_RE.fullmatch(bootstrap):
            raise ResearchViewError("bootstrap authority_id is invalid")
        self._bootstrap_authority_id = bootstrap

    def _default(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "authority_id": self._bootstrap_authority_id,
            "revision": 0,
            "views": [],
        }

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ResearchViewError("research-view authority is too large")
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except ResearchViewError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResearchViewError(
                f"cannot read research-view authority: {exc}") from exc
        return validate_state(value)

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        validated = validate_state(state)
        payload = (json.dumps(
            validated, ensure_ascii=False, sort_keys=True, indent=2,
            allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise ResearchViewError("research-view authority is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _public(state: Mapping[str, Any], *, ok: bool = True,
                conflict: bool = False, error: str | None = None,
                **extra: Any) -> dict[str, Any]:
        return {
            "ok": ok,
            "conflict": conflict,
            "authority_id": state["authority_id"],
            "revision": state["revision"],
            "views": copy.deepcopy(state["views"]),
            "error": error,
            **extra,
        }

    def read(self) -> dict[str, Any]:
        with _PROCESS_LOCK:
            return self._public(self._read_unlocked())

    @staticmethod
    def _cas_matches(current: Mapping[str, Any], authority_id: Any,
                     revision: Any) -> bool:
        supplied = str(authority_id or "")
        expected = _validate_revision(revision, field="expected_revision")
        return (bool(_AUTHORITY_RE.fullmatch(supplied))
                and supplied == current["authority_id"]
                and expected == current["revision"])

    def save(self, view: Any, *, authority_id: Any,
             expected_revision: Any) -> dict[str, Any]:
        canonical = normalize_view(view)
        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current = self._read_unlocked()
                if not self._cas_matches(current, authority_id, expected_revision):
                    return self._public(
                        current, ok=False, conflict=True,
                        error="research-view authority revision conflict", view=None)
                if current["revision"] >= MAX_REVISION:
                    raise ResearchViewError("research-view authority revision is exhausted")
                views = copy.deepcopy(current["views"])
                index = next((
                    i for i, item in enumerate(views) if item["id"] == canonical["id"]
                ), None)
                if index is None:
                    if len(views) >= MAX_VIEWS:
                        raise ResearchViewError("too many saved research views")
                    views.append(canonical)
                else:
                    views[index] = canonical
                updated = {
                    **current,
                    "revision": current["revision"] + 1,
                    "views": views,
                }
                self._write_unlocked(updated)
                updated = validate_state(updated)
                return self._public(updated, view=copy.deepcopy(canonical))

    def delete(self, view_id: Any, *, authority_id: Any,
               expected_revision: Any) -> dict[str, Any]:
        identifier = _safe_id(view_id, field="view_id")
        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current = self._read_unlocked()
                if not self._cas_matches(current, authority_id, expected_revision):
                    return self._public(
                        current, ok=False, conflict=True,
                        error="research-view authority revision conflict",
                        deleted_view_id=None)
                views = [
                    copy.deepcopy(item) for item in current["views"]
                    if item["id"] != identifier
                ]
                if len(views) == len(current["views"]):
                    raise ResearchViewError(f"unknown research view: {identifier}")
                if current["revision"] >= MAX_REVISION:
                    raise ResearchViewError("research-view authority revision is exhausted")
                updated = {
                    **current,
                    "revision": current["revision"] + 1,
                    "views": views,
                }
                self._write_unlocked(updated)
                updated = validate_state(updated)
                return self._public(updated, deleted_view_id=identifier)


__all__ = [
    "SCHEMA", "ResearchViewBusy", "ResearchViewError", "ResearchViewStore",
    "default_views_path", "normalize_axes", "normalize_filters", "normalize_sort",
    "normalize_view", "validate_state",
]
