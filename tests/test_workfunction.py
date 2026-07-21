"""功函数测试(project.workfunction):建 job(LVTOT/偶极)、LOCPOT 面平均手算对拍、
真空平台 φ 判定、非对称双 φ、wf_plot 出图。"""
from __future__ import annotations

import sys

import matplotlib

matplotlib.use('Agg')

import numpy as _numpy  # noqa: E402
import pytest  # noqa: E402

from vcstudio.project import workfunction as wf  # noqa: E402
from vcstudio.generate.incar_builder import parse_incar  # noqa: E402
from vcstudio.shared import manifest as manifest_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    sys.modules.setdefault('numpy', _numpy)
    yield


# 对称 slab(上下等价):偶极检测应为空
_SLAB_SYM = """slab
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu
2
Cartesian
0.0 0.0 9.0
1.5 1.5 11.0
"""
# 非对称 slab(吸附质偏上,z 质心远离盒中心)→ 偶极检测非空
_SLAB_ASYM = """slab+ads
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 20.0
Cu O
2 1
Cartesian
0.0 0.0 3.0
1.5 1.5 5.0
0.0 0.0 7.0
"""
_INCAR = "ENCUT = 400\nGGA = PE\nISMEAR = 0\nIBRION = 2\nNSW = 100\nISIF = 2\nEDIFFG = -0.02\n"


def _make_src(tmp_path, contcar=_SLAB_SYM):
    d = tmp_path / 'relax'
    d.mkdir()
    (d / 'CONTCAR').write_text(contcar, encoding='utf-8')
    (d / 'INCAR').write_text(_INCAR, encoding='utf-8')
    (d / 'KPOINTS').write_text("Automatic\n0\nGamma\n5 5 1\n0 0 0\n", encoding='utf-8')
    (d / 'POTCAR').write_text("PAW_PBE Cu\nPAW_PBE O\n", encoding='utf-8')
    return d


def _locpot(z_slices, ng_xy=(2, 2), cell=((3.0, 0, 0), (0, 3.0, 0), (0, 0, 8.0))):
    """按 z 切片值(每片一个常数)合成 LOCPOT 文本。NGZ=len(z_slices)。"""
    ngx, ngy = ng_xy
    ngz = len(z_slices)
    grid = []
    for zv in z_slices:                                  # i = ix + ngx*(iy + ngy*iz)
        grid.extend([zv] * (ngx * ngy))
    lines = ['LOCPOT', '1.0']
    for v in cell:
        lines.append(f' {v[0]} {v[1]} {v[2]}')
    lines += ['H', '1', 'Direct', ' 0 0 0', '', f' {ngx} {ngy} {ngz}']
    for i in range(0, len(grid), 5):
        lines.append(' '.join(str(x) for x in grid[i:i + 5]))
    return '\n'.join(lines) + '\n'


# ── build_workfunction_job ──────────────────────────────────────────────────────
def test_build_wf_job_sets_lvtot(tmp_path):
    src = _make_src(tmp_path)
    res = wf.build_workfunction_job(str(src), str(tmp_path / 'wf'))
    d = parse_incar((tmp_path / 'wf' / 'INCAR').read_text())
    assert d['LVTOT'] is True
    assert res['dipole'] is False                        # 对称 slab 不加偶极
    m = manifest_mod.load_manifest(tmp_path / 'wf')
    assert m['task_type'] == 'workfunction'
    assert set(m['inputs']['files']) == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    assert set(m['inputs']['sha256']) == {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'}
    assert m['inputs']['purpose'] == 'esp'


def test_build_wf_job_asymmetric_adds_dipole(tmp_path):
    src = _make_src(tmp_path, contcar=_SLAB_ASYM)
    res = wf.build_workfunction_job(str(src), str(tmp_path / 'wf'))
    d = parse_incar((tmp_path / 'wf' / 'INCAR').read_text())
    assert res['dipole'] is True and d.get('LDIPOL') is True
    assert any('偶极' in w for w in res['warnings'])


def test_build_wf_job_force_dipole_on_symmetric(tmp_path):
    src = _make_src(tmp_path)
    res = wf.build_workfunction_job(str(src), str(tmp_path / 'wf'), add_dipole=True)
    assert res['dipole'] is True


# ── parse_locpot_planar 手算对拍 ─────────────────────────────────────────────────
def test_parse_locpot_planar_hand_computed():
    txt = _locpot([1.0, 2.0, 5.0, 5.0])                  # 4 个 z 片
    r = wf.parse_locpot_planar(txt)
    assert r['v_planar'] == pytest.approx([1.0, 2.0, 5.0, 5.0])   # 面平均=各片常数
    assert r['z'] == pytest.approx([0.0, 2.0, 4.0, 6.0])          # |c|=8, na=4


def test_parse_locpot_planar_no_volume_division():
    # 势不除体积:大值不被 27 体积压小
    txt = _locpot([100.0, 100.0, 100.0, 100.0])
    r = wf.parse_locpot_planar(txt)
    assert r['v_planar'][0] == pytest.approx(100.0)


# ── work_function 平台判定 ──────────────────────────────────────────────────────
def test_work_function_single_plateau():
    # 真空平台=5,E_F=0.5 → φ=4.5
    z = [0, 1, 2, 3, 4, 5, 6, 7]
    v = [1.0, 2.0, 3.0, 1.0, 5.0, 5.0, 5.0, 5.0]
    r = wf.work_function(v, z, efermi=0.5, deriv_thresh=0.5, min_plateau_pts=3)
    assert r['vacuum_level'] == pytest.approx(5.0)
    assert r['phi'] == pytest.approx(4.5)
    assert len(r['phi_values']) == 1


def test_work_function_asymmetric_two_phi():
    z = list(range(9))
    v = [5.0, 5.0, 5.0, -2.0, -3.0, -2.0, 4.0, 4.0, 4.0]   # 两平台 5 与 4
    r = wf.work_function(v, z, efermi=0.0, deriv_thresh=0.5, min_plateau_pts=3)
    assert len(r['phi_values']) == 2
    assert set(round(p, 1) for p in r['phi_values']) == {5.0, 4.0}
    assert any('不对称' in w or '两个不同真空平台' in w for w in r['warnings'])


def test_work_function_no_plateau_warns():
    z = list(range(6))
    v = [0.0, 3.0, 6.0, 9.0, 12.0, 15.0]                 # 全程陡变,无平台
    r = wf.work_function(v, z, efermi=0.0, deriv_thresh=0.1, min_plateau_pts=3)
    assert any('未找到' in w or '退化' in w for w in r['warnings'])


def test_work_function_length_mismatch_raises():
    with pytest.raises(ValueError, match='长度'):
        wf.work_function([1, 2, 3], [0, 1], efermi=0.0)


# ── wf_plot 出图 ────────────────────────────────────────────────────────────────
def test_wf_plot_outputs(tmp_path):
    z = [0, 1, 2, 3, 4, 5, 6, 7]
    v = [1.0, 2.0, 3.0, 1.0, 5.0, 5.0, 5.0, 5.0]
    out = wf.wf_plot(v, z, efermi=0.5, out_path=tmp_path / 'wf_fig',
                     vacuum_level=5.0, phi=4.5, title='Cu(111)')
    assert len(out) == 2
    with open(out[0], 'rb') as f:
        assert f.read(8) == b'\x89PNG\r\n\x1a\n'
