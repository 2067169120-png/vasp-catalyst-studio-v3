"""频率作业生成端测试(generate.freq_builder,F14):吸附质识别 / 邻居周期镜像 /
Selective dynamics 标志 / 频率 INCAR 逐键派生 / 一键作业目录(纯函数 + tmp 目录)。"""
import pytest

from vcstudio.generate import freq_builder as fb
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared import manifest as manifest_mod

# 4 层 Cu slab(z=2/4/6/8)+ O 吸附质(z=11,顶上 3 Å 间隙),Cartesian。
_SLAB_O = """slab+O
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
_RELAX_INCAR = """SYSTEM = slabO
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


def _flag_lines(text):
    return [ln for ln in text.splitlines() if ln.endswith('T T T') or ln.endswith('F F F')]


# ── 吸附质识别 ──────────────────────────────────────────────────────────────────
def test_detect_adsorbate_z_gap_confident():
    det = fb.detect_adsorbate(_SLAB_O)
    assert det['indices'] == [4] and det['confident'] is True
    assert det['method'] == 'z_gap' and det['warnings'] == []


def test_detect_adsorbate_explicit_indices():
    det = fb.detect_adsorbate(_SLAB_O, adsorbate_indices=[0, 4])
    assert det['indices'] == [0, 4] and det['method'] == 'explicit'
    assert det['confident'] is True


def test_detect_adsorbate_explicit_out_of_range_raises():
    with pytest.raises(ValueError, match='越界'):
        fb.detect_adsorbate(_SLAB_O, adsorbate_indices=[99])


def test_detect_adsorbate_non_whitelist_top_warns():
    # 顶部是 Pt(非吸附质白名单)→ 不静默当真,附 warning 且 confident=False
    poscar = _SLAB_O.replace('Cu O', 'Cu Pt').replace('0.0 0.0 11.0', '0.0 0.0 11.0')
    det = fb.detect_adsorbate(poscar)
    assert det['indices'] == [4] and det['confident'] is False
    assert any('非常见吸附质元素' in w for w in det['warnings'])


def test_detect_adsorbate_no_clear_gap_warns():
    # 全原子挤在 0.4 Å z 跨度内 → 无明显吸附质-表面间隙 → warning
    flat = """flat
1.0
6 0 0
0 6 0
0 0 20
O
3
Cartesian
0.0 0.0 5.0
1.0 0.0 5.2
2.0 0.0 5.4
"""
    det = fb.detect_adsorbate(flat)
    assert det['confident'] is False
    assert any('z 间隙' in w for w in det['warnings'])


# ── 自由原子选择 + 周期邻居 ────────────────────────────────────────────────────
def test_select_free_atoms_basic_modes():
    assert fb.select_free_atoms(_SLAB_O, 'adsorbate_only') == [4]
    assert fb.select_free_atoms(_SLAB_O, 'all') == [0, 1, 2, 3, 4]
    assert fb.select_free_atoms(_SLAB_O, 'explicit', adsorbate_indices=[1, 4]) == [1, 4]


def test_select_free_atoms_neighbor_cutoff_discriminates():
    # 最近的 Cu(1.5,1.5,8)距 O 3.67 Å:cutoff=3 仅吸附质;cutoff=5 纳入两层近邻
    assert fb.select_free_atoms(_SLAB_O, 'adsorbate_and_neighbors', cutoff=3.0) == [4]
    assert fb.select_free_atoms(_SLAB_O, 'adsorbate_and_neighbors', cutoff=5.0) == [2, 3, 4]


