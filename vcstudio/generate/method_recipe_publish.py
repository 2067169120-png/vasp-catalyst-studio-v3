"""No-clobber, recoverable publication for confirmed Method Recipe bundles.

The complete six-file bundle is built in a sibling directory and committed with one atomic
no-replace directory rename.  A durable journal binds the parent and staged directory identities;
the same directory entity and every file hash are rechecked before and after ledger registration.
"""
from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from vcstudio.generate import method_recipes
from vcstudio.shared.config import user_config_dir


TRANSACTION_SCHEMA = "vcstudio.method-recipe-publish-transaction/v3"
TRANSACTION_DIR = "method-recipe-transactions"
_BUNDLE_FILES = (
    "INCAR", "POSCAR", "KPOINTS", "POTCAR", method_recipes.SIDECAR_NAME, "job.yaml",
)
_STATES = frozenset({
    "prepared", "bundle_published", "registering", "ledger_registered",
    "recovery_required",
})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class MethodRecipePublishError(RuntimeError):
    """A bounded publication failure that never reflects local paths or injected text."""

    def __init__(self, message: str, *, recovery_required: bool = False):
        super().__init__(message)
        self.recovery_required = bool(recovery_required)


def default_transaction_dir() -> Path:
    return user_config_dir() / TRANSACTION_DIR


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_id(target: Path) -> str:
    canonical = os.path.normcase(str(target.resolve(strict=False)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_durable(source: Path, target: Path) -> None:
    if os.name == "nt":
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_durable(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _advisory_lock(path: Path, *, timeout: float):
    """Cross-process target-operation lock retaining one stable inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
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
                        raise MethodRecipePublishError("method recipe target is busy") from exc
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
                        raise MethodRecipePublishError("method recipe target is busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _DirectoryBinding:
    """Open directory handle plus stable volume/device and file identity."""

    def __init__(self, path: Path, *, share_delete: bool):
        self.path = path
        self._handle: int | None = None
        self._descriptor: int | None = None
        if os.name == "nt":
            self._open_windows(share_delete=share_delete)
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            self._descriptor = os.open(path, flags)
            loaded = os.fstat(self._descriptor)
            if not stat.S_ISDIR(loaded.st_mode):
                self.close()
                raise NotADirectoryError
            self.identity = {
                "kind": "posix_inode", "device": int(loaded.st_dev),
                "file_id": int(loaded.st_ino),
            }

    def _open_windows(self, *, share_delete: bool) -> None:
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD), ("creation", FileTime),
                ("access", FileTime), ("write", FileTime),
                ("volume_serial", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD), ("file_index_low", wintypes.DWORD),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        shares = 0x1 | 0x2 | (0x4 if share_delete else 0)
        handle = create(str(self.path), 0x80, shares, None, 3, 0x02000000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = int(handle)
        info = FileInformation()
        query = kernel.GetFileInformationByHandle
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
        query.restype = wintypes.BOOL
        if not query(handle, ctypes.byref(info)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)
        if not info.attributes & 0x10:
            self.close()
            raise NotADirectoryError
        self.identity = {
            "kind": "windows_file_id", "volume_serial": int(info.volume_serial),
            "file_id": int((info.file_index_high << 32) | info.file_index_low),
        }

    def close(self) -> None:
        if self._handle is not None:
            from ctypes import wintypes

            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(self._handle)
            self._handle = None
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


def _identity_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("kind") == "windows_file_id":
        return (set(value) == {"kind", "volume_serial", "file_id"}
                and all(isinstance(value[key], int) and value[key] >= 0
                        for key in ("volume_serial", "file_id")))
    if value.get("kind") == "posix_inode":
        return (set(value) == {"kind", "device", "file_id"}
                and all(isinstance(value[key], int) and value[key] >= 0
                        for key in ("device", "file_id")))
    return False


def _identity_at_path(path: Path) -> dict[str, Any]:
    with _DirectoryBinding(path, share_delete=True) as binding:
        return dict(binding.identity)


def _assert_path_identity(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        actual = _identity_at_path(path)
    except OSError as exc:
        raise MethodRecipePublishError("published directory identity is unavailable") from exc
    if actual != dict(expected):
        raise MethodRecipePublishError("published directory identity changed")


def _rename_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically rename one sibling directory and fail if the destination exists."""
    if os.name == "nt":
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # WRITE_THROUGH only: deliberately omit REPLACE_EXISTING.
        if not move(str(source), str(target), 0x8):
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise FileExistsError(error, "target exists")
            raise ctypes.WinError(error)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise MethodRecipePublishError("atomic no-replace directory publish is unsupported")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    encoded_source = os.fsencode(source)
    encoded_target = os.fsencode(target)
    if renameat2(-100, encoded_source, -100, encoded_target, 1) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "target exists")
        raise OSError(error, os.strerror(error))
    _fsync_directory(target.parent)


class MethodRecipePublisher:
    """Stage, journal, no-clobber publish, identity-check, register, and recover."""

    def __init__(self, *, job_builder_mod, manifest_mod, ledger_mod,
                 registry_dir: str | os.PathLike | None = None,
                 lock_timeout: float = 10.0,
                 fault_hook: Callable[[str], None] | None = None):
        self.job_builder = job_builder_mod
        self.manifest = manifest_mod
        self.ledger = ledger_mod
        self.registry_dir = (Path(registry_dir) if registry_dir is not None
                             else default_transaction_dir())
        self.lock_timeout = float(lock_timeout)
        self.fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _journal_path(self, path_id: str) -> Path:
        return self.registry_dir / f"{path_id}.json"

    def _lock_path(self, path_id: str) -> Path:
        return self.registry_dir / f"{path_id}.lock"

    @staticmethod
    def _require_ledger_contract(ledger) -> None:
        required = {
            "register_owned", "registration_state", "unregister_owned",
            "release_registration_owner",
        }
        if not all(callable(getattr(ledger, name, None)) for name in required):
            raise MethodRecipePublishError("job ledger lacks transactional registration support")

    @staticmethod
    def _assert_bundle(root: Path, expected: Mapping[str, str]) -> None:
        try:
            entries = {item.name for item in root.iterdir()}
        except OSError as exc:
            raise MethodRecipePublishError("recipe bundle directory is unavailable") from exc
        if entries != set(_BUNDLE_FILES):
            raise MethodRecipePublishError("recipe bundle contains unexpected or missing files")
        for name in _BUNDLE_FILES:
            path = root / name
            try:
                loaded = path.lstat()
            except OSError as exc:
                raise MethodRecipePublishError("recipe bundle file is unavailable") from exc
            if (not stat.S_ISREG(loaded.st_mode) or path.is_symlink()
                    or _sha256_file(path) != expected[name]):
                raise MethodRecipePublishError("recipe bundle hash or file type mismatch")

    @staticmethod
    def _assert_manifest_lineage(written: Mapping[str, Any], expected: Mapping[str, str],
                                 sidecar_hash: str, record: Mapping[str, Any]) -> None:
        inputs = written.get("inputs") or {}
        reference = inputs.get("method_recipe") or {}
        if (dict(inputs.get("sha256") or {}) != {
                key: expected[key] for key in ("INCAR", "POSCAR", "KPOINTS", "POTCAR")}
                or inputs.get("poscar_sha256") != expected["POSCAR"]
                or inputs.get("potcar_sha256") != expected["POTCAR"]
                or reference.get("sidecar_sha256") != sidecar_hash
                or reference.get("incar_sha256") != expected["INCAR"]
                or reference.get("confirmation_sha256") != record["confirmation_sha256"]
                or record["incar_sha256"] != expected["INCAR"]
                or method_recipes.confirmation_binding_sha256(
                    record["confirmation_binding"]) != record["confirmation_sha256"]):
            raise MethodRecipePublishError("staged manifest lineage mismatch")

    @staticmethod
    def _validated_plan_binding(plan: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        binding = plan.get("confirmation_binding")
        claimed = str(plan.get("confirmation_sha256") or "")
        try:
            calculated = method_recipes.confirmation_binding_sha256(binding)
        except method_recipes.MethodRecipeError as exc:
            raise MethodRecipePublishError("confirmation binding is invalid") from exc
        expected = dict(plan.get("expected_hashes") or {})
        if (claimed != calculated or not isinstance(binding, Mapping)
                or dict(binding.get("expected_target_hashes") or {}) != expected
                or binding.get("target_id") != plan.get("target_id")
                or binding.get("preview_sha256") != plan.get("preview_sha256")
                or binding.get("recipe_semantic_sha256")
                != (plan.get("recipe") or {}).get("semantic_sha256")
                or dict(binding.get("resolutions") or {}) != dict(plan.get("resolutions") or {})):
            raise MethodRecipePublishError("confirmation binding differs from the write plan")
        return copy.deepcopy(dict(binding)), calculated

    @classmethod
    def _assert_plan_binding(cls, plan: Mapping[str, Any], journal: Mapping[str, Any]) -> None:
        binding, binding_hash = cls._validated_plan_binding(plan)
        if (binding != journal.get("confirmation_binding")
                or binding_hash != journal.get("confirmation_sha256")):
            raise MethodRecipePublishError(
                "recovery journal belongs to another confirmation", recovery_required=True)

    def _assert_persisted_binding(self, root: Path, journal: Mapping[str, Any]) -> None:
        try:
            record = json.loads(
                (root / method_recipes.SIDECAR_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MethodRecipePublishError("recipe confirmation record is unavailable") from exc
        written = self.manifest.load_manifest(root)
        reference = ((written or {}).get("inputs") or {}).get("method_recipe") or {}
        binding = journal["confirmation_binding"]
        binding_hash = journal["confirmation_sha256"]
        if (not isinstance(record, dict) or record.get("confirmation_binding") != binding
                or record.get("confirmation_sha256") != binding_hash
                or method_recipes.confirmation_binding_sha256(
                    record.get("confirmation_binding")) != binding_hash
                or record.get("target_id") != binding.get("target_id")
                or record.get("preview_sha256") != binding.get("preview_sha256")
                or record.get("recipe_semantic_sha256")
                != binding.get("recipe_semantic_sha256")
                or record.get("conflict_resolutions") != binding.get("resolutions")
                or record.get("incar_sha256")
                != binding.get("expected_target_hashes", {}).get("INCAR")
                or reference.get("confirmation_sha256") != binding_hash
                or reference.get("sidecar_sha256")
                != journal["files"][method_recipes.SIDECAR_NAME]):
            raise MethodRecipePublishError("persisted confirmation binding differs")

    def _write_journal(self, journal_path: Path, payload: dict[str, Any]) -> None:
        serializable = dict(payload)
        serializable["target"] = str(serializable["target"])
        serializable["stage"] = str(serializable["stage"])
        _atomic_json(journal_path, serializable)

    def _read_journal(self, journal_path: Path) -> dict[str, Any]:
        try:
            if journal_path.stat().st_size > 256 * 1024:
                raise ValueError
            value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise MethodRecipePublishError(
                "method recipe recovery journal is unreadable", recovery_required=True) from exc
        required = {
            "schema", "path_id", "transaction_id", "target", "stage", "state", "files",
            "target_id", "parent_identity", "stage_identity", "target_identity",
            "ledger_preexisting", "ledger_added", "registration_attempted", "created_at_unix",
            "confirmation_binding", "confirmation_sha256",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise MethodRecipePublishError(
                "method recipe recovery journal is invalid", recovery_required=True)
        target = Path(str(value["target"])).resolve(strict=False)
        stage = Path(str(value["stage"])).resolve(strict=False)
        path_id = str(value["path_id"])
        files = value["files"]
        target_identity = value["target_identity"]
        ledger_preexisting = value["ledger_preexisting"]
        ledger_added = value["ledger_added"]
        confirmation_binding = value["confirmation_binding"]
        confirmation_sha256 = str(value["confirmation_sha256"])
        try:
            calculated_confirmation = method_recipes.confirmation_binding_sha256(
                confirmation_binding)
        except method_recipes.MethodRecipeError as exc:
            raise MethodRecipePublishError(
                "method recipe recovery journal is invalid", recovery_required=True) from exc
        if (value["schema"] != TRANSACTION_SCHEMA or not _HEX64.fullmatch(path_id)
                or not _HEX64.fullmatch(str(value["transaction_id"]))
                or journal_path.name != f"{path_id}.json" or _path_id(target) != path_id
                or value["state"] not in _STATES or not isinstance(files, dict)
                or set(files) != set(_BUNDLE_FILES)
                or any(not _HEX64.fullmatch(str(item)) for item in files.values())
                or not _HEX64.fullmatch(str(value["target_id"]))
                or not _identity_valid(value["parent_identity"])
                or not _identity_valid(value["stage_identity"])
                or (target_identity is not None and not _identity_valid(target_identity))
                or (target_identity is not None and target_identity != value["stage_identity"])
                or ledger_preexisting not in {None, False, True}
                or ledger_added not in {None, False, True}
                or (ledger_preexisting is True and ledger_added is True)
                or not isinstance(value["registration_attempted"], bool)
                or confirmation_sha256 != calculated_confirmation
                or confirmation_binding.get("target_id") != value["target_id"]
                or any(confirmation_binding["expected_target_hashes"].get(key) != files[key]
                       for key in ("INCAR", "POSCAR", "KPOINTS", "POTCAR"))
                or stage.parent != target.parent
                or not stage.name.startswith(f".vcstudio-method-recipe-{path_id[:16]}-")):
            raise MethodRecipePublishError(
                "method recipe recovery journal is invalid", recovery_required=True)
        value["target"] = target
        value["stage"] = stage
        return value

    def _remove_journal(self, journal_path: Path) -> None:
        journal_path.unlink(missing_ok=True)
        if journal_path.parent.is_dir():
            _fsync_directory(journal_path.parent)

    def _assert_bound_bundle(self, journal: Mapping[str, Any], parent: _DirectoryBinding,
                             target: _DirectoryBinding) -> None:
        target_path = Path(journal["target"])
        if parent.identity != journal["parent_identity"]:
            raise MethodRecipePublishError("recipe target parent identity changed")
        _assert_path_identity(target_path.parent, parent.identity)
        if target.identity != journal["stage_identity"]:
            raise MethodRecipePublishError("published directory is not the staged entity")
        _assert_path_identity(target_path, target.identity)
        self._assert_bundle(target_path, journal["files"])
        self._assert_persisted_binding(target_path, journal)

    def _withdraw_owned_registration(self, journal: dict[str, Any]) -> None:
        # Rollback authority is deliberately narrower than registration authority.  A matching
        # ledger owner is necessary but not sufficient: the journal must also prove that this
        # transaction reached registration and durably observed that it added (rather than found)
        # the entry.  If register_owned committed and then failed before returning, recovery will
        # first observe the same owner idempotently and persist these flags before any withdrawal.
        if (journal.get("registration_attempted") is not True
                or journal.get("ledger_added") is not True
                or journal.get("ledger_preexisting") is not False):
            return
        state = self.ledger.registration_state(
            str(journal["target"]), journal["transaction_id"])
        if state.get("owned"):
            self.ledger.unregister_owned(
                str(journal["target"]), journal["transaction_id"])

    def _register_and_finalize(self, journal_path: Path, journal: dict[str, Any],
                               parent: _DirectoryBinding,
                               target_binding: _DirectoryBinding) -> None:
        self._assert_bound_bundle(journal, parent, target_binding)
        journal["registration_attempted"] = True
        journal["state"] = "registering"
        self._write_journal(journal_path, journal)
        try:
            result = self.ledger.register_owned(
                str(journal["target"]), journal["transaction_id"])
        except Exception as exc:  # noqa: BLE001 - complete bundle remains journaled for retry
            journal["state"] = "recovery_required"
            self._write_journal(journal_path, journal)
            raise MethodRecipePublishError(
                "valid recipe bundle awaits ledger recovery", recovery_required=True) from exc
        allowed_registration_results = {
            (True, False, True),
            (False, True, False),
        }
        if (not isinstance(result, Mapping)
                or set(result) != {"added", "preexisting", "owned"}
                or any(type(result[key]) is not bool for key in result)
                or (result["added"], result["preexisting"], result["owned"])
                not in allowed_registration_results):
            journal["state"] = "recovery_required"
            self._write_journal(journal_path, journal)
            raise MethodRecipePublishError(
                "job ledger returned invalid registration state", recovery_required=True)
        journal["ledger_added"] = bool(result["added"])
        journal["ledger_preexisting"] = bool(result["preexisting"])
        journal["state"] = "ledger_registered"
        self._write_journal(journal_path, journal)
        self._fault("after_ledger_register")
        try:
            self._assert_bound_bundle(journal, parent, target_binding)
            registered = self.ledger.registration_state(
                str(journal["target"]), journal["transaction_id"])
            if not registered.get("present"):
                raise MethodRecipePublishError("job ledger lost the registered recipe bundle")
            if journal["ledger_added"] and not registered.get("owned"):
                raise MethodRecipePublishError("recipe ledger ownership changed")
        except Exception:
            self._withdraw_owned_registration(journal)
            journal["state"] = "recovery_required"
            self._write_journal(journal_path, journal)
            raise
        if journal["ledger_added"]:
            try:
                released = self.ledger.release_registration_owner(
                    str(journal["target"]), journal["transaction_id"])
            except Exception as exc:  # noqa: BLE001 - recovery can idempotently finish
                journal["state"] = "recovery_required"
                self._write_journal(journal_path, journal)
                raise MethodRecipePublishError(
                    "recipe ledger ownership release awaits recovery",
                    recovery_required=True) from exc
            if not released:
                journal["state"] = "recovery_required"
                self._write_journal(journal_path, journal)
                raise MethodRecipePublishError(
                    "recipe ledger ownership release was not confirmed",
                    recovery_required=True)
        self._assert_bound_bundle(journal, parent, target_binding)
        final_registration = self.ledger.registration_state(
            str(journal["target"]), journal["transaction_id"])
        if not final_registration.get("present") or final_registration.get("owned"):
            journal["state"] = "recovery_required"
            self._write_journal(journal_path, journal)
            raise MethodRecipePublishError(
                "final recipe ledger registration was not stable",
                recovery_required=True)
        self._remove_journal(journal_path)

    def _ensure_published(self, journal_path: Path, journal: dict[str, Any],
                          parent: _DirectoryBinding) -> _DirectoryBinding:
        target = Path(journal["target"])
        stage = Path(journal["stage"])
        if parent.identity != journal["parent_identity"]:
            raise MethodRecipePublishError(
                "recipe target parent identity changed", recovery_required=True)
        _assert_path_identity(target.parent, parent.identity)
        if _lexists(target):
            if _lexists(stage):
                raise MethodRecipePublishError(
                    "target and staged recipe both exist", recovery_required=True)
            target_binding = _DirectoryBinding(target, share_delete=False)
            if target_binding.identity != journal["stage_identity"]:
                target_binding.close()
                raise MethodRecipePublishError(
                    "recipe target was replaced by another directory", recovery_required=True)
        else:
            if not _lexists(stage):
                raise MethodRecipePublishError(
                    "staged recipe directory is missing", recovery_required=True)
            with _DirectoryBinding(stage, share_delete=True) as stage_binding:
                if stage_binding.identity != journal["stage_identity"]:
                    raise MethodRecipePublishError(
                        "staged recipe directory identity changed", recovery_required=True)
                self._assert_bundle(stage, journal["files"])
                self._fault("before_bundle_publish")
                _rename_directory_no_replace(stage, target)
                self._fault("after_bundle_rename_before_bind")
                target_binding = _DirectoryBinding(target, share_delete=False)
                if target_binding.identity != stage_binding.identity:
                    target_binding.close()
                    raise MethodRecipePublishError(
                        "published directory is not the staged entity", recovery_required=True)
        journal["target_identity"] = dict(target_binding.identity)
        journal["state"] = "bundle_published"
        self._write_journal(journal_path, journal)
        self._assert_bound_bundle(journal, parent, target_binding)
        self._fault("after_bundle_publish")
        self._assert_bound_bundle(journal, parent, target_binding)
        return target_binding

    def _recover_locked(self, journal_path: Path) -> str:
        journal = self._read_journal(journal_path)
        try:
            with _DirectoryBinding(journal["target"].parent, share_delete=False) as parent:
                target_binding = self._ensure_published(journal_path, journal, parent)
                try:
                    self._register_and_finalize(journal_path, journal, parent, target_binding)
                finally:
                    target_binding.close()
            return "finalized"
        except MethodRecipePublishError:
            try:
                self._withdraw_owned_registration(journal)
            except Exception:  # noqa: BLE001 - never remove an unproven/preexisting entry
                pass
            journal["state"] = "recovery_required"
            self._write_journal(journal_path, journal)
            raise
        except Exception as exc:  # noqa: BLE001 - retain durable recovery authority
            try:
                self._withdraw_owned_registration(journal)
            except Exception:  # noqa: BLE001
                pass
            journal["state"] = "recovery_required"
            self._write_journal(journal_path, journal)
            raise MethodRecipePublishError(
                "method recipe transaction requires recovery", recovery_required=True) from exc

    def recover_all(self) -> dict[str, int]:
        """Resume journals after restart; foreign/replaced entities remain blocked and untouched."""
        result = {"finalized": 0, "rolled_back": 0, "blocked": 0}
        if not self.registry_dir.is_dir():
            return result
        for journal_path in sorted(self.registry_dir.glob("*.json")):
            path_id = journal_path.stem
            if not _HEX64.fullmatch(path_id):
                result["blocked"] += 1
                continue
            try:
                with _advisory_lock(self._lock_path(path_id), timeout=self.lock_timeout):
                    outcome = self._recover_locked(journal_path)
                result[outcome] += 1
            except MethodRecipePublishError:
                result["blocked"] += 1
        return result

    def _published_result(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        target = Path(plan["target"])
        written = self.manifest.load_manifest(target)
        if not isinstance(written, dict):
            raise MethodRecipePublishError("published recipe manifest is unavailable")
        try:
            record = json.loads((target / method_recipes.SIDECAR_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MethodRecipePublishError("published recipe sidecar is unavailable") from exc
        binding, binding_hash = self._validated_plan_binding(plan)
        reference = ((written.get("inputs") or {}).get("method_recipe") or {})
        if (record.get("target_id") != plan["target_id"]
                or record.get("confirmation_binding") != binding
                or record.get("confirmation_sha256") != binding_hash
                or reference.get("confirmation_sha256") != binding_hash
                or record.get("recipe_semantic_sha256")
                != binding["recipe_semantic_sha256"]
                or record.get("preview_sha256") != binding["preview_sha256"]
                or record.get("conflict_resolutions") != binding["resolutions"]
                or record.get("incar_sha256") != binding["expected_target_hashes"]["INCAR"]):
            raise MethodRecipePublishError("published recipe confirmation binding differs")
        return {
            "payload": {"warnings": list(written.get("warnings") or [])},
            "manifest": written, "record": record,
            "sidecar_sha256": _sha256_file(target / method_recipes.SIDECAR_NAME),
            "input_hashes": {
                key: _sha256_file(target / key)
                for key in ("INCAR", "POSCAR", "KPOINTS", "POTCAR")
            },
        }

    def publish(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        self._require_ledger_contract(self.ledger)
        confirmation_binding, confirmation_sha256 = self._validated_plan_binding(plan)
        target = Path(plan["target"]).resolve(strict=False)
        path_id = _path_id(target)
        journal_path = self._journal_path(path_id)
        stage: Path | None = None
        journal_written = False
        with _advisory_lock(self._lock_path(path_id), timeout=self.lock_timeout):
            if journal_path.is_file():
                journal = self._read_journal(journal_path)
                self._assert_plan_binding(plan, journal)
                plan["revalidate"]()
                self._recover_locked(journal_path)
                return self._published_result(plan)
            plan["revalidate"]()
            target.parent.mkdir(parents=True, exist_ok=True)
            if _lexists(target):
                raise MethodRecipePublishError("method recipe target must not already exist")
            with _DirectoryBinding(target.parent, share_delete=False) as parent:
                stage = Path(tempfile.mkdtemp(
                    prefix=f".vcstudio-method-recipe-{path_id[:16]}-", dir=str(target.parent)))
                try:
                    payload = self.job_builder.build_job_dir(
                        str(plan["poscar_path"]), plan["final_incar"], str(stage),
                        calc_type=plan["calc_type"], kpoints=plan["kpoints"],
                        validate=False, lib_root=plan["lib_root"])
                    expected = dict(plan["expected_hashes"])
                    self._fault("after_build")
                    record = method_recipes.sidecar_record(
                        plan, incar_sha256=expected["INCAR"])
                    _sidecar, sidecar_hash = method_recipes.write_sidecar(stage, record)
                    expected[method_recipes.SIDECAR_NAME] = sidecar_hash
                    self._fault("after_sidecar")
                    reference = method_recipes.manifest_reference(
                        record, sidecar_sha256=sidecar_hash)
                    written = self.manifest.create_from_build(
                        stage, payload, poscar_path=str(plan["poscar_path"]), validate=False,
                        task_type=plan["task"], method_recipe_ref=reference,
                        job_id=f"method-recipe-{plan['target_id'][:24]}")
                    expected["job.yaml"] = _sha256_file(stage / "job.yaml")
                    self._assert_bundle(stage, expected)
                    self._assert_manifest_lineage(written, expected, sidecar_hash, record)
                    self._fault("after_manifest_stage")
                    plan["revalidate"]()
                    if parent.identity != _identity_at_path(target.parent) or _lexists(target):
                        raise MethodRecipePublishError("method recipe target changed during staging")
                    with _DirectoryBinding(stage, share_delete=True) as stage_binding:
                        self._assert_bundle(stage, expected)
                        transaction_id = hashlib.sha256(os.urandom(32)).hexdigest()
                        journal = {
                            "schema": TRANSACTION_SCHEMA, "path_id": path_id,
                            "transaction_id": transaction_id, "target": str(target),
                            "stage": str(stage), "state": "prepared", "files": expected,
                            "target_id": plan["target_id"],
                            "parent_identity": dict(parent.identity),
                            "stage_identity": dict(stage_binding.identity),
                            "target_identity": None, "ledger_preexisting": None,
                            "ledger_added": None, "registration_attempted": False,
                            "confirmation_binding": confirmation_binding,
                            "confirmation_sha256": confirmation_sha256,
                            "created_at_unix": int(time.time()),
                        }
                        self._write_journal(journal_path, journal)
                        journal_written = True
                        self._fault("after_journal")
                    target_binding = self._ensure_published(journal_path, journal, parent)
                    try:
                        self._register_and_finalize(
                            journal_path, journal, parent, target_binding)
                    finally:
                        target_binding.close()
                    journal_written = False
                    return {
                        "payload": payload, "manifest": written, "record": record,
                        "sidecar_sha256": sidecar_hash,
                        "input_hashes": {
                            key: expected[key]
                            for key in ("INCAR", "POSCAR", "KPOINTS", "POTCAR")
                        },
                    }
                except MethodRecipePublishError as exc:
                    if journal_written and journal_path.is_file():
                        journal = self._read_journal(journal_path)
                        journal["state"] = "recovery_required"
                        self._write_journal(journal_path, journal)
                        raise MethodRecipePublishError(
                            "confirmed recipe publication requires recovery",
                            recovery_required=True) from exc
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve journal after first authority
                    if journal_written and journal_path.is_file():
                        try:
                            journal = self._read_journal(journal_path)
                            try:
                                self._withdraw_owned_registration(journal)
                            except Exception:  # noqa: BLE001 - fail closed, journal remains
                                pass
                            journal["state"] = "recovery_required"
                            self._write_journal(journal_path, journal)
                        except Exception:  # noqa: BLE001
                            pass
                        raise MethodRecipePublishError(
                            "confirmed recipe publication requires recovery",
                            recovery_required=True) from exc
                    raise MethodRecipePublishError(
                        "confirmed recipe publication failed") from exc
                finally:
                    if stage is not None and stage.exists() and not journal_written:
                        shutil.rmtree(stage, ignore_errors=True)


__all__ = [
    "MethodRecipePublishError", "MethodRecipePublisher", "TRANSACTION_SCHEMA",
    "default_transaction_dir",
]
