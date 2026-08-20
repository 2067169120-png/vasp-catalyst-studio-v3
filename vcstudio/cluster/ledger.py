"""本地任务登记表：跨进程串行维护受管作业目录。

状态真相仍在各目录的 ``job.yaml``；ledger 只存路径。所有 read-modify-write 都在同一
稳定锁文件下执行，使用唯一临时文件、文件 fsync、原子替换和目录 durability。Method
Recipe 事务可为自己新增的 entry 写内部 owner；普通调用方仍看到原有 list/bool API。
"""
from __future__ import annotations

import copy
import errno
import hashlib
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


class LedgerProjectionError(RuntimeError):
    """A strict lifecycle projection could not safely read or merge the ledger."""


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


def _projection_payload(raw: bytes) -> tuple[dict[str, object], list[str], dict[str, str]]:
    """Strictly decode a lifecycle authority snapshot without repairing or discarding metadata."""
    if not raw:
        return {'job_dirs': []}, [], {}
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerProjectionError('jobs ledger is not valid UTF-8 JSON') from exc
    if not isinstance(value, dict) or not isinstance(value.get('job_dirs'), list):
        raise LedgerProjectionError('jobs ledger has an invalid schema')
    dirs = value['job_dirs']
    if any(not isinstance(item, str) or not item.strip() for item in dirs):
        raise LedgerProjectionError('jobs ledger contains an invalid locator')
    raw_owners = value.get(_OWNERS_KEY, {})
    if not isinstance(raw_owners, dict):
        raise LedgerProjectionError('jobs ledger has invalid transaction owners')
    if any(not isinstance(entry, str) or entry not in dirs
           or not isinstance(owner, str) or not _HEX64.fullmatch(owner)
           for entry, owner in raw_owners.items()):
        raise LedgerProjectionError('jobs ledger has invalid transaction owners')
    return copy.deepcopy(value), list(dirs), dict(raw_owners)


def _snapshot_unlocked(path: Path) -> dict[str, object]:
    try:
        exists = path.is_file()
        raw = path.read_bytes() if exists else b''
    except OSError as exc:
        raise LedgerProjectionError('jobs ledger could not be read') from exc
    payload, dirs, owners = _projection_payload(raw)
    return {
        'path': str(path.resolve(strict=False)), 'exists': exists, 'raw': raw,
        'payload': payload, 'job_dirs': dirs, 'owners': owners,
        'sha256': hashlib.sha256(raw).hexdigest(),
    }


def _entry_key(value: str | os.PathLike) -> str:
    expanded = os.path.expanduser(os.fspath(value))
    return os.path.normcase(os.path.realpath(os.path.abspath(expanded)))


def _validated_entries(values, *, field: str) -> list[str]:
    if not isinstance(values, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in values):
        raise LedgerProjectionError(f'{field} contains an invalid locator')
    keys: set[str] = set()
    result = []
    for item in values:
        key = _entry_key(item)
        if key in keys:
            raise LedgerProjectionError(f'{field} contains duplicate locators')
        keys.add(key)
        result.append(item)
    return result


def _merge_entry_delta(current_payload: dict[str, object], from_entries: list[str],
                       to_entries: list[str], *, restored_owners: dict[str, str] | None = None
                       ) -> dict[str, object]:
    """Apply one list delta to current authority while preserving unrelated paths and metadata."""
    current = _validated_entries(current_payload.get('job_dirs'), field='current ledger')
    from_by_key = {_entry_key(item): item for item in from_entries}
    to_by_key = {_entry_key(item): item for item in to_entries}
    removed = set(from_by_key) - set(to_by_key)
    added = [item for item in to_entries if _entry_key(item) not in from_by_key]
    merged = [item for item in current if _entry_key(item) not in removed]
    merged_keys = {_entry_key(item) for item in merged}
    for item in added:
        key = _entry_key(item)
        if key not in merged_keys:
            merged.append(item)
            merged_keys.add(key)

    raw_owners = current_payload.get(_OWNERS_KEY, {})
    owners = dict(raw_owners) if isinstance(raw_owners, dict) else {}
    exact_entries = set(merged)
    owners = {entry: owner for entry, owner in owners.items() if entry in exact_entries}
    for entry, owner in (restored_owners or {}).items():
        if entry in exact_entries and entry not in owners:
            owners[entry] = owner

    updated = copy.deepcopy(current_payload)
    updated['job_dirs'] = merged
    if owners:
        updated[_OWNERS_KEY] = owners
    else:
        updated.pop(_OWNERS_KEY, None)
    return updated


