"""收敛扫描测试(generate.conv_scan):ENCUT/k/真空/层厚 系列派生 + changes/manifest +
收敛判定(相邻 1meV/atom)+ derive_incar 通用助手 + set_vacuum + conv_plot 出图。"""
from __future__ import annotations

import os
import sys

import matplotlib

matplotlib.use('Agg')

import numpy as _numpy  # noqa: E402
import pytest  # noqa: E402

from vcstudio.generate import conv_scan as cs  # noqa: E402
from vcstudio.generate.incar_builder import parse_incar  # noqa: E402
from vcstudio.shared import manifest as manifest_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    sys.modules.setdefault('numpy', _numpy)
    yield


_SLAB = """slab Cu
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu
2
Cartesian
0.0 0.0 8.0
1.5 1.5 10.0
"""
_INCAR = "ENCUT = 500\nGGA = PE\nISPIN = 2\nIBRION = 2\nNSW = 100\nISIF = 2\nEDIFFG = -0.02\n"
_KP = "Automatic\n0\nGamma\n5 5 1\n0 0 0\n"
_POT = "PAW_PBE Cu\n"


def _make_src(tmp_path, incar=_INCAR, kpoints=_KP, potcar=_POT, contcar=_SLAB):
    d = tmp_path / 'relax'
    d.mkdir()
    if contcar is not None:
        (d / 'CONTCAR').write_text(contcar, encoding='utf-8')
    if incar is not None:
        (d / 'INCAR').write_text(incar, encoding='utf-8')
    if kpoints is not None:
        (d / 'KPOINTS').write_text(kpoints, encoding='utf-8')
    if potcar is not None:
        (d / 'POTCAR').write_text(potcar, encoding='utf-8')
    return d


def _write_oszicar(job_dir, e0):
    txt = (f'       N       E\nDAV:   1   {e0:.6f}\n'
           f'   1 F= {e0:.8E} E0= {e0:.8E}  d E =0.0\n')
    (job_dir / 'OSZICAR').write_text(txt, encoding='utf-8')


# ── ENCUT 系列 ──────────────────────────────────────────────────────────────────
def test_encut_series_uses_fixed_geometry_static_baseline(tmp_path):
    src = _make_src(tmp_path)
    res = cs.build_encut_series(str(src), str(tmp_path / 'enc'), values=[400, 500, 600])
    assert set(res['dirs']) == {400, 500, 600}
    inc = parse_incar((tmp_path / 'enc' / 'encut_400' / 'INCAR').read_text())
    assert inc['ENCUT'] == 400
    assert inc['GGA'] == 'PE'
    assert inc['IBRION'] == -1 and inc['NSW'] == 0
    assert inc['ISTART'] == 0 and inc['ICHARG'] == 2
    assert 'ISIF' not in inc and 'EDIFFG' not in inc
    # KPOINTS/POTCAR 逐字复制
    assert (tmp_path / 'enc' / 'encut_400' / 'KPOINTS').read_text() == _KP


def test_encut_series_manifest_and_changes(tmp_path):
    src = _make_src(tmp_path)
    cs.build_encut_series(str(src), str(tmp_path / 'enc'), values=[450])
    m = manifest_mod.load_manifest(tmp_path / 'enc' / 'encut_450')
    assert m['task_type'] == 'conv_scan'
    assert m['inputs']['series'] == 'encut' and m['inputs']['series_value'] == 450
    assert m['inputs']['natoms'] == 2
    assert any(c['key'] == 'ENCUT' and c['new'] == 450 for c in m['inputs']['incar_changes'])


# ── k 网格系列 ──────────────────────────────────────────────────────────────────
def test_kmesh_series_changes_kpoints_on_fixed_static_baseline(tmp_path):
    src = _make_src(tmp_path)
    res = cs.build_kmesh_series(str(src), str(tmp_path / 'km'), meshes=[[3, 3, 1], [7, 7, 1]])
    assert set(res['dirs']) == {9, 49}                       # series_value = k 点积
    kp = (tmp_path / 'km' / 'kmesh_7x7x1' / 'KPOINTS').read_text()
    assert '7 7 1' in kp and 'Gamma' in kp
    inc = parse_incar((tmp_path / 'km' / 'kmesh_7x7x1' / 'INCAR').read_text())
    assert inc['IBRION'] == -1 and inc['NSW'] == 0 and inc['ICHARG'] == 2


