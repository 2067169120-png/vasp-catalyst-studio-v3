"""差分电荷工作流测试(project.chgdiff):CHGCAR 网格代数、POSCAR 拆分、三作业派生。"""
import yaml

import pytest

from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.project import chgdiff

_CUBE = [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 3.0]]   # 体积 27


def make_chgcar(lattice, species, counts, coords, ng, grid, scale=1.0):
    lines = ['synthetic', str(scale)]
    for v in lattice:
        lines.append(f' {v[0]} {v[1]} {v[2]}')
    lines.append(' '.join(species))
    lines.append(' '.join(str(c) for c in counts))
    lines.append('Direct')
    for c in coords:
        lines.append(f' {c[0]} {c[1]} {c[2]}')
    lines.append('')                                     # 坐标块后空行
    lines.append(f' {ng[0]} {ng[1]} {ng[2]}')
    for i in range(0, len(grid), 5):
        lines.append(' '.join(str(v) for v in grid[i:i + 5]))
    return '\n'.join(lines) + '\n'


def _coords(n):
    return [[round(0.1 * k, 3)] * 3 for k in range(n)]


# ── CHGCAR 读/写 ─────────────────────────────────────────────────────────────

def test_read_write_chgcar_roundtrip(tmp_path):
    grid = [float(i) for i in range(8)]
    txt = make_chgcar(_CUBE, ['Cu'], [1], _coords(1), (2, 2, 2), grid)
    c = chgdiff.read_chgcar(txt)
    assert (c['ngx'], c['ngy'], c['ngz']) == (2, 2, 2)
    assert c['natoms'] == 1
    assert c['grid'] == pytest.approx(grid)
    out = tmp_path / 'CHGCAR_rw'
    chgdiff.write_chgcar(c, c['grid'], out)
    c2 = chgdiff.read_chgcar(str(out))
    assert c2['grid'] == pytest.approx(grid)
    assert (c2['ngx'], c2['ngy'], c2['ngz']) == (2, 2, 2)


