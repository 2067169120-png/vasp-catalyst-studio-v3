"""campaign.gates 测试:三门 deny-by-default、机时/单点/指纹拦截、waive 需 decision_id。"""
import pytest

from vcstudio.campaign import gates


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
    v = gates.submit_gate(ctx)
    assert v['status'] == 'waived'
    assert v['waiver']['decision_id'] == 'dec-abc123'
    # 被豁免的阻断项仍保留可审计
    assert v['blocking_issues']


def test_waive_ignored_when_gate_passes():
    ctx = {'estimated_core_hours': 100, 'remaining_budget': 500,
           'waiver': {'decision_id': 'dec-abc123'}}
    v = gates.submit_gate(ctx)
    assert v['status'] == 'pass'
    assert 'waiver' not in v
