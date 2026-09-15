"""Gaussian Backend 测试:路线行 / 周期拒绝 / 电荷多重度 / SCF Done 解析 / 虚频计数。"""
import glob
import os

import pytest

from vcstudio.engines import get_backend
from vcstudio.engines.calcspec import HARTREE_TO_EV, CalcSpec
from vcstudio.engines.gaussian import build_gjf
from tests.test_engines_calcspec import make_poscar

MOL = make_poscar('water', [[12, 0, 0], [0, 12, 0], [0, 0, 12]], ['O', 'H'], [1, 2],
                  [[6.0, 6.0, 6.0], [6.96, 6.0, 6.0], [5.76, 6.93, 6.0]])
SLAB = make_poscar('slab', [[5, 0, 0], [0, 5, 0], [0, 0, 15]], ['Fe'], [1], [[0, 0, 5.0]])


def _spec(**kw):
    base = dict(structure=MOL, task='static', functional='PBE', periodic=False,
                charge=0, multiplicity=1)
    base.update(kw)
    return CalcSpec(**base)


def _route(spec):
    text, _ = build_gjf(spec)
    for ln in text.splitlines():
        if ln.lstrip().startswith('#'):
            return ln
    return ''


def test_gaussian_route_basic():
    route = _route(_spec())
    assert route.startswith('#P') and 'PBEPBE' in route and 'def2-SVP' in route and 'sp' in route


@pytest.mark.parametrize('functional,kw', [
    ('PBE', 'PBEPBE'), ('PBE0', 'PBE1PBE'), ('B3LYP', 'B3LYP'), ('TPSS', 'TPSSTPSS')])
def test_gaussian_functional_map(functional, kw):
    assert kw in _route(_spec(functional=functional))


def test_gaussian_unknown_functional_warns():
    text, warns = build_gjf(_spec(functional='FOOBAR'))
    assert any('未登记 Gaussian 泛函' in w for w in warns)


def test_gaussian_dispersion_keyword():
    route = _route(_spec(dispersion='D3'))
    assert 'PBEPBE-D3' not in route
    assert 'EmpiricalDispersion=GD3' in route


def test_gaussian_dispersion_warns_version():
    _, warns = build_gjf(_spec(dispersion='D3'))
    assert any('EmpiricalDispersion' in w for w in warns)


def test_gaussian_unknown_dispersion_is_rejected():
    with pytest.raises(ValueError, match='拒绝猜测'):
        build_gjf(_spec(dispersion='mystery'))


@pytest.mark.parametrize('task,kw', [('relax', 'opt'), ('static', 'sp'), ('freq', 'freq')])
def test_gaussian_job_keyword(task, kw):
    route = _route(_spec(task=task))
    assert route.split()[-1] == kw


def test_gaussian_basis_override():
    assert 'def2-TZVP' in _route(_spec(extras={'basis': 'def2-TZVP'}))


def test_gaussian_charge_multiplicity_line():
    text, _ = build_gjf(_spec(charge=-1, multiplicity=2))
    assert '-1 2' in text.splitlines()


def test_gaussian_coordinates_written():
    text, _ = build_gjf(_spec())
    body = text.splitlines()
    assert any(ln.strip().startswith('O ') for ln in body)
    assert sum(1 for ln in body if ln.strip()[:1] in ('O', 'H')) == 3


def test_gaussian_reject_periodic():
    with pytest.raises(ValueError, match='仅支持分子'):
        build_gjf(_spec(structure=SLAB, periodic=True, cutoff_ev=400))


def test_gaussian_generate_writes_gjf(tmp_path):
    res = get_backend('gaussian').generate_inputs(_spec(), str(tmp_path))
    assert glob.glob(os.path.join(str(tmp_path), '*.gjf'))
    assert any('用户自备' in w for w in res['warnings'])


# ── 解析 ──────────────────────────────────────────────────────────────────────
def _log(tmp_path, text):
    (tmp_path / 'job.log').write_text(text, encoding='utf-8')
    return get_backend('gaussian').parse_energy(str(tmp_path))


def test_gaussian_parse_scf_done_to_ev(tmp_path):
    r = _log(tmp_path,
             ' SCF Done:  E(RPBE) =  -76.0000000000     A.U. after 9 cycles\n'
             ' Normal termination of Gaussian 16\n')
    assert r['energy_ev'] == pytest.approx(-76.0 * HARTREE_TO_EV)
    assert r['converged'] is True and r['error'] is None