def test_read_chgcar_truncated_grid_raises():
    txt = make_chgcar(_CUBE, ['Cu'], [1], _coords(1), (2, 2, 2), [1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match='网格数据不足'):
        chgdiff.read_chgcar(txt)


# ── Δρ = AB − A − B ──────────────────────────────────────────────────────────

def test_compute_chgdiff_hand_computed(tmp_path):
    ng = (2, 2, 2)
    ab = make_chgcar(_CUBE, ['Cu', 'O'], [2, 1], _coords(3), ng, [10.0] * 8)
    a = make_chgcar(_CUBE, ['Cu'], [2], _coords(2), ng, [float(i) for i in range(1, 9)])
    b = make_chgcar(_CUBE, ['O'], [1], _coords(1), ng, [1.0] * 8)
    p_ab, p_a, p_b = (tmp_path / 'AB', tmp_path / 'A', tmp_path / 'B')
    for p, t in ((p_ab, ab), (p_a, a), (p_b, b)):
        p.write_text(t, encoding='utf-8')
    out = tmp_path / 'CHGDIFF.vasp'
    res = chgdiff.compute_chgdiff(str(p_ab), str(p_a), str(p_b), str(out))
    # Δ = 10 − i − 1 = 9 − i, i=1..8 → [8,7,6,5,4,3,2,1]
    diff = chgdiff.read_chgcar(str(out))['grid']
    assert diff == pytest.approx([8, 7, 6, 5, 4, 3, 2, 1])
    assert res['max'] == pytest.approx(8.0)
    assert res['min'] == pytest.approx(1.0)
    assert res['n_grid'] == 8


def test_compute_chgdiff_grid_mismatch(tmp_path):
    ab = make_chgcar(_CUBE, ['Cu', 'O'], [2, 1], _coords(3), (2, 2, 2), [1.0] * 8)
    a = make_chgcar(_CUBE, ['Cu'], [2], _coords(2), (2, 2, 1), [1.0] * 4)
    b = make_chgcar(_CUBE, ['O'], [1], _coords(1), (2, 2, 2), [1.0] * 8)
    with pytest.raises(ValueError, match='网格不一致'):
        chgdiff.compute_chgdiff(ab, a, b, tmp_path / 'd')


def test_compute_chgdiff_lattice_mismatch(tmp_path):
    other = [[4.0, 0, 0], [0, 3.0, 0], [0, 0, 3.0]]
    ab = make_chgcar(_CUBE, ['Cu', 'O'], [2, 1], _coords(3), (2, 2, 2), [1.0] * 8)
    a = make_chgcar(other, ['Cu'], [2], _coords(2), (2, 2, 2), [1.0] * 8)
    b = make_chgcar(_CUBE, ['O'], [1], _coords(1), (2, 2, 2), [1.0] * 8)
    with pytest.raises(ValueError, match='晶格不一致'):
        chgdiff.compute_chgdiff(ab, a, b, tmp_path / 'd')


def test_compute_chgdiff_atom_count_mismatch(tmp_path):
    # N(AB)=3 但 N(A)+N(B)=2+2=4 → 拒算
    ab = make_chgcar(_CUBE, ['Cu', 'O'], [2, 1], _coords(3), (2, 2, 2), [1.0] * 8)
    a = make_chgcar(_CUBE, ['Cu'], [2], _coords(2), (2, 2, 2), [1.0] * 8)
    b = make_chgcar(_CUBE, ['O'], [2], _coords(2), (2, 2, 2), [1.0] * 8)
    with pytest.raises(ValueError, match='原子数不守恒'):
        chgdiff.compute_chgdiff(ab, a, b, tmp_path / 'd')


# ── 面平均 ───────────────────────────────────────────────────────────────────

def test_plane_averaged_z_hand_computed():
    # 3×3×3 立方胞(V=27);z 平面 iz=0/1/2 的值 = 27/54/0 → 面均 /V = 1/2/0
    grid = [27.0] * 9 + [54.0] * 9 + [0.0] * 9        # i//9 = iz
    txt = make_chgcar(_CUBE, ['Cu'], [1], _coords(1), (3, 3, 3), grid)
    r = chgdiff.plane_averaged(txt, axis='z')
    assert r['rho'] == pytest.approx([1.0, 2.0, 0.0])
    assert r['z'] == pytest.approx([0.0, 1.0, 2.0])   # length 3, na 3 → 间距 1
    assert r['axis'] == 'z'


def test_plane_averaged_axis_x():
    # 值只依赖 ix = i%3:0/1/2 → 0/27/54 → 面均 /27 = 0/1/2
    grid = [float((i % 3) * 27) for i in range(27)]
    txt = make_chgcar(_CUBE, ['Cu'], [1], _coords(1), (3, 3, 3), grid)
    r = chgdiff.plane_averaged(txt, axis='x')
    assert r['rho'] == pytest.approx([0.0, 1.0, 2.0])


def test_plane_averaged_bad_axis():
    txt = make_chgcar(_CUBE, ['Cu'], [1], _coords(1), (2, 2, 2), [1.0] * 8)
    with pytest.raises(ValueError, match='axis'):
        chgdiff.plane_averaged(txt, axis='w')


# ── 吸附态 POSCAR 拆分 ───────────────────────────────────────────────────────

_ADS_POSCAR = """\
CuO slab + OH
1.0
 3.0 0.0 0.0
 0.0 3.0 0.0
 0.0 0.0 20.0
 Cu O H
 2 1 1
Selective dynamics
Direct
 0.00 0.00 0.10  F F F
 0.50 0.50 0.10  F F F
 0.25 0.25 0.30  T T T
 0.25 0.25 0.40  T T T
"""


def test_split_adsorption_basic():
    parts = chgdiff.split_adsorption_poscar(_ADS_POSCAR, [3, 4])   # O,H 为吸附质
    sp_ab, cn_ab = parse_poscar_species(parts['ab'])
    sp_a, cn_a = parse_poscar_species(parts['a'])
    sp_b, cn_b = parse_poscar_species(parts['b'])
    assert (sp_ab, cn_ab) == (['Cu', 'O', 'H'], [2, 1, 1])
    assert (sp_a, cn_a) == (['Cu'], [2])            # 仅表面
    assert (sp_b, cn_b) == (['O', 'H'], [1, 1])     # 仅吸附质
    # 冻结几何/坐标逐字保留(吸附质 H 那行含 T T T)
    assert '0.25 0.25 0.40  T T T' in parts['b']
    assert 'Selective dynamics' in parts['a']


def test_split_shared_species():
    # 氧化物表面含 O,吸附质也是 O:原子 5(第二个 O)为吸附质
    poscar = ('ox\n1.0\n 3 0 0\n 0 3 0\n 0 0 20\n Cu O\n 2 3\nDirect\n'
              ' 0 0 0.1\n 0.5 0.5 0.1\n 0.2 0.2 0.2\n 0.6 0.6 0.2\n 0.4 0.4 0.4\n')
    parts = chgdiff.split_adsorption_poscar(poscar, [5])
    assert parse_poscar_species(parts['a']) == (['Cu', 'O'], [2, 2])
    assert parse_poscar_species(parts['b']) == (['O'], [1])


def test_split_errors():
    with pytest.raises(ValueError, match='越界'):
        chgdiff.split_adsorption_poscar(_ADS_POSCAR, [99])
    with pytest.raises(ValueError, match='为空'):
        chgdiff.split_adsorption_poscar(_ADS_POSCAR, [])
    with pytest.raises(ValueError, match='无表面'):
        chgdiff.split_adsorption_poscar(_ADS_POSCAR, [1, 2, 3, 4])
    vasp4 = _ADS_POSCAR.replace(' Cu O H\n', '')       # 删元素行 → VASP4
    with pytest.raises(ValueError, match='VASP4'):
        chgdiff.split_adsorption_poscar(vasp4, [3])


# ── POTCAR 切片 ──────────────────────────────────────────────────────────────

_POTCAR3 = (' PAW_PBE Cu 05Jan2001\n POMASS=63; ZVAL=11\n End of Dataset\n'
            ' PAW_PBE O 08Apr2002\n POMASS=16; ZVAL=6\n End of Dataset\n'
            ' PAW_PBE H 15Jun2001\n POMASS=1; ZVAL=1\n End of Dataset\n')


def test_slice_potcar():
    only_o = chgdiff.slice_potcar(_POTCAR3, ['Cu', 'O', 'H'], ['O'])
    assert 'PAW_PBE O' in only_o and 'PAW_PBE Cu' not in only_o
    oh = chgdiff.slice_potcar(_POTCAR3, ['Cu', 'O', 'H'], ['O', 'H'])
    assert oh.index('PAW_PBE O') < oh.index('PAW_PBE H')
    # 块数与母物种数不符 → None(降级)
    assert chgdiff.slice_potcar(_POTCAR3, ['Cu', 'O'], ['O']) is None


# ── 三作业派生 ───────────────────────────────────────────────────────────────

def _make_ads_relax(tmp_path):
    d = tmp_path / 'ads_relax'
    d.mkdir()
    (d / 'CONTCAR').write_text(_ADS_POSCAR, encoding='utf-8')
    (d / 'INCAR').write_text('ENCUT = 500\nISPIN = 2\nMAGMOM = 4*0\n'
                             'IBRION = 2\nNSW = 100\nISMEAR = 1\nSIGMA = 0.1\n'
                             'EDIFF = 1E-04\n', encoding='utf-8')
    (d / 'KPOINTS').write_text('Automatic\n0\nGamma\n3 3 1\n0 0 0\n', encoding='utf-8')
    (d / 'POTCAR').write_text(_POTCAR3, encoding='utf-8')
    return d


def test_build_chgdiff_jobs(tmp_path):
    relax = _make_ads_relax(tmp_path)
    out_root = tmp_path / 'chgdiff'
    res = chgdiff.build_chgdiff_jobs(relax, out_root, [3, 4])
    for tag in ('_AB', '_A', '_B'):
        d = res['dirs'][tag]
        for name in ('POSCAR', 'INCAR', 'KPOINTS', 'job.yaml'):
            import os
            assert os.path.isfile(os.path.join(d, name)), f'{tag}/{name}'
        meta = yaml.safe_load(open(f'{d}/job.yaml', encoding='utf-8'))
        assert meta['purpose'] == 'chgdiff'
        assert meta['chgdiff_role'] == tag.strip('_')
        assert len(meta['siblings']) == 2
        inc = open(f'{d}/INCAR', encoding='utf-8').read()
        assert 'LCHARG' in inc
    # _A/_B 移除 MAGMOM;_AB 保留
    assert 'MAGMOM' not in open(f"{res['dirs']['_A']}/INCAR", encoding='utf-8').read()
    assert 'MAGMOM' in open(f"{res['dirs']['_AB']}/INCAR", encoding='utf-8').read()
    # _A 仅表面 Cu2;_B POTCAR 切成 O+H
    assert parse_poscar_species(
        open(f"{res['dirs']['_A']}/POSCAR", encoding='utf-8').read()) == (['Cu'], [2])
    pot_b = open(f"{res['dirs']['_B']}/POTCAR", encoding='utf-8').read()
    assert 'PAW_PBE O' in pot_b and 'PAW_PBE H' in pot_b and 'PAW_PBE Cu' not in pot_b
    # _AB 用母 POTCAR 原文(三物种齐全)
    pot_ab = open(f"{res['dirs']['_AB']}/POTCAR", encoding='utf-8').read()
    assert all(f'PAW_PBE {el}' in pot_ab for el in ('Cu', 'O', 'H'))
