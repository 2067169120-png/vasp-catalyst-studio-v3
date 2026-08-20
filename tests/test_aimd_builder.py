"""AIMD 作业生成端测试(generate.aimd_builder):MD INCAR 逐键派生(NVT/NVE)/ changes 留痕 /
电子学参数保留 / CONTCAR 回退 / Γ 点 KPOINTS / manifest 溯源 / OSZICAR 能量-时间解析。"""
import pytest

from vcstudio.generate import aimd_builder as ab
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared import manifest as manifest_mod

# 单原子催化剂弛豫末构型(Cu 载体 + 单 O),Cartesian。
_SLAB = """SAC slab
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu O
4 1
Cartesian
0.0 0.0 2.0
1.5 1.5 4.0
0.0 0.0 6.0
1.5 1.5 8.0
0.0 0.0 11.0
"""

# 弛豫 INCAR(含电子学 + 离子学 + 应力/判据)。
_RELAX_INCAR = """SYSTEM = SACslab
ENCUT = 500
GGA = PE
ISPIN = 2
MAGMOM = 4*0 1*0
IVDW = 12
LDAUU = 4 0
IBRION = 2
NSW = 200
ISIF = 3
POTIM = 0.5
EDIFF = 1E-05
EDIFFG = -0.02
"""

# 逼真 OSZICAR:电子自洽子行(DAV) + 3 条 MD 汇总行(N T= ... E= ...)。
_OSZICAR = """       N       E                     dE             d eps       ncg     rms
DAV:   1     0.114E+04   -0.11E+04   -0.55E+03   120   0.63E+02
DAV:   2    -0.512E+02   -0.16E+03    0.31E+02   140   0.11E+02
   1 T=  300. E= -.11453958E+03 F= -.11552627E+03 E0= -.11552627E+03  EK= 0.98668E+00 SP= 0.00 SK= 0.00
DAV:   1    -0.114E+03   -0.12E+00    0.90E-02   130   0.20E+00
   2 T=  298. E= -.11455010E+03 F= -.11553120E+03 E0= -.11553120E+03  EK= 0.98120E+00 SP= 0.00 SK= 0.00
   3 T=  312. E= -.11452900E+03 F= -.11551000E+03 E0= -.11551000E+03  EK= 1.05120E+00 SP= 0.00 SK= 0.00
"""


def _make_src_dir(tmp_path, *, contcar=_SLAB, poscar=None, incar=_RELAX_INCAR,
                  potcar='PAW_PBE Cu\nPAW_PBE O\n'):
    d = tmp_path / 'relax'
    d.mkdir()
    if contcar is not None:
        (d / 'CONTCAR').write_text(contcar, encoding='utf-8')
    if poscar is not None:
        (d / 'POSCAR').write_text(poscar, encoding='utf-8')
    if incar is not None:
        (d / 'INCAR').write_text(incar, encoding='utf-8')
    if potcar is not None:
        (d / 'POTCAR').write_text(potcar, encoding='utf-8')
    return d


# ── NVT INCAR 逐键派生 ──────────────────────────────────────────────────────────
def test_build_aimd_incar_nvt_keys():
    d = parse_incar(ab.build_aimd_incar(_RELAX_INCAR, steps=10000, potim_fs=1.0))
    assert d['IBRION'] == 0 and d['NSW'] == 10000
    assert d['POTIM'] == pytest.approx(1.0)
    assert d['MDALGO'] == 2 and d['SMASS'] == 0        # Nose-Hoover
    assert d['ISYM'] == 0 and d['NELMIN'] == 4
    assert d['TEBEG'] == pytest.approx(300.0) and d['TEEND'] == pytest.approx(300.0)


def test_build_aimd_incar_preserves_electronic_keys():
    d = parse_incar(ab.build_aimd_incar(_RELAX_INCAR))
    assert d['ENCUT'] == 500 and d['GGA'] == 'PE' and d['ISPIN'] == 2
    assert d['MAGMOM'] == '4*0 1*0' and d['IVDW'] == 12 and d['LDAUU'] == '4 0'


def test_build_aimd_incar_strips_ediffg_and_isif():
    d = parse_incar(ab.build_aimd_incar(_RELAX_INCAR))
    assert 'EDIFFG' not in d                            # MD 无离子弛豫判据
    assert 'ISIF' not in d                              # 固定胞 NVT/NVE 无需变胞


def test_build_aimd_incar_encut_not_lowered_by_default():
    d = parse_incar(ab.build_aimd_incar(_RELAX_INCAR))
    assert d['ENCUT'] == 500                            # 不自动下调,继承源


