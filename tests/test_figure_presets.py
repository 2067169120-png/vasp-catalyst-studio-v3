"""figure_presets 图表预设注册表测试:12+ 预设 schema 完备/renderer 有效/SVG 良构、
list 过滤与轻量化、get_preset 完整版、render_preset 分发(假 renderer + 真 matplotlib)、
数据契约校验、todo 路径、参数覆盖、preset_provenance 溯源工件。
"""
from __future__ import annotations

import xml.dom.minidom as minidom
import re

import pytest

from vcstudio.project import figure_presets as fp

_VALID_CATS = {'能量学', '电子结构', '结构', '电池'}


# ── 注册表完备性 ──────────────────────────────────────────────────────────────

def test_at_least_12_presets():
    assert len(fp.PRESETS) >= 12


def test_every_preset_schema_complete():
    required = {'key', 'name', 'category', 'description', 'required_data',
                'name_en', 'category_en', 'description_en', 'required_data_en',
                'thumbnail_svg', 'renderer', 'params_schema'}
    for p in fp.PRESETS:
        assert required <= set(p), (p.get('key'), required - set(p))
        assert p['category'] in _VALID_CATS, (p['key'], p['category'])
        assert isinstance(p['params_schema'], dict)
        assert isinstance(p['name'], str) and p['name']
        assert isinstance(p['required_data'], str) and p['required_data']
        for field in ('name_en', 'category_en', 'description_en', 'required_data_en'):
            assert isinstance(p[field], str) and p[field].strip(), (p['key'], field)
            assert not re.search(r'[\u3400-\u9fff]', p[field]), (p['key'], field)
        assert p['category_en'] == fp.CATEGORY_EN[p['category']]


def test_preset_keys_unique():
    keys = [p['key'] for p in fp.PRESETS]
    assert len(keys) == len(set(keys))


def test_renderer_names_valid():
    for p in fp.PRESETS:
        assert p['renderer'] == 'todo' or p['renderer'] in fp._RENDERER_TARGETS, p['renderer']


def test_thumbnail_svg_wellformed_80x60():
    for p in fp.PRESETS:
        svg = p['thumbnail_svg']
        assert svg.startswith('<svg') and svg.rstrip().endswith('</svg>')
        assert 'width="80"' in svg and 'height="60"' in svg
        minidom.parseString(svg)                    # 良构 XML(解析失败即抛)


def test_categories_cover_four_domains():
    cats = set(fp.categories())
    assert cats == _VALID_CATS                       # 四大分类全覆盖
    # 预期核心图型都在册
    keys = {p['key'] for p in fp.PRESETS}
    for k in ('adsorption_bar', 'delta_e_heatmap', 'scaling_relation', 'volcano',
              'free_energy_ladder', 'free_energy_ladder_multi', 'pdos', 'cohp',
              'neb_profile', 'charge_profile', 'convergence_curve',
              'energy_matrix_table'):
        assert k in keys, k


def test_has_todo_placeholder_preset():
    todo = [p for p in fp.PRESETS if p['renderer'] == 'todo']
    assert len(todo) >= 1
    assert todo[0]['key'] == 'convergence_curve'


# ── list_presets / get_preset ────────────────────────────────────────────────

def test_list_presets_light_without_svg_with_params():
    lst = fp.list_presets()
    assert len(lst) == len(fp.PRESETS)
    for item in lst:
        assert 'thumbnail_svg' not in item           # 轻量:不含 svg
        assert 'params_schema' in item               # 但含参数开关


def test_list_presets_category_filter():
    energetics = fp.list_presets('能量学')
    assert all(p['category'] == '能量学' for p in energetics)
    assert {p['key'] for p in energetics} >= {'adsorption_bar', 'delta_e_heatmap',
                                              'scaling_relation', 'volcano'}
    assert fp.list_presets('不存在的分类') == []


def test_list_presets_returns_copies():
    fp.list_presets()[0]['name'] = 'MUTATED'
    assert all(p['name'] != 'MUTATED' for p in fp.PRESETS)   # 深拷贝,改不动本体


def test_get_preset_full_with_svg():
    p = fp.get_preset('volcano')
    assert 'thumbnail_svg' in p and p['renderer'] == 'volcano_plot'


def test_get_preset_unknown_raises_keyerror():
    with pytest.raises(KeyError, match='未知图表预设'):
        fp.get_preset('nope')


# ── render_preset 分发(注入假 renderer,无需 matplotlib)─────────────────────

class _Spy:
    def __init__(self):
        self.args = None
        self.kw = None

    def __call__(self, *args, **kw):
        self.args, self.kw = args, kw
        return ['SPY.png', 'SPY.pdf']


def test_render_dispatch_dict_style(tmp_path):
    spy = _Spy()
    data = {'adsorbates': ['S8'], 'substrates': {'CoO': [-1.2]}}
    out = fp.render_preset('adsorption_bar', data, tmp_path / 'x', renderer_fn=spy)
    assert out == ['SPY.png', 'SPY.pdf']
    assert spy.args[0] == data                       # data 整体作首参
    assert spy.kw['palette'] == 'npg'                # params_schema 默认注入


