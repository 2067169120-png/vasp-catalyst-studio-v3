"""实际核时统计(诚实口径):state_history 时间戳 × 提交核数 → 近 N 天实耗核时。

对齐 starpivot 概览的"近 30 天核时",但口径全程可解释、缺数据绝不编造:
- **运行窗口**:首个 RUNNING 时间戳 → 其后首个终态时间戳;从未观测到 RUNNING(轮询
  间隙内跑完)则回退 SUBMITTED 起点(窗口略偏大,per-job basis 字段注明)。仍在跑
  (无终态)→ 截至 now 实时累计,结果标 running=True。
- **核数**:attempts 末条的 cores(v3.3.0 起 submit 时记录 nodes×ppn)→ 缺则查该作业
  所属集群的当前 profile 配置(可能与当年提交不同,basis='profile' 注明)→ 仍缺 →
  该作业**不计入总数**并列在 unknown(说明原因),绝不用猜的核数凑总量。
- 时间戳与 manifest._now_iso 同源(本地时区 ISO 秒级);状态轮询间隔即时间误差量级。

纯函数零 IO(entries 由调用方给 ledger.load_all() 结果),全部离线可测。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

TERMINAL_STATES = ('DONE', 'FAILED', 'UNCONVERGED', 'NEEDS_HUMAN')
_ACTIVE_STATES = ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING')


def _parse_iso(s):
    """ISO 时间串 → datetime;解析失败 → None(旧数据/手改容忍)。"""
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def runtime_window(manifest: dict, *, now: datetime | None = None):
    """manifest → (start, end, running, why)。

    - (datetime, datetime, False, None):完整窗口(RUNNING|SUBMITTED → 首个其后终态);
    - (datetime, now, True, None):仍在跑,截至 now 实时累计;
    - (None, None, False, why):无法确定窗口的中文原因;
    - (None, None, False, None):该作业从未提交(CREATED 等),不属于核时统计对象。
    """
    now = now or datetime.now()
    hist = [h for h in (manifest.get('state_history') or []) if isinstance(h, dict)]
    start_idx, start_at, basis_state = None, None, None
    for prefer in ('RUNNING', 'SUBMITTED'):              # RUNNING 优先;缺则退 SUBMITTED
        for i, h in enumerate(hist):
            if h.get('state') == prefer:
                t = _parse_iso(h.get('at'))
                if t is not None:
                    start_idx, start_at, basis_state = i, t, prefer
                break
        if start_at is not None:
            break
    if start_at is None:
        if any(h.get('state') in ('SUBMITTED', 'RUNNING') for h in hist):
            return None, None, False, '提交/运行状态缺时间戳(旧版数据),无法计时'
        return None, None, False, None                   # 从未提交:静默不计
    for h in hist[start_idx + 1:]:
        if h.get('state') in TERMINAL_STATES:
            end = _parse_iso(h.get('at'))
            if end is None:
                return None, None, False, '终态缺时间戳,无法计时'
            if end < start_at:
                return None, None, False, '状态时间戳倒挂(时钟被改?),不计入'
            return start_at, end, False, ('起点按 SUBMITTED(未观测到 RUNNING,含排队,'
                                          '窗口偏大)' if basis_state == 'SUBMITTED' else None)
    if manifest.get('state') in _ACTIVE_STATES:
        return start_at, now, True, ('起点按 SUBMITTED(含排队)'
                                     if basis_state == 'SUBMITTED' else None)
    return None, None, False, '已离开活动态但未记终态时间戳,无法计时'


def job_cores(manifest: dict, profile_cores=None):
    """manifest(+集群当前配置核数)→ (cores|None, basis 说明)。

    优先 attempts 末条 cores(提交时点真值)→ 回退 profile_cores(当前配置,可能与
    当年不同)→ 都没有 → (None, 原因),调用方据此把该作业列入 unknown。
    """
    attempts = [a for a in (manifest.get('attempts') or []) if isinstance(a, dict)]
    if attempts:
        c = attempts[-1].get('cores')
        if c:
            return int(c), 'attempt'
    if profile_cores:
        return int(profile_cores), 'profile(按集群当前 nodes×ppn 配置,可能与提交时不同)'
    return None, '无核数记录(旧版提交)且集群配置缺 ppn,无法折算核时'


def usage_stats(entries, *, days: int = 30, profile_cores_by_name: dict | None = None,
                now: datetime | None = None) -> dict:
    """台账条目 → 近 N 天实耗核时汇总。

    entries:``[(job_dir, manifest|None), ...]``(ledger.load_all() 原样)。
    统计口径:**结束时间(或 now,对在跑作业)落在近 N 天窗口内**的作业计全程核时
    (跨窗口边界的长作业不按比例切分——口径简单可解释,note 注明)。
    返回 {'days','core_hours','jobs':[逐作业,按核时降序],'jobs_counted',
    'running_jobs','unknown':[{'dir','reason'}],'note'}。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=int(days))
    cores_map = dict(profile_cores_by_name or {})
    jobs: list = []
    unknown: list = []
    total = 0.0
    running_n = 0
    for d, m in entries or []:
        if not isinstance(m, dict):
            continue
        start, end, running, why = runtime_window(m, now=now)
        if start is None:
            if why:
                unknown.append({'dir': str(d), 'reason': why})
            continue
        if end < cutoff:
            continue                                     # 窗口外的历史作业
        cores, basis = job_cores(m, cores_map.get(m.get('cluster')))
        if cores is None:
            unknown.append({'dir': str(d), 'reason': basis})
            continue
        hours = (end - start).total_seconds() / 3600.0
        ch = hours * cores
        note_bits = [b for b in (why, None if basis == 'attempt' else basis) if b]
        jobs.append({'dir': str(d),
                     'name': os.path.basename(os.path.normpath(str(d))),
                     'cluster': m.get('cluster'), 'state': m.get('state'),
                     'hours': round(hours, 3), 'cores': int(cores),
                     'core_hours': round(ch, 2), 'running': bool(running),
                     'basis': ';'.join(note_bits) if note_bits else '提交时点核数×实测窗口'})
        total += ch
        running_n += 1 if running else 0
    jobs.sort(key=lambda j: -j['core_hours'])
    return {'days': int(days), 'core_hours': round(total, 2), 'jobs': jobs,
            'jobs_counted': len(jobs), 'running_jobs': running_n, 'unknown': unknown,
            'note': ('实耗核时 =(RUNNING→终态)时长 × 提交核数;未观测到 RUNNING 的按 '
                     'SUBMITTED 起算(含排队,偏大);在跑作业截至当前实时累计;'
                     '状态轮询间隔即时间误差量级;缺核数记录的作业不计入并单列。')}
