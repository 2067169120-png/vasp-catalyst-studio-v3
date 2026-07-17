"""CP2K Backend 测试:必需 section / 坐标转换 / CUTOFF 口径 / Hartree 换算 / 收敛判定。"""
import os

import pytest

from vcstudio.engines import get_backend
from vcstudio.engines.calcspec import HARTREE_TO_EV, CalcSpec
from tests.test_engines_calcspec import make_poscar

# 立方 10 Å 盒 + 单 H(便于核对 &COORD 坐标与 &CELL 矢量)
MOL10 = make_poscar('probe', [[10, 0, 0], [0, 10, 0], [0, 0, 10]],
                    ['H'], [1], [[3.0, 4.0, 5.0]])
SLAB = make_poscar('Fe slab', [[5, 0, 0], [0, 5, 0], [0, 0, 15]],
                   ['Fe', 'O'], [1, 1], [[0, 0, 5.0], [2.5, 2.5, 5.0]])


def _gen(tmp_path, **kw):
    base = dict(structure=SLAB, task='static', functional='PBE', periodic=True,
                cutoff_ev=500, extras={'cutoff_ry': 400})
    base.update(kw)
    res = get_backend('cp2k').generate_inputs(CalcSpec(**base), str(tmp_path))
    text = open(os.path.join(str(tmp_path), 'cp2k.inp'), encoding='utf-8').read()
    return text, res


def test_cp2k_required_sections(tmp_path):
    text, _ = _gen(tmp_path)
    for sect in ('&GLOBAL', '&FORCE_EVAL', '&DFT', '&MGRID', '&XC',
                 '&SCF', '&SUBSYS', '&CELL', '&COORD', '&KIND'):
        assert sect in text, f'缺 {sect}'


def test_cp2k_cell_vectors_written(tmp_path):
    text, _ = _gen(tmp_path, structure=MOL10, periodic=False)
    assert 'A 10.00000000 0.00000000 0.00000000' in text
    assert 'C 0.00000000 0.00000000 10.00000000' in text


def test_cp2k_coord_conversion(tmp_path):
    text, _ = _gen(tmp_path, structure=MOL10, periodic=False)
    assert 'H 3.00000000 4.00000000 5.00000000' in text


def test_cp2k_cutoff_from_extras_no_warning(tmp_path):
    text, res = _gen(tmp_path, extras={'cutoff_ry': 650})
    assert 'CUTOFF 650' in text
    assert not any('不从 ENCUT 换算' in w for w in res['warnings'])


def test_cp2k_missing_cutoff_warns_exact_phrase(tmp_path):
    text, res = _gen(tmp_path, extras={})
    assert any('CP2K CUTOFF 需独立收敛测试,不从 ENCUT 换算' in w for w in res['warnings'])
    assert 'CUTOFF' in text          # 仍写占位默认值


def test_cp2k_rel_cutoff_default(tmp_path):
    text, _ = _gen(tmp_path)
    assert 'REL_CUTOFF 60' in text


def test_cp2k_dispersion_vdw_section(tmp_path):
    text, _ = _gen(tmp_path, dispersion='D3')
    assert '&VDW_POTENTIAL' in text and 'TYPE DFTD3' in text


def test_cp2k_dispersion_bj(tmp_path):
    text, _ = _gen(tmp_path, dispersion='D3(BJ)')
    assert 'TYPE DFTD3(BJ)' in text


def test_cp2k_no_dispersion_no_vdw(tmp_path):
    text, _ = _gen(tmp_path, dispersion=None)
    assert '&VDW_POTENTIAL' not in text


def test_cp2k_molecule_poisson_nonperiodic(tmp_path):
    text, _ = _gen(tmp_path, structure=MOL10, periodic=False)
    assert 'PERIODIC NONE' in text and 'POISSON_SOLVER MT' in text


