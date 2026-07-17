"""SAC 基底模板库测试(F1):石墨烯超胞 + 六类模板 + 矩阵 + POSCAR 写出。数值手算对拍。"""
import math
import sys

import numpy as np
import pytest

from vcstudio.generate import poscar, sac_builder as sb, slab_builder
from vcstudio.generate.structure_view import parse_positions

_CC = 2.468 / math.sqrt(3.0)   # 石墨烯最近邻 C-C = a/√3 ≈ 1.42490 Å


@pytest.fixture(autouse=True)
def _keep_numpy_in_sys_modules():
    """test_kpoints 会从 sys.modules pop 'numpy';本文件重度用 numpy,排其后运行会重载
    numpy 污染 sys.modules 击穿 native_charts 修复。放回收集期同一对象即可(见
    test_native_charts._heal_numpy_sys_modules 同一手法)。"""
    sys.modules.setdefault('numpy', np)
    yield


def _counts(text):
    els, cnts = poscar.parse_poscar_species(text)
    return dict(zip(els, cnts))


# ── 石墨烯超胞 ────────────────────────────────────────────────────────────────
def test_graphene_supercell_atom_count_and_bond():
    text = sb.graphene_supercell(4, 4)
    assert _counts(text) == {'C': 32}                       # 2·nx·ny
    assert slab_builder.min_interatomic_distance(text) == pytest.approx(_CC, abs=1e-4)


def test_graphene_supercell_sizes_and_centered_z():
    assert _counts(sb.graphene_supercell(2, 3)) == {'C': 12}
    p = parse_positions(sb.graphene_supercell(3, 3, vacuum=18.0))
    zs = [c[2] for c in p['coords']]
    assert all(z == pytest.approx(9.0) for z in zs)         # z 居中 = vacuum/2
    # 真空层 = |c| − z跨度 = 18 − 0 = 18
    assert slab_builder.vacuum_thickness(sb.graphene_supercell(3, 3, vacuum=18.0)) == pytest.approx(18.0)


def test_graphene_supercell_bad_size_raises():
    with pytest.raises(ValueError, match='正整数'):
        sb.graphene_supercell(0, 4)


# ── 六类 SAC 模板 ────────────────────────────────────────────────────────────
def test_build_sac_mn4_counts_and_metal_lift():
    r = sb.build_sac('MN4', 'Fe', vacuum=20.0)
    assert _counts(r['poscar']) == {'Fe': 1, 'N': 4, 'C': 26}   # 32−2空位−4→N = 26 C
    p = parse_positions(r['poscar'])
    si = r['site_indices']
    assert p['elements'][si['metal']] == 'Fe'
    assert [p['elements'][c] for c in si['coord']] == ['N', 'N', 'N', 'N']
    # 金属沿 z 抬升 0.4 → z = 10.4 > 平面 10.0
    assert p['coords'][si['metal']][2] == pytest.approx(10.4)
    # 双空位 M-N ≈ 1.927 Å(含 0.4 抬升)
    m = np.array(p['coords'][si['metal']])
    dists = [float(np.linalg.norm(m - np.array(p['coords'][c]))) for c in si['coord']]
    assert all(d == pytest.approx(1.927, abs=0.01) for d in dists)


def test_build_sac_mn3_monovacancy():
    r = sb.build_sac('MN3', 'Co')
    assert _counts(r['poscar']) == {'Co': 1, 'N': 3, 'C': 28}   # 32−1空位−3→N = 28 C
    p = parse_positions(r['poscar'])
    si = r['site_indices']
    assert len(si['coord']) == 3
    assert [p['elements'][c] for c in si['coord']] == ['N', 'N', 'N']
    assert p['coords'][si['metal']][2] == pytest.approx(10.4)


@pytest.mark.parametrize('template,hetero', [
    ('MP1N3', 'P'), ('MS1N3', 'S'), ('MB1N3', 'B')])