def test_select_free_atoms_neighbor_periodic_cross_boundary():
    # O 贴 x=0.2,近邻 Cu 贴 x=5.9(对侧胞边):仅周期最小镜像(dx=0.3)可达,直接距离 5.7 Å 不可达
    poscar = """cross
1.0
6 0 0
0 6 0
0 0 20
Cu O
4 1
Cartesian
5.9 0.0 10.0
3.0 3.0 10.1
1.5 1.5 10.0
0.2 3.0 10.2
0.2 0.0 12.3
"""
    det = fb.detect_adsorbate(poscar)
    assert det['indices'] == [4]                       # O 仍被 z-间隙正确分出
    free = fb.select_free_atoms(poscar, 'adsorbate_and_neighbors', cutoff=2.5)
    assert free == [0, 4]                              # 跨边界 Cu0 经周期镜像纳入,其余远邻排除


def test_select_free_atoms_explicit_requires_indices():
    with pytest.raises(ValueError, match='explicit'):
        fb.select_free_atoms(_SLAB_O, 'explicit')


def test_select_free_atoms_unknown_mode_raises():
    with pytest.raises(ValueError, match='未知 mode'):
        fb.select_free_atoms(_SLAB_O, 'bogus')


# ── Selective dynamics POSCAR ───────────────────────────────────────────────────
def test_build_freq_poscar_flags_free_vs_fixed():
    out = fb.build_freq_poscar(_SLAB_O, [4])
    assert out.splitlines().count('Selective dynamics') == 1     # 恰一行
    fl = _flag_lines(out)
    assert len(fl) == 5
    assert all(ln.endswith('F F F') for ln in fl[:4])            # slab 冻结
    assert fl[4].endswith('T T T')                               # 吸附质放开


def test_build_freq_poscar_rewrites_existing_selective():
    # 已有 Selective dynamics(全 T)→ 只重写标志,不产生重复行
    sd = """slab+O
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu O
4 1
Selective dynamics
Cartesian
0.0 0.0 2.0 T T T
1.5 1.5 4.0 T T T
0.0 0.0 6.0 T T T
1.5 1.5 8.0 T T T
0.0 0.0 11.0 T T T
"""
    out = fb.build_freq_poscar(sd, [4])
    assert out.splitlines().count('Selective dynamics') == 1
    fl = _flag_lines(out)
    assert fl[3].endswith('F F F') and fl[4].endswith('T T T')


def test_build_freq_poscar_preserves_direct_coords_and_velocity_tail():
    direct = """mol
1.0
10 0 0
0 10 0
0 0 10
H
2
Direct
0.10 0.20 0.30
0.40 0.50 0.60

0.0 0.0 0.0
0.0 0.0 0.0
"""
    out = fb.build_freq_poscar(direct, [0, 1])
    assert '0.10 0.20 0.30  T T T' in out                       # 坐标数值原样,只加标志
    assert out.rstrip().endswith('0.0 0.0 0.0')                 # 速度块尾部透传


def test_build_freq_poscar_empty_free_raises():
    with pytest.raises(ValueError, match='自由原子集合为空'):
        fb.build_freq_poscar(_SLAB_O, [])


def test_build_freq_poscar_out_of_range_raises():
    with pytest.raises(ValueError, match='越界'):
        fb.build_freq_poscar(_SLAB_O, [7])


# ── 频率 INCAR 逐键派生 ─────────────────────────────────────────────────────────
def test_build_freq_incar_replaces_ionic_keys():
    d = parse_incar(fb.build_freq_incar(_RELAX_INCAR))
    assert d['ISTART'] == 0 and d['ICHARG'] == 2
    assert d['IBRION'] == 5 and d['NFREE'] == 2 and d['NSW'] == 1
    assert d['ISYM'] == 0
    assert d['POTIM'] == pytest.approx(0.015)


def test_build_freq_incar_preserves_electronic_keys():
    d = parse_incar(fb.build_freq_incar(_RELAX_INCAR))
    assert d['ENCUT'] == 500 and d['GGA'] == 'PE' and d['ISPIN'] == 2
    assert d['MAGMOM'] == '4*0 1*0' and d['IVDW'] == 12 and d['LDAUU'] == '4 0'


