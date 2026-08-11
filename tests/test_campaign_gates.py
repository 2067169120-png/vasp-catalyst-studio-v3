"""campaign.gates 测试:三门 deny-by-default、机时/单点/指纹拦截、waive 需 decision_id。"""
import pytest

from vcstudio.campaign import gates, ledger, schema


# ── submit_gate ───────────────────────────────────────────────────────────────
def test_submit_gate_pass_within_budget():
    v = gates.submit_gate({'estimated_core_hours': 100, 'remaining_budget': 500})
    assert v['gate'] == 'submit_gate'
    assert v['status'] == 'pass'
    assert v['blocking_issues'] == []


def test_submit_gate_blocks_over_budget():
    v = gates.submit_gate({'estimated_core_hours': 800, 'remaining_budget': 500})
    assert v['status'] == 'blocked'
    assert any('机时预算不足' in s for s in v['blocking_issues'])


def test_submit_gate_unlimited_budget_passes():
    v = gates.submit_gate({'estimated_core_hours': 1e9, 'remaining_budget': None})
    assert v['status'] == 'pass'


def test_submit_gate_pilot_first_blocks_fanout():
    ctx = {'require_pilot': True, 'is_fanout': True, 'pilot_accepted_count': 0}
    v = gates.submit_gate(ctx)
    assert v['status'] == 'blocked'
    assert any('单点先行' in s for s in v['blocking_issues'])
    # 有一个已验收 pilot 后放行
    ctx['pilot_accepted_count'] = 1
    assert gates.submit_gate(ctx)['status'] == 'pass'


def test_submit_gate_advisor_p0_blocks():
    adv = [('P0', 'GAS_REF_SMEARING', '气相参考须 ISMEAR=0'),
           ('P2', 'NO_DISPERSION', '未启用色散')]
    v = gates.submit_gate({'advisor_results': adv})
    assert v['status'] == 'blocked'
    assert any('P0 阻断' in s for s in v['blocking_issues'])
    # 只有 P1/P2 时放行
    assert gates.submit_gate({'advisor_results': adv[1:]})['status'] == 'pass'


def test_submit_gate_accepts_dict_advisories():
    adv = [{'priority': 'P0', 'message': '指纹缺 ENCUT'}]
    assert gates.submit_gate({'advisor_results': adv})['status'] == 'blocked'


# ── accept_gate ───────────────────────────────────────────────────────────────
def test_accept_gate_pass():
    v = gates.accept_gate({'task_fingerprint_hash': 'abc', 'campaign_fingerprint_hash': 'abc',
                           'required_checks': [{'name': 'conv', 'ok': True}], 'energy_eV': -12.3})
    assert v['status'] == 'pass'


def test_accept_gate_fingerprint_mismatch_blocks():
    v = gates.accept_gate({'task_fingerprint_hash': 'abc', 'campaign_fingerprint_hash': 'xyz',
                           'required_checks': [{'name': 'conv', 'ok': True}], 'energy_eV': -1.0})
    assert v['status'] == 'blocked'
    assert any('方法指纹不一致' in s for s in v['blocking_issues'])


def test_accept_gate_required_check_and_energy():
    v = gates.accept_gate({'task_fingerprint_hash': 'a', 'campaign_fingerprint_hash': 'a',
                           'required_checks': {'conv': True, 'force': False},
                           'energy_eV': float('inf')})
    assert v['status'] == 'blocked'
    joined = ' '.join(v['blocking_issues'])
    assert 'required_checks 未全通过' in joined and 'force' in joined
    assert '能量缺失或非有限' in joined


def test_accept_gate_missing_energy_blocks():
    v = gates.accept_gate({'task_fingerprint_hash': 'a', 'campaign_fingerprint_hash': 'a',
                           'energy_eV': None})
    assert v['status'] == 'blocked'


# ── report_gate ───────────────────────────────────────────────────────────────
def test_report_gate_pass():
    ctx = {'report_tasks': [{'id': 't1', 'rung': 'accepted'}, {'id': 't2', 'rung': 'accepted'}],
           'figures': [{'id': 'fig1', 'provenance': ['t1', 't2']}]}
    assert gates.report_gate(ctx)['status'] == 'pass'


def test_report_gate_blocks_unaccepted_and_no_provenance():
    ctx = {'report_tasks': [{'id': 't1', 'rung': 'validated'}],
           'figures': [{'id': 'fig1', 'provenance': []}]}
    v = gates.report_gate(ctx)
    assert v['status'] == 'blocked'
    joined = ' '.join(v['blocking_issues'])
    assert '未 accepted' in joined and 'provenance' in joined


def test_report_gate_empty_tasklist_blocks():
    assert gates.report_gate({'report_tasks': []})['status'] == 'blocked'


# ── waive 机制 ────────────────────────────────────────────────────────────────
def test_waive_requires_decision_id():
    ctx = {'estimated_core_hours': 800, 'remaining_budget': 500, 'waiver': {}}
    with pytest.raises(ValueError, match='必须携带 decision_id'):
        gates.submit_gate(ctx)


