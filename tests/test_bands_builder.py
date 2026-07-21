"""能带作业生成测试(generate.bands_builder):高对称路径库 / 晶格粗判 / line-mode KPOINTS /
两步派生(ICHARG=11+LORBIT=11)/ 晶格判不出报错 / manifest。"""
import math

import pytest

from vcstudio.generate import bands_builder as bb
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared import manifest as manifest_mod

_INCAR = "ENCUT = 500\nGGA = PE\nISMEAR = -5\nIBRION = 2\nNSW = 100\nISIF = 3\nEDIFFG = -0.02\n"
_POT = "PAW_PBE Si\n"


def _bulk(cell_rows, comment='bulk'):
    lines = [comment, '1.0'] + [f'{r[0]} {r[1]} {r[2]}' for r in cell_rows]
    lines += ['Si', '1', 'Direct', '0.0 0.0 0.0']
    return '\n'.join(lines) + '\n'


_CUBIC = [[3.0, 0, 0], [0, 3.0, 0], [0, 0, 3.0]]
_HEX = [[3.0, 0, 0], [-1.5, 3.0 * math.sqrt(3) / 2, 0], [0, 0, 5.0]]
_TET = [[3.0, 0, 0], [0, 3.0, 0], [0, 0, 5.0]]
_ORC = [[3.0, 0, 0], [0, 4.0, 0], [0, 0, 5.0]]
_FCC = [[0, 2.0, 2.0], [2.0, 0, 2.0], [2.0, 2.0, 0]]
_BCC = [[-1.0, 1.0, 1.0], [1.0, -1.0, 1.0], [1.0, 1.0, -1.0]]


# ── 路径库 ──────────────────────────────────────────────────────────────────────
def test_all_lattices_have_gamma_and_note():
    for key, spec in bb.HIGH_SYMMETRY.items():
        assert 'GAMMA' in spec['points']
        assert 'Setyawan' in spec['note']


def test_kpath_segments_cubic_pairs():
    pairs = bb.kpath_segments('cubic')
    # Γ-X-M-Γ-R-X (5 对) + M-R (1 对) = 6 对
    assert len(pairs) == 6
    (f0, n0), (f1, n1) = pairs[0]
    assert n0 == 'GAMMA' and n1 == 'X' and f0 == (0.0, 0.0, 0.0)


def test_kpath_segments_unknown_raises():
    with pytest.raises(ValueError, match='未知晶格'):
        bb.kpath_segments('triclinic')


# ── 晶格粗判 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('cell,expect', [
    (_CUBIC, 'cubic'), (_HEX, 'hexagonal'), (_TET, 'tetragonal'),
    (_ORC, 'orthorhombic'), (_FCC, 'fcc'), (_BCC, 'bcc'),
])
def test_detect_lattice(cell, expect):
    assert bb.detect_lattice(cell) == expect


def test_detect_lattice_undetectable_returns_none():
    weird = [[3.0, 0, 0], [0.7, 3.0, 0], [0.2, 0.4, 5.0]]     # 低对称含糊角
    assert bb.detect_lattice(weird) is None


# ── line-mode KPOINTS ────────────────────────────────────────────────────────────
def test_kpoints_line_mode_format():
    txt = bb.kpoints_line_mode('cubic', npoints=40)
    lines = txt.splitlines()
    assert lines[1].strip() == '40'
    assert lines[2].strip().lower() == 'line-mode'
    assert lines[3].strip().lower() == 'reciprocal'
    assert 'Γ' in txt                                        # Gamma 标签


