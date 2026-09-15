"""CASTEP(Materials Studio)Backend 测试:晶格/分数坐标 / FIX 约束 / param 键 / Final energy。"""
import glob
import os

import pytest

from vcstudio.engines import get_backend
from vcstudio.engines.calcspec import CalcSpec
from vcstudio.engines.castep import build_cell, build_param
from tests.test_engines_calcspec import make_poscar

# 立方 10 Å + H(便于核对分数坐标 (0.3,0.4,0.5))
CUBE10 = make_poscar('probe', [[10, 0, 0], [0, 10, 0], [0, 0, 10]],
                     ['H'], [1], [[3.0, 4.0, 5.0]])
# Fe2O slab,前两 Fe 冻结(F F F),O 放开
SLAB_SD = make_poscar('FeO', [[5, 0, 0], [0, 5, 0], [0, 0, 15]], ['Fe', 'O'], [2, 1],
                      [[0, 0, 2.0], [2.5, 2.5, 2.0], [1.25, 1.25, 5.0]],
                      sd=['F F F', 'F F F', 'T T T'])
SLAB = make_poscar('FeO', [[5, 0, 0], [0, 5, 0], [0, 0, 15]], ['Fe', 'O'], [1, 1],
                   [[0, 0, 2.0], [2.5, 2.5, 5.0]])


def _spec(**kw):
    base = dict(structure=SLAB, task='relax', functional='PBE', periodic=True,
                cutoff_ev=500, kpoints=(4, 4, 1))
    base.update(kw)
    return CalcSpec(**base)


def _constraint_lines(cell):
    """抽取 IONIC_CONSTRAINTS 块内的约束行(排除晶格/坐标行)。"""
    lines = cell.splitlines()
    if '%BLOCK IONIC_CONSTRAINTS' not in cell:
        return []
    start = next(i for i, ln in enumerate(lines)
                 if 'BLOCK IONIC_CONSTRAINTS' in ln and 'ENDBLOCK' not in ln)
    end = next(i for i, ln in enumerate(lines) if 'ENDBLOCK IONIC_CONSTRAINTS' in ln)
    return [ln.strip() for ln in lines[start + 1:end] if ln.strip()]


def test_castep_lattice_cart():
    cell, _ = build_cell(_spec(structure=CUBE10))
    assert '%BLOCK LATTICE_CART' in cell and '%ENDBLOCK LATTICE_CART' in cell
    assert '10.00000000 0.00000000 0.00000000' in cell


def test_castep_positions_frac():
    cell, _ = build_cell(_spec(structure=CUBE10))
    assert '%BLOCK POSITIONS_FRAC' in cell
    assert 'H  0.30000000 0.40000000 0.50000000' in cell


def test_castep_kpoints_grid():
    cell, _ = build_cell(_spec(kpoints=(6, 6, 2)))
    assert 'KPOINTS_MP_GRID 6 6 2' in cell


def test_castep_kpoints_default_gamma():
    cell, _ = build_cell(_spec(kpoints=None))
    assert 'KPOINTS_MP_GRID 1 1 1' in cell


def test_castep_fix_constraints_from_sd():
    cell, warns = build_cell(_spec(structure=SLAB_SD))
    assert '%BLOCK IONIC_CONSTRAINTS' in cell
    # 两个冻结 Fe → 6 条约束(每原子 3 方向);带引擎内元素序号 Fe 1 / Fe 2
    body = _constraint_lines(cell)
    assert len(body) == 6
    assert any('Fe 1' in ln for ln in body) and any('Fe 2' in ln for ln in body)
    assert any('冻结' in w for w in warns)


def test_castep_no_constraints_without_sd():
    cell, _ = build_cell(_spec(structure=SLAB))
    assert 'IONIC_CONSTRAINTS' not in cell


def test_castep_partial_fix():
    # 单原子仅 z 固定 (T T F) → 1 条约束(z 轴)
    poscar = make_poscar('x', [[5, 0, 0], [0, 5, 0], [0, 0, 5]], ['H'], [1],
                         [[1.0, 1.0, 1.0]], sd=['T T F'])
    cell, _ = build_cell(_spec(structure=poscar))
    body = _constraint_lines(cell)
    assert len(body) == 1 and '0.0 0.0 1.0' in body[0]


