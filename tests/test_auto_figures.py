"""auto_figures 管线终点自动出图引擎测试:图计划条件判定 / 期刊风格 / 计划 / 全链渲染。"""
from __future__ import annotations

import os
import sys

import matplotlib

matplotlib.use('Agg')

import numpy as _numpy  # noqa: E402
import pytest  # noqa: E402

from vcstudio.project import auto_figures as af  # noqa: E402
from vcstudio.project import figure_presets as fp  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    sys.modules.setdefault('numpy', _numpy)
    yield


_E = [-8.0, -6.0, -4.0, -2.0, 0.0, 2.0, 4.0]


def _full_inputs():
    """齐活的 figure_inputs:ΔE + 台阶 + PDOS + 差分电荷 + ≥3 火山点。"""
    return {
        'adsorbates': ['S8', 'Li2S8', 'Li2S6'],
        'substrates': {'CoO': [-1.2, -1.6, -0.9]},
        'ladder': {'paths': [{'name': 'CoO', 'G': [0.0, -0.4, -0.8, -0.3, -1.0]}],
                   'step_labels': ['S8', 'Li2S8', 'Li2S6', 'Li2S4', 'Li2S2'],
                   'pds_index': 2, '_u_l': 1.66},
        'pdos': {'series': [{'label': 'Co 3d', 'energies': _E,
                             'dos_up': [0, 1, 2, 3, 2, 1, 0],
                             'dos_down': [0, 1, 2, 3, 2, 1, 0]}], 'efermi': 0.0},
        'chgdiff': {'z': [0, 1, 2, 3, 4, 5], 'rho': [0.0, 0.1, -0.2, 0.3, -0.1, 0.0]},
        'volcano_points': [{'name': 'A', 'x': -2.3, 'y': -0.5},
                           {'name': 'B', 'x': -1.8, 'y': -0.3},
                           {'name': 'C', 'x': -1.2, 'y': -0.6}],
    }


class _FakeAds:
    """注入式吸附能取数替身:两条最稳 ΔE 行 + 清洁表面 DONE。"""

    @staticmethod
    def delta_e_rows(project):
        return {'slab': ('DONE', -100.0), 'ref': ('DONE', -5.0), 'has_ref': True,
                'rows': [
                    {'name': 'p_ads_Li2S8', 'species': 'Li2S8', 'state': 'DONE',
                     'e_config': -110.0, 'delta_e': -1.6, 'is_most_stable': True,
                     'dd_e': 0.0},
                    {'name': 'p_ads_Li2S6', 'species': 'Li2S6', 'state': 'DONE',
                     'e_config': -108.0, 'delta_e': -0.9, 'is_most_stable': True,
                     'dd_e': 0.0}]}


# ── 期刊风格 ──────────────────────────────────────────────────────────────────

def test_journal_style_nature():
    js = af.journal_style('nature')
    assert js['style_kw'] == {'font': 'serif', 'base_size': 8.0, 'box': False}
    assert js['palette'] == 'npg' and js['col_mm'] == 89


def test_journal_style_acs_double_and_prb_box():
    assert af.journal_style('acs')['font'] == 'sans'
    assert af.journal_style('acs')['width_double'] == 7.00
    assert af.journal_style('prb')['box'] is True          # 四边框


def test_journal_style_unknown_falls_back_nature():
    js = af.journal_style('unknownJ')
    assert js['journal'] == 'nature' and js['palette'] == 'npg'


# ── 注册表完整性 ──────────────────────────────────────────────────────────────

def test_figure_plans_reference_valid_presets():
    valid = {p['key'] for p in fp.PRESETS}
    for scenario, items in af.FIGURE_PLANS.items():
        for it in items:
            assert it['key'] in valid, (scenario, it['key'])
            assert it['condition'] in af._CONDITIONS, (scenario, it['condition'])


def test_list_scenarios_four():
    assert set(af.list_scenarios()) == {'lis', 'electrocatalysis',
                                        'thermocatalysis', 'general'}


def test_plan_unknown_scenario_raises():
    with pytest.raises(ValueError):
        af.plan_for_project({}, 'nope', availability={'n_delta': 1})


# ── 条件判定(fake availability) ──────────────────────────────────────────────

def test_condition_has_delta_branches():
    ok, _ = af._condition_ok('has_delta', {'n_delta': 2}, {})
    assert ok
    bad, reason = af._condition_ok('has_delta', {'n_delta': 0}, {})
    assert not bad and 'ΔE' in reason


def test_condition_min_systems_needs_three_and_volcano():
    assert af._condition_ok('min_systems_3',
                            {'n_systems': 4, 'has_volcano': True}, {})[0]
    # 体系够但无火山点 → 不可用
    assert not af._condition_ok('min_systems_3',
                                {'n_systems': 4, 'has_volcano': False}, {})[0]
    # 火山点有但体系 <3 → 不可用
    assert not af._condition_ok('min_systems_3',
                                {'n_systems': 2, 'has_volcano': True}, {})[0]


def test_condition_unknown_name_denied():
    ok, reason = af._condition_ok('made_up', {}, {})
    assert not ok and '未知' in reason


# ── plan_for_project(fake availability 缺失/齐活分支) ─────────────────────────

def test_plan_lis_only_delta_plans_bar_table_skips_rest():
    avail = {'name': 'CoO', 'scenario': 'lis', 'n_delta': 3, 'n_systems': 1,
             'has_ladder': False, 'has_volcano': False, 'has_pdos': False,
             'has_chgdiff': False}
    plan = af.plan_for_project({}, 'lis', availability=avail)
    assert [p['key'] for p in plan['planned']] == ['adsorption_bar',
                                                   'energy_matrix_table']
    skipped_keys = {s['key'] for s in plan['skipped']}
    assert skipped_keys == {'free_energy_ladder', 'volcano', 'pdos', 'charge_profile'}
    for s in plan['skipped']:                       # 每条都有中文原因
        assert isinstance(s['reason'], str) and s['reason']