def test_build_freq_incar_strips_isif_and_ediffg():
    d = parse_incar(fb.build_freq_incar(_RELAX_INCAR))
    assert 'ISIF' not in d and 'EDIFFG' not in d


def test_build_freq_incar_tightens_loose_ediff():
    d = parse_incar(fb.build_freq_incar(_RELAX_INCAR))
    assert d['EDIFF'] == pytest.approx(1e-7)              # 1e-5 → 1e-7


def test_build_freq_incar_keeps_already_tight_ediff():
    base = 'ENCUT=500\nIBRION=2\nNSW=50\nEDIFF=1E-08\n'
    _txt, changes = fb._derive_freq_incar(base)
    # 已 ≤1e-7 → 不动 EDIFF(changes 里无 EDIFF 项)
    assert not any(c['key'] == 'EDIFF' for c in changes)
    assert parse_incar(_txt)['EDIFF'] == pytest.approx(1e-8)


def test_build_freq_incar_adds_missing_ionic_keys():
    base = 'ENCUT = 400\nGGA = PE\n'                      # 纯电子学,无任何离子学键
    d = parse_incar(fb.build_freq_incar(base))
    assert d['IBRION'] == 5 and d['NFREE'] == 2 and d['NSW'] == 1
    assert d['ISYM'] == 0 and d['POTIM'] == pytest.approx(0.015)
    assert d['ISTART'] == 0 and d['ICHARG'] == 2
    assert d['EDIFF'] == pytest.approx(1e-7)              # 缺 → 补
    assert d['ENCUT'] == 400 and d['GGA'] == 'PE'


def test_build_freq_incar_resets_parent_restart_state():
    base = _RELAX_INCAR + 'ISTART = 1\nICHARG = 11\n'
    d = parse_incar(fb.build_freq_incar(base))
    assert d['ISTART'] == 0 and d['ICHARG'] == 2


def test_derive_freq_incar_changes_manifest():
    _txt, changes = fb._derive_freq_incar(_RELAX_INCAR)
    by_key = {c['key']: c for c in changes}
    assert by_key['IBRION']['action'] == 'replace' and by_key['IBRION']['old'] == 2
    assert by_key['NFREE']['action'] == 'add'
    assert by_key['ISIF']['action'] == 'strip' and by_key['ISIF']['old'] == 3
    assert by_key['EDIFF']['action'] == 'tighten'


# ── 一键派生频率作业目录 ───────────────────────────────────────────────────────
def _make_relax_dir(tmp_path, *, kpoints='Auto\n0\nGamma\n3 3 1\n0 0 0\n', potcar='PAW_PBE Cu\n'):
    d = tmp_path / 'relax'
    d.mkdir()
    (d / 'CONTCAR').write_text(_SLAB_O, encoding='utf-8')
    (d / 'INCAR').write_text(_RELAX_INCAR, encoding='utf-8')
    if kpoints is not None:
        (d / 'KPOINTS').write_text(kpoints, encoding='utf-8')
    if potcar is not None:
        (d / 'POTCAR').write_text(potcar, encoding='utf-8')
    return d


def test_build_freq_job_end_to_end(tmp_path):
    relax = _make_relax_dir(tmp_path)
    out = tmp_path / 'freq'
    res = fb.build_freq_job(str(relax), str(out))
    assert res['free_atoms'] == [4] and res['warnings'] == []
    # 四件套 + manifest 齐全
    for name in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR', 'job.yaml'):
        assert (out / name).is_file()
    # 频率 POSCAR 冻结 slab、放开吸附质
    assert (out / 'POSCAR').read_text().splitlines().count('Selective dynamics') == 1
    # 派生 INCAR 生效
    d = parse_incar((out / 'INCAR').read_text())
    assert d['IBRION'] == 5 and 'ISIF' not in d
    # KPOINTS 复制原网格
    assert '3 3 1' in (out / 'KPOINTS').read_text()
    # manifest 溯源
    m = manifest_mod.load_manifest(out)
    assert m['task_type'] == 'freq'
    assert m['parent_job'] == str(relax.resolve())
    assert m['inputs']['free_indices'] == [4]
    assert m['inputs']['incar_changes']                 # changes 落档


