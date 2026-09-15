"""三门(蓝图 E3):deny-by-default,只卡 release 不卡创作。

- **submit_gate**(SSH 提交前):机时预算硬闸 + 单点先行闸 + advisor 无 P0 阻断。
- **accept_gate**(进 ΔE/图/报告前):方法指纹一致 + required_checks 全过 + 能量存在且有限。
- **report_gate**(出报告/可复现包前):进报告任务全 accepted + 每张图带 provenance。

统一裁决类型 ``GateDecision``:保留 Mapping 只读兼容，同时绑定 campaign/task/revision、
输入指纹、checks digest、named ledger decision digest、actor/time。

waive 机制:任何 blocked 可豁免,但 decision_id 必须来自当前 named campaign ledger 的
``gate-waiver`` 人工决策，并精确匹配 scope/task/gate/revision/fingerprints 且未撤销。

不带权威绑定时仍可纯函数预览；带完整绑定时会把最终 GateDecision 写入 ledger。
门禁不 import advisor、不碰数值链路。
上游建 slab、铺矩阵、试结构一律不卡——门只在下游 release 点生效。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

GATE_STATUSES = ('pass', 'blocked', 'waived')
GATE_DECISION_SCHEMA = 'vcstudio.gate-decision/v1'


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _json_copy(value):
    return json.loads(_canonical_json(value))


def checks_digest(checks) -> str:
    return hashlib.sha256(_canonical_json(list(checks or [])).encode('utf-8')).hexdigest()


def task_input_fingerprints(task: dict, campaign_meta: dict) -> dict:
    """构造 GateDecision 的完整输入绑定，不把一个模糊 hash 冒充全部输入。"""
    return _json_copy({
        'task_method_fingerprint': task.get('fingerprint_hash'),
        'campaign_method_fingerprint': campaign_meta.get('fingerprint_hash'),
        'task_inputs': dict(task.get('input_fingerprints') or {}),
    })


@dataclass(frozen=True)
class GateDecision(Mapping):
    """不可变、可账本复核的 gate 裁决。

    为兼容既有只读调用实现 ``Mapping``，仍可使用 ``decision['status']``；但 accepted
    提升只接受这个类型且要求 ``authoritative=True``，自由 dict 不再具有状态权威。
    可变容器以 canonical JSON 内存储，每次读取返回新对象，调用方无法原地篡改。
    """

    gate: str
    status: str
    campaign_id: str | None
    task_id: str | None
    task_revision: int | None
    checks_digest: str
    ledger_decision_id: str | None
    ledger_decision_digest: str | None
    actor: str
    decided_at: str
    _checks_json: str = field(repr=False)
    _issues_json: str = field(repr=False)
    _fingerprints_json: str = field(repr=False)
    _waiver_json: str = field(repr=False, default='null')
    schema: str = GATE_DECISION_SCHEMA

    @property
    def checks(self) -> list:
        return json.loads(self._checks_json)

    @property
    def blocking_issues(self) -> list:
        return json.loads(self._issues_json)

    @property
    def input_fingerprints(self) -> dict:
        return json.loads(self._fingerprints_json)

    @property
    def waiver(self) -> dict | None:
        return json.loads(self._waiver_json)

    @property
    def authoritative(self) -> bool:
        return bool(self.campaign_id and self.task_id
                    and self.task_revision is not None
                    and self.ledger_decision_id and self.ledger_decision_digest)

    def to_dict(self) -> dict:
        out = {
            'schema': self.schema,
            'gate': self.gate,
            'status': self.status,
            'checks': self.checks,
            'blocking_issues': self.blocking_issues,
            'campaign_id': self.campaign_id,
            'task_id': self.task_id,
            'task_revision': self.task_revision,
            'input_fingerprints': self.input_fingerprints,
            'checks_digest': self.checks_digest,
            'ledger_decision_id': self.ledger_decision_id,
            'ledger_decision_digest': self.ledger_decision_digest,
            'actor': self.actor,
            'decided_at': self.decided_at,
            'authoritative': self.authoritative,
        }
        if self.waiver is not None:
            out['waiver'] = self.waiver
        return out

    def __getitem__(self, key):
        return self.to_dict()[key]

    def __iter__(self) -> Iterator:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


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


def _finalize(gate: str, checks: list, blocking_issues: list, context: dict) -> GateDecision:
    """生成 GateDecision；waiver 只认 named campaign ledger 中精确绑定的人工决策。"""
    ctx = context or {}
    status = 'pass' if not blocking_issues else 'blocked'
    checks = _json_copy(checks)
    issues = _json_copy(list(blocking_issues))
    digest = checks_digest(checks)
    fingerprints = _json_copy(ctx.get('input_fingerprints') or {
        'task_method_fingerprint': ctx.get('task_fingerprint_hash'),
        'campaign_method_fingerprint': ctx.get('campaign_fingerprint_hash'),
        'task_inputs': {},
    })
    cdir = ctx.get('campaign_dir')
    campaign_id = ctx.get('campaign_id')
    task_id = ctx.get('task_id')
    task_revision = ctx.get('task_revision')
    supplied_binding = any(v is not None for v in (cdir, campaign_id, task_id, task_revision))
    bound = all(v is not None for v in (cdir, campaign_id, task_id, task_revision))
    if supplied_binding and not bound:
        raise ValueError('权威 GateDecision 必须同时绑定 campaign_dir/campaign_id/'
                         'task_id/task_revision')
    if task_revision is not None and (isinstance(task_revision, bool)
                                      or not isinstance(task_revision, int)
                                      or task_revision < 0):
        raise ValueError('GateDecision task_revision 必须是非负整数')

    waiver_payload = None
    actor = str(ctx.get('actor') or 'gate:auto')
    waiver = ctx.get('waiver')
    if status == 'blocked' and waiver is not None:
        decision_id = (waiver or {}).get('decision_id') if isinstance(waiver, dict) else None
        if not decision_id:
            raise ValueError('豁免(waive)必须携带 decision_id,指向账本里的人工决策记录')
        if not bound:
            raise ValueError('豁免必须绑定 named campaign/task/revision/input fingerprints')
        from vcstudio.campaign import ledger
        waiver_record, waiver_digest = ledger.validate_waiver_decision(
            cdir, decision_id, campaign_id=str(campaign_id), task_id=str(task_id),
            gate=gate, task_revision=task_revision, input_fingerprints=fingerprints)
        status = 'waived'
        actor = str((waiver_record.get('context') or {}).get('actor') or 'user')
        waiver_payload = {'decision_id': str(decision_id),
                          'decision_digest': waiver_digest}

    ledger_id = None
    ledger_digest = None
    decided_at = _now()
    if bound:
        from vcstudio.campaign import ledger
        record, ledger_digest = ledger.record_gate_decision(
            cdir, gate=gate, status=status, campaign_id=str(campaign_id),
            task_id=str(task_id), task_revision=task_revision,
            input_fingerprints=fingerprints, checks_digest=digest,
            actor=actor, waiver=waiver_payload)
        ledger_id = str(record['id'])
        decided_at = str(record['ts'])

    return GateDecision(
        gate=gate, status=status,
        campaign_id=(str(campaign_id) if campaign_id is not None else None),
        task_id=(str(task_id) if task_id is not None else None),
        task_revision=task_revision, checks_digest=digest,
        ledger_decision_id=ledger_id, ledger_decision_digest=ledger_digest,
        actor=actor, decided_at=decided_at,
        _checks_json=_canonical_json(checks),
        _issues_json=_canonical_json(issues),
        _fingerprints_json=_canonical_json(fingerprints),
        _waiver_json=_canonical_json(waiver_payload))


def verify_gate_decision(decision: GateDecision, campaign_dir, task: dict) -> bool:
    """重读 named ledger + 当前 task，验证 GateDecision 仍精确绑定且未撤销。"""
    if not isinstance(decision, GateDecision):
        raise ValueError('状态推进只接受 gates.GateDecision，不接受自由 dict')
    if not decision.authoritative:
        raise ValueError('GateDecision 缺 named ledger 权威绑定，不能推进状态')
    if decision.gate != 'accept_gate' or decision.status not in ('pass', 'waived'):
        raise ValueError('accepted 只接受通过或已豁免的 accept_gate GateDecision')

    from vcstudio.campaign import ledger, schema
    campaign = schema.load_campaign(campaign_dir)
    if not campaign:
        raise ValueError('named campaign 缺失或损坏，GateDecision 无法复核')
    meta = campaign.get('meta') or {}
    if str(meta.get('id')) != decision.campaign_id:
        raise ValueError('GateDecision campaign_id 与 named campaign 不一致')
    disk_task = schema.load_task(campaign_dir, decision.task_id)
    if disk_task is None or str(task.get('id')) != decision.task_id:
        raise ValueError('GateDecision task_id 与当前任务不一致或任务已损坏')
    if disk_task.get('rung') != 'validated' or task.get('rung') != 'validated':
        raise ValueError('GateDecision 只能消费磁盘上已持久化的 validated task revision')
    disk_revision = schema.task_revision(disk_task)
    if disk_revision != decision.task_revision or schema.task_revision(task) != disk_revision:
        raise ValueError(
            f'GateDecision task_revision 已陈旧:decision={decision.task_revision}, '
            f'disk={disk_revision}, memory={schema.task_revision(task)}')
    if disk_task != task:
        raise ValueError('内存任务不是 GateDecision 所绑定磁盘 revision 的精确快照')
    expected_fps = task_input_fingerprints(disk_task, meta)
    if decision.input_fingerprints != expected_fps:
        raise ValueError('GateDecision input fingerprints 与当前任务/campaign 不一致')
    if decision.checks_digest != checks_digest(decision.checks):
        raise ValueError('GateDecision checks digest 与裁决内容不一致')

    record = ledger.find_decision(campaign_dir, decision.ledger_decision_id)
    if record is None or record.get('kind') != ledger.GATE_DECISION_KIND:
        raise ValueError('GateDecision 对应的 named ledger 决策不存在或 kind 非法')
    if ledger.decision_digest(record) != decision.ledger_decision_digest:
        raise ValueError('GateDecision ledger decision digest 不匹配')
    ctx = record.get('context') or {}
    if type(ctx.get('task_revision')) is not int:  # bool 也不得冒充 revision
        raise ValueError('GateDecision ledger task_revision 类型非法')
    expected = {
        'authority_schema': GATE_DECISION_SCHEMA,
        'scope': 'task',
        'campaign_id': decision.campaign_id,
        'task_id': decision.task_id,
        'gate': decision.gate,
        'status': decision.status,
        'task_revision': decision.task_revision,
        'input_fingerprints': expected_fps,
        'input_fingerprints_digest': ledger.value_digest(expected_fps),
        'checks_digest': decision.checks_digest,
        'actor': decision.actor,
        'waiver': dict(decision.waiver or {}),
    }
    for key, value in expected.items():
        if ctx.get(key) != value:
            raise ValueError(f'GateDecision ledger binding 不匹配:{key}')
    if str(record.get('ts')) != decision.decided_at:
        raise ValueError('GateDecision decided_at 与 ledger 时间不一致')
    if ledger.is_decision_revoked(
            campaign_dir, decision.ledger_decision_id, campaign_id=decision.campaign_id):
        raise ValueError('GateDecision 已在 named campaign ledger 中撤销')
    if decision.status == 'waived':
        waiver = decision.waiver or {}
        _, waiver_digest = ledger.validate_waiver_decision(
            campaign_dir, waiver.get('decision_id'), campaign_id=decision.campaign_id,
            task_id=decision.task_id, gate=decision.gate,
            task_revision=decision.task_revision, input_fingerprints=expected_fps)
        if waiver_digest != waiver.get('decision_digest'):
            raise ValueError('GateDecision 引用的 waiver digest 不匹配')
    return True


# ── submit_gate ───────────────────────────────────────────────────────────────
def submit_gate(context: dict) -> GateDecision:
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

    return _finalize('submit_gate', checks, issues, ctx)


# ── accept_gate ───────────────────────────────────────────────────────────────
def accept_gate(context: dict) -> GateDecision:
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

    return _finalize('accept_gate', checks, issues, ctx)


# ── report_gate ───────────────────────────────────────────────────────────────
def report_gate(context: dict) -> GateDecision:
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

    return _finalize('report_gate', checks, issues, ctx)