def test_castep_param_core_keys():
    param, _ = build_param(_spec())
    for key in ('task', 'xc_functional', 'cut_off_energy', 'elec_energy_tol',
                'geom_force_tol'):
        assert key in param


@pytest.mark.parametrize('task,castep_task', [
    ('relax', 'GeometryOptimization'), ('static', 'SinglePoint'), ('freq', 'Phonon')])
def test_castep_task_map(task, castep_task):
    param, _ = build_param(_spec(task=task))
    assert f'task                 : {castep_task}' in param


def test_castep_xc_functional():
    assert 'xc_functional        : RPBE' in build_param(_spec(functional='RPBE'))[0]


def test_castep_cutoff_ev_and_warning():
    param, warns = build_param(_spec(cutoff_ev=600))
    assert 'cut_off_energy       : 600 eV' in param
    assert any('PAW' in w and '仅同引擎内可比' in w for w in warns)


def test_castep_spin():
    param, _ = build_param(_spec(spin=True, multiplicity=3))
    assert 'spin_polarized       : true' in param and 'spin                 : 2' in param


def test_castep_dispersion_sedc():
    param, _ = build_param(_spec(dispersion='D3'))
    assert 'sedc_apply           : true' in param and 'sedc_scheme          : D3' in param


def test_castep_d3bj_and_unknown_dispersion():
    assert 'sedc_scheme          : D3-BJ' in build_param(_spec(dispersion='D3(BJ)'))[0]
    with pytest.raises(ValueError, match='拒绝静默'):
        build_param(_spec(dispersion='unknown'))


def test_castep_unknown_xc_and_invalid_numeric_parameters_fail_closed():
    with pytest.raises(ValueError, match='xc_functional'):
        build_param(_spec(functional='made-up-xc'))
    with pytest.raises(ValueError, match='cut_off_energy'):
        build_param(_spec(cutoff_ev=float('nan')))
    with pytest.raises(ValueError, match='geom_max_iter'):
        build_param(_spec(task='relax', extras={'geom_max_iter': 1.5}))


def test_castep_d3_requires_registered_functional_parameters():
    with pytest.raises(ValueError, match='D3'):
        build_param(_spec(functional='RPBE', dispersion='D3'))


def test_castep_nonperiodic_is_explicitly_rejected():
    with pytest.raises(ValueError, match='periodic=False'):
        build_cell(_spec(periodic=False, kpoints=None))


def test_castep_param_passthrough_cannot_duplicate_scientific_keys():
    with pytest.raises(ValueError, match='重复键'):
        build_param(_spec(extras={'param': {'task': 'SinglePoint'}}))


def test_castep_generate_writes_cell_param(tmp_path):
    res = get_backend('castep').generate_inputs(_spec(), str(tmp_path))
    assert glob.glob(os.path.join(str(tmp_path), '*.cell'))
    assert glob.glob(os.path.join(str(tmp_path), '*.param'))
    assert any('用户自备' in w and 'Materials Studio' in w for w in res['warnings'])


def test_castep_seedname_from_extras(tmp_path):
    get_backend('castep').generate_inputs(_spec(extras={'seedname': 'myjob'}), str(tmp_path))
    assert os.path.isfile(os.path.join(str(tmp_path), 'myjob.cell'))


# ── 解析(.castep 报 eV,不作 Hartree 换算) ──────────────────────────────────
def _castep_out(tmp_path, text):
    (tmp_path / 'case.castep').write_text(text, encoding='utf-8')
    return get_backend('castep').parse_energy(str(tmp_path))


def test_castep_parse_final_energy(tmp_path):
    r = _castep_out(tmp_path,
                    'Final energy, E             =  -1234.5678901234     eV\n'
                    'Geometry optimization completed successfully\n'
                    'Total time = 12.3 s\n')
    assert r['energy_ev'] == pytest.approx(-1234.5678901234)   # 已是 eV,不换算
    assert r['converged'] is True and r['error'] is None


def test_castep_parse_singlepoint_finished(tmp_path):
    r = _castep_out(tmp_path,
                    'Final energy, E             =  -500.0     eV\n'
                    'Total time =    12.3 s\n')
    assert r['energy_ev'] == pytest.approx(-500.0) and r['converged'] is True