def test_build_sac_one_substitution(template, hetero):
    r = sb.build_sac(template, 'Ni')
    c = _counts(r['poscar'])
    assert c['Ni'] == 1 and c['N'] == 3 and c[hetero] == 1 and c['C'] == 26
    p = parse_positions(r['poscar'])
    coord_els = sorted(p['elements'][i] for i in r['site_indices']['coord'])
    assert coord_els == sorted(['N', 'N', 'N', hetero])         # 4 配位含 1 个杂原子


def test_build_sac_mn4_plus_b_second_shell_dopant():
    r = sb.build_sac('MN4+B', 'Fe')
    c = _counts(r['poscar'])
    assert c == {'Fe': 1, 'N': 4, 'B': 1, 'C': 25}             # 4N 配位 + 1 二壳 B
    si = r['site_indices']
    assert 'dopant' in si
    p = parse_positions(r['poscar'])
    assert p['elements'][si['dopant']] == 'B'
    # B 是近邻二壳,不在配位集
    assert si['dopant'] not in si['coord']


def test_build_sac_custom_lift():
    r = sb.build_sac('MN4', 'Fe', lift=0.5)
    p = parse_positions(r['poscar'])
    assert p['coords'][r['site_indices']['metal']][2] == pytest.approx(10.5)


def test_build_sac_no_overlap_and_roundtrip():
    for t in ('MN4', 'MN3', 'MP1N3', 'MS1N3', 'MB1N3', 'MN4+B'):
        r = sb.build_sac(t, 'Fe')
        parse_positions(r['poscar'])                           # 往返解析不抛
        assert slab_builder.min_interatomic_distance(r['poscar']) > 0.8
        assert '非终值' in r['description']


def test_build_sac_unknown_template_raises():
    with pytest.raises(ValueError, match='未知 SAC 模板'):
        sb.build_sac('MX9', 'Fe')


# ── 矩阵批量 ─────────────────────────────────────────────────────────────────
def test_sac_matrix_naming_and_size():
    metals = ['Fe', 'Co', 'Ni']
    templates = ['MN4', 'MN3']
    mat = sb.sac_matrix(metals, templates)
    assert len(mat) == 6
    assert [m['name'] for m in mat] == [
        'Fe@MN4', 'Fe@MN3', 'Co@MN4', 'Co@MN3', 'Ni@MN4', 'Ni@MN3']
    fe_mn4 = mat[0]
    assert fe_mn4['metal'] == 'Fe' and fe_mn4['template'] == 'MN4'
    assert _counts(fe_mn4['poscar'])['Fe'] == 1


# ── POSCAR 写出工具 ──────────────────────────────────────────────────────────
def test_write_poscar_groups_by_element():
    cell = [[10, 0, 0], [0, 10, 0], [0, 0, 10]]
    els = ['C', 'N', 'C', 'N']            # 构造序交错
    coords = [[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0]]
    text = sb.write_poscar('t', cell, els, coords, mode='Cartesian')
    assert poscar.parse_poscar_species(text) == (['C', 'N'], [2, 2])   # 分组计数正确
    p = parse_positions(text)
    assert p['elements'] == ['C', 'C', 'N', 'N']


def test_write_poscar_element_order_metal_first():
    cell = [[10, 0, 0], [0, 10, 0], [0, 0, 10]]
    els = ['C', 'C', 'Fe']
    coords = [[0, 0, 0], [1, 0, 0], [2, 0, 0]]
    text = sb.write_poscar('t', cell, els, coords, element_order=['Fe', 'N', 'C'])
    assert poscar.parse_poscar_species(text) == (['Fe', 'C'], [1, 2])


def test_cart_frac_roundtrip_nonorthogonal():
    cell = np.array([[4.0, 0, 0], [1.0, 3.0, 0], [0, 0, 12.0]])
    cart = np.array([1.5, 2.0, 3.0])
    frac = sb.cart_to_frac(cart, cell)
    back = sb.frac_to_cart(frac, cell)
    assert back == pytest.approx(cart)
