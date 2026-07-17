"""VASP Backend(参照实现)测试:生成 INCAR/KPOINTS/POSCAR + 解析合成 OSZICAR/OUTCAR。"""
import os

import pytest

from vcstudio.engines import get_backend
from vcstudio.engines.calcspec import CalcSpec
from vcstudio.generate.incar_builder import parse_incar
from tests.test_engines_calcspec import H2O_MOL, SLAB


def _spec(**kw):
    base = dict(structure=SLAB, task='relax', functional='PBE', periodic=True,
                cutoff_ev=520, kpoints=(3, 3, 1))
    base.update(kw)
    return CalcSpec(**base)


def _incar(tmp_path, spec):
    get_backend('vasp').generate_inputs(spec, str(tmp_path))
    with open(tmp_path / 'INCAR', encoding='utf-8') as f:
        return parse_incar(f.read())


def test_vasp_generate_writes_three_files(tmp_path):
    res = get_backend('vasp').generate_inputs(_spec(), str(tmp_path))
    names = {os.path.basename(f) for f in res['files']}
    assert names == {'INCAR', 'KPOINTS', 'POSCAR'}
    for n in names:
        assert (tmp_path / n).is_file()


def test_vasp_potcar_warning_present(tmp_path):
    res = get_backend('vasp').generate_inputs(_spec(), str(tmp_path))
    assert any('POTCAR' in w for w in res['warnings'])


def test_vasp_encut_from_cutoff(tmp_path):
    assert _incar(tmp_path, _spec(cutoff_ev=550))['ENCUT'] == 550


@pytest.mark.parametrize('functional,tag,val', [
    ('PBE', 'GGA', 'PE'), ('RPBE', 'GGA', 'RP'), ('PBEsol', 'GGA', 'PS'),
    ('SCAN', 'METAGGA', 'SCAN')])
def test_vasp_functional_map(tmp_path, functional, tag, val):
    incar = _incar(tmp_path, _spec(functional=functional))
    assert str(incar[tag]).upper() == val


def test_vasp_unknown_functional_warns_no_gga(tmp_path):
    res = get_backend('vasp').generate_inputs(_spec(functional='MADEUP'), str(tmp_path))
    incar = parse_incar(open(tmp_path / 'INCAR', encoding='utf-8').read())
    assert 'GGA' not in incar and 'METAGGA' not in incar
    assert any('未识别泛函' in w for w in res['warnings'])


@pytest.mark.parametrize('disp,ivdw', [('D3', 11), ('D3(BJ)', 12), ('TS', 2)])
def test_vasp_dispersion_ivdw(tmp_path, disp, ivdw):
    assert _incar(tmp_path, _spec(dispersion=disp))['IVDW'] == ivdw


def test_vasp_dispersion_none_no_ivdw(tmp_path):
    assert 'IVDW' not in _incar(tmp_path, _spec(dispersion=None))


@pytest.mark.parametrize('task,ibrion,nsw', [
    ('relax', 2, 200), ('static', -1, 0), ('freq', 5, 1)])
def test_vasp_task_tags(tmp_path, task, ibrion, nsw):
    incar = _incar(tmp_path, _spec(task=task))
    assert incar['IBRION'] == ibrion and incar['NSW'] == nsw


def test_vasp_spin_ispin(tmp_path):
    assert _incar(tmp_path, _spec(spin=True))['ISPIN'] == 2
    assert _incar(tmp_path, _spec(spin=False))['ISPIN'] == 1


def test_vasp_ediffg_from_force(tmp_path):
    incar = _incar(tmp_path, _spec(convergence={'energy_ev': 1e-6, 'force_ev_a': 0.03}))
    assert float(incar['EDIFFG']) == pytest.approx(-0.03)


def test_vasp_extras_incar_override(tmp_path):
    incar = _incar(tmp_path, _spec(extras={'incar': {'ISMEAR': 1, 'SIGMA': 0.2}}))
    assert incar['ISMEAR'] == 1 and float(incar['SIGMA']) == pytest.approx(0.2)


