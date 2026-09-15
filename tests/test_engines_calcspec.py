"""引擎无关 IR 测试:CalcSpec 校验各分支 / 跨引擎不等价清单 / 结构解析。

全部合成 POSCAR 离线(纯 python 拼串,不经 numpy),覆盖分子与周期两路。
"""
import pytest

from vcstudio.engines import calcspec
from vcstudio.engines.calcspec import (
    NONEQUIV_MAP, CalcSpec, get_run_contract, nonequivalence_report,
    parse_structure, validate,
)


# ── 合成 POSCAR 工具(纯 python) ──────────────────────────────────────────────
def make_poscar(comment, cell, species, counts, coords, *, mode='Cartesian', sd=None):
    lines = [comment, '1.0']
    for v in cell:
        lines.append(f'  {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}')
    lines.append('  ' + ' '.join(species))
    lines.append('  ' + ' '.join(str(c) for c in counts))
    if sd is not None:
        lines.append('Selective dynamics')
    lines.append(mode)
    for i, c in enumerate(coords):
        row = f'  {c[0]:.6f} {c[1]:.6f} {c[2]:.6f}'
        if sd is not None:
            row += f'  {sd[i]}'
        lines.append(row)
    return '\n'.join(lines) + '\n'


CUBE = [[12.0, 0, 0], [0, 12.0, 0], [0, 0, 12.0]]
H2O_MOL = make_poscar('H2O molecule', CUBE, ['O', 'H'], [1, 2],
                      [[6.0, 6.0, 6.0], [6.96, 6.0, 6.0], [5.76, 6.93, 6.0]])
SLAB = make_poscar('Fe slab', [[5.0, 0, 0], [0, 5.0, 0], [0, 0, 15.0]],
                   ['Fe', 'O'], [1, 1], [[0, 0, 5.0], [2.5, 2.5, 5.0]])


def _molecule_spec(**kw):
    base = dict(structure=H2O_MOL, task='static', functional='PBE',
                periodic=False, kpoints=None, multiplicity=1, spin=False)
    base.update(kw)
    return CalcSpec(**base)


def _periodic_spec(**kw):
    base = dict(structure=SLAB, task='relax', functional='PBE', periodic=True,
                cutoff_ev=500, kpoints=(3, 3, 1))
    base.update(kw)
    return CalcSpec(**base)


# ── validate 分支 ─────────────────────────────────────────────────────────────
def test_validate_ok_molecule():
    assert validate(_molecule_spec()) == []


def test_validate_ok_periodic():
    assert validate(_periodic_spec()) == []


def test_validate_bad_task():
    issues = validate(_periodic_spec(task='dos'))
    assert any('任务类型非法' in s for s in issues)


def test_validate_periodic_missing_cutoff():
    issues = validate(_periodic_spec(cutoff_ev=None))
    assert any('cutoff_ev' in s and '周期' in s for s in issues)


def test_engine_private_cp2k_cutoff_does_not_weaken_engine_neutral_validation():
    spec = _periodic_spec(cutoff_ev=None, extras={'cutoff_ry': 500})
    assert any('cutoff_ev' in issue for issue in validate(spec))


def test_validate_rejects_noninteger_kmesh_and_charge():
    issues = validate(_periodic_spec(kpoints=(3, 2.5, 1), charge=0.5))
    assert any('kpoints' in issue for issue in issues)
    assert any('charge' in issue for issue in issues)


def test_run_contracts_define_command_outputs_and_explicit_restart_policy():
    gaussian = get_run_contract('g16')
    assert gaussian.primary_input(['mol.gjf']) == 'mol.gjf'
    assert gaussian.result_files(['mol.gjf']) == ('mol.log', 'mol.chk')
    assert gaussian.restart_supported is False and gaussian.restart_note
    assert get_run_contract('castep').result_files(
        ['seed.cell', 'seed.param'], task='freq') == (
            'seed.castep', 'seed.check', 'seed.phonon')


def test_validate_periodic_nonpositive_cutoff():
    assert any('cutoff_ev' in s for s in validate(_periodic_spec(cutoff_ev=0)))


def test_validate_molecule_with_kpoints():
    issues = validate(_molecule_spec(kpoints=(2, 2, 2)))
    assert any('kpoints' in s and '分子' in s for s in issues)


