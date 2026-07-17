"""campaign.states 测试:三态推进红线、拒绝直写 accepted、降级记账。"""
import pytest

from vcstudio.campaign import ledger, schema, states


def _task():
    return schema.new_task('t', 'relax')


def _accept_verdict(status='pass'):
    return {'gate': 'accept_gate', 'status': status, 'checks': [], 'blocking_issues': []}


def test_full_chain_completed_validated_accepted():
    t = _task()
    assert states.rung(t) == 'pending'
    states.mark_completed(t)
    assert states.rung(t) == 'completed'
    states.promote_validated(t, [{'name': 'converged', 'ok': True}])
    assert states.rung(t) == 'validated'
    states.promote_accepted(t, _accept_verdict(), signed_by='yuhao')
    assert states.is_accepted(t)
    # 审计轨迹齐全
    rungs = [h['rung'] for h in t['rung_history']]
    assert rungs == ['completed', 'validated', 'accepted']
    assert t['rung_history'][-1]['by'] == 'yuhao'


def test_validated_rejects_failing_checker():
    t = _task()
    states.mark_completed(t)
    with pytest.raises(ValueError, match='确定性检查未全通过'):
        states.promote_validated(t, [{'name': 'force', 'ok': False}])
    assert states.rung(t) == 'completed'


def test_validated_requires_evidence():
    t = _task()
    states.mark_completed(t)
    with pytest.raises(ValueError, match='至少需要一项确定性 checker'):
        states.promote_validated(t, [])


def test_validated_only_from_completed():
    t = _task()  # 仍是 pending
    with pytest.raises(ValueError, match='只能从 completed 提升到 validated'):
        states.promote_validated(t, [{'name': 'x', 'ok': True}])


def test_cannot_write_accepted_directly():
    """AI/外部无权直接写 accepted:未到 validated 即拒绝。"""
    t = _task()
    with pytest.raises(ValueError, match='只能从 validated 提升到 accepted'):
        states.promote_accepted(t, _accept_verdict())


def test_accepted_requires_passing_gate_verdict():
    t = _task()
    states.mark_completed(t)
    states.promote_validated(t, [{'name': 'x', 'ok': True}])
    with pytest.raises(ValueError, match='未通过且未豁免'):
        states.promote_accepted(t, _accept_verdict(status='blocked'))
    # 非 accept_gate 的裁决也不认
    with pytest.raises(ValueError, match='只能由 accept_gate 裁决'):
        states.promote_accepted(t, {'gate': 'submit_gate', 'status': 'pass'})
    assert states.rung(t) == 'validated'


def test_accepted_accepts_waived_verdict():
    t = _task()
    states.mark_completed(t)
    states.promote_validated(t, [{'name': 'x', 'ok': True}])
    states.promote_accepted(t, _accept_verdict(status='waived'))
    assert states.is_accepted(t)


def test_downgrade_requires_reason_and_lower_target():
    t = _task()
    states.mark_completed(t)
    states.promote_validated(t, [{'name': 'x', 'ok': True}])
    with pytest.raises(ValueError, match='降级必须给出原因'):
        states.downgrade(t, 'completed', reason='')
    with pytest.raises(ValueError, match='必须低于当前 rung'):
        states.downgrade(t, 'accepted', reason='试图升级')


def test_downgrade_records_ledger(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[_task()])
    cdir = camp['dir']
    t = camp['tasks'][0]
    states.mark_completed(t)
    states.promote_validated(t, [{'name': 'x', 'ok': True}])
    states.downgrade(t, 'failed', reason='发现磁矩漂移,弛豫无效', campaign_dir=cdir)
    assert states.rung(t) == 'failed'
    events = ledger.read_events(cdir, kind='rung_downgrade')
    assert len(events) == 1
    assert events[0]['task_id'] == 't'
    assert events[0]['detail']['from'] == 'validated'
    assert events[0]['detail']['reason'] == '发现磁矩漂移,弛豫无效'


def test_mark_completed_rejects_backslide_from_accepted():
    t = _task()
    states.mark_completed(t)
    states.promote_validated(t, [{'name': 'x', 'ok': True}])
    states.promote_accepted(t, _accept_verdict())
    with pytest.raises(ValueError, match='须先 downgrade'):
        states.mark_completed(t)


def test_exec_states_and_events(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[_task()])
    cdir = camp['dir']
    t = camp['tasks'][0]
    states.mark_running(t, campaign_dir=cdir)
    assert states.rung(t) == 'running'
    states.mark_failed(t, reason='SCF 不收敛', campaign_dir=cdir)
    assert states.rung(t) == 'failed'
    with pytest.raises(ValueError, match='非法执行态'):
        states.set_exec_state(t, 'accepted')
    assert len(ledger.read_events(cdir, task_id='t')) == 2