def test_vasp_charge_warns(tmp_path):
    res = get_backend('vasp').generate_inputs(_spec(charge=1), str(tmp_path))
    assert any('NELECT' in w for w in res['warnings'])


def test_vasp_molecule_kpoints_gamma(tmp_path):
    spec = CalcSpec(structure=H2O_MOL, task='static', functional='PBE',
                    periodic=False, cutoff_ev=400)
    get_backend('vasp').generate_inputs(spec, str(tmp_path))
    assert '1 1 1' in open(tmp_path / 'KPOINTS', encoding='utf-8').read()


def test_vasp_periodic_kpoints_passthrough(tmp_path):
    get_backend('vasp').generate_inputs(_spec(kpoints=(5, 5, 2)), str(tmp_path))
    assert '5 5 2' in open(tmp_path / 'KPOINTS', encoding='utf-8').read()


def test_vasp_poscar_verbatim(tmp_path):
    get_backend('vasp').generate_inputs(_spec(), str(tmp_path))
    out = open(tmp_path / 'POSCAR', encoding='utf-8').read()
    assert out.startswith('Fe slab')


# ── 解析(合成 OSZICAR/OUTCAR) ───────────────────────────────────────────────
def _write(tmp_path, oszicar=None, outcar=None):
    if oszicar is not None:
        (tmp_path / 'OSZICAR').write_text(oszicar, encoding='utf-8')
    if outcar is not None:
        (tmp_path / 'OUTCAR').write_text(outcar, encoding='utf-8')


def test_vasp_parse_roundtrip_converged(tmp_path):
    _write(tmp_path,
           oszicar='   1 F= -.10E+02 E0= -.834521E+02  d E =0.0\n',
           outcar='reached required accuracy - stopping structural energy minimisation\n'
                  'General timing and accounting informations for this job:\n')
    r = get_backend('vasp').parse_energy(str(tmp_path))
    assert r['energy_ev'] == pytest.approx(-83.4521)
    assert r['converged'] is True and r['error'] is None


def test_vasp_parse_static_ediff_reached(tmp_path):
    _write(tmp_path,
           oszicar='   1 F= -.5E+01 E0= -.100000E+02  d E =0.0\n',
           outcar='aborting loop because EDIFF is reached\n'
                  'General timing and accounting informations\n')
    r = get_backend('vasp').parse_energy(str(tmp_path))
    assert r['energy_ev'] == pytest.approx(-10.0) and r['converged'] is True


def test_vasp_parse_toten_fallback(tmp_path):
    # 无 OSZICAR:退 OUTCAR TOTEN
    _write(tmp_path, outcar='  free  energy   TOTEN  =       -42.500000 eV\n'
                           'General timing and accounting\n')
    r = get_backend('vasp').parse_energy(str(tmp_path))
    assert r['energy_ev'] == pytest.approx(-42.5)


def test_vasp_parse_not_converged(tmp_path):
    _write(tmp_path, oszicar='   1 F= -.5E+01 E0= -.100000E+02  d E =0.0\n',
           outcar='  ...running...\n')
    r = get_backend('vasp').parse_energy(str(tmp_path))
    assert r['converged'] is False


def test_vasp_parse_missing_outputs(tmp_path):
    r = get_backend('vasp').parse_energy(str(tmp_path))
    assert r['energy_ev'] is None and r['converged'] is False and r['error']


# ── check_inputs ──────────────────────────────────────────────────────────────
def test_vasp_check_flags_missing_potcar(tmp_path):
    get_backend('vasp').generate_inputs(_spec(), str(tmp_path))
    issues = get_backend('vasp').check_inputs(str(tmp_path))
    assert any('POTCAR' in s for s in issues)


def test_vasp_check_missing_encut(tmp_path):
    (tmp_path / 'INCAR').write_text('SYSTEM = x\nGGA = PE\n', encoding='utf-8')
    (tmp_path / 'KPOINTS').write_text('Auto\n0\nGamma\n3 3 1\n0 0 0\n', encoding='utf-8')
    (tmp_path / 'POSCAR').write_text(SLAB, encoding='utf-8')
    issues = get_backend('vasp').check_inputs(str(tmp_path))
    assert any('ENCUT' in s for s in issues)
