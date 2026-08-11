"""campaign.states 测试:三态推进红线、拒绝直写 accepted、降级记账。"""
import copy
import multiprocessing
import threading
from dataclasses import replace

import pytest

from vcstudio.campaign import gates, ledger, schema, states


def _process_accept_after_persist_pause(campaign_dir, task, decision,
                                        entered, release, output):
    """Spawn-safe acceptance caller paused before the real persist transaction starts."""
    real_persist = schema.persist_task

    def paused_persist(*args, **kwargs):
        entered.set()
        if not release.wait(15):
            raise TimeoutError('acceptance persist release timed out')
        return real_persist(*args, **kwargs)

    schema.persist_task = paused_persist
    try:
        states.promote_accepted(task, decision, campaign_dir=campaign_dir)
        output.put(('accepted', task.get('rung')))
    except BaseException as exc:  # child must report the fail-closed reason
        output.put(('rejected', type(exc).__name__, str(exc)))


def _task():
    return schema.new_task('t', 'relax')


def _validated_campaign(tmp_path, *, task_id='t'):
    task = schema.new_task(task_id, 'relax', fingerprint_hash='method-fp')
    camp = schema.init_campaign(
        str(tmp_path), 'demo', tasks=[task], fingerprint_hash='method-fp')
    task = camp['tasks'][0]
    states.mark_completed(task)
    schema.persist_task(camp['dir'], task, expected_revision=0)
    states.promote_validated(task, [{'name': 'converged', 'ok': True}])
    schema.persist_task(camp['dir'], task, expected_revision=1)
    return camp, task


def _accept_decision(camp, task, *, actor='yuhao', energy=-1.0, waiver=None):
    meta = camp['meta']
    return gates.accept_gate({
        'campaign_dir': camp['dir'], 'campaign_id': meta['id'],
        'task_id': task['id'], 'task_revision': task['revision'],
        'input_fingerprints': gates.task_input_fingerprints(task, meta),
        'task_fingerprint_hash': task['fingerprint_hash'],
        'campaign_fingerprint_hash': meta['fingerprint_hash'],
        'required_checks': [{'name': 'converged', 'ok': True}],
        'energy_eV': energy, 'actor': actor, 'waiver': waiver,
    })


def test_full_chain_completed_validated_accepted(tmp_path):
    camp, t = _validated_campaign(tmp_path)
    decision = _accept_decision(camp, t, actor='yuhao')
    assert decision.authoritative is True
    states.promote_accepted(t, decision, signed_by='yuhao', campaign_dir=camp['dir'])
    assert states.is_accepted(t)
    # 审计轨迹齐全
    rungs = [h['rung'] for h in t['rung_history']]
    assert rungs == ['completed', 'validated', 'accepted']
    assert t['rung_history'][-1]['by'] == 'yuhao'
    assert t['revision'] == 3
    assert schema.load_task(camp['dir'], 't')['rung'] == 'accepted'


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
        states.promote_accepted(t, {'gate': 'accept_gate', 'status': 'pass'})


def test_accepted_requires_passing_gate_decision(tmp_path):
    camp, t = _validated_campaign(tmp_path)
    blocked = _accept_decision(camp, t, energy=None)
    with pytest.raises(ValueError, match='通过或已豁免'):
        states.promote_accepted(t, blocked, campaign_dir=camp['dir'])
    # 自由 dict 即使声称 accept/pass 也没有权威
    with pytest.raises(ValueError, match='GateDecision'):
        states.promote_accepted(
            t, {'gate': 'accept_gate', 'status': 'pass'}, campaign_dir=camp['dir'])
    assert states.rung(t) == 'validated'


def test_accepted_accepts_ledger_validated_waiver(tmp_path):
    camp, t = _validated_campaign(tmp_path)
    fps = gates.task_input_fingerprints(t, camp['meta'])
    waiver_id = ledger.record_waiver_decision(
        camp['dir'], campaign_id='demo', task_id='t', gate='accept_gate',
        task_revision=t['revision'], input_fingerprints=fps,
        detail='导师批准在缺失能量时仅作示例验收', actor='yuhao')
    decision = _accept_decision(
        camp, t, actor='ignored-for-waiver', energy=None,
        waiver={'decision_id': waiver_id})
    assert decision.status == 'waived' and decision.actor == 'yuhao'
    states.promote_accepted(t, decision, signed_by='yuhao', campaign_dir=camp['dir'])
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


def test_mark_completed_rejects_backslide_from_accepted(tmp_path):
    camp, t = _validated_campaign(tmp_path)
    states.promote_accepted(
        t, _accept_decision(camp, t, actor='gate:auto'), campaign_dir=camp['dir'])
    with pytest.raises(ValueError, match='须先 downgrade'):
        states.mark_completed(t)


def test_accepted_rejects_stale_task_revision_without_mutation(tmp_path):
    camp, t = _validated_campaign(tmp_path)
    decision = _accept_decision(camp, t)
    newer = copy.deepcopy(t)
    newer['note'] = 'concurrent writer'
    schema.persist_task(camp['dir'], newer, expected_revision=t['revision'])
    with pytest.raises(ValueError, match='revision 已陈旧'):
        states.promote_accepted(t, decision, campaign_dir=camp['dir'])
    assert schema.load_task(camp['dir'], 't')['rung'] == 'validated'


