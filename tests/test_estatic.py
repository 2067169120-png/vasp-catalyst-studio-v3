"""电子结构静态派生测试(generate.estatic):INCAR 逐键派生、KPOINTS 加密、ISMEAR 回退。"""
import yaml

import pytest

from vcstudio.generate import estatic
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared import manifest as manifest_mod

_CONTCAR = """\
slab Cu
1.0
 3.0000000 0.0000000 0.0000000
 0.0000000 3.0000000 0.0000000
 0.0000000 0.0000000 20.000000
 Cu
 2
Selective dynamics
Direct
 0.000000 0.000000 0.100000  T T T
 0.500000 0.500000 0.150000  T T T
"""

_INCAR = """\
SYSTEM = relax
ENCUT = 450
ISPIN = 2
MAGMOM = 2*0
GGA = PE
IBRION = 2
NSW = 120
ISIF = 2
ISMEAR = 1
SIGMA = 0.2
EDIFF = 1E-04
EDIFFG = -0.02
"""

_KPOINTS_33 = "Automatic\n0\nGamma\n3 3 1\n0 0 0\n"
_KPOINTS_11 = "Automatic\n0\nGamma\n1 1 1\n0 0 0\n"
_POTCAR = " PAW_PBE Cu 05Jan2001\n  11.0\n POMASS = 63.5; ZVAL = 11.0\n End of Dataset\n"


def _make_relax(tmp_path, kpoints=_KPOINTS_33, incar=_INCAR, contcar=_CONTCAR,
                potcar=_POTCAR):
    d = tmp_path / 'relax'
    d.mkdir()
    (d / 'CONTCAR').write_text(contcar, encoding='utf-8')
    (d / 'INCAR').write_text(incar, encoding='utf-8')
    if kpoints is not None:
        (d / 'KPOINTS').write_text(kpoints, encoding='utf-8')
    if potcar is not None:
        (d / 'POTCAR').write_text(potcar, encoding='utf-8')
    return d


def test_pdos_job_incar_keys_and_files(tmp_path):
    relax = _make_relax(tmp_path)
    out = tmp_path / 'static_pdos'
    res = estatic.build_static_job(relax, out, purpose='pdos', kpts_multiplier=2.0)
    for name in ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR', 'job.yaml'):
        assert (out / name).is_file(), name
    inc = parse_incar((out / 'INCAR').read_text(encoding='utf-8'))
    assert inc['NSW'] == 0
    assert inc['IBRION'] == -1
    assert inc['ISMEAR'] == -5                    # 3x3x1 ×2 → 7x7x1,积=49≥4
    assert inc['LORBIT'] == 11
    assert inc['NEDOS'] == 2000
    assert float(inc['EDIFF']) <= 1e-6
    # 保留电子学:ENCUT/ISPIN/MAGMOM/GGA 原样透传
    assert inc['ENCUT'] == 450 and inc['ISPIN'] == 2 and inc['GGA'] == 'PE'
    assert '2*0' in str(inc['MAGMOM'])
    # KPOINTS 加密 3x3x1 → 7x7x1
    assert '7 7 1' in (out / 'KPOINTS').read_text(encoding='utf-8')
    # POSCAR = CONTCAR;POTCAR 复制
    assert (out / 'POSCAR').read_text(encoding='utf-8') == _CONTCAR
    assert 'Cu' in (out / 'POTCAR').read_text(encoding='utf-8')
    assert res['out_dir'] == str(out)


def test_bader_job_flags(tmp_path):
    relax = _make_relax(tmp_path)
    out = tmp_path / 's_bader'
    estatic.build_static_job(relax, out, purpose='bader')
    inc = parse_incar((out / 'INCAR').read_text(encoding='utf-8'))
    assert inc['LAECHG'] is True
    assert inc['LCHARG'] is True


def test_chgdiff_and_esp_flags(tmp_path):
    relax = _make_relax(tmp_path)
    out_c = tmp_path / 's_chg'
    estatic.build_static_job(relax, out_c, purpose='chgdiff')
    inc_c = parse_incar((out_c / 'INCAR').read_text(encoding='utf-8'))
    assert inc_c['LCHARG'] is True
    out_e = tmp_path / 's_esp'
    estatic.build_static_job(relax, out_e, purpose='esp')
    inc_e = parse_incar((out_e / 'INCAR').read_text(encoding='utf-8'))
    assert inc_e['LVTOT'] is True


def test_ismear_fallback_when_few_kpoints(tmp_path):
    # 去掉母 SIGMA,验证回退 ISMEAR=0 时自动补 SIGMA=0.05
    incar = '\n'.join(ln for ln in _INCAR.splitlines() if not ln.startswith('SIGMA'))
    relax = _make_relax(tmp_path, kpoints=_KPOINTS_11, incar=incar)  # 1x1x1 → 积=1<4
    out = tmp_path / 's_gamma'
    res = estatic.build_static_job(relax, out, purpose='pdos')
    inc = parse_incar((out / 'INCAR').read_text(encoding='utf-8'))
    assert inc['ISMEAR'] == 0
    assert float(inc['SIGMA']) == pytest.approx(0.05)
    assert any('回退 ISMEAR=0' in w for w in res['warnings'])
    assert '1 1 1' in (out / 'KPOINTS').read_text(encoding='utf-8')


def test_ismear_fallback_preserves_user_sigma(tmp_path):
    relax = _make_relax(tmp_path, kpoints=_KPOINTS_11)   # 母 SIGMA=0.2 应保留
    out = tmp_path / 's_gamma2'
    estatic.build_static_job(relax, out, purpose='pdos')
    inc = parse_incar((out / 'INCAR').read_text(encoding='utf-8'))
    assert inc['ISMEAR'] == 0
    assert float(inc['SIGMA']) == pytest.approx(0.2)     # 保留电子学:不改用户 SIGMA