# ── build_bands_job ─────────────────────────────────────────────────────────────
def test_build_bands_job_auto_cubic(tmp_path):
    src = tmp_path / 'scf'
    src.mkdir()
    (src / 'CONTCAR').write_text(_bulk(_CUBIC), encoding='utf-8')
    (src / 'INCAR').write_text(_INCAR, encoding='utf-8')
    (src / 'POTCAR').write_text(_POT, encoding='utf-8')
    (src / 'CHGCAR').write_text('fake chgcar\n', encoding='utf-8')
    res = bb.build_bands_job(str(src), str(tmp_path / 'bd'))
    assert res['lattice'] == 'cubic'
    d = parse_incar((tmp_path / 'bd' / 'INCAR').read_text())
    assert d['ISTART'] == 0 and d['ICHARG'] == 11
    assert d['LORBIT'] == 11 and d['ISMEAR'] == 0
    assert 'ISIF' not in d and 'EDIFFG' not in d
    assert d['ENCUT'] == 500 and d['GGA'] == 'PE'            # 电子学保留
    kp = (tmp_path / 'bd' / 'KPOINTS').read_text()
    assert 'Line-mode' in kp
    assert (tmp_path / 'bd' / 'CHGCAR').is_file()            # CHGCAR 复制


def test_build_bands_job_explicit_lattice(tmp_path):
    src = tmp_path / 'scf'
    src.mkdir()
    (src / 'CONTCAR').write_text(_bulk(_FCC), encoding='utf-8')
    (src / 'INCAR').write_text(_INCAR, encoding='utf-8')
    (src / 'POTCAR').write_text(_POT, encoding='utf-8')
    res = bb.build_bands_job(str(src), str(tmp_path / 'bd'), lattice='fcc', npoints=60)
    assert res['lattice'] == 'fcc'
    assert bb.kpoints_line_mode('fcc', 60).splitlines()[1].strip() == '60'


def test_build_bands_job_undetectable_raises(tmp_path):
    src = tmp_path / 'scf'
    src.mkdir()
    weird = [[3.0, 0, 0], [0.7, 3.0, 0], [0.2, 0.4, 5.0]]
    (src / 'CONTCAR').write_text(_bulk(weird), encoding='utf-8')
    (src / 'INCAR').write_text(_INCAR, encoding='utf-8')
    with pytest.raises(ValueError, match='lattice'):
        bb.build_bands_job(str(src), str(tmp_path / 'bd'))


def test_build_bands_job_no_chgcar_warns(tmp_path):
    src = tmp_path / 'scf'
    src.mkdir()
    (src / 'CONTCAR').write_text(_bulk(_CUBIC), encoding='utf-8')
    (src / 'INCAR').write_text(_INCAR, encoding='utf-8')
    (src / 'POTCAR').write_text(_POT, encoding='utf-8')
    res = bb.build_bands_job(str(src), str(tmp_path / 'bd'))
    assert any('CHGCAR' in w for w in res['warnings'])


def test_build_bands_job_does_not_inherit_wavecar_restart(tmp_path):
    src = tmp_path / 'scf'
    src.mkdir()
    (src / 'CONTCAR').write_text(_bulk(_CUBIC), encoding='utf-8')
    (src / 'INCAR').write_text(_INCAR + 'ISTART = 1\n', encoding='utf-8')
    (src / 'CHGCAR').write_text('fake chgcar\n', encoding='utf-8')
    bb.build_bands_job(str(src), str(tmp_path / 'bd'))
    d = parse_incar((tmp_path / 'bd' / 'INCAR').read_text())
    assert d['ISTART'] == 0 and d['ICHARG'] == 11


def test_build_bands_job_manifest(tmp_path):
    src = tmp_path / 'scf'
    src.mkdir()
    (src / 'CONTCAR').write_text(_bulk(_CUBIC), encoding='utf-8')
    (src / 'INCAR').write_text(_INCAR, encoding='utf-8')
    (src / 'POTCAR').write_text(_POT, encoding='utf-8')
    bb.build_bands_job(str(src), str(tmp_path / 'bd'))
    m = manifest_mod.load_manifest(tmp_path / 'bd')
    assert m['task_type'] == 'bands' and m['inputs']['lattice'] == 'cubic'
    assert m['inputs']['npoints'] == 40
