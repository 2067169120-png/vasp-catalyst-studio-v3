"""本地任务登记表:记录"哪些作业目录归 vcstudio 管",状态真相在各目录的 job.yaml。

设计:登记表只存目录路径(去重、保序),不复制状态——避免双写不一致;
任务页每次刷新从 job.yaml 现读。文件落 %APPDATA%/vcstudio/jobs.json,原子写。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from vcstudio.shared.config import user_config_dir
from vcstudio.shared import manifest as manifest_mod

LEDGER_NAME = 'jobs.json'


def default_ledger_path() -> Path:
    return user_config_dir() / LEDGER_NAME


def _load_raw(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    dirs = data.get('job_dirs') if isinstance(data, dict) else None
    return [str(d) for d in dirs] if isinstance(dirs, list) else []


def _save_raw(path: Path, dirs: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'job_dirs': dirs}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_dirs(path: str | os.PathLike | None = None) -> list:
    return _load_raw(Path(path) if path is not None else default_ledger_path())


def register(job_dir: str | os.PathLike, path: str | os.PathLike | None = None) -> bool:
    """登记一个作业目录(绝对化、去重)。返回是否新增。"""
    target = Path(path) if path is not None else default_ledger_path()
    entry = str(Path(job_dir).resolve())
    dirs = _load_raw(target)
    if entry in dirs:
        return False
    dirs.append(entry)
    _save_raw(target, dirs)
    return True


def unregister(job_dir: str | os.PathLike, path: str | os.PathLike | None = None) -> bool:
    """移出登记(不动磁盘上的作业文件)。返回是否确有移除。"""
    target = Path(path) if path is not None else default_ledger_path()
    entry = str(Path(job_dir).resolve())
    dirs = _load_raw(target)
    if entry not in dirs:
        return False
    dirs = [d for d in dirs if d != entry]
    _save_raw(target, dirs)
    return True


def load_all(path: str | os.PathLike | None = None) -> list:
    """[(job_dir, manifest|None), …]:目录已消失或无 job.yaml → manifest=None(界面标注)。"""
    out = []
    for d in list_dirs(path):
        m = manifest_mod.load_manifest(d) if os.path.isdir(d) else None
        out.append((d, m))
    return out
