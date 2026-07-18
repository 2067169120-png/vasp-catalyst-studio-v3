"""状态方程测试(project.eos):EOS 系列派生(等比缩放/定容单点)、BM3 拟合回收已知参数、出图。"""
from __future__ import annotations

import sys

import matplotlib

matplotlib.use('Agg')

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from vcstudio.project import eos  # noqa: E402
from vcstudio.generate.incar_builder import parse_incar  # noqa: E402
from vcstudio.shared import manifest as manifest_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    sys.modules.setdefault('numpy', np)
    yield


_BULK = """Cu fcc
1.0
3.6 0.0 0.0
0.0 3.6 0.0
0.0 0.0 3.6
Cu
4
Direct
0.0 0.0 0.0
0.5 0.5 0.0
0.5 0.0 0.5
0.0 0.5 0.5
"""
_INCAR = "ENCUT = 400\nGGA = PE\nIBRION = 2\nNSW = 80\nISIF = 3\nEDIFFG = -0.02\n"


def _make_src(tmp_path, incar=_INCAR, contcar=_BULK):
    d = tmp_path / 'relax'
    d.mkdir()
    if contcar is not None:
        (d / 'CONTCAR').write_text(contcar, encoding='utf-8')
    if incar is not None:
        (d / 'INCAR').write_text(incar, encoding='utf-8')
    (d / 'KPOINTS').write_text("Automatic\n0\nGamma\n9 9 9\n0 0 0\n", encoding='utf-8')
    (d / 'POTCAR').write_text("PAW_PBE Cu\n", encoding='utf-8')
    return d


# ── BM3 拟合:已知参数回收 ───────────────────────────────────────────────────────
def test_fit_bm3_recovers_known_parameters():
    V0, E0, B0_evA3, Bp = 45.0, -12.34, 0.6, 4.5
    V = np.linspace(0.94, 1.06, 7) ** 3 * V0
    eta = (V0 / V) ** (2.0 / 3.0)
    E = E0 + 9 * V0 * B0_evA3 / 16 * ((eta - 1) ** 3 * Bp + (eta - 1) ** 2 * (6 - 4 * eta))
    fit = eos.fit_birch_murnaghan(V, E)
    assert fit['v0'] == pytest.approx(V0, rel=1e-6)
    assert fit['e0'] == pytest.approx(E0, rel=1e-6)
    assert fit['b0_evA3'] == pytest.approx(B0_evA3, rel=1e-5)
    assert fit['b0_prime'] == pytest.approx(Bp, rel=1e-5)
    assert fit['r2'] == pytest.approx(1.0, abs=1e-9)


def test_fit_bm3_b0_gpa_conversion():
    V0, E0, B0_evA3, Bp = 20.0, -5.0, 1.0, 4.0
    V = np.linspace(0.95, 1.05, 7) ** 3 * V0
    eta = (V0 / V) ** (2.0 / 3.0)
    E = E0 + 9 * V0 * B0_evA3 / 16 * ((eta - 1) ** 3 * Bp + (eta - 1) ** 2 * (6 - 4 * eta))
    fit = eos.fit_birch_murnaghan(V, E)
    assert fit['b0_gpa'] == pytest.approx(1.0 * 160.21766208, rel=1e-5)


def test_fit_bm3_too_few_points_raises():
    with pytest.raises(ValueError, match='至少需 4'):
        eos.fit_birch_murnaghan([1, 2, 3], [1, 2, 3])


def test_fit_bm3_length_mismatch_raises():
    with pytest.raises(ValueError, match='长度不一致'):
        eos.fit_birch_murnaghan([1, 2, 3, 4], [1, 2, 3])


def test_fit_bm3_non_convex_raises():
    # 单调 E(V)(无极小)→ 无物理极小
    V = np.linspace(30, 50, 7)
    E = -V                                              # 线性单调
    with pytest.raises(ValueError):
        eos.fit_birch_murnaghan(V, E)


# ── EOS 系列派生 ────────────────────────────────────────────────────────────────
def test_build_eos_series_scales_lattice(tmp_path):
    src = _make_src(tmp_path)
    res = eos.build_eos_series(str(src), str(tmp_path / 'eos'), scales=[0.98, 1.0, 1.02])
    assert set(round(s, 2) for s in res['dirs']) == {0.98, 1.0, 1.02}
    # 缩放因子写在 POSCAR 第2行
    txt = (tmp_path / 'eos' / 'eos_0.98' / 'POSCAR').read_text()
    assert float(txt.splitlines()[1].split()[0]) == pytest.approx(0.98)
    # 体积 ∝ 因子³:基础体积 3.6³=46.656
    v98 = next(s['volume'] for s in res['series'] if s['scale'] == 0.98)
    assert v98 == pytest.approx(3.6 ** 3 * 0.98 ** 3)


def test_build_eos_series_single_point_incar(tmp_path):
    src = _make_src(tmp_path)
    eos.build_eos_series(str(src), str(tmp_path / 'eos'), scales=[1.0])
    d = parse_incar((tmp_path / 'eos' / 'eos_1' / 'INCAR').read_text())
    assert d['NSW'] == 0 and d['IBRION'] == -1
    assert 'ISIF' not in d and 'EDIFFG' not in d
    assert d['ENCUT'] == 400 and d['GGA'] == 'PE'           # 电子学保留


def test_build_eos_series_manifest_and_kpoints(tmp_path):
    src = _make_src(tmp_path)
    eos.build_eos_series(str(src), str(tmp_path / 'eos'), scales=[1.02])
    m = manifest_mod.load_manifest(tmp_path / 'eos' / 'eos_1.02')
    assert m['task_type'] == 'eos' and m['inputs']['scale'] == 1.02
    # KPOINTS 复制(各体积同 k 网格)
    assert '9 9 9' in (tmp_path / 'eos' / 'eos_1.02' / 'KPOINTS').read_text()


def test_build_eos_series_default_seven_points(tmp_path):
    src = _make_src(tmp_path)
    res = eos.build_eos_series(str(src), str(tmp_path / 'eos'))
    assert len(res['series']) == 7                          # 0.94..1.06 七点


def test_scale_poscar_negative_scale_raises():
    neg = _BULK.replace('1.0\n3.6', '-46.0\n3.6')
    with pytest.raises(ValueError, match='缩放因子'):
        eos._scale_poscar(neg, 1.02)


# ── eos_plot 出图 ───────────────────────────────────────────────────────────────
def test_eos_plot_outputs(tmp_path):
    V0, E0, B0_evA3, Bp = 45.0, -12.0, 0.6, 4.3
    V = np.linspace(0.94, 1.06, 7) ** 3 * V0
    eta = (V0 / V) ** (2.0 / 3.0)
    E = E0 + 9 * V0 * B0_evA3 / 16 * ((eta - 1) ** 3 * Bp + (eta - 1) ** 2 * (6 - 4 * eta))
    fit = eos.fit_birch_murnaghan(V, E)
    points = [{'volume': v, 'energy': e} for v, e in zip(V, E)]
    out = eos.eos_plot(points, fit, tmp_path / 'eos_fig', title='Cu')
    assert len(out) == 2
    with open(out[0], 'rb') as f:
        assert f.read(8) == b'\x89PNG\r\n\x1a\n'
