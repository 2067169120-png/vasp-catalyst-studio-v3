"""原生 matplotlib 论文级出图引擎测试(Agg 后端,CI 无显示环境可跑)。

覆盖:三个出图函数的文件产出(PNG 魔数/PDF 魔数/非空)、CSV 内容、风格上下文
进出恢复、化学式下标、数据契约校验、以及"顶层导入不拖 matplotlib"的延迟依赖防线。
"""
from __future__ import annotations

import os
import subprocess
import sys

import matplotlib

matplotlib.use('Agg')                      # 必须在任何 pyplot 之前;无显示环境防线

import numpy as _numpy  # noqa: E402  见 _heal_numpy_sys_modules
import pytest  # noqa: E402

from vcstudio.external import native_charts as nc  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    """修复全套跑时的 sys.modules 污染:test_kpoints 的延迟依赖测试会 pop 'numpy'
    但留下全部 numpy.* 子模块——之后任何 fresh `import numpy`(matplotlib 内部懒加载)
    都会撞 numpy.__getattr__ → numpy.ma 的 RecursionError。把收集期保存的同一模块
    对象放回去即可(不重新执行 numpy,零副作用)。"""
    sys.modules.setdefault('numpy', _numpy)
    yield

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BAR_DATA = {
    'adsorbates': ['S8', 'Li2S8', 'Li2S4', 'Li2S'],
    'substrates': {
        'CoO':   [-0.82, -1.35, -2.10, -2.95],
        'Co9S8': [-0.55, -1.02, None, -2.31],      # 含缺值
    },
}

LADDER_PATHS = [
    {'name': 'CoO',   'G': [0.00, -0.31, -0.78, -0.42, -1.10]},   # 第 3 步上坡 → PDS
    {'name': 'Co9S8', 'G': [0.00, -0.20, -0.55, -0.90, -1.35]},   # 全下坡 → 无 PDS
]


def _assert_png(path):
    assert os.path.isfile(path), path
    with open(path, 'rb') as f:
        head = f.read(8)
    assert head == b'\x89PNG\r\n\x1a\n', f'{path} 不是合法 PNG'
    assert os.path.getsize(path) > 1000


def _assert_pdf(path):
    assert os.path.isfile(path), path
    with open(path, 'rb') as f:
        head = f.read(5)
    assert head == b'%PDF-', f'{path} 不是合法 PDF'
    assert os.path.getsize(path) > 500


# ── 风格层 ──────────────────────────────────────────────────────────────────

def test_paper_rc_keys():
    rc = nc.paper_rc()
    assert rc['xtick.direction'] == 'in'
    assert rc['savefig.dpi'] == 300
    assert rc['pdf.fonttype'] == 42
    assert rc['axes.spines.top'] is False       # Nature 风默认去顶右边框
    assert nc.paper_rc(box=True)['axes.spines.top'] is True
    assert nc.paper_rc(font='sans')['font.family'] == 'sans-serif'


def test_apply_paper_style_restores_rcparams():
    before = matplotlib.rcParams['xtick.direction']
    with nc.apply_paper_style():
        assert matplotlib.rcParams['xtick.direction'] == 'in'
        assert matplotlib.rcParams['savefig.dpi'] == 300
    assert matplotlib.rcParams['xtick.direction'] == before


def test_chem_label():
    assert nc.chem_label('Li2S8') == 'Li$_{2}$S$_{8}$'
    assert nc.chem_label('Co9S8') == 'Co$_{9}$S$_{8}$'
    assert nc.chem_label('NC') == 'NC'
    assert nc.chem_label('E$_x$') == 'E$_x$'    # 已含 mathtext 不重复处理


