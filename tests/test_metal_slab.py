"""金属 slab 建模测试(Backlog #2):六种(结构,晶面)构造不变量 + 堆垛正确性 + 配方再生
+ ASE 交叉验证(dev 依赖,缺则跳过)。数值以解析几何手算对拍,绝不以实现回验实现。"""
import math
import sys

import numpy as np
import pytest

from vcstudio.generate import metal_slab as ms
from vcstudio.generate.slab_builder import (count_layers, min_interatomic_distance,
                                            vacuum_thickness)
from vcstudio.generate.structure_view import parse_positions


@pytest.fixture(autouse=True)
def _keep_numpy_in_sys_modules():
    """同 test_sac_builder:防 test_kpoints pop numpy 后本文件重载污染 sys.modules。"""
    sys.modules.setdefault('numpy', np)
    yield


_ALL = [('fcc', '111', 'Pt'), ('fcc', '100', 'Cu'), ('fcc', '110', 'Ni'),
        ('bcc', '100', 'Fe'), ('bcc', '110', 'Fe'), ('hcp', '0001', 'Ru')]


# ── 目录与初猜表 ─────────────────────────────────────────────────────────────
def test_supported_surfaces_catalog():
    combos = {(s['structure'], s['miller']) for s in ms.supported_surfaces()}
    assert combos == {('fcc', '111'), ('fcc', '100'), ('fcc', '110'),
                      ('bcc', '100'), ('bcc', '110'), ('hcp', '0001')}


def test_lattice_guess_lookup():
    assert ms.lattice_guess('Pt', 'fcc') == {'a': 3.924}
    g = ms.lattice_guess('Ru', 'hcp')
    assert g['a'] == 2.706 and g['c'] == 4.282
    assert ms.lattice_guess('Xx', 'fcc') is None
    assert ms.lattice_guess('Pt', 'nope') is None


# ── 构造不变量:层数 / 真空 / 原子数 / 最近邻 = 体相理论值 ──────────────────────
@pytest.mark.parametrize('structure,miller,el', _ALL)
def test_build_invariants(structure, miller, el):
    r = ms.build_metal_slab(el, structure, miller, 5, nx=2, ny=2, vacuum=15)
    text = r['poscar']
    per_layer = 2 if (structure, miller) == ('bcc', '110') else 1
    assert r['natoms'] == 5 * 2 * 2 * per_layer
    assert count_layers(text) == 5
    assert abs(vacuum_thickness(text) - 15.0) < 1e-6
    rec = r['recipe']
    nn_theory = ms._theoretical_nn(structure, rec['a'], rec.get('c'))
    assert abs(min_interatomic_distance(text) - nn_theory) < 1e-6   # 不塌缩不虚胖
    assert rec['kind'] == 'metal_slab' and rec['layers'] == 5
    # 晶格常数走初猜表 → 必附"发表口径须 EOS/晶胞优化"提醒(科学正确红线)
    assert any('初猜' in w for w in r['warnings'])


def test_interlayer_spacing_matches_theory():
    # 层距解析解:fcc111 a/√3;fcc100 a/2;fcc110 a/(2√2);bcc100 a/2;bcc110 a/√2;hcp c/2
    for structure, miller, el, expect in [
            ('fcc', '111', 'Pt', 3.924 / math.sqrt(3)),
            ('fcc', '100', 'Cu', 3.615 / 2),
            ('fcc', '110', 'Ni', 3.524 / (2 * math.sqrt(2))),
            ('bcc', '100', 'Fe', 2.866 / 2),
            ('bcc', '110', 'Fe', 2.866 / math.sqrt(2)),
            ('hcp', '0001', 'Ru', 4.282 / 2)]:
        text = ms.build_metal_slab(el, structure, miller, 4, nx=1, ny=1, vacuum=12)['poscar']
        zs = sorted({round(c[2], 8) for c in parse_positions(text)['coords']})
        gaps = {round(zs[i + 1] - zs[i], 6) for i in range(len(zs) - 1)}
        assert gaps == {round(expect, 6)}, (structure, miller)


def _layer_xy(text):
    """POSCAR → {层号: [该层各原子 (x,y)]}(按 z 聚类,z 升序)。"""
    coords = parse_positions(text)['coords']
    zs = sorted({round(c[2], 6) for c in coords})
    return {k: sorted((round(c[0], 6), round(c[1], 6)) for c in coords
                      if round(c[2], 6) == zs[k]) for k in range(len(zs))}


def test_fcc111_abc_vs_hcp_abab_stacking():
    # fcc(111) ABC:第 0/3 层同横向位置,第 0/1/2 层两两不同;hcp(0001) ABAB:第 0/2 层同
    fcc = _layer_xy(ms.build_metal_slab('Pt', 'fcc', '111', 4, nx=1, ny=1, vacuum=12)['poscar'])
    assert fcc[0] == fcc[3] and fcc[0] != fcc[1] and fcc[1] != fcc[2] and fcc[0] != fcc[2]
    hcp = _layer_xy(ms.build_metal_slab('Ru', 'hcp', '0001', 4, nx=1, ny=1, vacuum=12)['poscar'])
    assert hcp[0] == hcp[2] and hcp[1] == hcp[3] and hcp[0] != hcp[1]


