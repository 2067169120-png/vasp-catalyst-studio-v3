"""User-level persistence for Phase D analysis templates and favourites.

The file managed here is deliberately independent of every project.  A saved
template may contain presentation choices, but never project identities,
baselines, paths, credentials, parsed results, or scientific evidence.  Save
requests are still validated as complete analysis requests against the
caller-supplied, server-owned ``project_id`` before their reusable patch is
persisted.

Writers use revision compare-and-swap, a process lock, an OS advisory lock,
and same-directory fsync/atomic replacement.  A stale or failed writer cannot
silently replace a newer snapshot or expose a partially written JSON file.
"""
from __future__ import annotations

import copy
import errno
import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from vcstudio.project import analysis_registry


SCHEMA = "vcstudio.analysis-preferences/v1"
FILENAME = "analysis-preferences.json"
LOCK_FILENAME = ".analysis-preferences.lock"

MAX_FILE_BYTES = 1024 * 1024
MAX_TEMPLATE_BYTES = 32 * 1024
MAX_TEMPLATES = 128
MAX_FAVORITES = 64
MAX_DEFAULTS = 64
MAX_NAME_LENGTH = 160
MAX_STRING_LENGTH = 2048
MAX_COLLECTION_ITEMS = 64
MAX_NESTING_DEPTH = 4
MAX_REVISION = 2**53 - 1

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_URL_RE = re.compile(r"(?i)\b(?:https?|s3)://[^\s]+")
_DRIVE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
_UNC_PATH_RE = re.compile(
    r"(?<![:A-Za-z0-9])(?:\\\\|//)[^\\/\s]+[\\/][^\s]+")
_POSIX_PATH_RE = re.compile(r"(?<![#/A-Za-z0-9_])/(?!/)[^\s]+")
_TILDE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])~[\\/]")
_FILE_URI_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])file:(?:/{0,3}|\\)")
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bBearer\s+\S+|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+)")
_SENSITIVE_KEY_WORDS = frozenset({
    "password", "passwd", "secret", "token", "credential", "credentials",
    "auth", "authorization", "cookie", "cookies",
})
_SENSITIVE_KEY_NAMES = frozenset({
    "apikey", "api_key", "accesskey", "access_key", "privatekey",
    "private_key", "secretkey", "secret_key", "clientsecret", "client_secret",
    "accesstoken", "access_token", "refreshtoken", "refresh_token", "sshkey",
    "ssh_key", "signingkey", "signing_key", "encryptionkey", "encryption_key",
})

# These are the only fields that can survive conversion from a project-bound
# AnalysisSpec to a user-level template.  In particular, schema/analysis and
# every project/baseline identity are validated but intentionally discarded.
_REUSABLE_SPEC_KEYS = frozenset({
    "data_mode", "near_degenerate_eV", "precision", "missing_policy", "sort",
    "sensitivity_deadbands_eV",
})
_REQUEST_TEMPLATE_KEYS = frozenset({"id", "name", "analysis_id", "spec"})
_STORED_TEMPLATE_KEYS = frozenset({
    "id", "name", "analysis_id", "spec", "created_at", "updated_at",
})
_STATE_KEYS = frozenset({
    "schema", "revision", "templates", "favorites", "default_template_by_analysis",
})
_VALIDATION_PROJECT_ID = "analysis-preferences-validation"

_BUILTIN_TEMPLATES = {
    item["id"]: item for item in analysis_registry.builtin_view_templates()
}
_PROCESS_LOCK = threading.RLock()


class AnalysisPreferencesError(ValueError):
    """A request or persisted analysis-preference snapshot is unsafe."""


class AnalysisPreferencesBusy(AnalysisPreferencesError):
    """Another process held the analysis-preference writer lock too long."""


def default_preferences_path() -> Path:
    """Return the single user-level JSON path, independent of project paths."""
    from vcstudio.shared.config import user_config_dir

    return user_config_dir() / FILENAME


def _default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "revision": 0,
        "templates": [],
        "favorites": [],
        "default_template_by_analysis": {},
    }


