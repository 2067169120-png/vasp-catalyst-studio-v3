"""LiPS 分子库测试(F4):几何健全 / 元素计数 / 盒装居中 / 元信息 / 自旋提示。"""
import sys
from collections import Counter

import numpy as np
import pytest

from vcstudio.generate import molecules as M
from vcstudio.generate import poscar
from vcstudio.generate.structure_view import parse_positions


@pytest.fixture(autouse=True)
def _keep_numpy_in_sys_modules():
    """test_kpoints 的延迟依赖测试会从 sys.modules pop 'numpy';本文件重度用 numpy,
    若排在其后运行,首个 numpy 运算会重载 numpy 并污染 sys.modules,击穿 native_charts
    的 setdefault 修复而致 RecursionError。收集期保存的同一 numpy 对象放回即可(零副作用,
    与 test_native_charts._heal_numpy_sys_modules 同一手法)。"""
    sys.modules.setdefault('numpy', np)
    yield

# 期望元素计数(元素 → 原子数)
_EXPECT = {
    'S8': {'S': 8}, 'Li2S8': {'Li': 2, 'S': 8}, 'Li2S6': {'Li': 2, 'S': 6},
    'Li2S4': {'Li': 2, 'S': 4}, 'Li2S3': {'Li': 2, 'S': 3}, 'Li2S2': {'Li': 2, 'S': 2},
    'Li2S': {'Li': 2, 'S': 1}, 'LiS2': {'Li': 1, 'S': 2}, 'LiS': {'Li': 1, 'S': 1},
    'DOL': {'C': 3, 'H': 6, 'O': 2}, 'DME': {'C': 4, 'H': 10, 'O': 2},
    'O2': {'O': 2}, 'H2': {'H': 2}, 'H2O': {'O': 1, 'H': 2}, 'CO': {'C': 1, 'O': 1},
}


def _min_dist(coords):
    n = len(coords)
    best = float('inf')
    for i in range(n):
        for j in range(i + 1, n):
            best = min(best, float(np.linalg.norm(coords[i] - coords[j])))
    return best


def test_list_molecules_complete():
    names = M.list_molecules()
    assert len(names) == 15
    assert set(names) == set(_EXPECT)


@pytest.mark.parametrize('name', list(_EXPECT))
def test_molecule_geometry_sound_and_counts(name):
    atoms = M.molecule_geometry(name)
    els = [e for e, _ in atoms]
    coords = np.array([xyz for _, xyz in atoms])
    assert Counter(els) == Counter(_EXPECT[name])          # 元素计数正确
    md = _min_dist(coords)
    if name == 'H2':
        assert md == pytest.approx(0.741, abs=1e-3)        # H-H 物理键长(<0.8 唯一例外)
    else:
        assert md > 0.8                                    # 无原子重叠/过近


def test_ss_and_lis_bond_lengths():
    # S8 冠状:近邻 S-S ≈ 2.05
    s8 = np.array([xyz for _, xyz in M.molecule_geometry('S8')])
    assert _min_dist(s8) == pytest.approx(2.05, abs=0.02)
    # Li2S:最近对为 Li-S ≈ 2.4
    li2s = np.array([xyz for _, xyz in M.molecule_geometry('Li2S')])
    assert _min_dist(li2s) == pytest.approx(2.4, abs=0.02)
    # Li2S4:S-S 链 2.05 为最近对
    li2s4 = np.array([xyz for _, xyz in M.molecule_geometry('Li2S4')])
    assert _min_dist(li2s4) == pytest.approx(2.05, abs=0.02)


def test_molecule_in_box_centered():
    p = parse_positions(M.molecule_in_box('S8', box=15.0))
    c = np.array(p['coords'])
    assert c.mean(axis=0) == pytest.approx([7.5, 7.5, 7.5], abs=1e-6)
    # 立方盒
    assert p['cell'][0][0] == pytest.approx(15.0)
    assert p['cell'][2][2] == pytest.approx(15.0)


def test_molecule_in_box_no_center_keeps_raw():
    p = parse_positions(M.molecule_in_box('CO', box=12.0, center=False))
    c = np.array(p['coords'])
    # 原始几何 C 在原点、O 在 (1.128,0,0);未居中则质心不在盒中心
    assert c.mean(axis=0)[0] != pytest.approx(6.0)


@pytest.mark.parametrize('name', list(_EXPECT))
def test_molecule_in_box_roundtrips(name):
    text = M.molecule_in_box(name)
    p = parse_positions(text)
    assert len(p['coords']) == sum(_EXPECT[name].values())
    els, cnts = poscar.parse_poscar_species(text)
    assert dict(zip(els, cnts)) == _EXPECT[name]           # 盒装 POSCAR 计数正确


def test_molecule_info_fields_and_source_note():
    info = M.molecule_info('Li2S4')
    assert info['formula'] == 'Li2S4'
    assert info['charge'] == 0
    assert info['spin_hint'] is None
    assert '非终值' in info['source_note']


@pytest.mark.parametrize('name,is_open', [
    ('LiS', True), ('LiS2', True), ('O2', True),           # 开壳
    ('S8', False), ('Li2S', False), ('H2O', False)])       # 闭壳
def test_open_shell_spin_hint(name, is_open):
    hint = M.molecule_info(name)['spin_hint']
    assert (hint is not None) == is_open
    if is_open:
        assert 'ISPIN=2' in hint


def test_unknown_molecule_raises():
    with pytest.raises(ValueError, match='未知分子'):
        M.molecule_geometry('Xe4')
    with pytest.raises(ValueError, match='未知分子'):
        M.molecule_info('Xe4')