def test_hcp_ideal_ratio_fallback_warns():
    # 初猜表无该元素的 hcp c:按理想轴比 √(8/3)·a 推算 + 显式 warning
    r = ms.build_metal_slab('Fe', 'hcp', '0001', 3, a=2.5, nx=1, ny=1, vacuum=12)
    assert abs(r['recipe']['c'] - math.sqrt(8.0 / 3.0) * 2.5) < 1e-9
    assert any('理想轴比' in w for w in r['warnings'])


def test_fix_bottom_layers_integration():
    text = ms.build_metal_slab('Pt', 'fcc', '111', 4, nx=1, ny=1, vacuum=12,
                               fix_bottom=2)['poscar']
    assert 'Selective dynamics' in text
    flags = [ln.split()[3:] for ln in text.splitlines() if len(ln.split()) == 6]
    assert flags.count(['F', 'F', 'F']) == 2 and flags.count(['T', 'T', 'T']) == 2


# ── 显式拒绝(不编造结构) ─────────────────────────────────────────────────────
def test_rejects_unsupported_surface():
    with pytest.raises(ValueError, match='结构工坊二期'):
        ms.build_metal_slab('Pt', 'fcc', '211', 4)


def test_rejects_unknown_element_without_a():
    with pytest.raises(ValueError, match='不猜测'):
        ms.build_metal_slab('Og', 'fcc', '111', 4)


def test_rejects_thin_vacuum_and_bad_args():
    with pytest.raises(ValueError, match='真空'):
        ms.build_metal_slab('Pt', 'fcc', '111', 4, vacuum=3)
    with pytest.raises(ValueError, match='层数'):
        ms.build_metal_slab('Pt', 'fcc', '111', 0)
    with pytest.raises(ValueError, match='元素符号'):
        ms.build_metal_slab('platinum', 'fcc', '111', 4)


def test_thin_but_legal_vacuum_warns():
    r = ms.build_metal_slab('Pt', 'fcc', '111', 3, vacuum=8, nx=1, ny=1)
    assert any('偏薄' in w for w in r['warnings'])


# ── 配方再生(层厚收敛的核心钩子) ──────────────────────────────────────────────
def test_slab_builder_from_recipe_roundtrip():
    r = ms.build_metal_slab('Pt', 'fcc', '111', 4, nx=2, ny=2, vacuum=15)
    fn = ms.slab_builder_from_recipe(r['recipe'])
    assert fn(4) == r['poscar']                       # 同层数逐字节还原
    text6 = fn(6)
    assert count_layers(text6) == 6                   # 只改层数,其余同配方
    assert abs(vacuum_thickness(text6) - 15.0) < 1e-6


def test_slab_builder_from_recipe_keeps_fix_bottom():
    r = ms.build_metal_slab('Fe', 'bcc', '110', 4, nx=1, ny=1, vacuum=12, fix_bottom=1)
    text5 = ms.slab_builder_from_recipe(r['recipe'])(5)
    assert 'Selective dynamics' in text5 and count_layers(text5) == 5


def test_slab_builder_from_recipe_rejects_bad_recipe():
    with pytest.raises(ValueError, match='metal_slab'):
        ms.slab_builder_from_recipe({'kind': 'sac'})
    with pytest.raises(ValueError, match='缺字段'):
        ms.slab_builder_from_recipe({'kind': 'metal_slab', 'element': 'Pt'})


# ── ASE 交叉验证(dev 依赖;几何以独立实现对拍) ────────────────────────────────
def test_cross_validate_against_ase():
    ase_build = pytest.importorskip('ase.build', reason='ASE 未装(dev 依赖),跳过交叉验证')
    ase_io = pytest.importorskip('ase.io')
    import io as _io
    from collections import Counter

    def spectrum(atoms, rmax):
        d = atoms.get_all_distances(mic=True)
        n = len(atoms)
        return Counter(round(d[i][j], 3) for i in range(n) for j in range(i + 1, n)
                       if d[i][j] < rmax)

    cases = [
        (ms.build_metal_slab('Pt', 'fcc', '111', 5, nx=2, ny=2, vacuum=15),
         ase_build.fcc111('Pt', (2, 2, 5), a=3.924, vacuum=7.5, periodic=True), 5.0),
        (ms.build_metal_slab('Ru', 'hcp', '0001', 5, nx=2, ny=2, vacuum=15),
         ase_build.hcp0001('Ru', (2, 2, 5), a=2.706, c=4.282, vacuum=7.5, periodic=True), 4.8),
        (ms.build_metal_slab('Fe', 'bcc', '110', 4, nx=2, ny=1, vacuum=15),
         ase_build.bcc110('Fe', (2, 2, 4), a=2.866, vacuum=7.5, orthogonal=True,
                          periodic=True), 4.6),
    ]
    for ours, ref, rmax in cases:
        got = ase_io.read(_io.StringIO(ours['poscar']), format='vasp')
        assert len(got) == len(ref)
        # 全对距离谱(MIC)一致 ⇒ 晶格/堆垛/层距全部与 ASE 参考实现一致
        assert spectrum(got, rmax) == spectrum(ref, rmax)
