"""Durable, non-scientific workspace UI state.

The workspace state deliberately lives outside projects and ``config.yaml``.
It contains navigation/selection preferences and references to separately
stored drafts, never scientific results, draft bodies, credentials, or local
filesystem paths.

Updates use optimistic compare-and-swap (``revision``), an in-process lock,
an OS advisory lock, and same-directory atomic replacement.  A failed writer
therefore cannot expose a partially written JSON document or silently replace
preferences read by a concurrent writer.
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
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl


SCHEMA = "vcstudio.workspace-state/v1"
FILENAME = "workspace-state.json"
LOCK_FILENAME = ".workspace-state.lock"
MAX_FILE_BYTES = 1024 * 1024
MAX_PREFERENCES_BYTES = 256 * 1024
MAX_COLLECTION_ITEMS = 256
MAX_STRING_LENGTH = 2048
MAX_DRAFT_SIZE = 64 * 1024 * 1024

ROUTE_AREAS = frozenset({
    "home", "project", "prepare", "run", "analyze", "publish", "environment",
})
PREFERENCE_KEYS = frozenset({
    "route", "current_project_id", "current_analysis_id", "selected_job_id",
    "panels", "filters", "sort", "scroll", "draft_refs",
})
_CONTAINER_KEYS = frozenset({"panels", "filters", "sort", "scroll", "draft_refs"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_ANALYSIS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SAFE_VIEW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_HEX_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_AUTHORITY_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_URL_RE = re.compile(r"(?i)\b(?:https?|s3)://[^\s]+")
_DRIVE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
_UNC_PATH_RE = re.compile(
    r"(?<![:A-Za-z0-9])(?:\\\\|//)[^\\/\s]+[\\/][^\s]+")
_POSIX_PATH_RE = re.compile(r"(?<![#/A-Za-z0-9_])/(?!/)[^\s]+")
_TILDE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])~[\\/]")
_FILE_URI_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])file:(?:/{0,3}|\\)")
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\bBearer\s+\S+|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----)")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:password|passwd|pwd|secret|token|credential|"
    r"credentials|auth|authorization|cookie|api[-_ ]?key|access[-_ ]?key|"
    r"private[-_ ]?key|client[-_ ]?secret|access[-_ ]?token|"
    r"refresh[-_ ]?token|aws[-_ ]?(?:access[-_ ]?key[-_ ]?id|"
    r"secret[-_ ]?access[-_ ]?key))\s*[:=]\s*[^\s,;&]+")
_URL_USERINFO_RE = re.compile(
    r"(?i)\b[A-Z][A-Z0-9+.-]{1,31}://[^\s/@]+(?::[^\s/@]*)?@[^\s/]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_SENSITIVE_KEY_WORDS = frozenset({
    "password", "passwd", "secret", "token", "credential", "credentials",
    "auth", "authorization", "cookie", "cookies", "pwd",
})
_SENSITIVE_KEY_NAMES = frozenset({
    "key", "apikey", "api_key", "accesskey", "access_key", "privatekey",
    "private_key", "secretkey", "secret_key", "clientsecret", "client_secret",
    "accesstoken", "access_token", "refreshtoken", "refresh_token", "sshkey",
    "ssh_key", "signingkey", "signing_key", "encryptionkey", "encryption_key",
})

_DEFAULT_PREFERENCES = {
    "route": None,
    "current_project_id": None,
    "current_analysis_id": None,
    "selected_job_id": None,
    "panels": {},
    "filters": {},
    "sort": {},
    "scroll": {},
    "draft_refs": {},
}

_PROCESS_LOCK = threading.RLock()


class WorkspaceStateError(ValueError):
    """The persisted or requested workspace state violates its contract."""


class WorkspaceStateBusy(WorkspaceStateError):
    """Another process held the workspace-state writer lock too long."""


def default_state_path() -> Path:
    """Return the user-only state path, independent of project/config lookup."""
    from vcstudio.shared.config import user_config_dir

    return user_config_dir() / FILENAME


def default_preferences() -> dict:
    return copy.deepcopy(_DEFAULT_PREFERENCES)


def _looks_like_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    # URLs are not local filesystem paths.  Mask them before looking for an
    # absolute path embedded in otherwise ordinary UI text.
    inspected = _URL_RE.sub("", text)
    if (_FILE_URI_RE.search(inspected) or _DRIVE_PATH_RE.search(inspected)
            or _UNC_PATH_RE.search(inspected) or _TILDE_PATH_RE.search(inspected)
            or _POSIX_PATH_RE.search(inspected)):
        return True
    normalised = inspected.replace("\\", "/")
    return any(part == ".." for part in normalised.split("/"))


def _looks_like_credential(value: str) -> bool:
    text = str(value or "")
    return bool(
        _CREDENTIAL_VALUE_RE.search(text)
        or _CREDENTIAL_ASSIGNMENT_RE.search(text)
        or _URL_USERINFO_RE.search(text)
    )


def _is_sensitive_key(value: str) -> bool:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    normalised = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
    if normalised in _SENSITIVE_KEY_NAMES:
        return True
    if set(normalised.split("_")) & _SENSITIVE_KEY_WORDS:
        return True
    compact = normalised.replace("_", "")
    if any(compound in compact for compound in (
        "apikey", "accesskey", "secretkey", "privatekey", "clientsecret",
        "accesstoken", "refreshtoken", "sshkey", "signingkey",
        "encryptionkey",
    )):
        return True
    return any(compact.endswith(word) for word in (
        "password", "passwd", "secret", "token", "credential", "credentials",
        "authorization", "cookie", "auth",
    ))


def _validate_string(value: Any, *, field: str, max_length: int = MAX_STRING_LENGTH,
                     allow_path: bool = False, allow_credential: bool = False) -> str:
    if not isinstance(value, str):
        raise WorkspaceStateError(f"{field} must be a string")
    if len(value) > max_length:
        raise WorkspaceStateError(f"{field} exceeds {max_length} characters")
    if _CONTROL_RE.search(value):
        raise WorkspaceStateError(f"{field} contains control characters")
    if not allow_path and _looks_like_path(value):
        raise WorkspaceStateError(f"{field} must not contain a filesystem path")
    if not allow_credential and _looks_like_credential(value):
        raise WorkspaceStateError(f"{field} must not contain a credential")
    return value


def _validate_identifier(value: Any, *, field: str, analysis: bool = False) -> str | None:
    if value is None:
        return None
    text = _validate_string(value, field=field, max_length=128)
    regex = _SAFE_ANALYSIS_RE if analysis else _SAFE_ID_RE
    if not regex.fullmatch(text):
        raise WorkspaceStateError(f"{field} is not a safe opaque identifier")
    return text


def _validate_route(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WorkspaceStateError("preferences.route must be an object or null")
    if set(value) != {"hash", "area", "view"}:
        raise WorkspaceStateError("preferences.route requires exactly hash, area, and view")
    area = _validate_string(value.get("area"), field="preferences.route.area", max_length=24)
    if area not in ROUTE_AREAS:
        raise WorkspaceStateError(
            "preferences.route.area must be one of " + ", ".join(sorted(ROUTE_AREAS)))
    view = _validate_string(value.get("view"), field="preferences.route.view", max_length=64)
    if not _SAFE_VIEW_RE.fullmatch(view):
        raise WorkspaceStateError("preferences.route.view is invalid")
    route_hash = _validate_string(
        value.get("hash"), field="preferences.route.hash", max_length=512,
        allow_path=True)
    expected_area, allowed_views = _parse_route_hash(route_hash)
    if area != expected_area or view not in allowed_views:
        raise WorkspaceStateError(
            "preferences.route.hash must match preferences.route.area/view")
    return {"hash": route_hash, "area": area, "view": view}


def _parse_route_hash(route_hash: str) -> tuple[str, set[str]]:
    """Parse the Phase B canonical hash router without accepting local paths."""
    if not route_hash.startswith("#/") or "\\" in route_hash:
        raise WorkspaceStateError("preferences.route.hash must be a safe #/ deep link")
    path_part, separator, query = route_hash.partition("?")
    if separator and not query:
        raise WorkspaceStateError("preferences.route.hash has an invalid query")
    query_pairs = []
    query_names = []
    if query:
        if re.search(r"%(?![0-9A-Fa-f]{2})", query):
            raise WorkspaceStateError("preferences.route.hash has an invalid query")
        try:
            query_pairs = parse_qsl(
                query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError(
                "preferences.route.hash has an invalid query") from exc
        query_names = [pair[0] for pair in query_pairs]
        if len(query_names) != len(set(query_names)):
            raise WorkspaceStateError("preferences.route.hash has duplicate query parameters")
    segments = path_part[2:].split("/")
    if not segments or any(not segment for segment in segments):
        raise WorkspaceStateError("preferences.route.hash must be a safe #/ deep link")
    if any(not _SAFE_ID_RE.fullmatch(segment) for segment in segments):
        raise WorkspaceStateError("preferences.route.hash must be a safe #/ deep link")
    if any(_is_sensitive_key(segment) or _looks_like_credential(segment)
           for segment in segments):
        raise WorkspaceStateError(
            "preferences.route.hash must not contain a sensitive route segment")

    def route_result(area: str, views: set[str], allowed_query=()) -> tuple[str, set[str]]:
        unexpected = set(query_names) - set(allowed_query)
        if unexpected:
            raise WorkspaceStateError(
                "preferences.route.hash has unsupported query parameters: "
                + ", ".join(sorted(unexpected)))
        if any(_looks_like_credential(value) for _name, value in query_pairs):
            raise WorkspaceStateError(
                "preferences.route.hash must not contain a credential")
        for name, query_value in query_pairs:
            _validate_string(
                query_value, field=f"preferences.route.query.{name}", max_length=128)
            if not query_value:
                raise WorkspaceStateError(
                    "preferences.route.hash has an invalid query value")
            if name == "cluster" and re.search(r"[\\/?#&=]", query_value):
                raise WorkspaceStateError(
                    "preferences.route.hash has an invalid cluster filter")
            if name == "revision" and not re.fullmatch(r"[1-9][0-9]{0,8}", query_value):
                raise WorkspaceStateError(
                    "preferences.route.hash has an invalid report revision")
            if name != "cluster" and not _SAFE_ID_RE.fullmatch(query_value):
                raise WorkspaceStateError(
                    "preferences.route.hash has an invalid query value")
        return area, views

    head = segments[0]
    if segments == ["home"]:
        return route_result("home", {"home"})
    if head == "projects":
        if len(segments) == 1:
            return route_result("project", {"project-overview"})
        project_id = segments[1]
        if not _SAFE_ID_RE.fullmatch(project_id):
            raise WorkspaceStateError("preferences.route.hash has an invalid project id")
        if len(segments) == 2:
            return route_result("project", {"project-overview"})
        project_views = {
            "overview": "project-overview",
            "members": "project-members",
            "workflow": "project-workflow",
            "runs": "project-runs",
            "activity": "project-activity",
        }
        if len(segments) == 3 and segments[2] in project_views:
            return route_result("project", {project_views[segments[2]]})
        analysis_views = {
            "adsorption": "analyze-energy",
            "thermo": "analyze-thermo",
            "kinetics": "analyze-kinetics",
            "electronic": "analyze-electronic",
            "charge": "analyze-charge",
            "comparison": "analyze-comparison",
            "custom": "analyze-custom",
            "properties": "analyze-properties",
        }
        if len(segments) == 4 and segments[2] == "analysis" \
                and segments[3] in analysis_views:
            return route_result("analyze", {analysis_views[segments[3]]})
        raise WorkspaceStateError("preferences.route.hash is not a canonical project route")
    if segments == ["jobs"]:
        query_values = dict(query_pairs)
        if query_values.get("status") not in {
                None, "queue", "run", "need", "done", "fail"}:
            raise WorkspaceStateError(
                "preferences.route.hash has an unsupported job status filter")
        return route_result("run", {"run-jobs"}, {"status", "cluster"})
    if segments == ["run", "remote"]:
        return route_result("run", {"run-remote"})
    if head == "prepare" and len(segments) == 2 \
            and segments[1] in {"structure", "input", "batch", "templates", "preflight"}:
        return route_result("prepare", {f"prepare-{segments[1]}"})
    if head == "publish" and len(segments) == 2 \
            and segments[1] in {"figures", "report", "si", "draftpack", "versions", "export"}:
        allowed = ({"project"} if segments[1] == "figures"
                   else {"project", "spec", "revision"})
        return route_result("publish", {f"publish-{segments[1]}"}, allowed)
    environment_views = {
        "cluster": "environment-cluster",
        "local-runner": "environment-local",
        "dependencies": "environment-dependencies",
        "data-paths": "environment-paths",
        "templates": "environment-templates",
        "settings": "environment-settings",
    }
    if head == "environment" and len(segments) == 2 \
            and segments[1] in environment_views:
        return route_result("environment", {environment_views[segments[1]]})
    raise WorkspaceStateError("preferences.route.hash is not a canonical Phase B route")


def _validate_json_value(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > 5:
        raise WorkspaceStateError(f"{field} is nested too deeply")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 10**12:
            raise WorkspaceStateError(f"{field} integer is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 10**12:
            raise WorkspaceStateError(f"{field} number is invalid")
        return value
    if isinstance(value, str):
        return _validate_string(value, field=field)
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise WorkspaceStateError(f"{field} contains too many items")
        return [_validate_json_value(item, field=f"{field}[{index}]", depth=depth + 1)
                for index, item in enumerate(value)]
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise WorkspaceStateError(f"{field} contains too many keys")
        out = {}
        for raw_key, item in value.items():
            key = _validate_string(raw_key, field=f"{field} key", max_length=128)
            if _looks_like_path(key):
                raise WorkspaceStateError(f"{field} key must not contain a filesystem path")
            if _is_sensitive_key(key):
                raise WorkspaceStateError(f"{field} contains a sensitive credential key")
            out[key] = _validate_json_value(
                item, field=f"{field}.{key}", depth=depth + 1)
        return out
    raise WorkspaceStateError(f"{field} contains unsupported type {type(value).__name__}")


def _validate_scroll(value: Any) -> dict:
    if not isinstance(value, dict):
        raise WorkspaceStateError("preferences.scroll must be an object")
    if len(value) > MAX_COLLECTION_ITEMS:
        raise WorkspaceStateError("preferences.scroll contains too many entries")
    out = {}
    for raw_key, raw_position in value.items():
        key = _validate_string(raw_key, field="preferences.scroll key", max_length=128)
        if _looks_like_path(key):
            raise WorkspaceStateError("preferences.scroll key must not contain a path")
        if _is_sensitive_key(key):
            raise WorkspaceStateError(
                "preferences.scroll contains a sensitive credential key")
        if (not isinstance(raw_position, int) or isinstance(raw_position, bool)
                or raw_position < 0 or raw_position > 100_000_000):
            raise WorkspaceStateError(
                f"preferences.scroll.{key} must be a bounded non-negative integer")
        out[key] = raw_position
    return out


def _validate_draft_refs(value: Any) -> dict:
    if not isinstance(value, dict):
        raise WorkspaceStateError("preferences.draft_refs must be an object")
    if len(value) > MAX_COLLECTION_ITEMS:
        raise WorkspaceStateError("preferences.draft_refs contains too many drafts")
    allowed = {
        "project_id", "route", "kind", "blob_sha256", "dirty", "updated_at", "size",
    }
    out = {}
    for raw_id, raw_ref in value.items():
        draft_id = _validate_identifier(raw_id, field="draft id")
        if not isinstance(raw_ref, dict) or not set(raw_ref).issubset(allowed):
            raise WorkspaceStateError(
                f"preferences.draft_refs.{draft_id} contains unsupported fields")
        ref = {}
        if "project_id" in raw_ref:
            ref["project_id"] = _validate_identifier(
                raw_ref.get("project_id"), field=f"draft_refs.{draft_id}.project_id")
        if "route" in raw_ref:
            route = raw_ref.get("route")
            if isinstance(route, dict) or route is None:
                ref["route"] = _validate_route(route)
            else:
                route = _validate_string(
                    route, field=f"draft_refs.{draft_id}.route", max_length=243)
                _parse_route_hash(route)
                ref["route"] = route
        if "kind" in raw_ref:
            kind = _validate_identifier(
                raw_ref.get("kind"), field=f"draft_refs.{draft_id}.kind", analysis=True)
            if kind != "unverified_draft":
                raise WorkspaceStateError(
                    f"draft_refs.{draft_id}.kind must be unverified_draft")
            ref["kind"] = kind
        if "blob_sha256" in raw_ref:
            digest = _validate_string(
                raw_ref.get("blob_sha256"), field=f"draft_refs.{draft_id}.blob_sha256",
                max_length=64)
            if not _HEX_SHA256_RE.fullmatch(digest):
                raise WorkspaceStateError(
                    f"draft_refs.{draft_id}.blob_sha256 must be a SHA-256 digest")
            ref["blob_sha256"] = digest.lower()
        if "dirty" in raw_ref:
            if not isinstance(raw_ref.get("dirty"), bool):
                raise WorkspaceStateError(f"draft_refs.{draft_id}.dirty must be boolean")
            ref["dirty"] = raw_ref["dirty"]
        if "updated_at" in raw_ref:
            updated_at = _validate_string(
                raw_ref.get("updated_at"), field=f"draft_refs.{draft_id}.updated_at",
                max_length=64)
            try:
                parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise WorkspaceStateError(
                    f"draft_refs.{draft_id}.updated_at must be ISO-8601") from exc
            if parsed.tzinfo is None:
                raise WorkspaceStateError(
                    f"draft_refs.{draft_id}.updated_at must include a timezone")
            ref["updated_at"] = updated_at
        if "size" in raw_ref:
            size = raw_ref.get("size")
            if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                    or size > MAX_DRAFT_SIZE):
                raise WorkspaceStateError(
                    f"draft_refs.{draft_id}.size must be a bounded non-negative integer")
            ref["size"] = size
        out[draft_id] = ref
    return out


def validate_preferences(value: Any) -> dict:
    if not isinstance(value, dict):
        raise WorkspaceStateError("preferences must be an object")
    unknown = set(value) - PREFERENCE_KEYS
    if unknown:
        raise WorkspaceStateError(
            "preferences contains unknown keys: " + ", ".join(sorted(unknown)))
    merged = default_preferences()
    merged.update(copy.deepcopy(value))
    out = {
        "route": _validate_route(merged["route"]),
        "current_project_id": _validate_identifier(
            merged["current_project_id"], field="preferences.current_project_id"),
        "current_analysis_id": _validate_identifier(
            merged["current_analysis_id"], field="preferences.current_analysis_id",
            analysis=True),
        "selected_job_id": _validate_identifier(
            merged["selected_job_id"], field="preferences.selected_job_id"),
        "panels": _validate_json_value(merged["panels"], field="preferences.panels"),
        "filters": _validate_json_value(merged["filters"], field="preferences.filters"),
        "sort": _validate_json_value(merged["sort"], field="preferences.sort"),
        "scroll": _validate_scroll(merged["scroll"]),
        "draft_refs": _validate_draft_refs(merged["draft_refs"]),
    }
    for key in ("panels", "filters", "sort"):
        if not isinstance(out[key], dict):
            raise WorkspaceStateError(f"preferences.{key} must be an object")
    encoded = json.dumps(out, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PREFERENCES_BYTES:
        raise WorkspaceStateError("preferences payload is too large")
    return out


def validate_state(value: Any, *, allow_legacy: bool = False) -> dict:
    if not isinstance(value, dict):
        raise WorkspaceStateError("workspace state must be an object")
    current_shape = {"schema", "authority_id", "revision", "preferences"}
    legacy_shape = {"schema", "revision", "preferences"}
    keys = set(value)
    if keys != current_shape and not (allow_legacy and keys == legacy_shape):
        raise WorkspaceStateError("workspace state has an invalid top-level shape")
    if value.get("schema") != SCHEMA:
        raise WorkspaceStateError(f"unsupported workspace state schema {value.get('schema')!r}")
    authority_id = value.get("authority_id")
    if keys == legacy_shape:
        authority_id = uuid.uuid4().hex
    if not isinstance(authority_id, str) or not _AUTHORITY_ID_RE.fullmatch(authority_id):
        raise WorkspaceStateError(
            "workspace state authority_id must be a 32-character lowercase hex token")
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise WorkspaceStateError("workspace state revision must be a non-negative integer")
    return {
        "schema": SCHEMA,
        "authority_id": authority_id,
        "revision": revision,
        "preferences": validate_preferences(value.get("preferences")),
    }


def _default_state() -> dict:
    return {
        "schema": SCHEMA,
        "authority_id": uuid.uuid4().hex,
        "revision": 0,
        "preferences": default_preferences(),
    }


@contextmanager
def _advisory_lock(lock_path: Path, *, timeout: float = 5.0):
    """Serialize writers across processes; keep the lock inode persistent."""
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
                        raise WorkspaceStateBusy("workspace state is busy") from exc
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
                        raise WorkspaceStateBusy("workspace state is busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_preference(preferences: dict, raw_path: Any) -> None:
    path = _validate_string(raw_path, field="patch.remove entry", max_length=256)
    head, separator, nested_key = path.partition(".")
    if head not in PREFERENCE_KEYS:
        raise WorkspaceStateError(f"patch.remove contains unsupported preference {path!r}")
    if not separator:
        preferences[head] = copy.deepcopy(_DEFAULT_PREFERENCES[head])
        return
    if head not in _CONTAINER_KEYS or not nested_key:
        raise WorkspaceStateError(f"patch.remove path {path!r} is invalid")
    key = _validate_string(nested_key, field="patch.remove nested key", max_length=128)
    container = preferences.get(head)
    if not isinstance(container, dict):
        container = {}
        preferences[head] = container
    container.pop(key, None)


class WorkspaceStateStore:
    """Atomic/CAS storage for the user-level workspace preferences."""

    def __init__(self, path: str | os.PathLike | None = None, *, lock_timeout: float = 5.0):
        self.path = Path(path) if path is not None else default_state_path()
        self.lock_path = self.path.parent / LOCK_FILENAME
        self.lock_timeout = float(lock_timeout)

    def _read_unlocked(self) -> tuple[dict, bool]:
        if not self.path.exists():
            return _default_state(), True
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise WorkspaceStateError(f"cannot stat workspace state: {exc}") from exc
        if size > MAX_FILE_BYTES:
            raise WorkspaceStateError("workspace state file is too large")
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceStateError(f"cannot read workspace state: {exc}") from exc
        legacy = isinstance(value, dict) and set(value) == {
            "schema", "revision", "preferences",
        }
        return validate_state(value, allow_legacy=True), legacy

    def read(self) -> dict:
        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                state, needs_persistence = self._read_unlocked()
                if needs_persistence:
                    self._write_unlocked(state)
                return copy.deepcopy(state)

    def _write_unlocked(self, state: dict) -> None:
        validated = validate_state(state)
        payload = (json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2)
                   + "\n").encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise WorkspaceStateError("workspace state file is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        tmp_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
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
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def update(self, patch: Any, expected_revision: Any,
               expected_authority_id: Any = None) -> dict:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) \
                or expected_revision < 0:
            raise WorkspaceStateError("expected_revision must be a non-negative integer")
        if expected_authority_id is not None and (
                not isinstance(expected_authority_id, str)
                or not _AUTHORITY_ID_RE.fullmatch(expected_authority_id)):
            raise WorkspaceStateError(
                "expected_authority_id must be a 32-character lowercase hex token or null")
        if not isinstance(patch, dict) or not set(patch).issubset({"set", "remove"}):
            raise WorkspaceStateError("patch must contain only set and remove")
        set_values = patch.get("set", {})
        removals = patch.get("remove", [])
        if not isinstance(set_values, dict):
            raise WorkspaceStateError("patch.set must be an object")
        if not isinstance(removals, list):
            raise WorkspaceStateError("patch.remove must be an array")
        unknown = set(set_values) - PREFERENCE_KEYS
        if unknown:
            raise WorkspaceStateError(
                "patch.set contains unknown keys: " + ", ".join(sorted(unknown)))
        if len(removals) > MAX_COLLECTION_ITEMS:
            raise WorkspaceStateError("patch.remove contains too many entries")

        # Validate every caller-controlled value before reading, creating, or
        # migrating the durable file.  Invalid credential/path-bearing updates
        # therefore cannot have a filesystem side effect, even when this is the
        # first access or the on-disk state still uses the legacy three-field
        # envelope.
        preflight = default_preferences()
        for key, value in set_values.items():
            preflight[key] = copy.deepcopy(value)
        for raw_path in removals:
            _remove_preference(preflight, raw_path)
        validate_preferences(preflight)

        with _PROCESS_LOCK:
            with _advisory_lock(self.lock_path, timeout=self.lock_timeout):
                current, needs_persistence = self._read_unlocked()
                authority_conflict = (
                    expected_authority_id is not None
                    and current["authority_id"] != expected_authority_id
                )
                if current["revision"] != expected_revision or authority_conflict:
                    if needs_persistence:
                        self._write_unlocked(current)
                    return {
                        "ok": False,
                        "conflict": True,
                        "authority_id": current["authority_id"],
                        "state_revision": current["revision"],
                        "preferences": copy.deepcopy(current["preferences"]),
                        "error": "workspace state revision conflict",
                    }
                preferences = copy.deepcopy(current["preferences"])
                for key, value in set_values.items():
                    preferences[key] = copy.deepcopy(value)
                for raw_path in removals:
                    _remove_preference(preferences, raw_path)
                preferences = validate_preferences(preferences)
                updated = {
                    "schema": SCHEMA,
                    "authority_id": current["authority_id"],
                    "revision": current["revision"] + 1,
                    "preferences": preferences,
                }
                self._write_unlocked(updated)
                return {
                    "ok": True,
                    "conflict": False,
                    "authority_id": updated["authority_id"],
                    "state_revision": updated["revision"],
                    "preferences": copy.deepcopy(preferences),
                    "error": None,
                }


__all__ = [
    "SCHEMA", "FILENAME", "ROUTE_AREAS", "WorkspaceStateError",
    "WorkspaceStateBusy", "WorkspaceStateStore", "default_preferences",
    "default_state_path", "validate_preferences", "validate_state",
]
