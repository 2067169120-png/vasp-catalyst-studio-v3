import importlib
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


def test_kpoints_module_does_not_import_numpy():
    sys.modules.pop('numpy', None)
    mod = importlib.reload(importlib.import_module('vcstudio.generate.kpoints'))
    mod.recommend_kpoints([[3, 0, 0], [0, 3, 0], [0, 0, 3]], 'bulk')
    assert 'numpy' not in sys.modules


def test_kpoints_str_format():
    assert kpoints_str([5, 5, 1]) == 'Automatic\n0\nGamma\n5 5 1\n0 0 0\n'