def test_plan_lis_full_availability_plans_all():
    avail = {'name': 'CoO', 'scenario': 'lis', 'n_delta': 3, 'n_systems': 4,
             'has_ladder': True, 'has_volcano': True, 'has_pdos': True,
             'has_chgdiff': True}
    plan = af.plan_for_project({}, 'lis', availability=avail)
    assert [p['key'] for p in plan['planned']] == [
        'adsorption_bar', 'energy_matrix_table', 'free_energy_ladder',
        'volcano', 'pdos', 'charge_profile']
    assert plan['skipped'] == []


# ── analyze_project(figure_inputs 种子 / 注入吸附取数) ────────────────────────

def test_analyze_from_figure_inputs_seed():
    proj = {'name': 'CoO', 'figure_inputs': _full_inputs()}
    avail = af.analyze_project(proj, 'lis')
    assert avail['has_bar'] and avail['has_table'] and avail['has_ladder']
    assert avail['has_pdos'] and avail['has_chgdiff'] and avail['has_volcano']
    assert avail['n_systems'] >= 3
    assert set(avail['_data']) >= {'adsorption_bar', 'energy_matrix_table',
                                   'free_energy_ladder', 'pdos', 'charge_profile',
                                   'volcano'}


def test_analyze_from_injected_adsorption():
    avail = af.analyze_project({'name': 'CoO', 'members': {}}, 'general',
                               adsorption_mod=_FakeAds)
    assert avail['n_delta'] == 2 and avail['has_bar']
    data = avail['_data']['adsorption_bar']
    assert data['adsorbates'] == ['Li2S8', 'Li2S6']
    assert data['substrates']['CoO'] == [-1.6, -0.9]


def test_analyze_no_data_marks_unavailable():
    class _Empty:
        @staticmethod
        def delta_e_rows(p):
            return {'slab': ('未完成', None), 'ref': ('无', None), 'has_ref': False,
                    'rows': []}
    avail = af.analyze_project({'name': 'X'}, 'lis', adsorption_mod=_Empty)
    assert avail['n_delta'] == 0 and not avail['has_bar']
    assert not avail['has_ladder']


# ── run_auto_figures 全链 ─────────────────────────────────────────────────────

def test_run_auto_figures_full_pipeline(tmp_path):
    proj = {'name': 'CoO', 'members': {'clean_slab': '/x/slab', 'configs': []},
            'figure_inputs': _full_inputs()}
    res = af.run_auto_figures(proj, 'lis', str(tmp_path), journal='nature')
    assert res['ok'] and res['error'] is None
    assert res['skipped'] == []
    keys = [m['key'] for m in res['manifest']]
    assert keys == ['adsorption_bar', 'energy_matrix_table', 'free_energy_ladder',
                    'volcano', 'pdos', 'charge_profile']
    assert all(os.path.isfile(f) for f in res['files'])
    # 组合大图(6 张 → 拼版)
    assert res['panel'] and res['panel']['n'] == 6
    assert all(os.path.isfile(f) for f in res['panel']['files'])


def test_run_auto_figures_manifest_provenance(tmp_path):
    proj = {'name': 'CoO', 'members': {'clean_slab': '/x/slab', 'configs': ['/x/c1']},
            'figure_inputs': _full_inputs()}
    res = af.run_auto_figures(proj, 'lis', str(tmp_path))
    m0 = res['manifest'][0]
    assert {'key', 'preset', 'params', 'data_source', 'files', 'timestamp'} <= set(m0)
    assert m0['data_source']['project'] == 'CoO'
    assert '/x/slab' in m0['data_source']['jobs']
    assert m0['files'] and all(os.path.isfile(f) for f in m0['files'])


def test_run_auto_figures_partial_skips_and_no_panel(tmp_path):
    # 仅 PDOS 可用(general 场景):1 张图 → 无需拼版,panel=None
    proj = {'name': 'X', 'figure_inputs': {
        'pdos': {'series': [{'label': 'd', 'energies': _E,
                             'dos_up': [0, 1, 2, 3, 2, 1, 0]}], 'efermi': 0.0}}}
    res = af.run_auto_figures(proj, 'general', str(tmp_path))
    assert res['ok']
    assert [m['key'] for m in res['manifest']] == ['pdos']
    assert res['panel'] is None                      # 单图不拼版
    skipped_keys = {s['key'] for s in res['skipped']}
    assert {'adsorption_bar', 'energy_matrix_table', 'charge_profile'} <= skipped_keys


def test_run_auto_figures_isolates_single_render_failure(tmp_path):
    class _FailVolcano:
        def get_preset(self, k):
            return fp.get_preset(k)

        def preset_provenance(self, *a, **k):
            return fp.preset_provenance(*a, **k)

        def render_preset(self, key, data, out, **p):
            if key == 'volcano':
                raise RuntimeError('boom')
            return fp.render_preset(key, data, out, **p)

    proj = {'name': 'CoO', 'figure_inputs': _full_inputs()}
    res = af.run_auto_figures(proj, 'lis', str(tmp_path),
                              figure_presets_mod=_FailVolcano())
    assert res['ok']
    keys = [m['key'] for m in res['manifest']]
    assert 'volcano' not in keys and 'adsorption_bar' in keys and 'pdos' in keys
    assert any(s['key'] == 'volcano' and '渲染失败' in s['reason']
               for s in res['skipped'])