def test_build_aimd_incar_encut_explicit_lowers():
    d = parse_incar(ab.build_aimd_incar(_RELAX_INCAR, encut=350))
    assert d['ENCUT'] == 350


def test_build_aimd_incar_temp_ramp():
    d = parse_incar(ab.build_aimd_incar(_RELAX_INCAR, temp_k=300, temp_end_k=700))
    assert d['TEBEG'] == pytest.approx(300.0) and d['TEEND'] == pytest.approx(700.0)


def test_build_aimd_incar_nve_uses_andersen_prob_zero():
    d = parse_incar(ab.build_aimd_incar(_RELAX_INCAR, ensemble='nve'))
    assert d['MDALGO'] == 1                             # Andersen
    assert d['ANDERSEN_PROB'] == pytest.approx(0.0)     # 碰撞概率0 → 微正则
    assert 'SMASS' not in d and 'TEEND' not in d        # NVE 无 Nose 热浴/终温


def test_build_aimd_incar_removes_stale_thermostat_keys():
    old_nvt = (_RELAX_INCAR + 'SMASS = 5\nTEEND = 900\n'
               'LANGEVIN_GAMMA = 10 10\nLANGEVIN_GAMMA_L = 1\nPMASS = 1000\n')
    nve = parse_incar(ab.build_aimd_incar(old_nvt, ensemble='nve'))
    for key in ('SMASS', 'TEEND', 'LANGEVIN_GAMMA', 'LANGEVIN_GAMMA_L', 'PMASS'):
        assert key not in nve

    old_andersen = _RELAX_INCAR + 'ANDERSEN_PROB = 0.25\n'
    nvt = parse_incar(ab.build_aimd_incar(old_andersen, ensemble='nvt'))
    assert 'ANDERSEN_PROB' not in nvt


def test_build_aimd_incar_adds_missing_ionic_keys():
    base = 'ENCUT = 400\nGGA = PE\n'                    # 纯电子学,无离子学键
    d = parse_incar(ab.build_aimd_incar(base))
    assert d['IBRION'] == 0 and d['NSW'] == 10000 and d['MDALGO'] == 2
    assert d['ENCUT'] == 400 and d['GGA'] == 'PE'       # 电子学原样


def test_derive_aimd_incar_changes_actions():
    target, _ens = ab._aimd_targets('nvt', 300.0, None, 10000, 1.0, None)
    _txt, changes = ab._derive_aimd_incar(_RELAX_INCAR, target)
    by_key = {c['key']: c for c in changes}
    assert by_key['IBRION']['action'] == 'replace' and by_key['IBRION']['old'] == 2
    assert by_key['MDALGO']['action'] == 'add'
    assert by_key['EDIFFG']['action'] == 'strip' and by_key['EDIFFG']['old'] == pytest.approx(-0.02)
    assert by_key['NSW']['action'] == 'replace' and by_key['NSW']['old'] == 200


def test_build_aimd_incar_unknown_ensemble_raises():
    with pytest.raises(ValueError, match='系综'):
        ab.build_aimd_incar(_RELAX_INCAR, ensemble='npt')


@pytest.mark.parametrize('kwargs', [
    {'steps': 0}, {'steps': 1.5}, {'potim_fs': 0}, {'potim_fs': float('nan')},
    {'temp_k': -1}, {'temp_end_k': float('inf')}, {'encut': -400},
])
def test_build_aimd_incar_rejects_invalid_physical_parameters(kwargs):
    with pytest.raises(ValueError):
        ab.build_aimd_incar(_RELAX_INCAR, **kwargs)


# ── 一键派生 AIMD 作业目录 ─────────────────────────────────────────────────────
def test_build_aimd_job_end_to_end(tmp_path):
    src = _make_src_dir(tmp_path)
    res = ab.build_aimd_job(str(src), str(tmp_path / 'aimd'))
    assert res['ok'] and res['error'] is None
    out = tmp_path / 'aimd'
    for name in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR', 'job.yaml'):
        assert (out / name).is_file()
    d = parse_incar((out / 'INCAR').read_text())
    assert d['IBRION'] == 0 and d['MDALGO'] == 2 and 'EDIFFG' not in d
    assert res['job_dir'] == str(out) and res['changes']


def test_build_aimd_job_kpoints_gamma(tmp_path):
    src = _make_src_dir(tmp_path)
    ab.build_aimd_job(str(src), str(tmp_path / 'aimd'))
    kp = (tmp_path / 'aimd' / 'KPOINTS').read_text()
    assert '1 1 1' in kp and 'Gamma' in kp


