"""campaign.lock 测试:原子互斥、过期只是信号绝不抢占、按 owner 释放。"""
import json
import time

from vcstudio.campaign import lock


def _write_lock(cdir, owner, ts):
    (cdir / lock.LOCK_NAME).write_text(
        json.dumps({'owner': owner, 'pid': 999, 'ts': ts}), encoding='utf-8')


def test_acquire_once_then_blocks(tmp_path):
    assert lock.acquire(str(tmp_path), 'autopilot') is True
    assert lock.acquire(str(tmp_path), 'gui') is False    # 已被占,第二次失败
    data = lock.read_lock(str(tmp_path))
    assert data['owner'] == 'autopilot'


def test_release_frees_lock(tmp_path):
    lock.acquire(str(tmp_path), 'autopilot')
    assert lock.release(str(tmp_path), owner='autopilot') is True
    assert lock.is_held(str(tmp_path)) is False
    # 释放后可再次获取
    assert lock.acquire(str(tmp_path), 'gui') is True


def test_release_wrong_owner_refused(tmp_path):
    lock.acquire(str(tmp_path), 'autopilot')
    assert lock.release(str(tmp_path), owner='someone-else') is False
    assert lock.is_held(str(tmp_path)) is True             # 不误删他人锁


def test_is_stale_reports_but_does_not_steal(tmp_path):
    old = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - 7200))
    _write_lock(tmp_path, 'dead-run', old)
    assert lock.is_stale(str(tmp_path), ttl_s=3600) is True
    # 过期只是安全信号:acquire 仍拒绝抢占,锁内容不变
    assert lock.acquire(str(tmp_path), 'newcomer') is False
    assert lock.read_lock(str(tmp_path))['owner'] == 'dead-run'


def test_fresh_lock_not_stale(tmp_path):
    lock.acquire(str(tmp_path), 'autopilot')
    assert lock.is_stale(str(tmp_path), ttl_s=3600) is False


def test_is_stale_on_missing_or_bad_ts(tmp_path):
    assert lock.is_stale(str(tmp_path)) is False           # 无锁 → 不声称过期
    _write_lock(tmp_path, 'x', 'not-a-timestamp')
    assert lock.is_stale(str(tmp_path)) is False           # 畸形时间戳 → 无从判定即 False


def test_is_stale_accepts_dict(tmp_path):
    old = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - 7200))
    assert lock.is_stale({'owner': 'x', 'ts': old}, ttl_s=3600) is True


def test_release_missing_lock_returns_false(tmp_path):
    assert lock.release(str(tmp_path)) is False
