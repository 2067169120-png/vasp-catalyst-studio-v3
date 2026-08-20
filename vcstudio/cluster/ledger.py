"""本地任务登记表:记录"哪些作业目录归 vcstudio 管",状态真相在各目录的 job.yaml。

设计:登记表存目录路径(去重、保序)及短暂的注册事务 owner,不复制作业状态;
owner 只用于崩溃安全补偿,最终状态真相仍在 job.yaml——避免双写不一致;
任务页每次刷新从 job.yaml 现读。文件落 %APPDATA%/vcstudio/jobs.json,原子写。
中文注释允许,英文标识符。
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import threading
import time

from vcstudio.shared.config import user_config_dir
from vcstudio.shared import manifest as manifest_mod

LEDGER_NAME = 'jobs.json'
_LEDGER_THREAD_LOCK = threading.RLock()
_TRANSACTION_ID_RE = re.compile(r'[a-f0-9]{32}')


def default_ledger_path() -> Path:
    return user_config_dir() / LEDGER_NAME


def _load_state(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.is_file():
        return [], {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return [], {}
    dirs = data.get('job_dirs') if isinstance(data, dict) else None
    normalized_dirs = [str(d) for d in dirs] if isinstance(dirs, list) else []
    raw_owners = data.get('registration_owners') if isinstance(data, dict) else None
    owners = {
        str(entry): str(transaction_id)
        for entry, transaction_id in (raw_owners.items()
                                      if isinstance(raw_owners, dict) else [])
        if (str(entry) in normalized_dirs
            and _TRANSACTION_ID_RE.fullmatch(str(transaction_id)))
    }
    return normalized_dirs, owners


def _load_raw(path: Path) -> list:
    return _load_state(path)[0]


def _save_raw(path: Path, dirs: list, owners: dict[str, str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        payload = {'job_dirs': dirs}
        if owners:
            payload['registration_owners'] = owners
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        directory_fd = os.open(path.parent, flags)
    except OSError:
        pass
    else:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)


@contextmanager
def _ledger_guard(path: Path):
    """Serialize read-modify-write on a stable, never-unlinked OS lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    with _LEDGER_THREAD_LOCK, open(lock_path, 'a+b') as handle:
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
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised by Linux CI
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _transaction_id(value: str) -> str:
    transaction_id = str(value or '').strip().lower()
    if not _TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise ValueError('ledger transaction_id must be 32 lowercase hexadecimal characters')
    return transaction_id


def list_dirs(path: str | os.PathLike | None = None) -> list:
    target = Path(path) if path is not None else default_ledger_path()
    with _ledger_guard(target):
        return _load_raw(target)


def register(job_dir: str | os.PathLike, path: str | os.PathLike | None = None) -> bool:
    """登记一个作业目录(绝对化、去重)。返回是否新增。"""
    target = Path(path) if path is not None else default_ledger_path()
    entry = str(Path(job_dir).resolve())
    with _ledger_guard(target):
        dirs, owners = _load_state(target)
        if entry in dirs:
            return False
        dirs.append(entry)
        _save_raw(target, dirs, owners)
        return True


def register_owned(job_dir: str | os.PathLike, transaction_id: str,
                   path: str | os.PathLike | None = None) -> bool:
    """登记并持久标记本事务所有权；返回是否由本次调用新增。

    已存在且无 owner 的普通登记不会被认领；另一事务持有时显式拒绝。
    同一事务在崩溃重启后重放是幂等的。
    """
    target = Path(path) if path is not None else default_ledger_path()
    entry = str(Path(job_dir).resolve())
    owner = _transaction_id(transaction_id)
    with _ledger_guard(target):
        dirs, owners = _load_state(target)
        if entry in dirs:
            current = owners.get(entry)
            if current not in {None, owner}:
                raise RuntimeError('ledger entry is owned by another registration transaction')
            return False
        dirs.append(entry)
        owners[entry] = owner
        _save_raw(target, dirs, owners)
        return True


def unregister_owned(job_dir: str | os.PathLike, transaction_id: str,
                     path: str | os.PathLike | None = None) -> bool:
    """仅撤销由指定事务新增且仍持有 owner 的登记。"""
    target = Path(path) if path is not None else default_ledger_path()
    entry = str(Path(job_dir).resolve())
    owner = _transaction_id(transaction_id)
    with _ledger_guard(target):
        dirs, owners = _load_state(target)
        if entry not in dirs or owners.get(entry) != owner:
            return False
        dirs = [value for value in dirs if value != entry]
        owners.pop(entry, None)
        _save_raw(target, dirs, owners)
        return True


def release_owner(job_dir: str | os.PathLike, transaction_id: str,
                  path: str | os.PathLike | None = None) -> bool:
    """在 manifest 与 batch authority 均持久后释放 owner，但保留登记。"""
    target = Path(path) if path is not None else default_ledger_path()
    entry = str(Path(job_dir).resolve())
    owner = _transaction_id(transaction_id)
    with _ledger_guard(target):
        dirs, owners = _load_state(target)
        if entry not in dirs or owners.get(entry) != owner:
            return False
        owners.pop(entry, None)
        _save_raw(target, dirs, owners)
        return True


def unregister(job_dir: str | os.PathLike, path: str | os.PathLike | None = None) -> bool:
    """移出登记(不动磁盘上的作业文件)。返回是否确有移除。"""
    target = Path(path) if path is not None else default_ledger_path()
    entry = str(Path(job_dir).resolve())
    with _ledger_guard(target):
        dirs, owners = _load_state(target)
        if entry not in dirs or entry in owners:
            return False
        dirs = [d for d in dirs if d != entry]
        _save_raw(target, dirs, owners)
        return True


def load_all(path: str | os.PathLike | None = None) -> list:
    """[(job_dir, manifest|None), …]:目录已消失或无 job.yaml → manifest=None(界面标注)。"""
    out = []
    for d in list_dirs(path):
        m = manifest_mod.load_manifest(d) if os.path.isdir(d) else None
        out.append((d, m))
    return out
