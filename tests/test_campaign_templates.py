"""campaign_templates 计算活动模板测试:实例化 DAG 依赖/机时 + next_derivations 幂等。"""
from __future__ import annotations

import pytest

from vcstudio.campaign import schema, states
from vcstudio.project import campaign_templates as ct


def _spec():
    return {'systems': ['Fe@MN4', 'Co@MN4'], 'adsorbates': ['Li2S8', 'Li2S6']}


def _validate(cdir, tid):
    """把某任务提升到 validated(completed → validated),落盘。"""
    camp = schema.load_campaign(cdir)
    t = {x['id']: x for x in camp['tasks']}[tid]
    states.mark_completed(t)
    states.promote_validated(t, {'converged': True})
    schema.save_task(cdir, t)
    return t


# ── 注册表 ────────────────────────────────────────────────────────────────────

def test_list_templates_has_named_entries():
    tpls = ct.list_templates()
    assert 'sac_lis_screening' in tpls
    assert tpls['sac_lis_screening']['name_zh'] == 'SAC 锂硫筛选(张洪毅式)'
    assert tpls['sac_lis_screening']['figures_scenario'] == 'lis'


def test_get_template_unknown_raises():
    with pytest.raises(ValueError):
        ct.get_template('no_such_template')


# ── instantiate:DAG 依赖 + 计数 + 机时 ───────────────────────────────────────

def test_instantiate_builds_stage_dag(tmp_path):
    res = ct.instantiate('sac_lis_screening', _spec(), str(tmp_path))
    # 2 体系 ×(1 清洁 + 2 吸附)= 6 relax;每 relax 一个 static;仅 4 吸附 relax 有 freq
    assert res['stages'] == {'relax': 6, 'static': 6, 'freq': 4, 'analysis': 3}
    assert res['n_jobs'] == 16                       # 6+6+4(analysis 非集群作业)
    assert res['figures_scenario'] == 'lis'

    camp = schema.load_campaign(res['campaign_dir'])
    by_id = {t['id']: t for t in camp['tasks']}
    statics = [t for t in camp['tasks'] if t.get('derive') == 'estatic']
    freqs = [t for t in camp['tasks'] if t.get('derive') == 'freq']
    # estatic/freq 节点 depends_on 对应 relax
    assert all(by_id[t['depends_on'][0]]['kind'] == 'relax' for t in statics)
    assert all(t['kind'] == 'static' for t in statics)
    assert all(t['derive_kinds'] == ['pdos', 'bader', 'chgdiff'] for t in statics)
    # freq 只挂吸附中间体
    assert freqs and all(by_id[t['depends_on'][0]].get('is_adsorbate') for t in freqs)


def test_instantiate_graph_valid_no_cycle(tmp_path):
    res = ct.instantiate('sac_lis_screening', _spec(), str(tmp_path))
    camp = schema.load_campaign(res['campaign_dir'])
    schema.validate_campaign(camp)                   # 重复 id / 悬空依赖 / 环 → 抛


def test_instantiate_estimate_core_hours(tmp_path):
    res = ct.instantiate('sac_lis_screening', _spec(), str(tmp_path))
    est = res['estimate']
    assert est['relax'] == 6 * ct.CORE_HOURS['relax']
    assert est['static'] == 6 * ct.CORE_HOURS['static']
    assert est['freq'] == 4 * ct.CORE_HOURS['freq']
    assert est['total'] == pytest.approx(est['relax'] + est['static'] + est['freq'])


def test_instantiate_adsorption_basic_relax_only(tmp_path):
    res = ct.instantiate('adsorption_basic',
                         {'systems': ['Fe@MN4'], 'adsorbates': ['Li2S8']},
                         str(tmp_path))
    assert 'static' not in res['stages'] and 'freq' not in res['stages']
    assert res['stages']['relax'] == 2               # 1 清洁 + 1 吸附


def test_instantiate_empty_systems_raises(tmp_path):
    with pytest.raises(ValueError):
        ct.instantiate('sac_lis_screening', {'systems': []}, str(tmp_path))


# ── next_derivations:全链推进大脑 + 幂等 ─────────────────────────────────────

def test_next_derivations_empty_until_validated(tmp_path):
    res = ct.instantiate('sac_lis_screening', _spec(), str(tmp_path))
    assert ct.next_derivations(res['campaign_dir']) == []


def test_next_derivations_after_validate_and_idempotent(tmp_path):
    res = ct.instantiate('sac_lis_screening', _spec(), str(tmp_path))
    cdir = res['campaign_dir']
    _validate(cdir, 'Fe_MN4__clean__relax')          # 清洁 relax 验证通过

    nd = ct.next_derivations(cdir)
    # 清洁 relax 只派生 estatic(非吸附 → 无 freq)
    got = {(x['src_id'], x['derive']) for x in nd}
    assert ('Fe_MN4__clean__relax', 'estatic') in got
    assert ('Fe_MN4__clean__relax', 'freq') not in got
    entry = next(x for x in nd if x['src_id'] == 'Fe_MN4__clean__relax')
    assert entry['kinds'] == ['pdos', 'bader', 'chgdiff']
    assert entry['src_dir'] and 'Fe_MN4__clean__relax' in entry['src_dir']

    # 幂等:标记已派生后不再返回
    for x in nd:
        ct.mark_derived(cdir, x['src_id'], x['derive'])
    assert ct.next_derivations(cdir) == []


def test_next_derivations_adsorbate_yields_estatic_and_freq(tmp_path):
    res = ct.instantiate('sac_lis_screening', _spec(), str(tmp_path))
    cdir = res['campaign_dir']
    _validate(cdir, 'Fe_MN4__Li2S8__relax')          # 吸附 relax 验证通过
    got = {(x['src_id'], x['derive']) for x in ct.next_derivations(cdir)}
    assert ('Fe_MN4__Li2S8__relax', 'estatic') in got
    assert ('Fe_MN4__Li2S8__relax', 'freq') in got   # 吸附中间体两阶段齐发


def test_next_derivations_ignores_merely_completed(tmp_path):
    res = ct.instantiate('sac_lis_screening', _spec(), str(tmp_path))
    cdir = res['campaign_dir']
    camp = schema.load_campaign(cdir)
    t = {x['id']: x for x in camp['tasks']}['Fe_MN4__clean__relax']
    states.mark_completed(t)                          # 仅 completed,未 validated
    schema.save_task(cdir, t)
    assert ct.next_derivations(cdir) == []            # 不建在未验证结果上