def _looks_like_path(value: str) -> bool:
    inspected = _URL_RE.sub("", str(value or "").strip())
    if (_FILE_URI_RE.search(inspected) or _DRIVE_PATH_RE.search(inspected)
            or _UNC_PATH_RE.search(inspected) or _TILDE_PATH_RE.search(inspected)
            or _POSIX_PATH_RE.search(inspected)):
        return True
    return any(part == ".." for part in inspected.replace("\\", "/").split("/"))


def _looks_like_credential(value: str) -> bool:
    return bool(_CREDENTIAL_VALUE_RE.search(str(value or "")))


def _is_sensitive_key(value: str) -> bool:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    normalised = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
    if normalised in _SENSITIVE_KEY_NAMES:
        return True
    if set(normalised.split("_")) & _SENSITIVE_KEY_WORDS:
        return True
    compact = normalised.replace("_", "")
    return any(compact.endswith(word) for word in (
        "password", "passwd", "secret", "token", "credential", "credentials",
        "authorization", "cookie", "auth",
    ))


def _safe_string(value: Any, *, field: str, maximum: int = MAX_STRING_LENGTH,
                 nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise AnalysisPreferencesError(f"{field} must be a string")
    text = value.strip() if nonempty else value
    if nonempty and not text:
        raise AnalysisPreferencesError(f"{field} must not be empty")
    if len(text) > maximum:
        raise AnalysisPreferencesError(f"{field} exceeds {maximum} characters")
    if _CONTROL_RE.search(text):
        raise AnalysisPreferencesError(f"{field} contains control characters")
    if _looks_like_path(text):
        raise AnalysisPreferencesError(f"{field} must not contain a filesystem path")
    if _looks_like_credential(text):
        raise AnalysisPreferencesError(f"{field} must not contain a credential")
    return text


def _safe_identifier(value: Any, *, field: str) -> str:
    text = _safe_string(value, field=field, maximum=128, nonempty=True)
    if not _SAFE_ID_RE.fullmatch(text):
        raise AnalysisPreferencesError(f"{field} must be a safe opaque identifier")
    return text


def _safe_template_id(value: Any, *, field: str) -> str:
    text = _safe_string(value, field=field, maximum=64, nonempty=True)
    if not _SAFE_TEMPLATE_ID_RE.fullmatch(text):
        raise AnalysisPreferencesError(
            f"{field} must be a lower-case opaque template identifier")
    return text


def _known_analysis_id(value: Any, *, field: str = "analysis_id") -> str:
    identifier = _safe_identifier(value, field=field)
    try:
        analysis_registry.get_analysis(identifier)
    except analysis_registry.AnalysisRequestError as exc:
        raise AnalysisPreferencesError(str(exc)) from exc
    return identifier


def _validate_json_tree(value: Any, *, field: str, depth: int = 0) -> Any:
    """Copy a bounded JSON tree while rejecting hidden paths and credentials."""
    if depth > MAX_NESTING_DEPTH:
        raise AnalysisPreferencesError(f"{field} is nested too deeply")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 10**12:
            raise AnalysisPreferencesError(f"{field} integer is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 10**12:
            raise AnalysisPreferencesError(f"{field} number must be finite and bounded")
        return value
    if isinstance(value, str):
        return _safe_string(value, field=field)
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise AnalysisPreferencesError(f"{field} contains too many items")
        return [
            _validate_json_tree(item, field=f"{field}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise AnalysisPreferencesError(f"{field} contains too many keys")
        copied = {}
        for raw_key, item in value.items():
            key = _safe_string(raw_key, field=f"{field} key", maximum=128,
                               nonempty=True)
            if _is_sensitive_key(key):
                raise AnalysisPreferencesError(f"{field} contains a sensitive key")
            copied[key] = _validate_json_tree(
                item, field=f"{field}.{key}", depth=depth + 1)
        return copied
    raise AnalysisPreferencesError(
        f"{field} contains unsupported type {type(value).__name__}")


def _normalise_spec_patch(raw_spec: Any, *, analysis_id: str,
                          project_id: str) -> dict[str, Any]:
    if not isinstance(raw_spec, Mapping):
        raise AnalysisPreferencesError("template.spec must be an object")
    request = _validate_json_tree(raw_spec, field="template.spec")
    supplied_analysis = request.get("analysis_id")
    if supplied_analysis is not None and supplied_analysis != analysis_id:
        raise AnalysisPreferencesError(
            "template.analysis_id does not match template.spec.analysis_id")
    request.setdefault("analysis_id", analysis_id)
    try:
        normalised = analysis_registry.normalize_analysis_request(
            request, project_id=project_id, default_analysis_id=analysis_id)
    except analysis_registry.AnalysisRequestError as exc:
        raise AnalysisPreferencesError(str(exc)) from exc
    canonical = normalised.to_dict()
    patch = {
        key: copy.deepcopy(canonical[key])
        for key in _REUSABLE_SPEC_KEYS
        if key in request and canonical.get(key) is not None
    }
    encoded = json.dumps(
        patch, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_TEMPLATE_BYTES:
        raise AnalysisPreferencesError("template.spec is too large")
    return patch


def _normalise_template_request(value: Any, *, project_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AnalysisPreferencesError("template must be an object")
    if set(value) != _REQUEST_TEMPLATE_KEYS:
        unknown = set(value) - _REQUEST_TEMPLATE_KEYS
        missing = _REQUEST_TEMPLATE_KEYS - set(value)
        details = []
        if unknown:
            # Do not echo attacker-controlled field names: they could themselves
            # be a local path or credential that an API bridge later logs.
            details.append("unknown fields")
        if missing:
            details.append("missing fields: " + ", ".join(sorted(missing)))
        raise AnalysisPreferencesError("template has an invalid shape (" + "; ".join(details) + ")")
    template_id = _safe_template_id(value.get("id"), field="template.id")
    name = _safe_string(
        value.get("name"), field="template.name", maximum=MAX_NAME_LENGTH,
        nonempty=True)
    analysis_id = _known_analysis_id(value.get("analysis_id"))
    spec = _normalise_spec_patch(
        value.get("spec"), analysis_id=analysis_id, project_id=project_id)
    return {"id": template_id, "name": name, "analysis_id": analysis_id, "spec": spec}


def _parse_timestamp(value: Any, *, field: str) -> tuple[str, datetime]:
    text = _safe_string(value, field=field, maximum=64, nonempty=True)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalysisPreferencesError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise AnalysisPreferencesError(f"{field} must include a timezone")
    return text, parsed.astimezone(timezone.utc)


def _validate_stored_template(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STORED_TEMPLATE_KEYS:
        raise AnalysisPreferencesError("stored template has an invalid shape")
    template_id = _safe_template_id(value.get("id"), field="template.id")
    if template_id in _BUILTIN_TEMPLATES:
        raise AnalysisPreferencesError("stored template must not override a built-in template")
    name = _safe_string(
        value.get("name"), field="template.name", maximum=MAX_NAME_LENGTH,
        nonempty=True)
    analysis_id = _known_analysis_id(value.get("analysis_id"))
    raw_spec = value.get("spec")
    if not isinstance(raw_spec, Mapping):
        raise AnalysisPreferencesError("template.spec must be an object")
    if not set(raw_spec).issubset(_REUSABLE_SPEC_KEYS):
        raise AnalysisPreferencesError(
            "stored template.spec contains identity or unknown fields")
    spec = _normalise_spec_patch(
        raw_spec, analysis_id=analysis_id, project_id=_VALIDATION_PROJECT_ID)
    created_text, created = _parse_timestamp(
        value.get("created_at"), field="template.created_at")
    updated_text, updated = _parse_timestamp(
        value.get("updated_at"), field="template.updated_at")
    if updated < created:
        raise AnalysisPreferencesError("template.updated_at precedes template.created_at")
    result = {
        "id": template_id,
        "name": name,
        "analysis_id": analysis_id,
        "spec": spec,
        "created_at": created_text,
        "updated_at": updated_text,
    }
    encoded = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_TEMPLATE_BYTES:
        raise AnalysisPreferencesError("stored template is too large")
    return result


def validate_state(value: Any) -> dict[str, Any]:
    """Validate and detach the exact v1 persistence DTO."""
    if not isinstance(value, Mapping) or set(value) != _STATE_KEYS:
        raise AnalysisPreferencesError("analysis preferences have an invalid top-level shape")
    if value.get("schema") != SCHEMA:
        raise AnalysisPreferencesError(
            f"unsupported analysis preferences schema {value.get('schema')!r}")
    revision = value.get("revision")
    if (not isinstance(revision, int) or isinstance(revision, bool)
            or not 0 <= revision <= MAX_REVISION):
        raise AnalysisPreferencesError(
            "analysis preferences revision must be a bounded non-negative integer")

    raw_templates = value.get("templates")
    if not isinstance(raw_templates, list) or len(raw_templates) > MAX_TEMPLATES:
        raise AnalysisPreferencesError(
            f"templates must be an array with at most {MAX_TEMPLATES} entries")
    templates = [_validate_stored_template(item) for item in raw_templates]
    template_by_id = {}
    for template in templates:
        if template["id"] in template_by_id:
            raise AnalysisPreferencesError("templates contain duplicate ids")
        template_by_id[template["id"]] = template

    raw_favorites = value.get("favorites")
    if not isinstance(raw_favorites, list) or len(raw_favorites) > MAX_FAVORITES:
        raise AnalysisPreferencesError(
            f"favorites must be an array with at most {MAX_FAVORITES} entries")
    favorites = []
    for index, item in enumerate(raw_favorites):
        analysis_id = _known_analysis_id(item, field=f"favorites[{index}]")
        if analysis_id in favorites:
            raise AnalysisPreferencesError("favorites contain duplicate analysis ids")
        favorites.append(analysis_id)

    raw_defaults = value.get("default_template_by_analysis")
    if not isinstance(raw_defaults, Mapping) or len(raw_defaults) > MAX_DEFAULTS:
        raise AnalysisPreferencesError(
            f"default_template_by_analysis must contain at most {MAX_DEFAULTS} entries")
    defaults = {}
    for raw_analysis_id, raw_template_id in raw_defaults.items():
        analysis_id = _known_analysis_id(
            raw_analysis_id, field="default_template_by_analysis key")
        template_id = _safe_template_id(
            raw_template_id,
            field=f"default_template_by_analysis.{analysis_id}")
        template = template_by_id.get(template_id) or _BUILTIN_TEMPLATES.get(template_id)
        if template is None:
            raise AnalysisPreferencesError(
                f"default template {template_id!r} does not exist")
        if template.get("analysis_id") != analysis_id:
            raise AnalysisPreferencesError(
                f"default template {template_id!r} belongs to another analysis")
        defaults[analysis_id] = template_id

    state = {
        "schema": SCHEMA,
        "revision": revision,
        "templates": templates,
        "favorites": favorites,
        "default_template_by_analysis": defaults,
    }
    encoded = (json.dumps(
        state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise AnalysisPreferencesError("analysis preferences file is too large")
    return state


def _validate_expected_revision(value: Any) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= MAX_REVISION):
        raise AnalysisPreferencesError(
            "expected_revision must be a bounded non-negative integer")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def _advisory_lock(lock_path: Path, *, timeout: float = 5.0):
    """Serialize writers across processes while retaining one stable lock inode."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, float(timeout))
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
                        raise AnalysisPreferencesBusy(
                            "analysis preferences are busy") from exc
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
                        raise AnalysisPreferencesBusy(
                            "analysis preferences are busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class AnalysisPreferencesStore:
    """Atomic, CAS-protected storage for user analysis-view preferences."""

    def __init__(self, path: str | os.PathLike | None = None, *,
                 lock_timeout: float = 5.0,
                 clock: Callable[[], str] | None = None):
        self.path = Path(path) if path is not None else default_preferences_path()
        self.lock_path = self.path.parent / LOCK_FILENAME
        if (isinstance(lock_timeout, bool)
                or not isinstance(lock_timeout, (int, float))
                or not math.isfinite(float(lock_timeout))
                or not 0.0 <= float(lock_timeout) <= 60.0):
            raise AnalysisPreferencesError("lock_timeout must be between 0 and 60 seconds")
        self.lock_timeout = float(lock_timeout)
        self._clock = clock or _utc_now

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _default_state()
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise AnalysisPreferencesError(
                f"cannot stat analysis preferences: {exc}") from exc
        if size > MAX_FILE_BYTES:
            raise AnalysisPreferencesError("analysis preferences file is too large")
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AnalysisPreferencesError(
                f"cannot read analysis preferences: {exc}") from exc
        return validate_state(raw)

    @staticmethod
    def _preferences(state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "templates": copy.deepcopy(state["templates"]),
            "favorites": list(state["favorites"]),
            "default_template_by_analysis": copy.deepcopy(
                state["default_template_by_analysis"]),
        }

    @classmethod
    def _result(cls, state: Mapping[str, Any], *, ok: bool,
                conflict: bool = False, error: str | None = None,
                **extra: Any) -> dict[str, Any]:
        result = {
            "ok": bool(ok),
            "conflict": bool(conflict),
            "revision": state["revision"],
            "preferences": cls._preferences(state),
            "error": error,
        }
        result.update(copy.deepcopy(extra))
        return result

    def read(self) -> dict[str, Any]:
        """Return a detached safe snapshot without creating the file or lock."""
        with _PROCESS_LOCK:
            state = self._read_unlocked()
            return self._result(state, ok=True)

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        validated = validate_state(state)
        payload = (json.dumps(
            validated, ensure_ascii=False, sort_keys=True, indent=2,
            allow_nan=False,
        ) + "\n").encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise AnalysisPreferencesError("analysis preferences file is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    # Directory handles/fsync are unavailable on some Windows
                    # filesystems; the file itself was fsynced before replace.
                    pass
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _next_revision(current: Mapping[str, Any]) -> int:
        if current["revision"] >= MAX_REVISION:
            raise AnalysisPreferencesError("analysis preferences revision is exhausted")
        return current["revision"] + 1

    def _timestamp(self) -> str:
        value = self._clock()
        text, _parsed = _parse_timestamp(value, field="clock timestamp")
        return text

    def update(self, template: Any, *, project_id: str,
               expected_revision: Any, set_default: bool = False) -> dict[str, Any]:
        """Create/update one custom template after project-bound normalization."""
        expected = _validate_expected_revision(expected_revision)
        if not isinstance(set_default, bool):
            raise AnalysisPreferencesError("set_default must be boolean")
        core = _normalise_template_request(template, project_id=project_id)
        if core["id"] in _BUILTIN_TEMPLATES:
            raise AnalysisPreferencesError("built-in template ids cannot be overwritten")

        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current = self._read_unlocked()
                if current["revision"] != expected:
                    return self._result(
                        current, ok=False, conflict=True,
                        error="analysis preferences revision conflict",
                        template=None)
                templates = copy.deepcopy(current["templates"])
                existing_index = next((
                    index for index, item in enumerate(templates)
                    if item["id"] == core["id"]
                ), None)
                now = self._timestamp()
                if existing_index is None:
                    if len(templates) >= MAX_TEMPLATES:
                        raise AnalysisPreferencesError("too many analysis templates")
                    canonical = {
                        **core, "created_at": now, "updated_at": now,
                    }
                    templates.append(canonical)
                else:
                    existing = templates[existing_index]
                    if existing["analysis_id"] != core["analysis_id"]:
                        raise AnalysisPreferencesError(
                            "an existing template's analysis_id cannot be changed")
                    canonical = {
                        **core,
                        "created_at": existing["created_at"],
                        "updated_at": now,
                    }
                    templates[existing_index] = canonical

                defaults = copy.deepcopy(current["default_template_by_analysis"])
                if set_default:
                    defaults[core["analysis_id"]] = core["id"]
                updated = {
                    "schema": SCHEMA,
                    "revision": self._next_revision(current),
                    "templates": templates,
                    "favorites": list(current["favorites"]),
                    "default_template_by_analysis": defaults,
                }
                updated = validate_state(updated)
                self._write_unlocked(updated)
                canonical = next(
                    item for item in updated["templates"] if item["id"] == core["id"])
                return self._result(
                    updated, ok=True, template=copy.deepcopy(canonical))

    def delete(self, template_id: Any, *, expected_revision: Any) -> dict[str, Any]:
        """Delete one custom template and clear defaults that reference it."""
        expected = _validate_expected_revision(expected_revision)
        identifier = _safe_template_id(template_id, field="template_id")
        if identifier in _BUILTIN_TEMPLATES:
            raise AnalysisPreferencesError("built-in templates cannot be deleted")
        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current = self._read_unlocked()
                if current["revision"] != expected:
                    return self._result(
                        current, ok=False, conflict=True,
                        error="analysis preferences revision conflict",
                        deleted_template_id=None)
                templates = [
                    copy.deepcopy(item) for item in current["templates"]
                    if item["id"] != identifier
                ]
                if len(templates) == len(current["templates"]):
                    raise AnalysisPreferencesError(f"unknown template id: {identifier}")
                defaults = {
                    analysis_id: selected
                    for analysis_id, selected
                    in current["default_template_by_analysis"].items()
                    if selected != identifier
                }
                updated = {
                    "schema": SCHEMA,
                    "revision": self._next_revision(current),
                    "templates": templates,
                    "favorites": list(current["favorites"]),
                    "default_template_by_analysis": defaults,
                }
                updated = validate_state(updated)
                self._write_unlocked(updated)
                return self._result(
                    updated, ok=True, deleted_template_id=identifier)

    def favorite(self, analysis_id: Any, *, favorite: bool,
                 expected_revision: Any) -> dict[str, Any]:
        """Add or remove one known analysis id from the ordered favourites."""
        expected = _validate_expected_revision(expected_revision)
        identifier = _known_analysis_id(analysis_id)
        if not isinstance(favorite, bool):
            raise AnalysisPreferencesError("favorite must be boolean")
        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current = self._read_unlocked()
                if current["revision"] != expected:
                    return self._result(
                        current, ok=False, conflict=True,
                        error="analysis preferences revision conflict",
                        analysis_id=identifier, favorite=None)
                favorites = list(current["favorites"])
                if favorite and identifier not in favorites:
                    if len(favorites) >= MAX_FAVORITES:
                        raise AnalysisPreferencesError("too many favorite analyses")
                    favorites.append(identifier)
                elif not favorite and identifier in favorites:
                    favorites.remove(identifier)
                updated = {
                    "schema": SCHEMA,
                    "revision": self._next_revision(current),
                    "templates": copy.deepcopy(current["templates"]),
                    "favorites": favorites,
                    "default_template_by_analysis": copy.deepcopy(
                        current["default_template_by_analysis"]),
                }
                updated = validate_state(updated)
                self._write_unlocked(updated)
                return self._result(
                    updated, ok=True, analysis_id=identifier, favorite=favorite)

    def set_default(self, analysis_id: Any, template_id: Any | None, *,
                    expected_revision: Any) -> dict[str, Any]:
        """Select a matching custom/built-in template, or clear the selection."""
        expected = _validate_expected_revision(expected_revision)
        identifier = _known_analysis_id(analysis_id)
        selected = None if template_id is None else _safe_template_id(
            template_id, field="template_id")
        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current = self._read_unlocked()
                if current["revision"] != expected:
                    return self._result(
                        current, ok=False, conflict=True,
                        error="analysis preferences revision conflict",
                        analysis_id=identifier, template_id=None)
                defaults = copy.deepcopy(current["default_template_by_analysis"])
                if selected is None:
                    defaults.pop(identifier, None)
                else:
                    templates = {
                        item["id"]: item for item in current["templates"]
                    }
                    template = templates.get(selected) or _BUILTIN_TEMPLATES.get(selected)
                    if template is None:
                        raise AnalysisPreferencesError(
                            f"unknown template id: {selected}")
                    if template.get("analysis_id") != identifier:
                        raise AnalysisPreferencesError(
                            "default template belongs to another analysis")
                    defaults[identifier] = selected
                updated = {
                    "schema": SCHEMA,
                    "revision": self._next_revision(current),
                    "templates": copy.deepcopy(current["templates"]),
                    "favorites": list(current["favorites"]),
                    "default_template_by_analysis": defaults,
                }
                updated = validate_state(updated)
                self._write_unlocked(updated)
                return self._result(
                    updated, ok=True, analysis_id=identifier, template_id=selected)


__all__ = [
    "SCHEMA", "FILENAME", "LOCK_FILENAME", "MAX_FILE_BYTES", "MAX_TEMPLATES",
    "AnalysisPreferencesBusy", "AnalysisPreferencesError",
    "AnalysisPreferencesStore", "default_preferences_path", "validate_state",
]
