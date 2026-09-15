"""slab_builder 测试:分层冻结 / 真空层 / 层数 / 周期最小镜像间距(纯函数,离线)。"""
import pytest

from vcstudio.generate import slab_builder

# 4 层 Cu slab(c=20 Å,原子 z=2/4/6/8,层间距 2>0.5 → 各自一层),Cartesian。
_SLAB = """slab
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu
4
Cartesian
0.0 0.0 2.0
1.5 1.5 4.0
0.0 0.0 6.0
1.5 1.5 8.0
"""

# 同结构但已带 Selective dynamics(全 T)。
_SLAB_SD = """slab
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu
4
Selective dynamics
Cartesian
0.0 0.0 2.0 T T T
1.5 1.5 4.0 T T T
0.0 0.0 6.0 T T T
1.5 1.5 8.0 T T T
"""


def _flag_lines(text):
    return [ln for ln in text.splitlines() if 'F F F' in ln or 'T T T' in ln]


def test_count_layers_and_vacuum():
    assert slab_builder.count_layers(_SLAB) == 4
    # 真空 = |c| − (z_max − z_min) = 20 − (8 − 2) = 14
    assert slab_builder.vacuum_thickness(_SLAB) == pytest.approx(14.0)


def test_fix_bottom_layers_flags_bottom_fixed():
    out = slab_builder.fix_bottom_layers(_SLAB, 2)
    assert out.splitlines().count('Selective dynamics') == 1     # 恰一行,不重复
    fl = _flag_lines(out)
    assert len(fl) == 4
    assert fl[0].endswith('F F F') and fl[1].endswith('F F F')   # z=2/4 底两层冻结
    assert fl[2].endswith('T T T') and fl[3].endswith('T T T')   # z=6/8 放开
    # 只动标志:重解析后层数/真空/坐标不变
    assert slab_builder.count_layers(out) == 4
    assert slab_builder.vacuum_thickness(out) == pytest.approx(14.0)


def test_fix_bottom_layers_direct_mode_preserves_frac_coords():
    # Direct(分数坐标)slab:z=0.1/0.2/0.3(×c=20 → 2/4/6 Å,间距 2>0.5 → 3 层)
    direct = ('d\n1.0\n3 0 0\n0 3 0\n0 0 20\nC\n3\nDirect\n'
              '0.0 0.0 0.1\n0.5 0.5 0.2\n0.0 0.0 0.3\n')
    out = slab_builder.fix_bottom_layers(direct, 1)
    assert 'Direct' in out.splitlines()                          # 模式行保留
    fl = _flag_lines(out)
    assert fl[0].startswith('  0.0 0.0 0.1') and fl[0].endswith('F F F')  # 分数坐标原样
    assert fl[1].endswith('T T T') and fl[2].endswith('T T T')
    assert slab_builder.count_layers(out) == 3                   # 结构不变


def test_fix_bottom_layers_only_flips_existing_sd():
    out = slab_builder.fix_bottom_layers(_SLAB_SD, 1)             # 只冻最底 1 层
    assert out.splitlines().count('Selective dynamics') == 1     # 原有的不重复
    fl = _flag_lines(out)
    assert fl[0].endswith('F F F')                               # z=2 冻结
    assert all(ln.endswith('T T T') for ln in fl[1:])            # 其余放开


def test_fix_bottom_layers_bad_n_raises():
    with pytest.raises(ValueError, match='正整数'):
        slab_builder.fix_bottom_layers(_SLAB, 0)
    with pytest.raises(ValueError, match='总层数'):
        slab_builder.fix_bottom_layers(_SLAB, 4)                 # = 总层数 → 冻整块
    with pytest.raises(ValueError, match='总层数'):
        slab_builder.fix_bottom_layers(_SLAB, 9)


def test_layer_clustering_tolerance():
    # 两组各 2 原子,组内 z 差 0.3<0.5 同层,组间差 5 → 2 层
    txt = ('t\n1.0\n5 0 0\n0 5 0\n0 0 20\nC\n4\nCartesian\n'
           '0 0 2.0\n0 0 2.3\n0 0 7.0\n0 0 7.3\n')
    assert slab_builder.count_layers(txt) == 2


def test_min_interatomic_distance_pair():
    txt = ('pair\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n2\nCartesian\n0 0 0\n0 0 0.9\n')
    assert slab_builder.min_interatomic_distance(txt) == pytest.approx(0.9)


def test_min_interatomic_distance_across_periodic_boundary():
    # 两原子贴 a 边两端(frac 0.02 与 0.98),真正最近的是跨边界镜像 → 0.2 Å
    txt = ('pbc\n1.0\n5 0 0\n0 5 0\n0 0 5\nH\n2\nDirect\n0.02 0 0\n0.98 0 0\n')
    assert slab_builder.min_interatomic_distance(txt) == pytest.approx(0.2, abs=1e-6)


def test_min_interatomic_distance_nonorthogonal_needs_neighbor_search():
    """强斜胞:纯 wrap 到 [−0.5,0.5) 会给 3.54 Å,真正最小镜像 0.707 Å(需邻域搜索)。"""
    txt = ('mono\n1.0\n4 0 0\n3 1 0\n0 0 10\nH\n2\nDirect\n0 0 0\n0.5 0.5 0\n')
    assert slab_builder.min_interatomic_distance(txt) == pytest.approx(0.70710678, abs=1e-6)


def test_min_interatomic_distance_single_atom_is_inf():
    txt = ('one\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n1\nCartesian\n0 0 0\n')
    assert slab_builder.min_interatomic_distance(txt) == float('inf')
