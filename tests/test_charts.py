"""charts.py 测试:四种图 SVG 渲染 + 契约构建(纯函数,离线)。"""
import pytest

from vcstudio.project import charts


_BAR = {'rows': ['Zn-Ta'], 'cols': ['S8', 'Li2S8', 'Li2S6'],
        'matrix': [[-4.10, -3.55, -3.02]],
        'band': (-2.90, -1.65), 'band_label': '理想窗口 -2.90~-1.65 eV'}


def _is_svg(s):
    return s.startswith('<svg') and s.endswith('</svg>') and 'http://www.w3.org/2000/svg' in s


# ── 柱状图 ──
def test_bar_svg_basic():
    svg = charts.render_bar_svg(_BAR, title='吸附能')
    assert _is_svg(svg)
    assert '-4.10' in svg and 'S8' in svg           # 柱顶数值 + 类目标签
    assert '理想窗口' in svg                          # 窗口带标签
    assert svg.count('<rect') >= 5                   # 背景+带+3柱


def test_bar_svg_no_band_multi_row_legend():
    data = {'rows': ['A', 'B'], 'cols': ['X'], 'matrix': [[-1.0], [-2.0]], 'band': None}
    svg = charts.render_bar_svg(data)
    assert _is_svg(svg) and '>A<' in svg and '>B<' in svg   # 多体系有图例


def test_bar_svg_empty_raises():
    with pytest.raises(ValueError, match='无可画数据'):
        charts.render_bar_svg({'rows': ['A'], 'cols': ['X'], 'matrix': [[None]], 'band': None})


def test_bar_svg_escapes_labels():
    data = {'rows': ['<b>'], 'cols': ['<script>'], 'matrix': [[-1.0]], 'band': None}
    svg = charts.render_bar_svg(data)
    assert '<script>' not in svg and '&lt;script&gt;' in svg


# ── 热图 ──
def test_heatmap_svg_colors_and_none():
    data = {'rows': ['Co', 'Zn'], 'cols': ['S8', 'Li2S'],
            'matrix': [[-4.5, -0.5], [None, 0.5]]}
    svg = charts.render_heatmap_svg(data)
    assert _is_svg(svg)
    assert '-4.50' in svg and '0.50' in svg
    assert '#f3f4f6' in svg                          # 缺格灰
    assert svg.count('<rect') >= 5


def test_heat_color_semantics():
    strong = charts._heat_color(-5.0)                # 强吸附 → 绿
    weak = charts._heat_color(1.0)                   # 弱/正 → 红
    assert strong == 'rgb(26,152,80)' and weak == 'rgb(215,48,39)'
    mid = charts._heat_color(-2.0)                   # 中间 → 黄系(R,G 都高)
    assert mid.startswith('rgb(')


def test_heatmap_empty_raises():
    with pytest.raises(ValueError):
        charts.render_heatmap_svg({'rows': [], 'cols': [], 'matrix': []})


# ── 阶梯图 ──
_STEPS = [{'label': 'S8*', 'G': 0.0},
          {'label': 'Li2S8*', 'G': -0.85},
          {'label': 'Li2S6*', 'G': -0.60, 'sub_label': '1×Li2S2'},
          {'label': 'Li2S*', 'G': -1.90}]


def test_ladder_data_finds_pds():
    d = charts.ladder_data(_STEPS, u_l=0.25)
    assert d['pds_index'] == 1                       # -0.85→-0.60 是最陡上坡
    assert d['u_l'] == 0.25


def test_ladder_svg():
    svg = charts.render_ladder_svg(charts.ladder_data(_STEPS, u_l=0.25), title='放电路径')
    assert _is_svg(svg)
    assert 'PDS' in svg and '+0.25 eV' in svg        # 决速步标注
    assert 'U_L = 0.25 V' in svg
    assert 'Li2S8*' in svg and '1×Li2S2' in svg


def test_ladder_too_few_steps_raises():
    with pytest.raises(ValueError, match='至少'):
        charts.ladder_data([{'label': 'A', 'G': 0.0}])