def test_gaussian_parse_last_scf_done(tmp_path):
    r = _log(tmp_path,
             ' SCF Done:  E(RPBE) =  -70.0  A.U.\n'
             ' SCF Done:  E(RPBE) =  -76.5  A.U.\n'
             ' Normal termination of Gaussian\n')
    assert r['energy_ev'] == pytest.approx(-76.5 * HARTREE_TO_EV)


def test_gaussian_parse_imaginary_freq_count(tmp_path):
    r = _log(tmp_path,
             ' SCF Done:  E(RPBE) =  -76.0  A.U.\n'
             ' Frequencies --   -180.4   -55.2   1200.0\n'
             ' Frequencies --   1500.0   1600.0   3700.0\n'
             ' Normal termination of Gaussian\n')
    assert r['n_imaginary'] == 2


def test_gaussian_parse_no_imaginary(tmp_path):
    r = _log(tmp_path,
             ' SCF Done:  E(RPBE) =  -76.0  A.U.\n'
             ' Frequencies --   120.0   1200.0   1300.0\n'
             ' Normal termination of Gaussian\n')
    assert r['n_imaginary'] == 0


def test_gaussian_parse_no_freq_none(tmp_path):
    r = _log(tmp_path, ' SCF Done:  E(RPBE) =  -76.0  A.U.\n Normal termination\n')
    assert r['n_imaginary'] is None


def test_gaussian_parse_error_termination(tmp_path):
    r = _log(tmp_path,
             ' SCF Done:  E(RPBE) =  -76.0  A.U.\n'
             ' Error termination via Lnk1e\n')
    assert r['converged'] is False and 'Error termination' in r['error']


def test_gaussian_opt_needs_stationary_point_not_just_normal_footer(tmp_path):
    (tmp_path / 'job.gjf').write_text(
        '#P PBEPBE/def2-SVP opt\n\njob\n\n0 1\nH 0 0 0\n\n', encoding='utf-8')
    r = _log(tmp_path,
             ' SCF Done: E(RHF) = -1.0 A.U.\n'
             ' Normal termination of Gaussian 16\n')
    assert r['converged'] is False and r['task'] == 'opt'
    assert '任务级完成证据' in r['error']


def test_gaussian_mp2_d_exponent_and_frequency_list(tmp_path):
    r = _log(tmp_path,
             ' SCF Done: E(RHF) = -2.0 A.U.\n'
             ' EUMP2 = -0.250000000D+01\n'
             ' Frequencies -- -120.0 500.0 1000.0\n'
             ' Normal termination of Gaussian 16\n')
    assert r['energy_ev'] == pytest.approx(-2.5 * HARTREE_TO_EV)
    assert r['energy_source'] == 'MP2'
    assert r['frequencies_cm1'] == [-120.0, 500.0, 1000.0]


def test_gaussian_last_orientation_is_extracted(tmp_path):
    r = _log(tmp_path,
             ' SCF Done: E(RHF) = -1.0 A.U.\n'
             ' Standard orientation:\n'
             ' ---------------------------------------------------------------------\n'
             ' Center     Atomic      Atomic             Coordinates (Angstroms)\n'
             ' Number     Number       Type             X           Y           Z\n'
             ' ---------------------------------------------------------------------\n'
             '      1          8           0        1.000000    2.000000    3.000000\n'
             ' ---------------------------------------------------------------------\n'
             ' Normal termination of Gaussian 16\n')
    assert r['final_structure']['atoms'][0]['element'] == 'O'
    assert r['final_structure']['atoms'][0]['xyz_angstrom'] == [1.0, 2.0, 3.0]


def test_gaussian_generate_reports_custom_checkpoint(tmp_path):
    result = get_backend('gaussian').generate_inputs(
        _spec(extras={'chk': 'wavefunction'}), str(tmp_path))
    assert 'water.log' in result['output_files']
    assert 'wavefunction.chk' in result['output_files']
    assert result['restart']['supported'] is False


def test_gaussian_parse_missing(tmp_path):
    r = get_backend('gaussian').parse_energy(str(tmp_path))
    assert r['energy_ev'] is None and r['error']


# ── check_inputs ──────────────────────────────────────────────────────────────
def test_gaussian_check_ok(tmp_path):
    get_backend('gaussian').generate_inputs(_spec(), str(tmp_path))
    assert get_backend('gaussian').check_inputs(str(tmp_path)) == []


def test_gaussian_check_missing_gjf(tmp_path):
    assert any('.gjf' in s for s in get_backend('gaussian').check_inputs(str(tmp_path)))