def test_kmesh_series_manifest(tmp_path):
    src = _make_src(tmp_path)
    cs.build_kmesh_series(str(src), str(tmp_path / 'km'), meshes=[[5, 5, 1]])
    m = manifest_mod.load_manifest(tmp_path / 'km' / 'kmesh_5x5x1')
    assert m['inputs']['series'] == 'kmesh' and m['inputs']['series_label'] == '5×5×1'
    assert m['inputs']['series_value'] == 25


# ── 真空系列 ────────────────────────────────────────────────────────────────────
def test_vacuum_series_rebuilds_poscar(tmp_path):
    src = _make_src(tmp_path)
    cs.build_vacuum_series(str(src), str(tmp_path / 'vac'), vacuums=[12, 18])
    txt = (tmp_path / 'vac' / 'vac_12' / 'POSCAR').read_text()
    # slab z 跨度 = 2 Å(8→10),真空 12 → |c| = 14
    c_line = txt.splitlines()[4].split()
    assert float(c_line[2]) == pytest.approx(14.0)
    # 所有点共享固定几何静态 INCAR；KPOINTS 复制
    inc = parse_incar((tmp_path / 'vac' / 'vac_12' / 'INCAR').read_text())
    assert inc['IBRION'] == -1 and inc['NSW'] == 0 and inc['ICHARG'] == 2


def test_set_vacuum_centers_and_sets_c(tmp_path):
    new, warns = cs.set_vacuum(_SLAB, 10.0)
    lines = new.splitlines()
    assert float(lines[4].split()[2]) == pytest.approx(12.0)     # 跨度2 + 真空10
    # slab 沿 z 居中:z_min = 真空/2 = 5.0
    zmin = min(float(ln.split()[2]) for ln in lines[8:10])
    assert zmin == pytest.approx(5.0)
    assert not warns


def test_set_vacuum_tilted_c_warns():
    tilted = _SLAB.replace('0.0 0.0 20.0', '2.0 0.0 20.0')       # c 有面内分量
    _new, warns = cs.set_vacuum(tilted, 12.0)
    assert any('面内分量' in w or 'c⊥ab' in w for w in warns)


# ── 层厚系列 ────────────────────────────────────────────────────────────────────
def test_slab_thickness_no_builder_returns_note(tmp_path):
    src = _make_src(tmp_path)
    res = cs.build_slab_thickness_series(str(src), str(tmp_path / 'th'), layers=[3, 4, 5])
    assert res['series'] == [] and not res['dirs']
    assert res['note'] and '重建' in res['note']                # 不编造结构


def test_slab_thickness_with_builder_fn(tmp_path):
    src = _make_src(tmp_path)

    def _fake_slab(n):
        return _SLAB.replace('slab Cu', f'slab Cu {n} layers')

    res = cs.build_slab_thickness_series(str(src), str(tmp_path / 'th'),
                                         layers=[3, 4], slab_builder_fn=_fake_slab)
    assert set(res['dirs']) == {3, 4}
    assert '3 layers' in (tmp_path / 'th' / 'nlayers_3' / 'POSCAR').read_text()
    inc = parse_incar((tmp_path / 'th' / 'nlayers_3' / 'INCAR').read_text())
    assert inc['IBRION'] == -1 and inc['NSW'] == 0 and inc['ICHARG'] == 2
    m = manifest_mod.load_manifest(tmp_path / 'th' / 'nlayers_4')
    assert m['inputs']['series'] == 'slab_thickness' and m['inputs']['series_value'] == 4


# ── derive_incar 通用助手 ────────────────────────────────────────────────────────
def test_derive_incar_replace_add_strip():
    base = "ENCUT = 500\nISIF = 3\nGGA = PE\n"
    txt, changes = cs.derive_incar(base, set_keys={'ENCUT': 600, 'NSW': 0},
                                   strip_keys=('ISIF',))
    d = parse_incar(txt)
    assert d['ENCUT'] == 600 and d['NSW'] == 0 and 'ISIF' not in d and d['GGA'] == 'PE'
    by = {c['key']: c for c in changes}
    assert by['ENCUT']['action'] == 'replace' and by['ENCUT']['old'] == 500
    assert by['NSW']['action'] == 'add'
    assert by['ISIF']['action'] == 'strip' and by['ISIF']['old'] == 3