def test_render_dispatch_scaling_splits_xs_ys(tmp_path):
    spy = _Spy()
    fp.render_preset('scaling_relation', {'xs': [1, 2, 3], 'ys': [2, 4, 6],
                                          'labels': ['a', 'b', 'c']},
                     tmp_path / 'x', renderer_fn=spy)
    assert spy.args[0] == [1, 2, 3] and spy.args[1] == [2, 4, 6]   # xs/ys 拆成位置参
    assert spy.kw['labels'] == ['a', 'b', 'c']       # labels 走关键字


def test_render_dispatch_volcano_legs_passthrough(tmp_path):
    spy = _Spy()
    fp.render_preset('volcano', {'points': [{'name': 'A', 'x': 1, 'y': 2}],
                                 'legs': [{'slope': 1, 'intercept': 0}]},
                     tmp_path / 'x', renderer_fn=spy)
    assert spy.kw['legs'] == [{'slope': 1, 'intercept': 0}]
    assert 'activity_label' in spy.kw                # 必填标签走 schema 默认


def test_render_dispatch_ladder_forwards_pds_index(tmp_path):
    spy = _Spy()
    fp.render_preset('free_energy_ladder',
                     {'paths': {'name': 'CoO', 'G': [0.0, -0.4]}, 'pds_index': 0,
                      'step_labels': ['S8', 'Li2S8']},
                     tmp_path / 'x', renderer_fn=spy)
    assert spy.kw['pds_index'] == 0                  # 逐电子决速步须透传
    assert spy.kw['step_labels'] == ['S8', 'Li2S8']


def test_render_params_override_schema_default(tmp_path):
    spy = _Spy()
    fp.render_preset('adsorption_bar', {'adsorbates': ['S8'], 'substrates': {'CoO': [-1.2]}},
                     tmp_path / 'x', renderer_fn=spy, palette='okabe_ito', title='T')
    assert spy.kw['palette'] == 'okabe_ito'          # 显式参数覆盖默认
    assert spy.kw['title'] == 'T'


# ── 数据契约校验 ──────────────────────────────────────────────────────────────

def test_contract_missing_key_raises_chinese(tmp_path):
    spy = _Spy()
    with pytest.raises(ValueError, match='缺少'):
        fp.render_preset('adsorption_bar', {'adsorbates': ['S8']}, tmp_path / 'x',
                         renderer_fn=spy)
    assert spy.args is None                           # 校验失败 → 未触及渲染


def test_contract_empty_list_raises(tmp_path):
    with pytest.raises(ValueError, match='ΔE 热图'):
        fp.render_preset('delta_e_heatmap', {'rows': [], 'cols': ['a'], 'values': [[1]]},
                         tmp_path / 'x', renderer_fn=_Spy())


def test_contract_non_dict_raises(tmp_path):
    with pytest.raises(ValueError, match='须为 dict'):
        fp.render_preset('adsorption_bar', ['not', 'a', 'dict'], tmp_path / 'x',
                         renderer_fn=_Spy())


# ── todo 路径 ─────────────────────────────────────────────────────────────────

def test_todo_renderer_raises_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError, match='后续版本提供'):
        fp.render_preset('convergence_curve', {'anything': 1}, tmp_path / 'x')


# ── 溯源工件 ──────────────────────────────────────────────────────────────────

def test_preset_provenance_structure():
    prov = fp.preset_provenance('volcano', {'project': 'p1', 'source': 'screening'},
                                params={'title': 'V'})
    assert prov['preset'] == 'volcano'
    assert prov['renderer'] == 'volcano_plot'
    assert prov['data_source'] == {'project': 'p1', 'source': 'screening'}
    assert prov['params']['title'] == 'V'            # 覆盖后的参数
    assert prov['params']['mark_top'] is True        # schema 默认保留
    assert prov['category'] == '能量学'


def test_preset_provenance_defaults_when_no_params():
    prov = fp.preset_provenance('pdos', {'project': 'p2'})
    assert prov['params']['mirror_spin'] is True     # 全走 schema 默认
    assert prov['renderer'] == 'pdos_plot'


# ── 真实渲染(matplotlib 集成:分发真落到 native_charts,产出文件)─────────────

def test_render_real_adsorption_bar_creates_files(tmp_path):
    data = {'adsorbates': ['S8', 'Li2S8'],
            'substrates': {'CoO': [-1.2, -1.6], 'Co9S8': [-0.8, -1.1]}}
    out = fp.render_preset('adsorption_bar', data, tmp_path / 'bar')
    assert len(out) == 2                             # png + pdf
    import os
    assert all(os.path.isfile(p) for p in out)
    assert out[0].endswith('.png') and out[1].endswith('.pdf')


def test_render_real_energy_matrix_table_creates_csv(tmp_path):
    data = {'adsorbates': ['S8', 'Li2S8'],
            'substrates': {'CoO': [-1.2, -1.6]}, 'dg': {'CoO': [0.1, -0.2]}}
    out = fp.render_preset('energy_matrix_table', data, tmp_path / 'tab')
    import os
    assert any(p.endswith('.csv') for p in out)      # 三线表附带 CSV
    assert all(os.path.isfile(p) for p in out)


def test_render_real_ladder_dispatches_to_native_charts(tmp_path):
    out = fp.render_preset('free_energy_ladder',
                           {'paths': {'name': 'CoO', 'G': [0.0, -0.4, 0.3, -0.2]},
                            'pds_index': 1}, tmp_path / 'fed')
    import os
    assert len(out) == 2 and all(os.path.isfile(p) for p in out)
