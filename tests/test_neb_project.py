"""NEB 分析端测试(project.neb):合成 fixtures 的能量/能垒解析、质量闸各分支、MEP 出图冒烟。"""
import os
import sys

import matplotlib

matplotlib.use('Agg')

import numpy as _numpy  # noqa: E402
import pytest  # noqa: E402

from vcstudio.project import neb as pneb  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    """与 test_native_charts 同款:全套跑时修复 sys.modules['numpy'] 被 pop 的污染。"""
    sys.modules.setdefault('numpy', _numpy)
    yield


def _osz(e0):
    """合成 OSZICAR:一个离子步 + 末行 E0。"""
    return f'DAV:   5  {e0:.4E}  -0.1E-04\n   1 F= {e0:.6E} E0= {e0:.6E}  d E =-.1E-05\n'


def _force_block(fmax):
    """合成 OUTCAR 力块(单原子,|F|=fmax);TOTAL-FORCE 6 列格式。"""
    return (' NIONS =      1 ions\n'
            ' aborting loop because EDIFF is reached\n'
            '  POSITION          TOTAL-FORCE (eV/Angst)\n'
            ' -----------------------------------\n'
            f'  0.0 0.0 0.0   {fmax:.6f} 0.0 0.0\n'
            ' -----------------------------------\n')


def _make_neb(tmp_path, energies, forces=None):
    """在临时目录造 NEB 布局(仅 image 子目录 + OSZICAR/OUTCAR),供离线解析。"""
    jd = str(tmp_path / 'neb')
    os.makedirs(jd, exist_ok=True)
    for i, e in enumerate(energies):
        sub = os.path.join(jd, f'{i:02d}')
        os.makedirs(sub, exist_ok=True)
        open(os.path.join(sub, 'POSCAR'), 'w').write(
            'H\n1\n1 0 0\n0 1 0\n0 0 1\nH\n1\nDirect\n0 0 0\n')
        if e is not None:
            open(os.path.join(sub, 'OSZICAR'), 'w').write(_osz(e))
        if forces is not None and forces[i] is not None:
            open(os.path.join(sub, 'OUTCAR'), 'w').write(_force_block(forces[i]))
    return jd


# 典型势垒路径:00→04,过渡态在 image 02(rel 峰 +0.7)
_BARRIER = [-10.0, -9.6, -9.3, -9.7, -10.2]


def test_parse_energies_barrier_and_ts(tmp_path):
    jd = _make_neb(tmp_path, _BARRIER)
    d = pneb.parse_neb_energies(jd)
    assert d['rel'][0] == pytest.approx(0.0)
    assert d['barrier_f'] == pytest.approx(0.7, abs=1e-6)
    assert d['ts_index'] == 2
    assert d['n_frames'] == 5


def test_parse_energies_reverse_barrier(tmp_path):
    jd = _make_neb(tmp_path, _BARRIER)
    d = pneb.parse_neb_energies(jd)
    # 逆向:相对末态 −10.2 的最高点 −9.3 → 0.9
    assert d['barrier_r'] == pytest.approx(0.9, abs=1e-6)


def test_parse_energies_relative_baseline(tmp_path):
    jd = _make_neb(tmp_path, _BARRIER)
    d = pneb.parse_neb_energies(jd)
    assert d['rel'] == [pytest.approx(e - _BARRIER[0]) for e in _BARRIER]


def test_parse_energies_endpoint_highest_warns(tmp_path):
    jd = _make_neb(tmp_path, [-10.0, -9.9, -9.8, -9.7, -9.5])   # 单调升,峰在末端点
    d = pneb.parse_neb_energies(jd)
    assert d['ts_index'] == 4
    assert any('端点' in w for w in d['warnings'])


def test_parse_energies_missing_initial_raises(tmp_path):
    jd = _make_neb(tmp_path, [None, -9.6, -9.3, -9.7, -10.2])
    with pytest.raises(ValueError, match='初态'):
        pneb.parse_neb_energies(jd)


def test_parse_energies_too_few_frames_raises(tmp_path):
    jd = _make_neb(tmp_path, [-10.0, -9.5])                     # 只有 2 帧
    with pytest.raises(ValueError, match='不足'):
        pneb.parse_neb_energies(jd)


def test_parse_energies_outcar_sigma0_fallback(tmp_path):
    """缺 OSZICAR 时退 OUTCAR 的 energy(sigma->0)。"""
    jd = str(tmp_path / 'neb')
    os.makedirs(jd)
    vals = [-10.0, -9.6, -9.3, -9.7, -10.2]
    for i, e in enumerate(vals):
        sub = os.path.join(jd, f'{i:02d}')
        os.makedirs(sub)
        open(os.path.join(sub, 'OUTCAR'), 'w').write(
            f'  energy  without entropy=  {e:.6f}  energy(sigma->0) =  {e:.6f}\n')
    d = pneb.parse_neb_energies(jd)
    assert d['barrier_f'] == pytest.approx(0.7, abs=1e-6)


