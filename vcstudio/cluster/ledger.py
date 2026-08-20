"""本地任务登记表：跨进程串行维护受管作业目录。

状态真相仍在各目录的 ``job.yaml``；ledger 只存路径。所有 read-modify-write 都在同一
稳定锁文件下执行，使用唯一临时文件、文件 fsync、原子替换和目录 durability。Method
Recipe 事务可为自己新增的 entry 写内部 owner；普通调用方仍看到原有 list/bool API。
"""
from __future__ import annotations

import errno
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from vcstudio.shared import manifest as manifest_mod
from vcstudio.shared.config import user_config_dir


LEDGER_NAME = 'jobs.json'
_OWNERS_KEY = '_transaction_owners'
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def default_ledger_path() -> Path:
    return user_config_dir() / LEDGER_NAME


def _canonical_entry(job_dir: str | os.PathLike) -> str:
    return str(Path(job_dir).resolve())


def _ledger_path(path: str | os.PathLike | None) -> Path:
    return Path(path) if path is not None else default_ledger_path()


def _lock_path(path: Path) -> Path:
    return path.with_name(f'.{path.name}.lock')


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _cross_process_lock(path: Path):
    """Lock one stable byte; the lock inode is never replaced by ledger writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == 'nt':
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _mutation_lock(path: Path):
    with _process_lock(path):
        with _cross_process_lock(_lock_path(path)):
            yield


def _load_state(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.is_file():
        return [], {}
    try:
        with path.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return [], {}
    raw_dirs = data.get('job_dirs') if isinstance(data, dict) else None
    dirs = [str(item) for item in raw_dirs] if isinstance(raw_dirs, list) else []
    raw_owners = data.get(_OWNERS_KEY) if isinstance(data, dict) else None
    owners = {
        str(entry): str(owner)
        for entry, owner in (raw_owners.items() if isinstance(raw_owners, dict) else [])
        if str(entry) in dirs and _HEX64.fullmatch(str(owner))
    }
    return dirs, owners


def _load_raw(path: Path) -> list:
    """Backward-compatible internal helper used by older tests/callers."""
    return _load_state(path)[0]


def _fsync_directory(path: Path) -> None:
    if os.name == 'nt':
        # MOVEFILE_WRITE_THROUGH below is the Windows directory durability boundary.
        return
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_durable(source: Path, target: Path) -> None:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        if not move(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)


def _save_state(path: Path, dirs: list[str], owners: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {'job_dirs': dirs}
    retained = {entry: owner for entry, owner in owners.items() if entry in dirs}
    if retained:
        payload[_OWNERS_KEY] = retained
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        _replace_durable(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _save_raw(path: Path, dirs: list) -> None:
    """Backward-compatible helper; production mutations call it only while locked."""
    _save_state(path, [str(item) for item in dirs], {})


def list_dirs(path: str | os.PathLike | None = None) -> list:
    return _load_state(_ledger_path(path))[0]


def register(job_dir: str | os.PathLike, path: str | os.PathLike | None = None) -> bool:
    """登记一个作业目录（绝对化、去重）。返回是否新增。"""
    target = _ledger_path(path)
    entry = _canonical_entry(job_dir)
    with _mutation_lock(target):
        dirs, owners = _load_state(target)
        if entry in dirs:
            return False
        dirs.append(entry)
        _save_state(target, dirs, owners)
        return True


def unregister(job_dir: str | os.PathLike, path: str | os.PathLike | None = None) -> bool:
    """移出登记（不动作业文件）。返回是否确有移除。"""
    target = _ledger_path(path)
    entry = _canonical_entry(job_dir)
    with _mutation_lock(target):
        dirs, owners = _load_state(target)
        if entry not in dirs:
            return False
        dirs = [item for item in dirs if item != entry]
        owners.pop(entry, None)
        _save_state(target, dirs, owners)
        return True


def register_owned(job_dir: str | os.PathLike, transaction_id: str,
                   path: str | os.PathLike | None = None) -> dict[str, bool]:
    """Idempotently register and durably attribute a newly added entry to one transaction."""
    if not _HEX64.fullmatch(str(transaction_id)):
        raise ValueError('transaction_id must be a lowercase sha256')
    target = _ledger_path(path)
    entry = _canonical_entry(job_dir)
    owner_id = str(transaction_id)
    with _mutation_lock(target):
        dirs, owners = _load_state(target)
        if entry in dirs:
            owned = owners.get(entry) == owner_id
            return {'added': owned, 'preexisting': not owned, 'owned': owned}
        dirs.append(entry)
        owners[entry] = owner_id
        _save_state(target, dirs, owners)
        return {'added': True, 'preexisting': False, 'owned': True}


def registration_state(job_dir: str | os.PathLike, transaction_id: str,
                       path: str | os.PathLike | None = None) -> dict[str, bool]:
    """Read entry/owner state under the same lock used by every mutation."""
    target = _ledger_path(path)
    entry = _canonical_entry(job_dir)
    with _mutation_lock(target):
        dirs, owners = _load_state(target)
        present = entry in dirs
        owned = present and owners.get(entry) == str(transaction_id)
        return {'present': present, 'owned': owned, 'preexisting': present and not owned}


def unregister_owned(job_dir: str | os.PathLike, transaction_id: str,
                     path: str | os.PathLike | None = None) -> bool:
    """Remove only an entry whose durable owner is exactly this transaction."""
    target = _ledger_path(path)
    entry = _canonical_entry(job_dir)
    with _mutation_lock(target):
        dirs, owners = _load_state(target)
        if entry not in dirs or owners.get(entry) != str(transaction_id):
            return False
        dirs = [item for item in dirs if item != entry]
        owners.pop(entry, None)
        _save_state(target, dirs, owners)
        return True


def release_registration_owner(job_dir: str | os.PathLike, transaction_id: str,
                               path: str | os.PathLike | None = None) -> bool:
    """Keep the entry but remove transient rollback authority after final verification."""
    target = _ledger_path(path)
    entry = _canonical_entry(job_dir)
    with _mutation_lock(target):
        dirs, owners = _load_state(target)
        if entry not in dirs or owners.get(entry) != str(transaction_id):
            return False
        owners.pop(entry, None)
        _save_state(target, dirs, owners)
        return True


def load_all(path: str | os.PathLike | None = None) -> list:
    """[(job_dir, manifest|None), …]；目录消失或无 job.yaml 时 manifest=None。"""
    out = []
    for directory in list_dirs(path):
        loaded = manifest_mod.load_manifest(directory) if os.path.isdir(directory) else None
        out.append((directory, loaded))
    return out