def test_castep_parse_last_energy(tmp_path):
    r = _castep_out(tmp_path,
                    'Final energy, E             =  -100.0     eV\n'
                    'Final energy, E             =  -120.0     eV\n'
                    'Geometry optimization completed successfully\n')
    assert r['energy_ev'] == pytest.approx(-120.0)


def test_castep_energy_convention_prefers_zero_k_estimate(tmp_path):
    r = _castep_out(tmp_path,
                    'Final energy, E = -100.0 eV\n'
                    'Final free energy (E-TS) = -101.0 eV\n'
                    'NB est. 0K energy (E-0.5TS) = -100.5 eV\n'
                    'Total time = 1.0 s\n')
    assert r['energy_ev'] == pytest.approx(-100.5)
    assert r['energy_source'] == 'NB est. 0K energy'


def test_castep_relax_total_time_without_geo_convergence_is_not_done(tmp_path):
    (tmp_path / 'case.param').write_text(
        'task : GeometryOptimization\n', encoding='utf-8')
    r = _castep_out(tmp_path,
                    'Final energy, E = -100.0 eV\nTotal time = 1.0 s\n')
    assert r['converged'] is False and r['task_converged'] is False


def test_castep_phonon_file_and_geom_structure_are_parsed(tmp_path):
    (tmp_path / 'case.param').write_text('task : Phonon\n', encoding='utf-8')
    (tmp_path / 'case.phonon').write_text(
        'q-pt= 1 0.0 0.0 0.0\n1 -15.0\n2 100.0\n', encoding='utf-8')
    (tmp_path / 'case.geom').write_text(
        '-1.0 <-- E\n10 0 0 <-- h\n0 10 0 <-- h\n0 0 10 <-- h\n'
        'H 1 1.0 2.0 3.0 <-- R\n', encoding='utf-8')
    r = _castep_out(tmp_path,
                    'Final energy, E = -100.0 eV\nTotal time = 1.0 s\n')
    assert r['converged'] is True and r['n_imaginary'] == 1
    xyz = r['final_structure']['atoms'][0]['xyz_angstrom']
    assert xyz == pytest.approx([0.529177210903, 1.058354421806, 1.587531632709])


def test_castep_phonon_file_does_not_double_count_castep_echo(tmp_path):
    (tmp_path / 'case.param').write_text('task : Phonon\n', encoding='utf-8')
    (tmp_path / 'case.phonon').write_text(
        'q-pt= 1 0.0 0.0 0.0\n1 -15.0\n2 100.0\n', encoding='utf-8')
    r = _castep_out(tmp_path,
                    'Final energy, E = -100.0 eV\n'
                    'q-pt= 1 0.0 0.0 0.0\n1 -15.0\n2 100.0\n'
                    'Total time = 1.0 s\n')
    assert r['frequencies_cm1'] == [-15.0, 100.0]
    assert r['n_imaginary'] == 1 and r['converged'] is True


def test_castep_check_rejects_mismatched_seed_pair(tmp_path):
    (tmp_path / 'a.cell').write_text(
        '%BLOCK LATTICE_CART\n%ENDBLOCK LATTICE_CART\n'
        '%BLOCK POSITIONS_FRAC\n%ENDBLOCK POSITIONS_FRAC\nKPOINTS_MP_GRID 1 1 1\n',
        encoding='utf-8')
    (tmp_path / 'b.param').write_text(
        'task: SinglePoint\nxc_functional: PBE\ncut_off_energy: 500 eV\n', encoding='utf-8')
    assert any('同名' in issue for issue in get_backend('castep').check_inputs(str(tmp_path)))


def test_castep_parse_missing(tmp_path):
    r = get_backend('castep').parse_energy(str(tmp_path))
    assert r['energy_ev'] is None and r['error']


# ── check_inputs ──────────────────────────────────────────────────────────────
def test_castep_check_ok(tmp_path):
    get_backend('castep').generate_inputs(_spec(), str(tmp_path))
    assert get_backend('castep').check_inputs(str(tmp_path)) == []


def test_castep_check_missing_files(tmp_path):
    issues = get_backend('castep').check_inputs(str(tmp_path))
    assert any('.cell' in s for s in issues) and any('.param' in s for s in issues)
