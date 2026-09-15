"""单机锁(蓝图 E6):防「GUI 手动操作 与 自动驾驶循环」或「两次自动提交」打架。

- `acquire(campaign_dir, owner)`:原子创建 run.lock(O_CREAT|O_EXCL),含 owner/pid/时间戳。
  已存在即返回 False——**无论是否过期都绝不抢占**(蓝图 D9:砍掉多主机租约/心跳/仲裁)。
- `is_stale(lock, ttl_s=3600)`:**过期只是安全信号,不是重投许可**。仅报告年龄超阈,
  是否提示用户/证据驱动恢复由调用方决定,本函数绝不自动删锁或抢占。
- `release(campaign_dir, owner=None)`:删锁;给 owner 时只删自己的锁(不误删他人)。

比多主机版极简:只做本机互斥。原子写借鉴 tmp/os.replace 家族(O_EXCL 本身即原子)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOCK_NAME = 'run.lock'
DEFAULT_TTL_S = 3600


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def lock_path(campaign_dir) -> Path:
    return Path(campaign_dir) / LOCK_NAME


def acquire(campaign_dir, owner: str) -> bool:
    """原子创建 run.lock。成功 True;已存在(有人持锁)False——绝不抢占。"""
    p = lock_path(campaign_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'owner': owner, 'pid': os.getpid(), 'ts': _now()},
                         ensure_ascii=False)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, payload.encode('utf-8'))
    finally:
        os.close(fd)
    return True


def read_lock(campaign_dir):
    """读锁内容 dict;无锁/畸形 → None。"""
    p = lock_path(campaign_dir)
    if not p.is_file():
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_held(campaign_dir) -> bool:
    return lock_path(campaign_dir).is_file()


def is_stale(lock, ttl_s: int = DEFAULT_TTL_S) -> bool:
    """锁年龄是否超 ttl。lock 可为锁 dict 或 campaign_dir。**只报告,不抢占。**

    无锁/无时间戳/时间戳畸形 → False(无从判定即不声称过期)。
    """
    data = lock if isinstance(lock, dict) else read_lock(lock)
    if not data:
        return False
    ts = data.get('ts')
    try:
        age = time.time() - time.mktime(time.strptime(ts, '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return False
    return age > ttl_s


def release(campaign_dir, owner: str | None = None) -> bool:
    """删锁。给 owner 时只删属于该 owner 的锁(避免误删他人锁);返回是否确有删除。"""
    p = lock_path(campaign_dir)
    if not p.is_file():
        return False
    if owner is not None:
        data = read_lock(campaign_dir)
        if data and data.get('owner') != owner:
            return False
    try:
        p.unlink()
        return True
    except OSError:
        return False
