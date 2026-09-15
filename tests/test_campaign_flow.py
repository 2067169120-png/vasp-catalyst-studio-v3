"""campaign 集成流:建 campaign → 派生 → 三态全链 → 三门 → 账本 → 预算,一条龙。

覆盖任务书要求的端到端正常流,以及门禁与账本、预算、单点先行的联动。
"""
import pytest

from vcstudio.campaign import (
    accept_gate, budget, derive, fingerprint, gates, ledger, lock,
    report_gate, schema, states, submit_gate,
)


def _pbe_fingerprint():
    incar = 'GGA=PE\nENCUT=500\nISPIN=2\nIVDW=12\nEDIFF=1E-5\nEDIFFG=-0.02\n'
    kpts = 'auto\n0\nGamma\n3 3 1\n'
    potcar = [{'element': 'C', 'titel': 'PAW_PBE C 08Apr2002'}]
    return fingerprint.extract_from_inputs(incar, kpts, potcar,
                                           reference_convention='gas-phase O2')


def _persist(cdir, task, transition):
    revision = task['revision']
    transition(task)
    schema.persist_task(cdir, task, expected_revision=revision)
    return task


def _bound_accept(camp, task, checks, energy, *, actor='gate:auto'):
    return accept_gate({
        'campaign_dir': camp['dir'], 'campaign_id': camp['meta']['id'],
        'task_id': task['id'], 'task_revision': task['revision'],
        'input_fingerprints': gates.task_input_fingerprints(task, camp['meta']),
        'task_fingerprint_hash': task.get('fingerprint_hash'),
        'campaign_fingerprint_hash': camp['meta'].get('fingerprint_hash'),
        'required_checks': checks, 'energy_eV': energy, 'actor': actor,
    })


def test_end_to_end_pilot_then_fanout(tmp_path):
    fp = _pbe_fingerprint()
    camp_fp_hash = fingerprint.fingerprint_hash(fp)

    # 1) 建 campaign:一个 pilot + 两个 fan-out 矩阵任务(依赖 pilot)
    pilot = schema.new_task('pilot-relax', 'relax', is_pilot=True,
                            fingerprint_hash=camp_fp_hash)
    m1 = schema.new_task('mat-1', 'relax', depends_on=['pilot-relax'],
                         fingerprint_hash=camp_fp_hash)
    m2 = schema.new_task('mat-2', 'relax', depends_on=['pilot-relax'],
                         fingerprint_hash=camp_fp_hash)
    camp = schema.init_campaign(str(tmp_path), 'sac-study', tasks=[pilot, m1, m2],
                                budget_core_hours=10000.0, require_pilot=True,
                                fingerprint_hash=camp_fp_hash, fingerprint=fp)
    cdir = camp['dir']
    assert derive.campaign_stage(camp) == '提交'
    assert derive.derive_ready(camp)['ready'] == ['pilot-relax']

    # 2) 单点先行:fan-out 在无 accepted pilot 时被 submit_gate 挡下
    est = budget.estimate_job(48, 9, 'relax', 128)
    fan_ctx = {'estimated_core_hours': est, 'remaining_budget': budget.remaining(camp),
               'require_pilot': True, 'is_fanout': True, 'pilot_accepted_count': 0}
    assert submit_gate(fan_ctx)['status'] == 'blocked'

    # 3) 跑 pilot:提交 → completed → validated → accepted 全链
    assert lock.acquire(cdir, 'autopilot') is True
    pilot_ctx = {'estimated_core_hours': est, 'remaining_budget': budget.remaining(camp),
                 'require_pilot': True, 'is_fanout': False, 'advisor_results': []}
    assert submit_gate(pilot_ctx)['status'] == 'pass'
    _persist(cdir, pilot, lambda t: states.mark_running(t))
    _persist(cdir, pilot, lambda t: states.mark_completed(t))
    budget.record_actual(cdir, 'pilot-relax', 260.0)
    _persist(cdir, pilot, lambda t: states.promote_validated(
        t, [{'name': 'converged', 'ok': True},
            {'name': 'force<EDIFFG', 'ok': True}]))
    av = _bound_accept(
        camp, pilot, [{'name': 'lit-value-aligned', 'ok': True}],
        -123.45, actor='yuhao')
    assert av['status'] == 'pass'
    states.promote_accepted(pilot, av, signed_by='yuhao', campaign_dir=cdir)
    assert states.is_accepted(pilot)
    assert lock.release(cdir, owner='autopilot') is True

    # 4) 现在 fan-out 放行,矩阵任务变就绪
    fan_ctx['pilot_accepted_count'] = 1
    fan_ctx['remaining_budget'] = budget.remaining(camp)
    assert submit_gate(fan_ctx)['status'] == 'pass'
    assert set(derive.derive_ready(camp)['ready']) == {'mat-1', 'mat-2'}

    # 5) 推完矩阵两任务至 accepted
    for t in (m1, m2):
        _persist(cdir, t, lambda task: states.mark_completed(task))
        _persist(cdir, t, lambda task: states.promote_validated(
            task, [{'name': 'converged', 'ok': True}]))
        v = _bound_accept(camp, t, [{'name': 'converged', 'ok': True}], -120.0)
        states.promote_accepted(t, v, campaign_dir=cdir)

    # 6) report_gate:全 accepted + 图带 provenance → 通过
    rep = report_gate({
        'report_tasks': [{'id': t['id'], 'rung': t['rung']} for t in camp['tasks']],
        'figures': [{'id': 'delta-e-bar', 'provenance': ['mat-1', 'mat-2']}]})
    assert rep['status'] == 'pass'
    assert derive.campaign_stage(camp) == '报告'

    # 账本留痕齐全,预算已扣减
    assert budget.remaining(camp) == 10000.0 - 260.0
    assert len(ledger.read_events(cdir, kind='accepted')) == 3


