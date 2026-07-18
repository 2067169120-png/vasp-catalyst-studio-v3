"""实际核时统计测试(v3.3.0):运行窗口判定 + 核数来源优先级 + 汇总口径,全部手算对拍。"""
from datetime import datetime

from vcstudio.cluster import usage as ug

NOW = datetime(2026, 7, 18, 12, 0, 0)


def _mani(states, *, state=None, attempts=None, cluster='hpc'):
    return {'state': state or (states[-1][0] if states else 'CREATED'),
            'state_history': [{'state': s, 'at': t} for s, t in states],
            'attempts': attempts if attempts is not None else [],
            'cluster': cluster}


# ── runtime_window ───────────────────────────────────────────────────────────
def test_window_running_to_terminal():
    m = _mani([('CREATED', '2026-07-18T08:00:00'), ('SUBMITTED', '2026-07-18T08:10:00'),
               ('RUNNING', '2026-07-18T09:00:00'), ('DONE', '2026-07-18T11:00:00')])
    start, end, running, why = ug.runtime_window(m, now=NOW)
    assert (end - start).total_seconds() == 7200 and running is False and why is None


def test_window_submitted_fallback_notes_bias():
    # 轮询间隙内跑完,没观测到 RUNNING → 按 SUBMITTED 起算并注明口径偏大
    m = _mani([('SUBMITTED', '2026-07-18T08:00:00'), ('DONE', '2026-07-18T09:30:00')])
    start, end, running, why = ug.runtime_window(m, now=NOW)
    assert (end - start).total_seconds() == 5400 and 'SUBMITTED' in why


def test_window_still_running_counts_to_now():
    m = _mani([('SUBMITTED', '2026-07-18T08:00:00'), ('RUNNING', '2026-07-18T10:00:00')],
              state='RUNNING')
    start, end, running, why = ug.runtime_window(m, now=NOW)
    assert end == NOW and running is True and (end - start).total_seconds() == 7200


def test_window_never_submitted_is_silent():
    start, end, running, why = ug.runtime_window(_mani([('CREATED', '2026-07-18T08:00:00')]),
                                                 now=NOW)
    assert start is None and why is None                 # 未提交:不计也不报


def test_window_clock_skew_rejected():
    m = _mani([('RUNNING', '2026-07-18T10:00:00'), ('DONE', '2026-07-18T09:00:00')])
    start, _end, _r, why = ug.runtime_window(m, now=NOW)
    assert start is None and '倒挂' in why


# ── job_cores 优先级 ─────────────────────────────────────────────────────────
def test_cores_prefers_attempt_then_profile_then_honest_none():
    m = _mani([], attempts=[{'cores': 64}])
    assert ug.job_cores(m) == (64, 'attempt')
    c, basis = ug.job_cores(_mani([]), profile_cores=32)
    assert c == 32 and 'profile' in basis
    c2, basis2 = ug.job_cores(_mani([]))
    assert c2 is None and '无核数记录' in basis2


# ── usage_stats 汇总 ─────────────────────────────────────────────────────────
def test_usage_stats_totals_and_unknown():
    entries = [
        # 2h × 64 核 = 128 核时(attempt 记录)
        ('/j/a', _mani([('RUNNING', '2026-07-18T09:00:00'), ('DONE', '2026-07-18T11:00:00')],
                       attempts=[{'cores': 64}])),
        # 在跑:10:00 → now(12:00)= 2h × 32 核(profile 回退)= 64 核时
        ('/j/b', _mani([('RUNNING', '2026-07-18T10:00:00')], state='RUNNING')),
        # 窗口外历史作业(40 天前结束)→ 不计
        ('/j/old', _mani([('RUNNING', '2026-06-01T00:00:00'), ('DONE', '2026-06-02T00:00:00')],
                         attempts=[{'cores': 64}])),
        # 无核数可依 → unknown 列明,不进总数
        ('/j/nc', _mani([('RUNNING', '2026-07-18T09:00:00'), ('DONE', '2026-07-18T10:00:00')],
                        cluster='unknown-cluster')),
        # 未提交 → 静默跳过
        ('/j/new', _mani([('CREATED', '2026-07-18T08:00:00')])),
        ('/j/broken', None),
    ]
    out = ug.usage_stats(entries, days=30, profile_cores_by_name={'hpc': 32}, now=NOW)
    assert out['jobs_counted'] == 2 and out['running_jobs'] == 1
    assert abs(out['core_hours'] - 192.0) < 1e-9         # 128 + 64,手算对拍
    assert out['jobs'][0]['core_hours'] == 128.0         # 按核时降序
    assert len(out['unknown']) == 1 and '无核数记录' in out['unknown'][0]['reason']
    assert '不计入' in out['note'] or '缺核数' in out['note']


def test_usage_stats_empty_entries():
    out = ug.usage_stats([], days=30, now=NOW)
    assert out['core_hours'] == 0.0 and out['jobs'] == [] and out['unknown'] == []
