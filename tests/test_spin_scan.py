"""多自旋并跑 + 磁矩/电子熵守卫测试(project.spin_scan,F3/F10):
自旋族生成 / 变体克隆 / 基态判定(含 pending 与近简并)/ 磁矩审计 / 电子熵守卫。"""
import pytest

from vcstudio.project import spin_scan as ss
from vcstudio.shared import manifest as manifest_mod

# Fe-N-C 单原子催化剂片段(C2 N2 Fe1)。
_POSCAR_FENC = """FeNC
1.0
5.0 0.0 0.0
0.0 5.0 0.0
0.0 0.0 20.0
C N Fe
2 2 1
Cartesian
0.0 0.0 5.0
1.0 0.0 5.0
0.0 1.0 5.0
1.0 1.0 5.0
0.5 0.5 6.0
"""
_INCAR_BASE = 'SYSTEM = FeNC\nENCUT = 500\nISPIN = 1\nIBRION = 2\nNSW = 100\n'


# ── 自旋初猜族 ──────────────────────────────────────────────────────────────────
def test_spin_candidates_3d_metal_gives_nm_ls_hs():
    cands = ss.spin_candidates(['C', 'N', 'Fe'])
    names = [c['name'] for c in cands]
    assert names == ['nm', 'ls', 'hs']                      # 3d → 三档,升序
    by = {c['name']: c['magmom_overrides'] for c in cands}
    assert by['nm'] == {'Fe': 0}
    assert by['hs'] == {'Fe': 4}                            # Fe 高自旋 4
    assert by['ls'] == {'Fe': 2}                            # 减半向下取整
    assert all(c['nupdown'] is None for c in cands)


def test_spin_candidates_4d_metal_no_low_spin():
    cands = ss.spin_candidates(['Mo', 'S'])
    assert [c['name'] for c in cands] == ['nm', 'hs']       # 非 3d → 不加 ls
    assert cands[1]['magmom_overrides'] == {'Mo': 3}


def test_spin_candidates_low_spin_floor_values():
    by = {c['name']: c['magmom_overrides'] for c in ss.spin_candidates(['Cr', 'Mn', 'Ni'])}
    # HS: Cr5/Mn5/Ni2 → LS: 2/2/1(向下取整)
    assert by['hs'] == {'Cr': 5, 'Mn': 5, 'Ni': 2}
    assert by['ls'] == {'Cr': 2, 'Mn': 2, 'Ni': 1}


def test_spin_candidates_nonmagnetic_single_nm():
    cands = ss.spin_candidates(['C', 'O', 'H'])
    assert len(cands) == 1 and cands[0]['name'] == 'nm'     # 无磁性元素 → 不造重复变体
    assert cands[0]['magmom_overrides'] == {}


def test_spin_candidates_magnetic_override():
    cands = ss.spin_candidates(['Fe'], magnetic_elements={'Fe': 6})
    assert {c['name']: c['magmom_overrides'] for c in cands}['hs'] == {'Fe': 6}


def test_nupdown_ladder_range_and_shape():
    lad = ss.nupdown_ladder(3)
    assert [c['nupdown'] for c in lad] == [0, 1, 2, 3]
    assert all(c['magmom_overrides'] is None for c in lad)
    assert lad[2]['name'] == 'nupdown2'


def test_nupdown_ladder_negative_raises():
    with pytest.raises(ValueError, match='非负整数'):
        ss.nupdown_ladder(-1)


# ── 变体克隆 ────────────────────────────────────────────────────────────────────
def _make_job(tmp_path, incar=_INCAR_BASE, poscar=_POSCAR_FENC):
    d = tmp_path / 'job_Fe'
    d.mkdir()
    (d / 'POSCAR').write_text(poscar, encoding='utf-8')
    (d / 'INCAR').write_text(incar, encoding='utf-8')
    (d / 'KPOINTS').write_text('Auto\n0\nGamma\n3 3 1\n0 0 0\n', encoding='utf-8')
    (d / 'POTCAR').write_text('C N Fe\n', encoding='utf-8')
    manifest_mod.save_manifest(d, manifest_mod.new_manifest(
        job_id='job_Fe', system='FeNC', task_type='relax', calc_type='slab', inputs={}))
    return d


