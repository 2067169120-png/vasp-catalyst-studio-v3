"""候选筛选引擎测试:描述符汇总(注入假件/最稳过滤/缺失)、最小二乘对拍、火山图
自动构造(双支 apex/单调 issue/点少报错)、皮尔逊相关对拍、CSV 往返。"""
from __future__ import annotations

import csv

import pytest

from vcstudio.project import screening as sc


# ── fit_scaling 手算对拍 ─────────────────────────────────────────────────────

def test_fit_scaling_exact_line():
    f = sc.fit_scaling([0, 1, 2, 3], [1, 3, 5, 7])   # y = 2x + 1
    assert f['slope'] == pytest.approx(2.0)
    assert f['intercept'] == pytest.approx(1.0)
    assert f['r2'] == pytest.approx(1.0)
    assert f['n'] == 4


def test_fit_scaling_partial_r2_handcheck():
    # 点 (0,0)(1,1)(2,1):mean_x=1, mean_y=2/3
    # sxx=2, sxy=(0-1)(0-2/3)+(1-1)(...)+(2-1)(1-2/3)=2/3+1/3=1 → slope=0.5
    # intercept=2/3-0.5=1/6;残差:yhat=[1/6,2/3,7/6],ss_res=(1/6)^2+(1/3)^2+(-1/6)^2
    f = sc.fit_scaling([0, 1, 2], [0, 1, 1])
    assert f['slope'] == pytest.approx(0.5)
    assert f['intercept'] == pytest.approx(1 / 6)
    ss_res = (1 / 6) ** 2 + (1 / 3) ** 2 + (1 / 6) ** 2
    ss_tot = (0 - 2 / 3) ** 2 + (1 - 2 / 3) ** 2 + (1 - 2 / 3) ** 2
    assert f['r2'] == pytest.approx(1 - ss_res / ss_tot)


def test_fit_scaling_needs_three_points():
    with pytest.raises(ValueError, match='至少需 3'):
        sc.fit_scaling([1, 2], [3, 4])


def test_fit_scaling_constant_x_raises():
    with pytest.raises(ValueError, match='全部相同|区分度'):
        sc.fit_scaling([2, 2, 2], [1, 2, 3])


def test_fit_scaling_length_mismatch():
    with pytest.raises(ValueError, match='长度'):
        sc.fit_scaling([1, 2, 3], [1, 2])


# ── collect_descriptors(注入假件) ──────────────────────────────────────────

class _FakeAds:
    """假 adsorption 模块:注入 load_project / delta_e_rows。"""

    def __init__(self, projects, summaries):
        self._p = projects
        self._s = summaries

    def load_project(self, path):
        return self._p.get(str(path))

    def delta_e_rows(self, project):
        return self._s.get(project['name'], {'rows': []})


def _fake_env():
    projects = {'/p/a': {'name': 'FeN4', 'members': {}},
                '/p/b': {'name': 'CoN4', 'members': {}},
                '/p/bad': None}
    summaries = {
        'FeN4': {'rows': [
            {'species': 'Li2S2', 'delta_e': -1.5, 'is_most_stable': True},
            {'species': 'Li2S2', 'delta_e': -1.0, 'is_most_stable': False},
            {'species': 'S8', 'delta_e': -0.4, 'is_most_stable': True},
            {'species': 'X', 'delta_e': None, 'is_most_stable': False}]},
        'CoN4': {'rows': [
            {'species': 'Li2S2', 'delta_e': -1.8, 'is_most_stable': True}]},
    }
    return _FakeAds(projects, summaries)


def test_collect_descriptors_most_stable_filter():
    fake = _fake_env()
    res = sc.collect_descriptors(
        ['/p/a', '/p/b'], adsorption_mod=fake,
        dos_fn=lambda ctx: {'FeN4': -2.1, 'CoN4': -2.6}.get(ctx['catalyst']),
        icohp_fn=lambda ctx: {'FeN4': -1.5, 'CoN4': -2.0}.get(ctx['catalyst']),
        ul_fn=lambda ctx: {'FeN4': 0.42, 'CoN4': 0.55}.get(ctx['catalyst']))
    rows = {r['catalyst']: r for r in res['rows']}
    # 只收 is_most_stable 行,delta_e=None 行不进
    assert rows['FeN4']['de'] == {'Li2S2': -1.5, 'S8': -0.4}
    assert rows['FeN4']['d_band_center'] == -2.1
    assert rows['FeN4']['icohp_ms'] == -1.5
    assert rows['FeN4']['u_l'] == 0.42