def test_build_freq_job_gamma_kpoints(tmp_path):
    relax = _make_relax_dir(tmp_path)
    out = tmp_path / 'freqG'
    fb.build_freq_job(str(relax), str(out), kpoints='gamma')
    kp = (out / 'KPOINTS').read_text()
    assert '1 1 1' in kp                                 # Γ 单点覆盖原 3×3×1


def test_build_freq_job_missing_kpoints_warns(tmp_path):
    relax = _make_relax_dir(tmp_path, kpoints=None)
    out = tmp_path / 'freqNoK'
    res = fb.build_freq_job(str(relax), str(out))
    assert (out / 'KPOINTS').is_file()                   # 退化为 Γ
    assert any('KPOINTS' in w for w in res['warnings'])


def test_build_molecule_freq_job_all_free_gamma(tmp_path):
    relax = _make_relax_dir(tmp_path)
    out = tmp_path / 'freqmol'
    res = fb.build_molecule_freq_job(str(relax), str(out))
    assert res['free_atoms'] == [0, 1, 2, 3, 4]          # 全原子放开
    assert '1 1 1' in (out / 'KPOINTS').read_text()      # 气相默认 Γ
    m = manifest_mod.load_manifest(out)
    assert m['calc_type'] == 'molecule'
    # 全 T T T
    fl = _flag_lines((out / 'POSCAR').read_text())
    assert all(ln.endswith('T T T') for ln in fl)


def test_build_freq_job_surfaces_detection_warning(tmp_path):
    # 顶部为非白名单元素 → 识别 warning 必须冒泡到 build_freq_job 的 warnings(不静默)
    d = tmp_path / 'relaxPt'
    d.mkdir()
    (d / 'CONTCAR').write_text(_SLAB_O.replace('Cu O', 'Cu Pt'), encoding='utf-8')
    (d / 'INCAR').write_text(_RELAX_INCAR, encoding='utf-8')
    (d / 'KPOINTS').write_text('Auto\n0\nGamma\n3 3 1\n0 0 0\n', encoding='utf-8')
    (d / 'POTCAR').write_text('PAW_PBE Cu\n', encoding='utf-8')
    res = fb.build_freq_job(str(d), str(tmp_path / 'freqPt'))
    assert any('非常见吸附质元素' in w for w in res['warnings'])


def test_build_freq_job_missing_contcar_raises(tmp_path):
    d = tmp_path / 'empty'
    d.mkdir()
    with pytest.raises(ValueError, match='CONTCAR/POSCAR'):
        fb.build_freq_job(str(d), str(tmp_path / 'freqX'))


def test_build_freq_job_empty_contcar_falls_back_to_poscar(tmp_path):
    # VASP 常见陷阱:作业刚起时 CONTCAR 为空 → 应退回 POSCAR 而非用空文件
    d = tmp_path / 'relaxEmptyC'
    d.mkdir()
    (d / 'CONTCAR').write_text('', encoding='utf-8')       # 空 CONTCAR
    (d / 'POSCAR').write_text(_SLAB_O, encoding='utf-8')
    (d / 'INCAR').write_text(_RELAX_INCAR, encoding='utf-8')
    (d / 'KPOINTS').write_text('Auto\n0\nGamma\n3 3 1\n0 0 0\n', encoding='utf-8')
    (d / 'POTCAR').write_text('PAW_PBE Cu\n', encoding='utf-8')
    res = fb.build_freq_job(str(d), str(tmp_path / 'freqEC'))
    assert res['free_atoms'] == [4]
    assert manifest_mod.load_manifest(tmp_path / 'freqEC')['inputs']['derived_from'] == 'POSCAR'