def test_parse_energies_per_image_forces(tmp_path):
    forces = [0.01, 0.03, 0.04, 0.02, 0.01]
    jd = _make_neb(tmp_path, _BARRIER, forces=forces)
    d = pneb.parse_neb_energies(jd)
    assert d['per_image_forces'][2] == pytest.approx(0.04, abs=1e-6)
    assert d['climbing_converged'] is True                     # 中间 image 力全 < 0.05


def test_climbing_not_converged_when_force_high(tmp_path):
    forces = [0.01, 0.03, 0.12, 0.02, 0.01]                    # image02 力 0.12 > 0.05
    jd = _make_neb(tmp_path, _BARRIER, forces=forces)
    d = pneb.parse_neb_energies(jd)
    assert d['climbing_converged'] is False


def test_climbing_not_converged_when_no_forces(tmp_path):
    jd = _make_neb(tmp_path, _BARRIER)                         # 无 OUTCAR
    d = pneb.parse_neb_energies(jd)
    assert d['climbing_converged'] is False


def test_last_complete_step_does_not_reuse_earlier_ediff_success():
    text = (
        _force_block(0.01)
        + ' electronic convergence not reached\n'
        + '  POSITION          TOTAL-FORCE (eV/Angst)\n'
        + ' -----------------------------------\n'
        + '  0.0 0.0 0.0   0.010000 0.0 0.0\n'
        + ' -----------------------------------\n'
    )

    parsed = pneb.parse_last_complete_image_step(text, 1)

    assert parsed['status'] == 'unavailable'
    assert parsed['fmax'] is None
    assert any('EDIFF' in issue for issue in parsed['issues'])


def test_last_complete_step_rejects_starred_force_row():
    text = (
        ' NIONS = 1 ions\n'
        ' aborting loop because EDIFF is reached\n'
        ' POSITION TOTAL-FORCE (eV/Angst)\n'
        ' -----------------------------------\n'
        ' 0.0 0.0 0.0 ******** 0.0 0.0\n'
    )

    parsed = pneb.parse_last_complete_image_step(text, 1)

    assert parsed['status'] == 'unavailable'
    assert parsed['fmax'] is None
    assert any('fully numeric' in issue for issue in parsed['issues'])


# ── 质量闸各分支 ──
def _data(rel, *, converged=True, barrier_f=None, ts_index=None, n_frames=None):
    n = n_frames if n_frames is not None else len(rel)
    bf = barrier_f if barrier_f is not None else max(r for r in rel if r is not None)
    ti = ts_index if ts_index is not None else rel.index(bf)
    return {'rel': rel, 'barrier_f': bf, 'barrier_r': bf, 'ts_index': ti,
            'climbing_converged': converged, 'n_frames': n}


def test_quality_gate_all_good():
    g = pneb.neb_quality_gate(_data([0.0, 0.4, 0.7, 0.3, -0.2]))
    assert g['ok'] and g['issues'] == []


def test_quality_gate_not_converged():
    g = pneb.neb_quality_gate(_data([0.0, 0.4, 0.7, 0.3, -0.2], converged=False))
    assert not g['ok'] and any('收敛' in i for i in g['issues'])


def test_quality_gate_low_barrier():
    g = pneb.neb_quality_gate(_data([0.0, 0.02, 0.03, 0.01, -0.2], barrier_f=0.03))
    assert any('无势垒' in i or '能垒' in i for i in g['issues'])


def test_quality_gate_endpoint_ts():
    g = pneb.neb_quality_gate(_data([0.0, 0.2, 0.4, 0.6, 0.8], barrier_f=0.8, ts_index=4))
    assert any('端点' in i for i in g['issues'])


def test_quality_gate_monotonic_no_extremum():
    g = pneb.neb_quality_gate(_data([0.0, 0.2, 0.4, 0.6, 0.8], barrier_f=0.8, ts_index=4))
    assert any('单调' in i for i in g['issues'])


# ── MEP 出图冒烟(matplotlib 已装 → 真渲染,校验文件非空) ──
def _assert_png(p):
    assert os.path.isfile(p) and open(p, 'rb').read(8) == b'\x89PNG\r\n\x1a\n'
    assert os.path.getsize(p) > 1000


def _assert_pdf(p):
    assert os.path.isfile(p) and open(p, 'rb').read(5) == b'%PDF-'
    assert os.path.getsize(p) > 500


def test_neb_profile_plot_renders(tmp_path):
    jd = _make_neb(tmp_path, _BARRIER, forces=[0.01, 0.03, 0.04, 0.02, 0.01])
    d = pneb.parse_neb_energies(jd)
    paths = pneb.neb_profile_plot(d, str(tmp_path / 'mep'), title='CI-NEB MEP',
                                  point_labels=True)
    assert len(paths) == 2
    _assert_png(paths[0])
    _assert_pdf(paths[1])


def test_neb_profile_plot_too_few_points_raises(tmp_path):
    with pytest.raises(ValueError, match='2 个'):
        pneb.neb_profile_plot({'rel': [0.0, None, None], 'ts_index': 0,
                               'barrier_f': 0.0, 'barrier_r': None},
                              str(tmp_path / 'mep'))