def test_validate_bad_multiplicity():
    issues = validate(_molecule_spec(multiplicity=0))
    assert any('多重度' in s for s in issues)


def test_validate_open_shell_needs_spin():
    issues = validate(_molecule_spec(multiplicity=3, spin=False))
    assert any('开壳' in s and 'spin' in s for s in issues)


def test_validate_open_shell_with_spin_ok():
    assert validate(_molecule_spec(multiplicity=3, spin=True)) == []


def test_validate_periodic_multiplicity_ignored():
    # spin=True 隔离开壳分支,只留"周期忽略多重度"告警
    issues = validate(_periodic_spec(multiplicity=3, spin=True))
    assert any('周期' in s and '多重度' in s for s in issues)


def test_validate_bad_convergence():
    issues = validate(_periodic_spec(convergence={'energy_ev': -1, 'force_ev_a': 0.02}))
    assert any("energy_ev" in s for s in issues)


def test_validate_multiple_issues_accumulate():
    issues = validate(_periodic_spec(task='band', cutoff_ev=None))
    assert len(issues) >= 2


# ── 不等价清单 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('dst', ['cp2k', 'gaussian', 'castep'])
def test_nonequivalence_vasp_pairs_nonempty_chinese(dst):
    rep = nonequivalence_report('vasp', dst)
    assert rep, f'vasp→{dst} 清单不应为空'
    # 含中文字符
    assert any(any('一' <= ch <= '鿿' for ch in item) for item in rep)


def test_nonequivalence_vasp_cp2k_has_cutoff_and_pseudo():
    entries = NONEQUIV_MAP[('vasp', 'cp2k')]
    fields = {e['field'] for e in entries}
    assert {'cutoff', 'pseudopotential'} <= fields
    cutoff_note = next(e['note'] for e in entries if e['field'] == 'cutoff')
    assert 'ENCUT' in cutoff_note and 'CUTOFF' in cutoff_note


def test_nonequivalence_symmetric_lookup():
    fwd = nonequivalence_report('vasp', 'cp2k')
    rev = nonequivalence_report('cp2k', 'vasp')
    assert len(fwd) == len(rev) and fwd == rev


def test_nonequivalence_same_engine_empty():
    assert nonequivalence_report('vasp', 'vasp') == []


def test_nonequivalence_unlisted_pair_generic_nonempty():
    rep = nonequivalence_report('cp2k', 'gaussian')
    assert len(rep) == 1 and '尚未登记' in rep[0]


def test_nonequivalence_case_insensitive():
    assert nonequivalence_report('VASP', 'CP2K') == nonequivalence_report('vasp', 'cp2k')


# ── 结构解析 ──────────────────────────────────────────────────────────────────
def test_parse_structure_molecule_fields():
    s = parse_structure(H2O_MOL)
    assert s['elements'] == ['O', 'H', 'H']
    assert s['sd'] is None
    assert s['comment'] == 'H2O molecule'
    assert s['cart'][0] == pytest.approx([6.0, 6.0, 6.0])


def test_parse_structure_frac_roundtrip():
    # 立方 10 Å 盒,笛卡尔 (5,5,5) → 分数 (0.5,0.5,0.5)
    poscar = make_poscar('c', [[10, 0, 0], [0, 10, 0], [0, 0, 10]],
                         ['H'], [1], [[5.0, 5.0, 5.0]])
    s = parse_structure(poscar)
    assert s['frac'][0] == pytest.approx([0.5, 0.5, 0.5])


def test_parse_structure_sd_flags():
    poscar = make_poscar('slab', [[5, 0, 0], [0, 5, 0], [0, 0, 15]],
                         ['Fe'], [2], [[0, 0, 2.0], [2.5, 2.5, 5.0]],
                         sd=['F F F', 'T T T'])
    s = parse_structure(poscar)
    assert s['sd'] == ['F F F', 'T T T']


def test_parse_structure_vasp4_raises():
    vasp4 = 'no elem line\n1.0\n5 0 0\n0 5 0\n0 0 5\n2\nCartesian\n0 0 0\n1 1 1\n'
    with pytest.raises(ValueError):
        parse_structure(vasp4)


def test_structure_species():
    els, cnts = calcspec.structure_species(H2O_MOL)
    assert els == ['O', 'H'] and cnts == [1, 2]