def test_build_spin_variants_clones_and_sets_magmom(tmp_path):
    job = _make_job(tmp_path)
    variants = ss.build_spin_variants(str(job), str(tmp_path / 'spin'))
    assert [v['name'] for v in variants] == ['nm', 'ls', 'hs']
    for v in variants:
        from pathlib import Path
        assert Path(v['out_dir']).name == f'job_Fe_spin_{v["name"]}'
        d = parse_incar_file(v['out_dir'])
        assert d['ISPIN'] == 2                              # ISPIN=1 被纠正为 2
        assert d['MAGMOM'] == v['magmom']
    # 三档 MAGMOM 互不相同
    mags = {v['name']: v['magmom'] for v in variants}
    assert mags['nm'] != mags['ls'] != mags['hs']
    assert mags['hs'].endswith('1*4')


def test_build_spin_variants_only_touches_incar(tmp_path):
    job = _make_job(tmp_path)
    variants = ss.build_spin_variants(str(job), str(tmp_path / 'spin'))
    from pathlib import Path
    for v in variants:
        vd = Path(v['out_dir'])
        # POSCAR/KPOINTS/POTCAR 与父目录逐字一致(只改 INCAR)
        assert (vd / 'POSCAR').read_text() == (job / 'POSCAR').read_text()
        assert (vd / 'KPOINTS').read_text() == (job / 'KPOINTS').read_text()
        assert (vd / 'POTCAR').read_text() == (job / 'POTCAR').read_text()


def test_build_spin_variants_manifest_records_parent(tmp_path):
    job = _make_job(tmp_path)
    variants = ss.build_spin_variants(str(job), str(tmp_path / 'spin'))
    for v in variants:
        m = manifest_mod.load_manifest(v['out_dir'])
        assert m['spin_variant'] == v['name']
        assert m['parent_job'] == 'job_Fe'
        assert m['inputs']['spin_magmom'] == v['magmom']


def test_build_spin_variants_nupdown_mode(tmp_path):
    job = _make_job(tmp_path)
    variants = ss.build_spin_variants(str(job), str(tmp_path / 'spinN'),
                                      candidates=ss.nupdown_ladder(2))
    assert [v['name'] for v in variants] == ['nupdown0', 'nupdown1', 'nupdown2']
    for v, n in zip(variants, [0, 1, 2]):
        d = parse_incar_file(v['out_dir'])
        assert d['NUPDOWN'] == n
        assert d['ISPIN'] == 2
        assert d.get('MAGMOM') is None                      # nupdown 模式不改 MAGMOM


def parse_incar_file(job_dir):
    from vcstudio.generate.incar_builder import parse_incar
    import os
    with open(os.path.join(job_dir, 'INCAR'), encoding='utf-8') as f:
        return parse_incar(f.read())


# ── 基态判定 ────────────────────────────────────────────────────────────────────
def _variant_dir(tmp_path, name, *, state=None, energy=None, oszicar=None):
    d = tmp_path / f'job_spin_{name}'
    d.mkdir()
    m = manifest_mod.new_manifest(job_id=f'job_spin_{name}', system='s',
                                  task_type='relax', calc_type='slab', inputs={})
    m['spin_variant'] = name
    if state:
        m['state'] = state
    if energy is not None:
        m.setdefault('results', {})['energy_e0_eV'] = energy
    manifest_mod.save_manifest(d, m)
    if oszicar is not None:
        (d / 'OSZICAR').write_text(oszicar, encoding='utf-8')
    return str(d)


def test_pick_ground_state_pending_when_not_done(tmp_path):
    dirs = [_variant_dir(tmp_path, 'nm', state='DONE', energy=-10.0),
            _variant_dir(tmp_path, 'hs', state='RUNNING')]
    res = ss.pick_ground_state(dirs)
    assert 'winner' not in res
    assert res['pending'] == ['hs']


def test_pick_ground_state_winner_and_de_mev(tmp_path):
    dirs = [_variant_dir(tmp_path, 'nm', state='DONE', energy=-10.000),
            _variant_dir(tmp_path, 'ls', state='DONE', energy=-10.200),
            _variant_dir(tmp_path, 'hs', state='DONE', energy=-10.050)]
    res = ss.pick_ground_state(dirs)
    assert res['winner'] == 'ls'
    assert res['de_meV']['nm'] == pytest.approx(200.0)
    assert res['de_meV']['ls'] == pytest.approx(0.0)
    assert res['warning'] is None                           # 差 150 meV,不近简并