def test_build_aimd_job_encut_warning_when_not_given(tmp_path):
    src = _make_src_dir(tmp_path)
    res = ab.build_aimd_job(str(src), str(tmp_path / 'aimd'))
    assert any('350' in w for w in res['warnings'])     # 提示论文口径 350eV
    assert any('Γ 点' in w or 'KPOINTS' in w for w in res['warnings'])


def test_build_aimd_job_nve_warns_and_sets_keys(tmp_path):
    src = _make_src_dir(tmp_path)
    res = ab.build_aimd_job(str(src), str(tmp_path / 'aimd'), ensemble='nve')
    assert any('NVE' in w for w in res['warnings'])
    d = parse_incar((tmp_path / 'aimd' / 'INCAR').read_text())
    assert d['MDALGO'] == 1 and d['ANDERSEN_PROB'] == pytest.approx(0.0)


def test_build_aimd_job_contcar_fallback_to_poscar(tmp_path):
    # 空 CONTCAR → 退回 POSCAR + warning + manifest derived_from=POSCAR
    src = _make_src_dir(tmp_path, contcar='', poscar=_SLAB)
    res = ab.build_aimd_job(str(src), str(tmp_path / 'aimd'))
    assert res['ok'] and any('POSCAR' in w for w in res['warnings'])
    m = manifest_mod.load_manifest(tmp_path / 'aimd')
    assert m['inputs']['derived_from'] == 'POSCAR'


def test_build_aimd_job_manifest_fields(tmp_path):
    src = _make_src_dir(tmp_path)
    ab.build_aimd_job(str(src), str(tmp_path / 'aimd'),
                      ensemble='nvt', temp_k=300, steps=5000, potim_fs=1.0)
    m = manifest_mod.load_manifest(tmp_path / 'aimd')
    assert m['task_type'] == 'aimd'
    assert m['parent_job'] == str(src.resolve())
    assert m['inputs']['ensemble'] == 'nvt' and m['inputs']['steps'] == 5000
    assert m['inputs']['temp_k'] == 300.0 and m['inputs']['potim_fs'] == 1.0
    assert m['inputs']['incar_changes']                 # changes 落档


def test_build_aimd_job_missing_structure_errors(tmp_path):
    d = tmp_path / 'empty'
    d.mkdir()
    (d / 'INCAR').write_text(_RELAX_INCAR, encoding='utf-8')
    res = ab.build_aimd_job(str(d), str(tmp_path / 'aimd'))
    assert not res['ok'] and 'CONTCAR/POSCAR' in res['error'] and res['job_dir'] is None


def test_build_aimd_job_missing_incar_errors(tmp_path):
    src = _make_src_dir(tmp_path, incar=None)
    res = ab.build_aimd_job(str(src), str(tmp_path / 'aimd'))
    assert not res['ok'] and 'INCAR' in res['error']


def test_build_aimd_job_unknown_ensemble_errors(tmp_path):
    src = _make_src_dir(tmp_path)
    res = ab.build_aimd_job(str(src), str(tmp_path / 'aimd'), ensemble='npt')
    assert not res['ok'] and '系综' in res['error']


def test_build_aimd_job_missing_potcar_warns(tmp_path):
    src = _make_src_dir(tmp_path, potcar=None)
    res = ab.build_aimd_job(str(src), str(tmp_path / 'aimd'))
    assert res['ok'] and any('POTCAR' in w for w in res['warnings'])
    assert not (tmp_path / 'aimd' / 'POTCAR').is_file()


# ── OSZICAR 能量-时间解析 ───────────────────────────────────────────────────────
def test_parse_aimd_energy_fixture():
    res = ab.parse_aimd_energy(_OSZICAR)
    assert res['n'] == 3                                # 3 条 MD 行(电子子行被跳过)
    s0 = res['steps'][0]
    assert s0['step'] == 1
    assert s0['t_fs'] == pytest.approx(1.0)             # 步号1 × potim 1fs
    assert s0['e_tot'] == pytest.approx(-114.53958)
    assert s0['temp_k'] == pytest.approx(300.0)
    assert res['steps'][2]['temp_k'] == pytest.approx(312.0)


def test_parse_aimd_energy_potim_scales_time():
    res = ab.parse_aimd_energy(_OSZICAR, potim_fs=0.5)
    assert res['steps'][1]['t_fs'] == pytest.approx(1.0)   # 步号2 × 0.5fs = 1.0fs


def test_parse_aimd_energy_empty():
    assert ab.parse_aimd_energy('no md lines here\nDAV: 1 0.1\n') == {'steps': [], 'n': 0}