def test_derive_incar_preserves_inline_comment_and_order():
    base = "ENCUT = 500  # cutoff\nGGA = PE\n"
    txt, _ = cs.derive_incar(base, set_keys={'GGA': 'RP'})
    assert '# cutoff' in txt                                  # 行内注释保留
    assert txt.index('ENCUT') < txt.index('GGA')             # 顺序不动


# ── 系列解析 + 收敛判定 ──────────────────────────────────────────────────────────
def test_analyze_series_converged_at_min_x(tmp_path):
    # natoms=2,阈 1 meV/atom = 2 meV 总能。energies 让 500 处相邻差<2meV。
    energies = {400: -10.000, 450: -10.010, 500: -10.0105, 550: -10.01055}
    dirs = {}
    for x, e in energies.items():
        d = tmp_path / f'e{x}'
        d.mkdir()
        _write_oszicar(d, e)
        dirs[x] = str(d)
    res = cs.analyze_series(dirs, natoms=2, threshold_mev=1.0)
    # 400→450 差 10meV(5/atom)>1;450→500 差 0.5meV(0.25/atom)<1 → 收敛点 500
    assert res['converged_at'] == 500
    pts = {p['x']: p for p in res['points']}
    assert pts[500]['converged'] is True and pts[450]['converged'] is False
    assert pts[400]['energy'] == pytest.approx(-10.0)


def test_analyze_series_not_converged(tmp_path):
    energies = {400: -10.0, 450: -10.1, 500: -10.2}          # 每步 100meV,永不收敛
    dirs = {}
    for x, e in energies.items():
        d = tmp_path / f'e{x}'
        d.mkdir()
        _write_oszicar(d, e)
        dirs[x] = str(d)
    res = cs.analyze_series(dirs, natoms=2, threshold_mev=1.0)
    assert res['converged_at'] is None and '尚未收敛' in res['note']


def test_analyze_series_missing_oszicar_energy_none(tmp_path):
    d = tmp_path / 'e400'
    d.mkdir()                                                # 无 OSZICAR
    res = cs.analyze_series({400: str(d)}, natoms=2)
    assert res['points'][0]['energy'] is None
    assert '无任一' in res['note']


def test_analyze_series_ignores_partial_energy_from_created_job(tmp_path):
    src = _make_src(tmp_path)
    built = cs.build_encut_series(str(src), str(tmp_path / 'enc'), values=[400])
    job = __import__('pathlib').Path(built['dirs'][400])
    _write_oszicar(job, -10.0)
    res = cs.analyze_series(built['dirs'], natoms=2)
    assert res['points'][0]['energy'] is None


def test_analyze_series_from_dir_list_reads_manifest(tmp_path):
    src = _make_src(tmp_path)
    r = cs.build_encut_series(str(src), str(tmp_path / 'enc'), values=[400, 500])
    for x, d in r['dirs'].items():
        _write_oszicar(__import__('pathlib').Path(d), -10.0 - 0.0001 * x)
        m = manifest_mod.load_manifest(d)
        manifest_mod.set_state(m, 'DONE')
        manifest_mod.save_manifest(d, m)
    res = cs.analyze_series(list(r['dirs'].values()))        # 列表 → 从 manifest 取 x
    assert [p['x'] for p in res['points']] == [400, 500]


# ── conv_plot 出图 ──────────────────────────────────────────────────────────────
def test_conv_plot_outputs_png_pdf(tmp_path):
    points = [{'x': 400, 'energy': -10.0, 'converged': False},
              {'x': 450, 'energy': -10.01, 'converged': False},
              {'x': 500, 'energy': -10.0105, 'converged': True}]
    out = cs.conv_plot(points, tmp_path / 'conv', xlabel='ENCUT (eV)',
                       converged_at=500, natoms=2)
    assert len(out) == 2
    with open(out[0], 'rb') as f:
        assert f.read(8) == b'\x89PNG\r\n\x1a\n'
    assert os.path.getsize(out[1]) > 500


def test_conv_plot_needs_two_points(tmp_path):
    with pytest.raises(ValueError, match='2 个'):
        cs.conv_plot([{'x': 400, 'energy': -10.0}], tmp_path / 'x')
