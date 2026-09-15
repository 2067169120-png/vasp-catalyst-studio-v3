"""表面能测试(project.surface_energy):γ 公式/单位换算/表面积叉积/非对称口径 warning。"""
import math

import pytest

from vcstudio.project import surface_energy as se

# 3×3 面内正交胞,c=20 真空(slab);|a×b|=9 Å²。
_SLAB = """slab
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu
4
Cartesian
0.0 0.0 8.0
1.5 1.5 9.0
0.0 0.0 10.0
1.5 1.5 11.0
"""


def test_area_from_poscar_cross_product():
    assert se.area_from_poscar(_SLAB) == pytest.approx(9.0)


def test_area_from_poscar_skewed_uses_cross_not_product():
    # 斜胞:a=(3,0,0),b=(1.5, 3, 0) → |a×b| = 3*3 = 9(≠ |a||b|)
    skew = _SLAB.replace('0.0 3.0 0.0', '1.5 3.0 0.0')
    assert se.area_from_poscar(skew) == pytest.approx(9.0)
    assert se.area_from_poscar(skew) < 3.0 * math.sqrt(1.5**2 + 3.0**2)   # < |a||b|


def test_surface_energy_hand_computed():
    # excess = E_slab − N·E_bulk = -90 − 10·(-9.5) = 5 eV;γ = 5/(2·9) = 0.27778 eV/Å²
    r = se.surface_energy(-90.0, 10, -9.5, 9.0)
    assert r['gamma_evA2'] == pytest.approx(5.0 / 18.0)
    assert r['gamma_jm2'] == pytest.approx(5.0 / 18.0 * 16.02176634)
    assert not r['warnings']                                   # 对称且正 γ,无警告


def test_surface_energy_unit_conversion_constant():
    r = se.surface_energy(-90.0, 10, -9.5, 9.0)
    assert r['gamma_jm2'] / r['gamma_evA2'] == pytest.approx(16.02176634)


def test_surface_energy_asymmetric_warns():
    r = se.surface_energy(-90.0, 10, -9.5, 9.0, relaxed_both_sides=False)
    assert any('非对称' in w for w in r['warnings'])


def test_surface_energy_negative_gamma_warns():
    # E_bulk 取错口径导致 γ<0 → 明确 warning 提示核对
    r = se.surface_energy(-100.0, 10, -9.5, 9.0)
    assert r['gamma_evA2'] < 0
    assert any('负' in w for w in r['warnings'])


def test_surface_energy_bad_area_raises():
    with pytest.raises(ValueError, match='表面积'):
        se.surface_energy(-90.0, 10, -9.5, 0.0)


def test_surface_energy_bad_natoms_raises():
    with pytest.raises(ValueError, match='原子数'):
        se.surface_energy(-90.0, 0, -9.5, 9.0)


def test_surface_energy_area_from_poscar_integration():
    area = se.area_from_poscar(_SLAB)
    r = se.surface_energy(-90.0, 4, -22.0, area)      # 用真实解析面积
    assert r['gamma_evA2'] == pytest.approx((-90.0 - 4 * -22.0) / (2 * area))