def test_pick_ground_state_near_degenerate_warns(tmp_path):
    dirs = [_variant_dir(tmp_path, 'ls', state='DONE', energy=-10.000),
            _variant_dir(tmp_path, 'hs', state='DONE', energy=-10.005)]  # 差 5 meV
    res = ss.pick_ground_state(dirs)
    assert res['winner'] == 'hs'
    assert res['warning'] is not None and '近简并' in res['warning']


def test_pick_ground_state_oszicar_energy_fallback(tmp_path):
    # DONE 但 manifest 无 results 能量 → 退 OSZICAR read_e0
    dirs = [_variant_dir(tmp_path, 'nm', state='DONE',
                         oszicar='   1 F= -1.0E+02 E0= -100.100 d E = 0.0\n'),
            _variant_dir(tmp_path, 'hs', state='DONE',
                         oszicar='   1 F= -1.0E+02 E0= -100.500 d E = 0.0\n')]
    res = ss.pick_ground_state(dirs)
    assert res['winner'] == 'hs'
    assert res['energies']['hs'] == pytest.approx(-100.5)


def test_pick_ground_state_done_but_no_energy_pending(tmp_path):
    dirs = [_variant_dir(tmp_path, 'nm', state='DONE', energy=-10.0),
            _variant_dir(tmp_path, 'hs', state='DONE')]     # DONE 却无能量
    assert ss.pick_ground_state(dirs)['pending'] == ['hs']


# ── 磁矩审计(F3) ──────────────────────────────────────────────────────────────
_OUTCAR_MAG = ' number of electron  16.0 magnetization  4.4200000\n'


def test_audit_magmom_stable_no_warning():
    r = ss.audit_magmom(_OUTCAR_MAG, 4)
    assert r['audited'] and not r['collapsed'] and not r['flipped']
    assert r['warning'] is None
    assert r['final_magnetization'] == pytest.approx(4.42)


def test_audit_magmom_collapse_to_zero():
    r = ss.audit_magmom(' number of electron 16 magnetization 0.05\n', 5)
    assert r['collapsed'] and r['warning'] and '塌缩' in r['warning']


def test_audit_magmom_flip_sign():
    r = ss.audit_magmom(_OUTCAR_MAG, -4)                    # 初猜负,末态 +4.42
    assert r['flipped'] and not r['collapsed']
    assert '翻转' in r['warning']


def test_audit_magmom_no_magnetization_not_audited():
    r = ss.audit_magmom('reached required accuracy\n', 5)
    assert r['audited'] is False and r['final_magnetization'] is None
    assert '无法审计' in r['warning']


# ── 电子熵守卫(F10) ───────────────────────────────────────────────────────────
def test_electronic_entropy_over_threshold():
    oc = '  NIONS =      4\n  entropy T*S    EENTRO =        -0.02000000\n'
    r = ss.electronic_entropy_check(oc)                     # 20 meV / 4 atom = 5 meV/atom
    assert r['ts_meV_per_atom'] == pytest.approx(5.0)
    assert r['over_threshold'] and 'SIGMA' in r['warning']


def test_electronic_entropy_under_threshold():
    oc = '  NIONS =   10\n  EENTRO =   -0.002\n'            # 2 meV / 10 atom = 0.2 meV/atom
    r = ss.electronic_entropy_check(oc)
    assert r['over_threshold'] is False and r['warning'] is None


def test_electronic_entropy_ions_per_type_fallback():
    # 无 NIONS → 退 'ions per type' 求和(2+2+1=5)
    oc = '   ions per type =           2   2   1\n  EENTRO =   -0.010\n'
    r = ss.electronic_entropy_check(oc)
    assert r['natoms'] == 5
    assert r['ts_meV_per_atom'] == pytest.approx(2.0)       # 10 meV / 5 atom


def test_electronic_entropy_no_eentro():
    r = ss.electronic_entropy_check('just a relax OUTCAR\n')
    assert r['eentro_ev'] is None and '无法核算' in r['warning']


def test_electronic_entropy_custom_threshold():
    oc = '  NIONS = 4\n  EENTRO = -0.02\n'                  # 5 meV/atom
    assert ss.electronic_entropy_check(oc, threshold_meV_per_atom=10.0)['over_threshold'] is False