def test_collect_descriptors_missing_project_recorded():
    fake = _fake_env()
    res = sc.collect_descriptors(['/p/bad'], adsorption_mod=fake)
    assert res['rows'] == []
    assert any(m['field'] == 'project' for m in res['missing'])


def test_collect_descriptors_missing_descriptors_not_guessed():
    fake = _fake_env()
    # 只给 dos_fn,icohp/u_l 缺 → None + 记 missing(不猜)
    res = sc.collect_descriptors(
        ['/p/a'], adsorption_mod=fake,
        dos_fn=lambda ctx: -2.1)
    row = res['rows'][0]
    assert row['d_band_center'] == -2.1
    assert row['icohp_ms'] is None and row['u_l'] is None
    fields = {m['field'] for m in res['missing'] if m['catalyst'] == 'FeN4'}
    assert 'icohp_ms' in fields and 'u_l' in fields


def test_collect_descriptors_no_most_stable_de():
    projects = {'/p/x': {'name': 'Empty', 'members': {}}}
    summaries = {'Empty': {'rows': [
        {'species': 'Li2S2', 'delta_e': None, 'is_most_stable': False}]}}
    fake = _FakeAds(projects, summaries)
    res = sc.collect_descriptors(['/p/x'], adsorption_mod=fake)
    assert res['rows'][0]['de'] == {}
    assert any(m['field'] == 'de' for m in res['missing'])


def test_collect_descriptors_provider_exception_degrades():
    fake = _fake_env()

    def boom(ctx):
        raise RuntimeError('provider blew up')

    res = sc.collect_descriptors(['/p/a'], adsorption_mod=fake, dos_fn=boom)
    assert res['rows'][0]['d_band_center'] is None    # 异常降级为 None,不中断


# ── screening_table_csv 往返 ────────────────────────────────────────────────

