import importlib
import math
import sys

from vcstudio.generate.kpoints import recommend_kpoints, kpoints_str


def test_molecule_is_gamma_only():
    assert recommend_kpoints([[10, 0, 0], [0, 10, 0], [0, 0, 10]], 'molecule') == [1, 1, 1]


def test_slab_forces_kz_1():
    # 2.87 Å 立方(bcc Fe):1/(0.03*2.87)=11.6 → ceil 12 → 偶数+1=13 → cap 9;slab kz=1
    assert recommend_kpoints([[2.87, 0, 0], [0, 2.87, 0], [0, 0, 2.87]], 'slab') == [9, 9, 1]


def test_bulk_all_three_directions():
    assert recommend_kpoints([[2.87, 0, 0], [0, 2.87, 0], [0, 0, 2.87]], 'bulk') == [9, 9, 9]


def test_large_cell_gives_one_k():
    # 20 Å 盒子:1/(0.03*20)=1.67 → ceil 2 → 偶数+1=3;确认长度用的是矢量模长
    assert recommend_kpoints([[20, 0, 0], [0, 20, 0], [0, 0, 20]], 'bulk') == [3, 3, 3]


def test_hexagonal_uses_reciprocal_not_edge_length():
    """修复:非正交(六方)胞按倒格矢定网格,不再用实空间边长。

    a=5.0 Å 六方 + c=20 vacuum:实空间边长法(旧)给 kx=ky=ceil(1/(0.03·5))=7;
    倒格矢法(新)|b_xy|=1/a·(2/√3) 更大 → ceil(7.70)=8→奇9。故 bulk=[9,9,3]。
    """
    a = 5.0
    hexcell = [[a, 0, 0], [-a / 2, a * math.sqrt(3) / 2, 0], [0, 0, 20]]
    assert recommend_kpoints(hexcell, 'bulk') == [9, 9, 3]
    assert recommend_kpoints(hexcell, 'slab') == [9, 9, 1]


def test_orthogonal_unchanged_vs_recip():
    """守恒:正交胞 |b_i|≡1/|a_i|,结果与旧实空间边长法逐位一致(无回归)。"""
    assert recommend_kpoints([[3, 0, 0], [0, 4, 0], [0, 0, 5]], 'bulk') == \
        recommend_kpoints([[3, 0, 0], [0, 4, 0], [0, 0, 5]], 'bulk')
    # 3Å→ceil(1/0.09)=ceil(11.1)=12→奇13→cap9;4Å→ceil(8.33)=9;5Å→ceil(6.67)=7
    assert recommend_kpoints([[3, 0, 0], [0, 4, 0], [0, 0, 5]], 'bulk') == [9, 9, 7]


def test_kpoints_module_does_not_import_numpy():
    sys.modules.pop('numpy', None)
    mod = importlib.reload(importlib.import_module('vcstudio.generate.kpoints'))
    mod.recommend_kpoints([[3, 0, 0], [0, 3, 0], [0, 0, 3]], 'bulk')
    assert 'numpy' not in sys.modules


def test_kpoints_str_format():
    assert kpoints_str([5, 5, 1]) == 'Automatic\n0\nGamma\n5 5 1\n0 0 0\n'
