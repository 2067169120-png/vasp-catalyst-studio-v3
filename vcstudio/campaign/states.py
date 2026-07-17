"""三态 rung 状态机(蓝图 E2):completed ≠ validated ≠ accepted,词汇绝不塌缩。

三档权威边界(由弱到强):
- **completed**:执行层自述「作业退出了」——最弱,由执行/监控代码写。
- **validated**:确定性 checker 判定通过(解析 + 收敛判据),机器权威。
- **accepted**:门禁通过 +(可选)人工签字,科学权威。只有 accepted 能进
  ΔE / 图 / 报告。

另有三个执行态:pending(待办)/ running(在跑)/ failed(失败)。

**红线**:AI / 外部调用无权直接写 accepted。本模块只暴露两条提升边——
`promote_validated(task, checker_results)`(须 checker 全通过)与
`promote_accepted(task, gate_verdict, signed_by)`(须携带 accept_gate 的通过/豁免裁决)。
发现问题可 `downgrade` 回退,并记入账本(append-only,可审计)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import time

# 执行态与三态 rung
EXEC_STATES = ('pending', 'running', 'failed')
RUNG_STATES = ('completed', 'validated', 'accepted')
ALL_RUNGS = EXEC_STATES + RUNG_STATES

# 权威秩(用于降级方向判定):失败/待办同秩 0,依次升高
_RANK = {'pending': 0, 'failed': 0, 'running': 1,
         'completed': 2, 'validated': 3, 'accepted': 4}


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def rung(task: dict) -> str:
    return task.get('rung', 'pending')


def rung_rank(rung_value: str) -> int:
    return _RANK.get(rung_value, -1)


def is_accepted(task: dict) -> bool:
    return rung(task) == 'accepted'


def _normalize_checks(checker_results) -> list:
    """把 checker 结果归一成 [{'name','ok'}]:接受 dict(name→bool)/[(name,ok)]/[{...}]。"""
    if isinstance(checker_results, dict):
        return [{'name': str(k), 'ok': bool(v)} for k, v in checker_results.items()]
    out = []
    for c in (checker_results or []):
        if isinstance(c, dict):
            out.append({'name': str(c.get('name', '?')), 'ok': bool(c.get('ok'))})
        elif isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append({'name': str(c[0]), 'ok': bool(c[1])})
        else:
            out.append({'name': str(c), 'ok': False})
    return out


def _record(task: dict, new_rung: str, *, by: str, note: str) -> dict:
    task['rung'] = new_rung
    task.setdefault('rung_history', []).append(
        {'rung': new_rung, 'at': _now(), 'by': by, 'note': note})
    return task


def _emit_event(campaign_dir, kind: str, task: dict, detail: dict) -> None:
    """可选地把一次 rung 变更写进事件账本(campaign_dir 为空则跳过)。"""
    if not campaign_dir:
        return
    from vcstudio.campaign import ledger  # 延迟导入,避免包内加载顺序耦合
    ledger.record_event(campaign_dir, kind, task.get('id'), detail)


# ── 执行态(由执行/监控层写) ──────────────────────────────────────────────────
def set_exec_state(task: dict, state: str, *, by: str = 'executor', note: str = '',
                   campaign_dir=None) -> dict:
    if state not in EXEC_STATES:
        raise ValueError(f'非法执行态: {state!r};合法值: {", ".join(EXEC_STATES)}')
    _record(task, state, by=by, note=note)
    _emit_event(campaign_dir, 'exec_state', task, {'rung': state, 'note': note})
    return task


def mark_running(task: dict, *, by: str = 'executor', note: str = '', campaign_dir=None) -> dict:
    return set_exec_state(task, 'running', by=by, note=note, campaign_dir=campaign_dir)


def mark_failed(task: dict, *, reason: str = '', by: str = 'executor', campaign_dir=None) -> dict:
    return set_exec_state(task, 'failed', by=by, note=reason, campaign_dir=campaign_dir)


def mark_completed(task: dict, *, by: str = 'executor', note: str = '', campaign_dir=None) -> dict:
    """执行层:作业退出 → completed。只能从执行态或 completed 进入(幂等)。"""
    cur = rung(task)
    if cur not in ('pending', 'running', 'failed', 'completed'):
        raise ValueError(f'不能从 {cur} 直接标记 completed(须先 downgrade);'
                         f'completed 只由执行层从执行态写入')
    _record(task, 'completed', by=by, note=note)
    _emit_event(campaign_dir, 'completed', task, {'note': note})
    return task


# ── validated:只由确定性 checker 提升 ────────────────────────────────────────
def promote_validated(task: dict, checker_results, *, by: str = 'checker',
                      campaign_dir=None) -> dict:
    """completed → validated;须携带确定性 checker 结果且全部通过,否则拒绝(绝不静默)。"""
    cur = rung(task)
    if cur != 'completed':
        raise ValueError(f'只能从 completed 提升到 validated,当前 rung={cur}')
    checks = _normalize_checks(checker_results)
    if not checks:
        raise ValueError('validated 至少需要一项确定性 checker 结果作为证据')
    failed = [c['name'] for c in checks if not c['ok']]
    if failed:
        raise ValueError('确定性检查未全通过,不能提升到 validated:' + '、'.join(failed))
    _record(task, 'validated', by=by, note=f'checks={[c["name"] for c in checks]}')
    _emit_event(campaign_dir, 'validated', task, {'checks': [c['name'] for c in checks]})
    return task


# ── accepted:只由 accept_gate 通过/豁免 +(可选)人工签字提升 ─────────────────
def promote_accepted(task: dict, gate_verdict: dict, *, signed_by: str | None = None,
                     campaign_dir=None) -> dict:
    """validated → accepted。gate_verdict 须是 accept_gate 的 pass/waived 裁决。

    这条边是「AI/外部无权直接写 accepted」的执行点:必须出示一份真实的 accept_gate
    通过或已豁免裁决;signed_by 记录人工签字(半自动模式下的强制人工确认闸)。
    """
    cur = rung(task)
    if cur != 'validated':
        raise ValueError(f'只能从 validated 提升到 accepted,当前 rung={cur}')
    if not isinstance(gate_verdict, dict):
        raise ValueError('accepted 需要 accept_gate 裁决 dict')
    if gate_verdict.get('gate') != 'accept_gate':
        raise ValueError('accepted 只能由 accept_gate 裁决提升,'
                         f'收到的 gate={gate_verdict.get("gate")!r}')
    if gate_verdict.get('status') not in ('pass', 'waived'):
        raise ValueError('accept_gate 未通过且未豁免,不能提升到 accepted;'
                         f'当前 status={gate_verdict.get("status")!r}')
    note = f'gate={gate_verdict.get("status")}'
    if signed_by:
        note += f'; signed_by={signed_by}'
    _record(task, 'accepted', by=(signed_by or 'gate'), note=note)
    _emit_event(campaign_dir, 'accepted', task,
                {'gate_status': gate_verdict.get('status'), 'signed_by': signed_by})
    return task


# ── 降级(发现问题回退 rung,必记账本) ────────────────────────────────────────
def downgrade(task: dict, to_rung: str, *, reason: str, by: str = 'auto',
              campaign_dir=None) -> dict:
    """发现问题回退到更低 rung。必须给 reason;记入 rung_history,并写事件账本。"""
    if to_rung not in ALL_RUNGS:
        raise ValueError(f'降级目标 rung 非法: {to_rung!r};合法值: {", ".join(ALL_RUNGS)}')
    if not reason:
        raise ValueError('降级必须给出原因(reason),以供审计')
    cur = rung(task)
    if _RANK.get(to_rung, -1) >= _RANK.get(cur, -1):
        raise ValueError(f'降级目标 rung({to_rung})必须低于当前 rung({cur})')
    _record(task, to_rung, by=by, note=f'降级:{reason}(自 {cur})')
    _emit_event(campaign_dir, 'rung_downgrade', task,
                {'from': cur, 'to': to_rung, 'reason': reason, 'by': by})
    return task