def render_projection(payload: dict[str, object], entries) -> bytes:
    """Render a journal projection while retaining only owners whose entries remain present."""
    if not isinstance(payload, dict):
        raise LedgerProjectionError('base ledger payload is invalid')
    projected = _validated_entries(entries, field='projected ledger')
    # Reuse the strict decoder so malformed owner metadata can never be normalized away.
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    current_payload, _dirs, owners = _projection_payload(encoded)
    current_payload['job_dirs'] = projected
    retained = {entry: owner for entry, owner in owners.items() if entry in projected}
    if retained:
        current_payload[_OWNERS_KEY] = retained
    else:
        current_payload.pop(_OWNERS_KEY, None)
    return (json.dumps(
        current_payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


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


def _save_payload(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _save_state(path: Path, dirs: list[str], owners: dict[str, str]) -> None:
    payload: dict[str, object] = {'job_dirs': dirs}
    retained = {entry: owner for entry, owner in owners.items() if entry in dirs}
    if retained:
        payload[_OWNERS_KEY] = retained
    _save_payload(path, payload)


def _save_raw(path: Path, dirs: list) -> None:
    """Backward-compatible helper; production mutations call it only while locked."""
    _save_state(path, [str(item) for item in dirs], {})


def projection_snapshot(path: str | os.PathLike | None = None) -> dict[str, object]:
    """Return a strict byte/hash/payload snapshot under the shared ledger OS lock."""
    target = _ledger_path(path)
    with _mutation_lock(target):
        return _snapshot_unlocked(target)


def merge_projection(*, base_sha256: str, base_entries, projected_entries,
                     path: str | os.PathLike | None = None) -> dict[str, object]:
    """CAS-or-merge a lifecycle list delta without erasing concurrent entries or owners."""
    if not _HEX64.fullmatch(str(base_sha256)):
        raise LedgerProjectionError('base ledger hash is invalid')
    base = _validated_entries(base_entries, field='base projection')
    projected = _validated_entries(projected_entries, field='projected ledger')
    target = _ledger_path(path)
    with _mutation_lock(target):
        current = _snapshot_unlocked(target)
        removed = {_entry_key(item) for item in base} - {
            _entry_key(item) for item in projected}
        if any(_entry_key(entry) in removed for entry in current['owners']):
            raise LedgerProjectionError(
                'lifecycle projection cannot remove a transaction-owned ledger entry')
        updated = _merge_entry_delta(current['payload'], base, projected)
        if updated != current['payload']:
            _save_payload(target, updated)
        result = _snapshot_unlocked(target)
        result['cas_matched'] = current['sha256'] == str(base_sha256)
        return result


def rollback_projection(*, original: bytes, projected: bytes,
                        path: str | os.PathLike | None = None) -> dict[str, object]:
    """Inverse-merge one lifecycle delta while preserving later writers and their owners."""
    original_payload, original_entries, original_owners = _projection_payload(original)
    _projected_payload, projected_entries, _projected_owners = _projection_payload(projected)
    target = _ledger_path(path)
    with _mutation_lock(target):
        current = _snapshot_unlocked(target)
        if current['raw'] == original:
            return current
        updated = _merge_entry_delta(
            current['payload'], projected_entries, original_entries,
            restored_owners=original_owners,
        )
        if (not original and updated == {'job_dirs': []}
                and current['payload'].keys() <= {'job_dirs', _OWNERS_KEY}):
            try:
                target.unlink(missing_ok=True)
                _fsync_directory(target.parent)
            except OSError as exc:
                raise LedgerProjectionError('jobs ledger rollback could not be published') from exc
        elif updated != current['payload']:
            _save_payload(target, updated)
        return _snapshot_unlocked(target)


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