def test_kpts_multiplier_three(tmp_path):
    relax = _make_relax(tmp_path)
    out = tmp_path / 's_9'
    estatic.build_static_job(relax, out, purpose='pdos', kpts_multiplier=3.0)
    assert '9 9 1' in (out / 'KPOINTS').read_text(encoding='utf-8')


def test_ediff_already_tight_preserved(tmp_path):
    incar = _INCAR.replace('EDIFF = 1E-04', 'EDIFF = 1E-07')
    relax = _make_relax(tmp_path, incar=incar)
    out = tmp_path / 's_tight'
    estatic.build_static_job(relax, out, purpose='pdos')
    inc = parse_incar((out / 'INCAR').read_text(encoding='utf-8'))
    assert float(inc['EDIFF']) == pytest.approx(1e-7)   # 已 ≤1e-6,不放松


def test_job_yaml_records_provenance(tmp_path):
    relax = _make_relax(tmp_path)
    out = tmp_path / 's_yaml'
    estatic.build_static_job(relax, out, purpose='pdos')
    meta = yaml.safe_load((out / 'job.yaml').read_text(encoding='utf-8'))
    # 标准 manifest 核心字段：可直接进入提交/监控/报告状态机。
    assert meta['schema'] == manifest_mod.SCHEMA_VERSION
    assert meta['state'] == 'CREATED'
    assert meta['task_type'] == 'dos_pdos'
    assert meta['calc_type'] == 'slab'
    assert meta['state_history'][0]['state'] == 'CREATED'
    assert meta['inputs']['engine'] == 'vasp'
    assert meta['inputs']['parent_job'] == str(relax.resolve())
    assert meta['inputs']['purpose'] == 'pdos'
    assert set(meta['inputs']['files']) == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    assert set(meta['inputs']['sha256']) == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    assert meta['inputs']['poscar_sha256'] == manifest_mod.sha256_file(out / 'POSCAR')
    # 旧扩展键仍可读，但同一事实以 inputs/derivation 为规范位置。
    assert meta['parent'] == str(relax)
    assert meta['purpose'] == 'pdos'
    assert meta['kpoints'] == [7, 7, 1]
    assert any('NSW' in c for c in meta['changes'])
    assert any('LORBIT' in c for c in meta['changes'])
    assert meta['derivation']['changes'] == meta['changes']


@pytest.mark.parametrize(('purpose', 'task_type'), [
    ('pdos', 'dos_pdos'), ('bader', 'bader'), ('chgdiff', 'chgdiff'),
    ('esp', 'static'), ('elf', 'elf'),
])
def test_purpose_maps_to_canonical_manifest_task_type(tmp_path, purpose, task_type):
    relax = _make_relax(tmp_path)
    out = tmp_path / f's_{purpose}'
    estatic.build_static_job(relax, out, purpose=purpose)
    assert manifest_mod.load_manifest(out)['task_type'] == task_type


def test_static_manifest_inherits_parent_calc_type_and_extra_metadata(tmp_path):
    relax = _make_relax(tmp_path)
    parent = manifest_mod.new_manifest(
        job_id='parent', system='Cu cluster', task_type='relax', calc_type='molecule',
        inputs={})
    manifest_mod.save_manifest(relax, parent)
    out = tmp_path / 'derived'
    estatic.build_static_job(
        relax, out, purpose='bader', extra_meta={'member_role': 'reference'})
    meta = manifest_mod.load_manifest(out)
    assert meta['calc_type'] == 'molecule'
    assert meta['inputs']['derived_metadata'] == {'member_role': 'reference'}
    assert meta['derivation']['member_role'] == 'reference'
    assert meta['member_role'] == 'reference'       # 旧扩展读取兼容


def test_bad_purpose_rejected(tmp_path):
    relax = _make_relax(tmp_path)
    with pytest.raises(ValueError, match='purpose'):
        estatic.build_static_job(relax, tmp_path / 'x', purpose='wavefun')


def test_missing_contcar_and_poscar_raises(tmp_path):
    d = tmp_path / 'empty'
    d.mkdir()
    (d / 'INCAR').write_text(_INCAR, encoding='utf-8')
    with pytest.raises(ValueError, match='CONTCAR/POSCAR'):
        estatic.build_static_job(d, tmp_path / 'x', purpose='pdos')


def test_poscar_fallback_when_no_contcar(tmp_path):
    d = tmp_path / 'relax2'
    d.mkdir()
    (d / 'POSCAR').write_text(_CONTCAR, encoding='utf-8')     # 只有 POSCAR
    (d / 'INCAR').write_text(_INCAR, encoding='utf-8')
    (d / 'KPOINTS').write_text(_KPOINTS_33, encoding='utf-8')
    res = estatic.build_static_job(d, tmp_path / 's_fb', purpose='pdos')
    assert any('回退用 POSCAR' in w for w in res['warnings'])


def test_kpoints_fallback_recommend_when_missing(tmp_path):
    relax = _make_relax(tmp_path, kpoints=None)              # 无母 KPOINTS
    out = tmp_path / 's_rec'
    res = estatic.build_static_job(relax, out, purpose='pdos')
    txt = (out / 'KPOINTS').read_text(encoding='utf-8')
    assert txt.strip().splitlines()[-1] == '0 0 0'           # 合法自动网格体
    assert any('重新推荐' in w for w in res['warnings'])