def test_waive_with_decision_id_flips_to_waived():
    ctx = {'estimated_core_hours': 800, 'remaining_budget': 500,
           'waiver': {'decision_id': 'dec-abc123'}}
    with pytest.raises(ValueError, match='named campaign'):
        gates.submit_gate(ctx)


def test_waive_ignored_when_gate_passes():
    ctx = {'estimated_core_hours': 100, 'remaining_budget': 500,
           'waiver': {'decision_id': 'dec-abc123'}}
    v = gates.submit_gate(ctx)
    assert v['status'] == 'pass'
    assert 'waiver' not in v


def _waiver_fixture(tmp_path):
    task = schema.new_task('a', 'relax', fingerprint_hash='fp')
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[task], fingerprint_hash='fp')
    task = camp['tasks'][0]
    fps = gates.task_input_fingerprints(task, camp['meta'])
    context = {
        'campaign_dir': camp['dir'], 'campaign_id': 'demo',
        'task_id': 'a', 'task_revision': 0, 'input_fingerprints': fps,
        'estimated_core_hours': 800, 'remaining_budget': 500,
    }
    return camp, task, fps, context


def test_real_task_bound_waiver_is_authoritative_and_keeps_blockers(tmp_path):
    camp, _task, fps, context = _waiver_fixture(tmp_path)
    decision_id = ledger.record_waiver_decision(
        camp['dir'], campaign_id='demo', task_id='a', gate='submit_gate',
        task_revision=0, input_fingerprints=fps,
        detail='导师批准本任务超预算一次', actor='reviewer-A')
    decision = gates.submit_gate({**context, 'waiver': {'decision_id': decision_id}})
    assert isinstance(decision, gates.GateDecision)
    assert decision.status == 'waived' and decision.authoritative is True
    assert (decision.campaign_id, decision.task_id, decision.task_revision) == ('demo', 'a', 0)
    assert decision.input_fingerprints == fps
    assert decision.checks_digest == gates.checks_digest(decision.checks)
    assert decision.ledger_decision_digest and decision.actor == 'reviewer-A'
    assert decision.decided_at
    assert decision.waiver['decision_id'] == decision_id
    assert decision.waiver['decision_digest']
    assert decision.blocking_issues


@pytest.mark.parametrize('mismatch', ['kind', 'task', 'gate', 'fingerprint', 'revision'])
def test_waiver_scope_mismatch_fails_closed(tmp_path, mismatch):
    camp, _task, fps, context = _waiver_fixture(tmp_path)
    if mismatch == 'kind':
        decision_id = ledger.record_decision(
            camp['dir'], 'budget-waiver', '旧式自由决策', 'user', context={'task': 'a'})
    else:
        bound = {
            'campaign_id': 'demo', 'task_id': 'a', 'gate': 'submit_gate',
            'task_revision': 0, 'input_fingerprints': fps,
        }
        if mismatch == 'task':
            bound['task_id'] = 'other'
        elif mismatch == 'gate':
            bound['gate'] = 'accept_gate'
        elif mismatch == 'fingerprint':
            bound['input_fingerprints'] = {**fps, 'task_method_fingerprint': 'other'}
        elif mismatch == 'revision':
            bound['task_revision'] = 1
        decision_id = ledger.record_waiver_decision(
            camp['dir'], **bound, detail='不匹配的授权', actor='reviewer-A')
    with pytest.raises(ValueError, match='waiver'):
        gates.submit_gate({**context, 'waiver': {'decision_id': decision_id}})


def test_waiver_from_other_named_campaign_is_not_authority(tmp_path):
    _camp, _task, _fps, context = _waiver_fixture(tmp_path / 'one')
    other, _otask, other_fps, _other_context = _waiver_fixture(tmp_path / 'two')
    decision_id = ledger.record_waiver_decision(
        other['dir'], campaign_id='demo', task_id='a', gate='submit_gate',
        task_revision=0, input_fingerprints=other_fps,
        detail='另一个 named campaign 的决策', actor='reviewer-A')
    with pytest.raises(ValueError, match='不存在'):
        gates.submit_gate({**context, 'waiver': {'decision_id': decision_id}})


def test_revoked_waiver_fails_closed(tmp_path):
    camp, _task, fps, context = _waiver_fixture(tmp_path)
    decision_id = ledger.record_waiver_decision(
        camp['dir'], campaign_id='demo', task_id='a', gate='submit_gate',
        task_revision=0, input_fingerprints=fps,
        detail='临时授权', actor='reviewer-A')
    ledger.revoke_decision(
        camp['dir'], decision_id, actor='reviewer-A', reason='输入重新核对后撤销')
    with pytest.raises(ValueError, match='已撤销'):
        gates.submit_gate({**context, 'waiver': {'decision_id': decision_id}})