def test_lazy_import_no_matplotlib():
    """硬约束防线:顶层 import 本模块绝不能拖进 matplotlib/numpy(守 EXE 体积)。"""
    code = ('import sys; import vcstudio.external.native_charts; '
            "bad = [m for m in ('matplotlib', 'numpy') if m in sys.modules]; "
            'sys.exit(1 if bad else 0)')
    env = dict(os.environ, PYTHONPATH=_REPO_ROOT)
    proc = subprocess.run([sys.executable, '-c', code], env=env,
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode(errors='replace')


# ── 吸附能分组柱状图 ────────────────────────────────────────────────────────

def test_adsorption_bar_outputs(tmp_path):
    out = nc.adsorption_bar(BAR_DATA, tmp_path / 'ads_bar', panel='a')
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_adsorption_bar_negative_up_and_dg(tmp_path):
    out = nc.adsorption_bar(BAR_DATA, tmp_path / 'ads_bar_g.png',
                            negative_up=True,
                            ylabel=r'$\Delta G_\mathrm{ads}$ (eV)',
                            palette='tol_bright')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_adsorption_bar_bad_data():
    with pytest.raises(ValueError):
        nc.adsorption_bar({'adsorbates': [], 'substrates': {}}, 'x')
    with pytest.raises(ValueError):
        nc.adsorption_bar({'adsorbates': ['S8'], 'substrates': {'CoO': [1.0, 2.0]}}, 'x')


# ── 能量数据表 ──────────────────────────────────────────────────────────────

def test_energy_matrix_table_outputs(tmp_path):
    data = dict(BAR_DATA, dg={'CoO': [-0.61, -1.02, -1.66, -2.20]})
    out = nc.energy_matrix_table(data, tmp_path / 'tab', title='Table 3-2')
    assert len(out) == 3
    _assert_png(out[0])
    _assert_pdf(out[1])
    csv_path = out[2]
    assert csv_path.endswith('.csv')
    text = open(csv_path, encoding='utf-8-sig').read()
    assert 'Adsorbate' in text and 'CoO' in text and 'Co9S8' in text
    assert '-2.95' in text and '-0.61' in text   # ΔE 与 ΔG 值都落表
    assert '--' in text                          # None → 占位符


def test_energy_matrix_table_dg_validation():
    with pytest.raises(ValueError):
        nc.energy_matrix_table(dict(BAR_DATA, dg={'Ghost': [0] * 4}), 'x')
    with pytest.raises(ValueError):
        nc.energy_matrix_table(dict(BAR_DATA, dg={'CoO': [0.0]}), 'x')


# ── 自由能台阶图 ────────────────────────────────────────────────────────────

def test_free_energy_ladder_outputs(tmp_path):
    out = nc.free_energy_ladder(
        LADDER_PATHS, tmp_path / 'ladder',
        step_labels=['S8', 'Li2S8', 'Li2S6', 'Li2S4', 'Li2S'],
        show_ul=True, panel='c')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_free_energy_ladder_single_dict(tmp_path):
    out = nc.free_energy_ladder({'name': 'CoO', 'G': [0.0, 0.4, -0.2]},
                                tmp_path / 'one.png', mark_pds=True)
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_free_energy_ladder_bad_data():
    with pytest.raises(ValueError):
        nc.free_energy_ladder([], 'x')
    with pytest.raises(ValueError):
        nc.free_energy_ladder({'name': 'p', 'G': [0.0]}, 'x')


# ── 矩阵热图 ────────────────────────────────────────────────────────────────

HEAT_DATA = {
    'rows': ['CoO', 'Co9S8', 'Graphene'],
    'cols': ['S8', 'Li2S8', 'Li2S4', 'Li2S'],
    'values': [
        [-0.86, -1.61, -2.06, -3.01],
        [-0.90, -1.23, -1.18, -1.98],
        [-0.79, -0.62, None, -0.36],           # 含缺值 → 空白格
    ],
}


def test_heatmap_matrix_outputs(tmp_path):
    out = nc.heatmap_matrix(HEAT_DATA, tmp_path / 'heat', panel='a',
                            cbar_label=r'$E_\mathrm{ads}$ (eV)')
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_heatmap_matrix_no_annotate_custom_range(tmp_path):
    out = nc.heatmap_matrix(HEAT_DATA, tmp_path / 'heat2.png', annotate=False,
                            cmap='viridis', vmin=-3.5, vmax=0.0,
                            title='LiPS anchoring')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_heatmap_matrix_bad_data():
    with pytest.raises(ValueError):                       # rows 空
        nc.heatmap_matrix({'rows': [], 'cols': ['S8'], 'values': []}, 'x')
    with pytest.raises(ValueError):                       # 行数不齐
        nc.heatmap_matrix({'rows': ['CoO'], 'cols': ['S8'], 'values': []}, 'x')
    with pytest.raises(ValueError):                       # 列数不齐
        nc.heatmap_matrix({'rows': ['CoO'], 'cols': ['S8'],
                           'values': [[1.0, 2.0]]}, 'x')
    with pytest.raises(ValueError):                       # 全 None 无色标范围
        nc.heatmap_matrix({'rows': ['CoO'], 'cols': ['S8'],
                           'values': [[None]]}, 'x')


# ── 火山图 ──────────────────────────────────────────────────────────────────

VOLCANO_POINTS = [
    {'name': 'V@N4',  'x': 0.95, 'y': 0.05},
    {'name': 'Fe@N4', 'x': 1.62, 'y': 0.33},
    {'name': 'Co@N4', 'x': 1.90, 'y': 0.35},
    {'name': 'Ni@N4', 'x': 2.28, 'y': 0.16},
]
VOLCANO_LEGS = [
    {'slope': 0.42,  'intercept': -0.33, 'label': 'Li2S2* formation'},
    {'slope': -0.50, 'intercept': 1.32,  'label': 'Li2S2* reduction'},
]


def test_volcano_plot_with_legs(tmp_path):
    out = nc.volcano_plot(
        VOLCANO_POINTS, tmp_path / 'vol', legs=VOLCANO_LEGS, panel='b',
        descriptor_label=r'$-\Delta G_\mathrm{ads}$(*LiS$_2$) (eV)',
        activity_label=r'$U_\mathrm{L}$ (V)',
        side_labels=('Weak adsorption', 'Strong adsorption'))
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_volcano_plot_scatter_quadratic_fit(tmp_path):
    """无 legs:散点 + numpy 二次拟合引导线(开口向下 → 画星)。"""
    out = nc.volcano_plot(VOLCANO_POINTS, tmp_path / 'vol2.png',
                          descriptor_label='descriptor (eV)',
                          activity_label='activity (V)')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_volcano_plot_parallel_legs_no_apex(tmp_path):
    legs = [{'slope': 0.5, 'intercept': 0.0, 'label': 'leg 1'},
            {'slope': 0.5, 'intercept': 1.0, 'label': 'leg 2'}]
    out = nc.volcano_plot(VOLCANO_POINTS, tmp_path / 'vol3', legs=legs,
                          descriptor_label='d', activity_label='a')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_volcano_plot_single_point_dict(tmp_path):
    out = nc.volcano_plot({'name': 'Fe@N4', 'x': 1.0, 'y': 0.2},
                          tmp_path / 'vol4',
                          descriptor_label='d', activity_label='a')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_volcano_plot_bad_data():
    with pytest.raises(ValueError):                       # points 空
        nc.volcano_plot([], 'x', descriptor_label='d', activity_label='a')
    with pytest.raises(ValueError):                       # 缺 x/y
        nc.volcano_plot([{'name': 'Fe'}], 'x',
                        descriptor_label='d', activity_label='a')
    with pytest.raises(ValueError):                       # 腿缺 intercept
        nc.volcano_plot(VOLCANO_POINTS, 'x', descriptor_label='d',
                        activity_label='a', legs=[{'slope': 1.0}])


# ── 标度关系 ────────────────────────────────────────────────────────────────

def test_scaling_relation_outputs(tmp_path):
    xs = [-0.53, -1.07, -1.79, -1.11, -1.63]
    ys = [-0.48, -1.01, -2.06, -1.18, -1.88]
    out = nc.scaling_relation(
        xs, ys, tmp_path / 'scal', panel='c',
        xlabel=r'$E_\mathrm{ads}$(Li$_2$S$_6$) (eV)',
        ylabel=r'$E_\mathrm{ads}$(Li$_2$S$_4$) (eV)',
        labels=['Graphene', 'NG', 'CoO', 'Co9S8', 'Co3O4'])
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_scaling_relation_negative_slope(tmp_path):
    """负斜率:拟合式标注走右上角分支。"""
    out = nc.scaling_relation([0.0, 1.0, 2.0, 3.0], [2.0, 1.4, 0.9, 0.1],
                              tmp_path / 'scal2', xlabel='x', ylabel='y')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_scaling_relation_no_fit(tmp_path):
    out = nc.scaling_relation([0.0, 1.0, 2.0], [0.1, 0.9, 2.2],
                              tmp_path / 'scal3.png', xlabel='x', ylabel='y',
                              fit=False)
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_scaling_relation_bad_data():
    with pytest.raises(ValueError):                       # 点数 < 2
        nc.scaling_relation([1.0], [1.0], 'x', xlabel='x', ylabel='y')
    with pytest.raises(ValueError):                       # xs/ys 长度不齐
        nc.scaling_relation([1.0, 2.0], [1.0], 'x', xlabel='x', ylabel='y')
    with pytest.raises(ValueError):                       # labels 长度不齐
        nc.scaling_relation([1.0, 2.0], [1.0, 2.0], 'x', xlabel='x',
                            ylabel='y', labels=['a'])
    with pytest.raises(ValueError):                       # xs 全同,无法线性拟合
        nc.scaling_relation([1.0, 1.0], [1.0, 2.0], 'x', xlabel='x', ylabel='y')


# ── 导出格式扩展 ────────────────────────────────────────────────────────────

def test_svg_export(tmp_path):
    out = nc.adsorption_bar(BAR_DATA, tmp_path / 'ads', formats=('png', 'pdf', 'svg'))
    assert len(out) == 3
    svg = out[2]
    assert svg.endswith('.svg') and os.path.getsize(svg) > 500
    head = open(svg, encoding='utf-8').read(300)
    assert '<svg' in head or '<?xml' in head