def test_screening_table_csv_roundtrip(tmp_path):
    data = {'rows': [
        {'catalyst': 'FeN4', 'de': {'Li2S2': -1.23, 'S8': -0.5}, 'dg': {},
         'd_band_center': -2.1, 'icohp_ms': -1.5, 'u_l': 0.42, 'extras': {}},
        {'catalyst': 'CoN4', 'de': {'Li2S2': -1.80}, 'dg': {},
         'd_band_center': None, 'icohp_ms': -2.0, 'u_l': None, 'extras': {}}],
        'missing': []}
    out = str(tmp_path / 'screen.csv')
    sc.screening_table_csv(data, out)
    with open(out, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    assert rows[0]['催化剂'] == 'FeN4'
    assert rows[0]['ΔE(Li2S2)/eV'] == '-1.2300'
    assert rows[0]['d带中心/eV'] == '-2.1000'
    # 缺失值留空(不编数)
    assert rows[1]['d带中心/eV'] == '' and rows[1]['U_L/V'] == ''
    assert rows[1]['ΔE(S8)/eV'] == ''                 # CoN4 无 S8
    # 含来源列
    assert 'ΔE' in rows[0]['数据来源'] and 'U_L' in rows[0]['数据来源']


def test_screening_table_csv_species_union(tmp_path):
    data = {'rows': [
        {'catalyst': 'A', 'de': {'S8': -0.1}, 'dg': {}, 'd_band_center': None,
         'icohp_ms': None, 'u_l': None, 'extras': {}},
        {'catalyst': 'B', 'de': {'Li2S': -2.0}, 'dg': {}, 'd_band_center': None,
         'icohp_ms': None, 'u_l': None, 'extras': {}}], 'missing': []}
    out = str(tmp_path / 's.csv')
    sc.screening_table_csv(data, out)
    with open(out, encoding='utf-8-sig') as f:
        header = next(csv.reader(f))
    assert 'ΔE(Li2S)/eV' in header and 'ΔE(S8)/eV' in header    # 并集


# ── volcano_construct ────────────────────────────────────────────────────────

def _rows_xy(xs, ys, xkey='d_band_center', ykey='u_l'):
    return [{'catalyst': f'C{i}', 'de': {}, xkey: x, ykey: y}
            for i, (x, y) in enumerate(zip(xs, ys))]


def test_volcano_two_branch_apex():
    rows = _rows_xy([-3, -2, -1, 0, 1, 2, 3], [1, 2, 3, 4, 3, 2, 1])
    v = sc.volcano_construct(rows, descriptor_key='d_band_center')
    assert len(v['legs']) == 2
    assert v['apex']['x'] == pytest.approx(0.0)
    assert v['apex']['y'] == pytest.approx(4.0)
    assert v['legs'][0]['slope'] == pytest.approx(1.0)
    assert v['legs'][1]['slope'] == pytest.approx(-1.0)
    assert v['quality']['ok'] and not v['quality']['issues']


def test_volcano_monotonic_reports_issue():
    rows = _rows_xy([-3, -2, -1, 0, 1, 2, 3], [1, 2, 3, 4, 5, 6, 7])
    v = sc.volcano_construct(rows, descriptor_key='d_band_center')
    assert v['apex'] is None
    assert not v['quality']['ok']
    assert any('未见火山形' in s for s in v['quality']['issues'])
    assert v['legs'][0]['side'] == 'single'


def test_volcano_too_few_points_raises():
    rows = _rows_xy([-1, 0], [1, 2])
    with pytest.raises(ValueError, match='至少需 3'):
        sc.volcano_construct(rows, descriptor_key='d_band_center')


def test_volcano_weak_scaling_issue():
    # 双支各 4 点但含噪 → 至少一支 R²<0.6
    rows = _rows_xy([-3, -2, -1, 0, 1, 2, 3], [1, 3, 1, 4, 1, 3, 0])
    v = sc.volcano_construct(rows, descriptor_key='d_band_center')
    assert any('解释力有限' in s or '标度关系弱' in s
               for s in v['quality']['issues'])


def test_volcano_explicit_numeric_split():
    rows = _rows_xy([-3, -2, -1, 1, 2, 3], [1, 2, 3, 3, 2, 1])
    v = sc.volcano_construct(rows, descriptor_key='d_band_center', split=0.0)
    assert len(v['legs']) == 2
    assert v['legs'][0]['side'] == 'left' and v['legs'][1]['side'] == 'right'


def test_volcano_explicit_name_split():
    rows = _rows_xy([-3, -2, -1, 0, 1, 2, 3], [1, 2, 3, 4, 3, 2, 1])
    # C3 是 x=0 峰点
    v = sc.volcano_construct(rows, descriptor_key='d_band_center', split='C3')
    assert v['apex']['x'] == pytest.approx(0.0)


def test_volcano_bad_name_split_raises():
    rows = _rows_xy([-3, -2, -1, 0, 1, 2, 3], [1, 2, 3, 4, 3, 2, 1])
    with pytest.raises(ValueError, match='split'):
        sc.volcano_construct(rows, descriptor_key='d_band_center', split='NoSuch')


def test_volcano_parallel_legs_no_apex():
    # 全局线性,强制 x=0 分割 → 两支斜率相等(平行)
    rows = _rows_xy([-3, -2, -1, 1, 2, 3], [-3, -2, -1, 1, 2, 3])
    v = sc.volcano_construct(rows, descriptor_key='d_band_center', split=0.0)
    assert v['apex'] is None
    assert any('平行' in s for s in v['quality']['issues'])


def test_volcano_valley_not_peak_flagged():
    # 谷形(左降右升),显式分割在谷底 → 交点为谷,应告警
    rows = _rows_xy([-3, -2, -1, 0, 1, 2, 3], [4, 3, 2, 1, 2, 3, 4])
    v = sc.volcano_construct(rows, descriptor_key='d_band_center', split=0.0)
    assert v['apex'] is not None
    assert any('谷' in s or '峰形' in s for s in v['quality']['issues'])


def test_volcano_de_descriptor_key():
    rows = [{'catalyst': f'C{i}', 'de': {'Li2S2': x}, 'u_l': y}
            for i, (x, y) in enumerate(
                zip([-3, -2, -1, 0, 1, 2, 3], [1, 2, 3, 4, 3, 2, 1]))]
    v = sc.volcano_construct(rows, descriptor_key='de:Li2S2')
    assert v['apex']['x'] == pytest.approx(0.0)


def test_volcano_output_feeds_native_charts(tmp_path):
    pytest.importorskip('matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    from vcstudio.external import native_charts as nc
    rows = _rows_xy([-3, -2, -1, 0, 1, 2, 3], [1, 2, 3, 4, 3, 2, 1])
    v = sc.volcano_construct(rows, descriptor_key='d_band_center')
    paths = nc.volcano_plot(v['points'], str(tmp_path / 'volcano'),
                            descriptor_label='d-band center (eV)',
                            activity_label=r'$U_L$ (V)', legs=v['legs'])
    assert all(__import__('os').path.getsize(p) > 1000 for p in paths)


# ── descriptor_correlation 手算对拍 ─────────────────────────────────────────

def test_descriptor_correlation_perfect():
    rows = [{'catalyst': 'a', 'de': {'Li2S2': -1.0}, 'd_band_center': -2.0, 'u_l': 0.5},
            {'catalyst': 'b', 'de': {'Li2S2': -2.0}, 'd_band_center': -3.0, 'u_l': 0.6},
            {'catalyst': 'c', 'de': {'Li2S2': -3.0}, 'd_band_center': -4.0, 'u_l': 0.7}]
    corr = {c['pair']: c for c in
            sc.descriptor_correlation(rows, ['d_band_center', 'de:Li2S2', 'u_l'])}
    assert corr[('d_band_center', 'de:Li2S2')]['r'] == pytest.approx(1.0)
    assert corr[('d_band_center', 'de:Li2S2')]['n'] == 3
    assert corr[('d_band_center', 'u_l')]['r'] == pytest.approx(-1.0)


def test_descriptor_correlation_handcheck_value():
    # x=[1,2,3], y=[2,1,4]:mx=2,my=7/3
    # sxx=2, syy=(2-7/3)^2+(1-7/3)^2+(4-7/3)^2 = 1/9+16/9+25/9=42/9
    # sxy=(1-2)(2-7/3)+(2-2)(...)+(3-2)(4-7/3)=1/3+5/3=2 → r=2/sqrt(2*42/9)
    import math
    rows = [{'catalyst': 'a', 'd_band_center': 1.0, 'u_l': 2.0},
            {'catalyst': 'b', 'd_band_center': 2.0, 'u_l': 1.0},
            {'catalyst': 'c', 'd_band_center': 3.0, 'u_l': 4.0}]
    corr = sc.descriptor_correlation(rows, ['d_band_center', 'u_l'])
    expect = 2.0 / math.sqrt(2 * (42 / 9))
    assert corr[0]['r'] == pytest.approx(round(expect, 4))


def test_descriptor_correlation_insufficient_n():
    rows = [{'catalyst': 'a', 'd_band_center': 1.0, 'u_l': 2.0},
            {'catalyst': 'b', 'd_band_center': 2.0, 'u_l': 1.0}]      # n=2
    corr = sc.descriptor_correlation(rows, ['d_band_center', 'u_l'])
    assert corr[0]['r'] is None and corr[0]['n'] == 2


def test_descriptor_correlation_zero_variance():
    rows = [{'catalyst': 'a', 'd_band_center': 2.0, 'u_l': 1.0},
            {'catalyst': 'b', 'd_band_center': 2.0, 'u_l': 2.0},
            {'catalyst': 'c', 'd_band_center': 2.0, 'u_l': 3.0}]       # x 常数
    corr = sc.descriptor_correlation(rows, ['d_band_center', 'u_l'])
    assert corr[0]['r'] is None
