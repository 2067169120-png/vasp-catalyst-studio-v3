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
    assert 'sedc_apply           : true' in param and 'sedc_scheme' in param


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
                    'Geometry optimization completed successfully\n')
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
