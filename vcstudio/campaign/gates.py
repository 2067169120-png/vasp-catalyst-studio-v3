"""三门(蓝图 E3):deny-by-default,只卡 release 不卡创作。

- **submit_gate**(SSH 提交前):机时预算硬闸 + 单点先行闸 + advisor 无 P0 阻断。
- **accept_gate**(进 ΔE/图/报告前):方法指纹一致 + required_checks 全过 + 能量存在且有限。
- **report_gate**(出报告/可复现包前):进报告任务全 accepted + 每张图带 provenance。

统一裁决形状:
    {'gate', 'status'('pass'|'blocked'|'waived'), 'checks':[{'name','ok','detail'}],
     'blocking_issues':[中文]}

waive 机制:任何 blocked 可豁免,但必须携带 `waiver={'decision_id': ...}` 指向账本里的
人工决策记录;豁免后 status='waived',blocking_issues 仍保留(可审计),并附 waiver。
无 decision_id 的豁免尝试直接抛 ValueError(绝不无凭豁免)。

纯函数:每门只吃一个上下文 dict,不 import advisor / 不读文件 / 不碰数值链路。
上游建 slab、铺矩阵、试结构一律不卡——门只在下游 release 点生效。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math

GATE_STATUSES = ('pass', 'blocked', 'waived')


def _normalize_checks(items) -> list:
    """required_checks 归一成 [{'name','ok'}]:接受 dict(name→bool)/[(name,ok)]/[{...}]。"""
    if isinstance(items, dict):
        return [{'name': str(k), 'ok': bool(v)} for k, v in items.items()]
    out = []
    for c in (items or []):
        if isinstance(c, dict):
            out.append({'name': str(c.get('name', '?')), 'ok': bool(c.get('ok'))})
        elif isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append({'name': str(c[0]), 'ok': bool(c[1])})
        else:
            out.append({'name': str(c), 'ok': False})
    return out


def _priority_of(advisory) -> str:
    """advisor 结果的优先级:兼容 (priority,name,msg) 元组与 {'priority':...} dict。"""
    if isinstance(advisory, (list, tuple)) and advisory:
        return str(advisory[0])
    if isinstance(advisory, dict):
        return str(advisory.get('priority') or advisory.get('level') or '')
    return ''


def _message_of(advisory) -> str:
    if isinstance(advisory, (list, tuple)):
        if len(advisory) > 2:
            return str(advisory[2])
        return str(advisory[1]) if len(advisory) > 1 else str(advisory)
    if isinstance(advisory, dict):
        return str(advisory.get('message') or advisory.get('detail')
                   or advisory.get('name') or advisory)
    return str(advisory)


def _finalize(gate: str, checks: list, blocking_issues: list, waiver) -> dict:
    """按 blocking_issues 定 status;若 blocked 且带有效 waiver → waived。"""
    status = 'pass' if not blocking_issues else 'blocked'
    verdict = {'gate': gate, 'status': status, 'checks': checks,
               'blocking_issues': list(blocking_issues)}
    if status == 'blocked' and waiver is not None:
        decision_id = (waiver or {}).get('decision_id')
        if not decision_id:
            raise ValueError('豁免(waive)必须携带 decision_id,指向账本里的人工决策记录')
        verdict['status'] = 'waived'
        verdict['waiver'] = {'decision_id': decision_id}
    return verdict


# ── submit_gate ───────────────────────────────────────────────────────────────
def submit_gate(context: dict) -> dict:
    """提交前门。上下文键:estimated_core_hours / remaining_budget(None=不限)/
    require_pilot / is_fanout / pilot_accepted_count / advisor_results / waiver。
    """
    ctx = context or {}
    checks: list = []
    issues: list = []

    # 1) 机时预算硬闸
    remaining = ctx.get('remaining_budget')
    est = float(ctx.get('estimated_core_hours') or 0.0)
    if remaining is None:
        checks.append({'name': 'core_hours_budget', 'ok': True,
                       'detail': '未设机时预算(不限)'})
    else:
        ok = est <= float(remaining)
        checks.append({'name': 'core_hours_budget', 'ok': ok,
                       'detail': f'预估 {est:g} 核时 vs 剩余预算 {float(remaining):g} 核时'})
        if not ok:
            issues.append(f'机时预算不足:预估 {est:g} 核时 超过剩余 {float(remaining):g} 核时')

    # 2) 单点先行闸:require_pilot 且这是矩阵 fan-out 时,须已有 ≥1 个 accepted 的 pilot
    if bool(ctx.get('require_pilot')) and bool(ctx.get('is_fanout')):
        n = int(ctx.get('pilot_accepted_count') or 0)
        ok = n >= 1
        checks.append({'name': 'pilot_first', 'ok': ok,
                       'detail': f'已验收 pilot 数={n}(fan-out 前需 ≥1)'})
        if not ok:
            issues.append('单点先行闸未过:矩阵 fan-out 前必须存在至少 1 个 '
                          'rung=accepted 的 pilot 任务对齐文献值')
    else:
        checks.append({'name': 'pilot_first', 'ok': True,
                       'detail': '非 fan-out 或未要求单点先行,跳过'})

    # 3) advisor 无 P0 级阻断(结果由外部传入,门不自己 import advisor)
    advisories = ctx.get('advisor_results') or []
    p0 = [a for a in advisories if _priority_of(a) == 'P0']
    checks.append({'name': 'advisor_no_p0', 'ok': not p0,
                   'detail': '无 P0 级方法学阻断' if not p0 else f'{len(p0)} 个 P0 级阻断项'})
    for a in p0:
        issues.append('方法学 P0 阻断:' + _message_of(a))

    return _finalize('submit_gate', checks, issues, ctx.get('waiver'))


# ── accept_gate ───────────────────────────────────────────────────────────────
def accept_gate(context: dict) -> dict:
    """验收门。上下文键:task_fingerprint_hash / campaign_fingerprint_hash /
    required_checks / energy_eV / waiver。
    """
    ctx = context or {}
    checks: list = []
    issues: list = []

    # 1) 方法指纹一致(本任务 hash == campaign 基准 hash)
    th = ctx.get('task_fingerprint_hash')
    ch = ctx.get('campaign_fingerprint_hash')
    ok = bool(th) and bool(ch) and th == ch
    checks.append({'name': 'fingerprint_consistent', 'ok': ok,
                   'detail': f'任务指纹 {th} vs campaign 指纹 {ch}'})
    if not ok:
        if not th or not ch:
            issues.append('方法指纹缺失:任务或 campaign 指纹为空,无法核对一致性')
        else:
            issues.append(f'方法指纹不一致:任务 {th} ≠ campaign {ch},'
                          f'该能量与本 campaign 基准不可比,不得进入 ΔE 比较')

    # 2) required_checks 全通过
    rc = _normalize_checks(ctx.get('required_checks'))
    failed = [c['name'] for c in rc if not c['ok']]
    if rc:
        detail = '全部通过' if not failed else f'未过: {", ".join(failed)}'
    else:
        detail = '无 required_checks'
    checks.append({'name': 'required_checks', 'ok': not failed, 'detail': detail})
    if failed:
        issues.append('required_checks 未全通过:' + '、'.join(failed))

    # 3) 能量存在且有限
    e = ctx.get('energy_eV')
    ok_e = isinstance(e, (int, float)) and not isinstance(e, bool) and math.isfinite(e)
    checks.append({'name': 'energy_present_finite', 'ok': ok_e,
                   'detail': f'能量 {e} eV' if ok_e else f'能量缺失或非有限: {e!r}'})
    if not ok_e:
        issues.append('能量缺失或非有限,不能进入 ΔE/图/报告')

    return _finalize('accept_gate', checks, issues, ctx.get('waiver'))


# ── report_gate ───────────────────────────────────────────────────────────────
def report_gate(context: dict) -> dict:
    """报告门。上下文键:report_tasks([{'id','rung'}]) / figures([{'id','provenance':[...]}]) /
    waiver。
    """
    ctx = context or {}
    checks: list = []
    issues: list = []

    # 1) 所有进报告任务 rung=accepted
    report_tasks = ctx.get('report_tasks') or []
    not_accepted = [t.get('id', '?') for t in report_tasks if t.get('rung') != 'accepted']
    if not report_tasks:
        detail = '无进报告任务'
    else:
        detail = '全部 accepted' if not not_accepted \
            else f'未验收: {", ".join(map(str, not_accepted))}'
    checks.append({'name': 'all_accepted', 'ok': bool(report_tasks) and not not_accepted,
                   'detail': detail})
    if not report_tasks:
        issues.append('报告任务清单为空,无可发布内容')
    elif not_accepted:
        issues.append('存在未 accepted 的报告任务:' + '、'.join(map(str, not_accepted)))

    # 2) 每张图工件带 provenance(数据源任务 id 列表非空)
    figures = ctx.get('figures') or []
    no_prov = [(f.get('id') or f.get('name') or '?') for f in figures if not f.get('provenance')]
    checks.append({'name': 'figure_provenance', 'ok': not no_prov,
                   'detail': ('每图均有 provenance' if not no_prov
                              else f'缺 provenance: {", ".join(map(str, no_prov))}')
                   if figures else '无图工件'})
    if no_prov:
        issues.append('图工件缺少 provenance(数据源任务 id):' + '、'.join(map(str, no_prov)))

    return _finalize('report_gate', checks, issues, ctx.get('waiver'))
