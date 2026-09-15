"""Atomic authority for the selected workflow, engine, and calculation.

The three values are one user intent.  Storing them through independent
``set_ui_state`` calls lets a slow request publish a torn or stale context.
The server-owned revision and intent identifier live in a dedicated JSON
snapshot so an unrelated legacy ``config.yaml`` save cannot roll them back.
Writers use both an in-process lock and a stable OS advisory lock, then
durably replace the whole snapshot.  ``config.yaml`` is read only as a
revision-zero migration input and is never rewritten by the file-backed
store, so context commits cannot clobber unrelated settings.
"""
from __future__ import annotations

import copy
import errno
import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any


SCHEMA = "vcstudio.settings-context/v1"
FILENAME = "workspace-context.json"
REVISION_KEY = "workspace_context_revision"
INTENT_KEY = "workspace_context_intent"
SCHEMA_KEY = "workspace_context_schema"
CONTEXT_KEYS = frozenset({"scenario", "engine", "calculation"})
MAX_REVISION = (1 << 53) - 1
MAX_VALUE_LENGTH = 128
MAX_STATE_BYTES = 64 * 1024
LOCK_FILENAME = ".workspace-context.lock"

_PROCESS_LOCK = threading.RLock()


class WorkspaceContextError(RuntimeError):
    """The workspace context could not be validated or persisted."""


class WorkspaceContextBusy(WorkspaceContextError):
    """Another process held the workspace-context writer lock too long."""


def _bounded_text(value: Any, *, field: str, allow_blank: bool = True) -> str:
    if not isinstance(value, str):
        raise WorkspaceContextError(f"{field} must be a string")
    text = value.strip()
    if not allow_blank and not text:
        raise WorkspaceContextError(f"{field} must not be blank")
    if len(text) > MAX_VALUE_LENGTH or any(ord(char) < 32 for char in text):
        raise WorkspaceContextError(f"{field} is invalid")
    return text


def _revision(value: Any) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= MAX_REVISION):
        raise WorkspaceContextError(
            "workspace context revision must be a bounded non-negative integer")
    return value


def _snapshot_from_config(config: Any) -> dict:
    if not isinstance(config, dict):
        raise WorkspaceContextError("config must be an object")
    raw_ui = config.get("ui")
    ui = raw_ui if isinstance(raw_ui, dict) else {}
    revision = _revision(ui.get(REVISION_KEY, 0))
    intent_value = ui.get(INTENT_KEY)
    intent = None
    if intent_value not in (None, ""):
        intent = _bounded_text(intent_value, field="workspace context intent",
                               allow_blank=False)
    return {
        "schema": SCHEMA,
        "revision": revision,
        "last_intent_id": intent,
        "scenario": _bounded_text(ui.get("scenario", ""), field="scenario"),
        "engine": _bounded_text(ui.get("active_engine", ""), field="engine"),
        "calculation": _bounded_text(
            ui.get("active_calculation", ""), field="calculation"),
    }