def test_fingerprint_mismatch_blocks_accept_in_flow(tmp_path):
    """一个矩阵任务用了不同 ENCUT(指纹变了)→ accept_gate 拦截,不得进 ΔE。"""
    fp = _pbe_fingerprint()
    good = fingerprint.fingerprint_hash(fp)
    drifted = dict(fp)
    drifted['encut'] = 450.0
    bad = fingerprint.fingerprint_hash(drifted)
    assert good != bad

    t = schema.new_task('mat-x', 'relax', fingerprint_hash=bad)
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[t],
                                fingerprint_hash=good)
    states.mark_completed(t)
    states.promote_validated(t, [{'name': 'x', 'ok': True}])
    v = accept_gate({'task_fingerprint_hash': t['fingerprint_hash'],
                     'campaign_fingerprint_hash': camp['meta']['fingerprint_hash'],
                     'required_checks': [{'name': 'x', 'ok': True}], 'energy_eV': -1.0})
    assert v['status'] == 'blocked'
    # 门未过 → 不能提升到 accepted
    with pytest.raises(ValueError):
        states.promote_accepted(t, v, campaign_dir=camp['dir'])


def test_budget_overrun_blocks_submit_in_flow(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'aimd')],
                                budget_core_hours=100.0)
    budget.record_actual(camp['dir'], 'a', 90.0)   # 已用 90,剩 10
    est = budget.estimate_job(60, 8, 'aimd', 256)   # 远超 10
    assert est > 10
    v = submit_gate({'estimated_core_hours': est, 'remaining_budget': budget.remaining(camp)})
    assert v['status'] == 'blocked'
    assert any('机时预算不足' in s for s in v['blocking_issues'])


def test_waive_flow_with_real_decision_id(tmp_path):
    """blocked 门凭账本里真实的人工决策 id 豁免,verdict 记 waived 且可回溯决策。"""
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'relax')],
                                budget_core_hours=100.0)
    cdir = camp['dir']
    task = camp['tasks'][0]
    fps = gates.task_input_fingerprints(task, camp['meta'])
    # 人工决策精确绑定 named campaign/task/revision/gate/fingerprints
    did = ledger.record_waiver_decision(
        cdir, campaign_id='demo', task_id='a', gate='submit_gate',
        task_revision=0, input_fingerprints=fps,
        detail='导师批准本次超预算 20 核时', actor='导师')
    v = submit_gate({
        'campaign_dir': cdir, 'campaign_id': 'demo', 'task_id': 'a',
        'task_revision': 0, 'input_fingerprints': fps,
        'estimated_core_hours': 120, 'remaining_budget': 100,
        'waiver': {'decision_id': did}})
    assert v['status'] == 'waived'
    assert v['waiver']['decision_id'] == did
    # 豁免引用的决策在账本里确实存在
    assert ledger.find_decision(cdir, did) is not None


def test_downgrade_after_problem_records_and_reblocks(tmp_path):
    """验收后发现问题 → 降级回 failed,记账本,派生重新把它算作待处理。"""
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'relax', fingerprint_hash='fp')],
                                fingerprint_hash='fp')
    cdir = camp['dir']
    t = camp['tasks'][0]
    _persist(cdir, t, lambda task: states.mark_completed(task))
    _persist(cdir, t, lambda task: states.promote_validated(
        task, [{'name': 'x', 'ok': True}]))
    decision = _bound_accept(camp, t, [{'name': 'x', 'ok': True}], -1.0)
    states.promote_accepted(t, decision, campaign_dir=cdir)
    assert 'a' in derive.derive_ready(camp)['done']

    revision = t['revision']
    states.downgrade(t, 'failed', reason='复现值与文献偏差超误差带', campaign_dir=cdir)
    schema.persist_task(cdir, t, expected_revision=revision)
    d = derive.derive_ready(camp)
    assert any(x['task'] == 'a' for x in d['blocked'])
    assert ledger.read_events(cdir, kind='rung_downgrade')[0]['detail']['to'] == 'failed'