# ── 收敛曲线 ──
def test_convergence_svg():
    data = {'x': [300, 400, 500, 600], 'y': [-10.02, -10.21, -10.249, -10.250],
            'xlabel': 'ENCUT (eV)', 'epsilon': 0.005}
    svg = charts.render_convergence_svg(data, title='ENCUT 收敛')
    assert _is_svg(svg)
    assert '收敛于 500' in svg                        # 500 处 |y-ref|<ε 首次达标
    assert 'ENCUT (eV)' in svg and '-10.250' in svg


def test_convergence_mismatched_raises():
    with pytest.raises(ValueError):
        charts.render_convergence_svg({'x': [1, 2], 'y': [1.0], 'epsilon': 0.01})


# ── 契约构建 ──
def test_bar_data_from_delta():
    rows = [{'name': 'ads_S8', 'delta_e': -4.1}, {'name': 'bad', 'delta_e': None}]
    d = charts.bar_data_from_delta('proj', rows, band=(-2.9, -1.65))
    assert d == {'rows': ['proj'], 'cols': ['ads_S8'], 'matrix': [[-4.1]],
                 'band': (-2.9, -1.65), 'band_label': ''}


def test_bar_data_from_delta_all_none_raises():
    with pytest.raises(ValueError, match='ΔE'):
        charts.bar_data_from_delta('p', [{'name': 'a', 'delta_e': None}])


def test_heatmap_data_from_projects_union_cols():
    p1 = ('A', [{'name': 'x', 'delta_e': -1.0}, {'name': 'y', 'delta_e': -2.0}])
    p2 = ('B', [{'name': 'y', 'delta_e': -3.0}, {'name': 'z', 'delta_e': -4.0}])
    d = charts.heatmap_data_from_projects([p1, p2])
    assert d['rows'] == ['A', 'B'] and d['cols'] == ['x', 'y', 'z']
    assert d['matrix'] == [[-1.0, -2.0, None], [None, -3.0, -4.0]]


def test_nice_ticks():
    t = charts._nice_ticks(-4.5, 0.5)
    assert t[0] <= -4.5 and t[-1] >= 0.5
    assert all(t[i] < t[i + 1] for i in range(len(t) - 1))
    assert charts._nice_ticks(1.0, 1.0)              # 退化区间不崩


# ── DOS(C4) ────────────────────────────────────────────────────────────────
def _dos(spin2=False):
    return {'efermi': -2.0,
            'energies': [-12.0, -7.0, -2.0, 2.0],
            'spin_up': [0.0, 1.25, 3.75, 0.5],
            'spin_down': [0.0, 1.10, 3.20, 0.4] if spin2 else None}


def test_render_dos_svg_spin_polarized():
    svg = charts.render_dos_svg(_dos(spin2=True), title='Ta_S8 DOS')
    assert svg.startswith('<svg') and svg.endswith('</svg>')
    assert svg.count('<path') == 2                    # 上/下自旋两条曲线
    assert 'stroke-dasharray' in svg                  # E_F 竖虚线(+网格)
    assert 'spin' in svg                              # 图例
    assert 'DOS' in svg and 'Ta_S8 DOS' in svg


def test_render_dos_svg_single_spin():
    svg = charts.render_dos_svg(_dos())
    assert svg.count('<path') == 1
    assert 'spin' not in svg                          # 单自旋不画图例


def test_render_dos_svg_empty_raises():
    with pytest.raises(ValueError):
        charts.render_dos_svg({'efermi': 0.0, 'energies': [],
                               'spin_up': [], 'spin_down': None})


def test_render_dos_svg_single_point_no_zerodiv():
    # 退化区间(单能量点):两侧扩 1 eV,不得除零(C4 自验探针抓到的真 bug)
    svg = charts.render_dos_svg({'efermi': 0.0, 'energies': [0.0],
                                 'spin_up': [1.0], 'spin_down': None})
    assert svg.startswith('<svg')