def _validated_context(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != CONTEXT_KEYS:
        raise WorkspaceContextError(
            "context must contain exactly scenario, engine, and calculation")
    return {
        key: _bounded_text(value.get(key), field=key, allow_blank=False)
        for key in ("scenario", "engine", "calculation")
    }


@contextmanager
def _advisory_lock(lock_path: Path, *, timeout: float):
    """Serialize writers across processes while retaining one lock inode."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
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
                        raise WorkspaceContextBusy("workspace context is busy") from exc
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
                        raise WorkspaceContextBusy("workspace context is busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorkspaceContextStore:
    """Revisioned CAS store with a dedicated durable authority snapshot.

    ``config_path`` is normally inferred from ``config_mod.writable_config_path``.
    Test adapters without a filesystem path remain process-safe and use their
    injected ``load_config``/``save_config`` functions.
    """

    def __init__(self, config_mod=None, config_path: str | os.PathLike | None = None,
                 *, state_path: str | os.PathLike | None = None,
                 lock_timeout: float = 5.0):
        if config_mod is None:
            from vcstudio.shared import config as config_mod
        if (isinstance(lock_timeout, bool)
                or not isinstance(lock_timeout, (int, float))
                or not math.isfinite(float(lock_timeout))
                or not 0.0 <= float(lock_timeout) <= 60.0):
            raise WorkspaceContextError("lock_timeout must be between 0 and 60 seconds")
        self._config = config_mod
        if config_path is not None:
            self.config_path = Path(config_path)
        else:
            resolver = getattr(config_mod, "writable_config_path", None)
            self.config_path = Path(resolver()) if callable(resolver) else None
        if state_path is not None:
            self.path = Path(state_path)
        elif self.config_path is not None:
            self.path = self.config_path.parent / FILENAME
        else:
            self.path = None
        self.lock_timeout = float(lock_timeout)
        self.lock_path = (self.path.parent / LOCK_FILENAME
                          if self.path is not None else None)

    def _load_config_unlocked(self) -> dict:
        try:
            config = (self._config.load_config(self.config_path)
                      if self.config_path is not None else self._config.load_config())
        except Exception as exc:  # noqa: BLE001 configuration adapter boundary
            raise WorkspaceContextError(f"cannot read workspace context: {exc}") from exc
        config = copy.deepcopy(config if isinstance(config, dict) else {})
        if not isinstance(config.get("ui"), dict):
            ui_loader = getattr(self._config, "get_ui_state", None)
            if callable(ui_loader):
                try:
                    ui = ui_loader(config)
                except Exception:  # noqa: BLE001 optional adapter compatibility
                    ui = None
                if isinstance(ui, dict):
                    config["ui"] = copy.deepcopy(ui)
        return config

    @staticmethod
    def _atomic_write(path: Path, encoded: bytes, *, limit: int) -> None:
        if len(encoded) > limit:
            raise WorkspaceContextError("workspace context payload is too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            try:
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
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
            if temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _read_authority_unlocked(self) -> dict:
        if self.path is None:
            # In-memory/test adapters have no sidecar location, so their
            # revision metadata remains the process-local authority.
            return _snapshot_from_config(self._load_config_unlocked())
        if not self.path.exists():
            config = self._load_config_unlocked()
            raw_ui = config.get("ui")
            if isinstance(raw_ui, dict):
                # Revision metadata in config was used by an earlier
                # implementation.  It is not durable authority: a missing
                # sidecar always starts one unambiguous migration epoch.
                ui = dict(raw_ui)
                ui.pop(REVISION_KEY, None)
                ui.pop(INTENT_KEY, None)
                ui.pop(SCHEMA_KEY, None)
                config["ui"] = ui
            legacy = _snapshot_from_config(config)
            # A config without a separate authority file is always the
            # migration baseline, regardless of any abandoned metadata keys.
            legacy.update({'schema': SCHEMA, 'revision': 0, 'last_intent_id': None})
            return legacy
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise WorkspaceContextError("workspace context file is too large")
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except WorkspaceContextError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceContextError(f"cannot read workspace context: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {
                'schema', 'revision', 'last_intent_id',
                'scenario', 'engine', 'calculation'}:
            raise WorkspaceContextError("workspace context file has an invalid shape")
        if value.get('schema') != SCHEMA:
            raise WorkspaceContextError(
                f"unsupported workspace context schema {value.get('schema')!r}")
        revision = _revision(value.get('revision'))
        intent_value = value.get('last_intent_id')
        intent = None if intent_value is None else _bounded_text(
            intent_value, field='workspace context intent', allow_blank=False)
        context = _validated_context({key: value.get(key) for key in CONTEXT_KEYS})
        return {'schema': SCHEMA, 'revision': revision, 'last_intent_id': intent,
                **context}

    def _write_authority_unlocked(self, snapshot: dict) -> None:
        if self.path is None:
            config = self._load_config_unlocked()
            ui = config.get("ui")
            ui = dict(ui) if isinstance(ui, dict) else {}
            ui.update({
                "scenario": snapshot["scenario"],
                "active_engine": snapshot["engine"],
                "active_calculation": snapshot["calculation"],
                REVISION_KEY: snapshot["revision"],
                INTENT_KEY: snapshot["last_intent_id"],
                SCHEMA_KEY: SCHEMA,
            })
            config["ui"] = ui
            try:
                self._config.save_config(config)
            except Exception as exc:  # noqa: BLE001 configuration adapter boundary
                raise WorkspaceContextError(
                    f"cannot persist workspace context: {exc}") from exc
            return
        encoded = (json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, indent=2,
            allow_nan=False) + "\n").encode("utf-8")
        self._atomic_write(self.path, encoded, limit=MAX_STATE_BYTES)

    def read(self) -> dict:
        with _PROCESS_LOCK:
            return copy.deepcopy(self._read_authority_unlocked())

    def compare_and_swap(self, context: Any, *, expected_revision: Any,
                         intent_id: Any) -> dict:
        expected = _revision(expected_revision)
        intent = _bounded_text(intent_id, field="intent_id", allow_blank=False)
        requested = _validated_context(context)

        with _PROCESS_LOCK:
            lock = (_advisory_lock(self.lock_path, timeout=self.lock_timeout)
                    if self.lock_path is not None else nullcontext())
            with lock:
                current = self._read_authority_unlocked()
                current_context = {key: current[key] for key in CONTEXT_KEYS}
                if current["last_intent_id"] == intent:
                    if current_context != requested:
                        return {
                            "ok": False, "conflict": True, "replayed": False,
                            "context": copy.deepcopy(current),
                            "error": "intent_id was already used for another context",
                        }
                    return {
                        "ok": True, "conflict": False, "replayed": True,
                        "context": copy.deepcopy(current), "error": None,
                    }
                if current["revision"] != expected:
                    return {
                        "ok": False, "conflict": True, "replayed": False,
                        "context": copy.deepcopy(current),
                        "error": "workspace context revision conflict",
                    }
                if expected >= MAX_REVISION:
                    raise WorkspaceContextError("workspace context revision is exhausted")
                updated = {
                    'schema': SCHEMA, 'revision': expected + 1,
                    'last_intent_id': intent, **requested,
                }
                self._write_authority_unlocked(updated)
                return {
                    "ok": True, "conflict": False, "replayed": False,
                    "context": copy.deepcopy(updated), "error": None,
                }