def test_cp2k_periodic_xyz(tmp_path):
    text, _ = _gen(tmp_path)
    assert 'PERIODIC XYZ' in text


def test_cp2k_multiplicity_uks(tmp_path):
    text, _ = _gen(tmp_path, spin=True, multiplicity=3)
    assert 'MULTIPLICITY 3' in text and 'UKS .TRUE.' in text


def test_cp2k_kind_gth_naming(tmp_path):
    text, _ = _gen(tmp_path)
    assert '&KIND Fe' in text and 'POTENTIAL GTH-PBE' in text
    assert 'BASIS_SET DZVP-MOLOPT-SR-GTH' in text


def test_cp2k_relax_geoopt_maxforce(tmp_path):
    text, _ = _gen(tmp_path, task='relax')
    assert '&MOTION' in text and '&GEO_OPT' in text and 'MAX_FORCE' in text


def test_cp2k_run_type_map(tmp_path):
    for task, rt in (('relax', 'GEO_OPT'), ('static', 'ENERGY_FORCE'),
                     ('freq', 'VIBRATIONAL_ANALYSIS')):
        text, _ = _gen(tmp_path, task=task)
        assert f'RUN_TYPE {rt}' in text


def test_cp2k_software_selfsupplied_warning(tmp_path):
    _, res = _gen(tmp_path)
    assert any('用户自备' in w for w in res['warnings'])


# ── 解析 ──────────────────────────────────────────────────────────────────────
def _out(tmp_path, text):
    (tmp_path / 'cp2k.out').write_text(text, encoding='utf-8')
    return get_backend('cp2k').parse_energy(str(tmp_path))


def test_cp2k_parse_hartree_to_ev(tmp_path):
    r = _out(tmp_path,
             ' SCF run converged in 10 steps\n'
             ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]:      -17.000000000000\n')
    assert r['energy_ev'] == pytest.approx(-17.0 * HARTREE_TO_EV)
    assert r['converged'] is True and r['error'] is None


def test_cp2k_parse_last_energy_wins(tmp_path):
    r = _out(tmp_path,
             ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]:      -10.0\n'
             ' SCF run converged in 8 steps\n'
             ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]:      -12.5\n')
    assert r['energy_ev'] == pytest.approx(-12.5 * HARTREE_TO_EV)


def test_cp2k_parse_geoopt_completed(tmp_path):
    r = _out(tmp_path,
             ' SCF run converged in 5 steps\n'
             ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]:      -5.0\n'
             ' *** GEOMETRY OPTIMIZATION COMPLETED ***\n')
    assert r['converged'] is True


def test_cp2k_parse_not_converged(tmp_path):
    r = _out(tmp_path,
             ' *** SCF run NOT converged ***\n'
             ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]:      -1.0\n')
    assert r['converged'] is False


def test_cp2k_parse_missing_output(tmp_path):
    r = get_backend('cp2k').parse_energy(str(tmp_path))
    assert r['energy_ev'] is None and r['error']


# ── check_inputs ──────────────────────────────────────────────────────────────
def test_cp2k_check_ok(tmp_path):
    _gen(tmp_path)
    assert get_backend('cp2k').check_inputs(str(tmp_path)) == []


def test_cp2k_check_missing_file(tmp_path):
    assert any('cp2k.inp' in s for s in get_backend('cp2k').check_inputs(str(tmp_path)))


def test_cp2k_check_missing_cutoff_section(tmp_path):
    (tmp_path / 'cp2k.inp').write_text(
        '&GLOBAL\n&END GLOBAL\n&FORCE_EVAL\n&DFT\n&END DFT\n'
        '&SUBSYS\n&CELL\n&END CELL\n&COORD\n&END COORD\n&KIND H\n&END KIND\n'
        '&END SUBSYS\n&END FORCE_EVAL\n', encoding='utf-8')
    assert any('CUTOFF' in s for s in get_backend('cp2k').check_inputs(str(tmp_path)))