def test_accepted_rejects_unpersisted_in_memory_validated_bypass(tmp_path):
    task = schema.new_task('t', 'relax', fingerprint_hash='method-fp')
    camp = schema.init_campaign(
        str(tmp_path), 'demo', tasks=[task], fingerprint_hash='method-fp')
    task = camp['tasks'][0]
    # 只改内存，不把 completed/validated 权威状态写入磁盘。
    states.mark_completed(task)
    states.promote_validated(task, [{'name': 'converged', 'ok': True}])
    decision = _accept_decision(camp, task)
    with pytest.raises(ValueError, match='磁盘上已持久化'):
        states.promote_accepted(task, decision, campaign_dir=camp['dir'])
    assert schema.load_task(camp['dir'], 't')['rung'] == 'pending'


def test_accepted_rejects_revoked_gate_decision(tmp_path):
    camp, t = _validated_campaign(tmp_path)
    decision = _accept_decision(camp, t)
    ledger.revoke_decision(
        camp['dir'], decision.ledger_decision_id, actor='yuhao', reason='撤回验收')
    with pytest.raises(ValueError, match='已.*撤销'):
        states.promote_accepted(t, decision, campaign_dir=camp['dir'])


def test_accepted_rechecks_waiver_revocation_after_gate_decision(tmp_path):
    camp, t = _validated_campaign(tmp_path)
    fps = gates.task_input_fingerprints(t, camp['meta'])
    waiver_id = ledger.record_waiver_decision(
        camp['dir'], campaign_id='demo', task_id='t', gate='accept_gate',
        task_revision=t['revision'], input_fingerprints=fps,
        detail='临时豁免缺失能量', actor='yuhao')
    decision = _accept_decision(
        camp, t, energy=None, waiver={'decision_id': waiver_id})
    ledger.revoke_decision(
        camp['dir'], waiver_id, actor='yuhao', reason='在最终推进前撤销')
    with pytest.raises(ValueError, match='waiver decision 已撤销'):
        states.promote_accepted(t, decision, campaign_dir=camp['dir'])
    assert schema.load_task(camp['dir'], 't')['rung'] == 'validated'


def test_gate_revocation_before_real_persist_wins_thread_race(
        tmp_path, monkeypatch):
    camp, task = _validated_campaign(tmp_path)
    decision = _accept_decision(camp, task)
    entered = threading.Event()
    release = threading.Event()
    outcome = []
    real_persist = schema.persist_task

    def paused_persist(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError('acceptance persist release timed out')
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(schema, 'persist_task', paused_persist)

    def accept():
        try:
            states.promote_accepted(task, decision, campaign_dir=camp['dir'])
            outcome.append(('accepted',))
        except BaseException as exc:
            outcome.append(('rejected', type(exc).__name__, str(exc)))

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        assert entered.wait(10), 'acceptance never reached persist entry'
        ledger.revoke_decision(
            camp['dir'], decision.ledger_decision_id,
            actor='yuhao', reason='persist 入口前撤回验收')
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert outcome and outcome[0][0] == 'rejected'
    assert '撤销' in outcome[0][2]
    disk = schema.load_task(camp['dir'], 't')
    assert disk['rung'] == 'validated' and disk['revision'] == decision.task_revision


def test_waiver_revocation_before_real_persist_wins_spawn_race(tmp_path):
    camp, task = _validated_campaign(tmp_path)
    fingerprints = gates.task_input_fingerprints(task, camp['meta'])
    waiver_id = ledger.record_waiver_decision(
        camp['dir'], campaign_id='demo', task_id='t', gate='accept_gate',
        task_revision=task['revision'], input_fingerprints=fingerprints,
        detail='竞态测试临时豁免', actor='yuhao')
    decision = _accept_decision(
        camp, task, energy=None, waiver={'decision_id': waiver_id})
    context = multiprocessing.get_context('spawn')
    entered = context.Event()
    release = context.Event()
    output = context.Queue()
    process = context.Process(
        target=_process_accept_after_persist_pause,
        args=(camp['dir'], task, decision, entered, release, output))
    process.start()
    try:
        assert entered.wait(20), 'spawn acceptance never reached persist entry'
        ledger.revoke_decision(
            camp['dir'], waiver_id, actor='yuhao', reason='persist 入口前撤回豁免')
    finally:
        release.set()
        process.join(20)
        if process.is_alive():  # pragma: no cover - cleanup for failed synchronization
            process.terminate()
            process.join(5)

    outcome = output.get(timeout=5)
    assert process.exitcode == 0
    assert outcome[0] == 'rejected', outcome
    assert '撤销' in outcome[2]
    disk = schema.load_task(camp['dir'], 't')
    assert disk['rung'] == 'validated' and disk['revision'] == decision.task_revision


@pytest.mark.parametrize(
    ('field', 'value', 'message'),
    [('ledger_decision_digest', '0' * 64, 'ledger decision digest'),
     ('actor', 'forged-actor', 'actor'),
     ('decided_at', '2099-01-01T00:00:00', 'decided_at'),
     ('checks_digest', 'f' * 64, 'checks digest')])
def test_accepted_rejects_tampered_gate_decision_bindings(tmp_path, field, value, message):
    camp, t = _validated_campaign(tmp_path)
    decision = _accept_decision(camp, t)
    forged = replace(decision, **{field: value})
    with pytest.raises(ValueError, match=message):
        states.promote_accepted(t, forged, campaign_dir=camp['dir'])
    assert schema.load_task(camp['dir'], 't')['rung'] == 'validated'


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
