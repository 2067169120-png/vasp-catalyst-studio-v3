"""管线新增图冒烟测试:native_charts.convergence_plot / energy_time_plot(Agg 后端)。"""
from __future__ import annotations

import os
import sys

import matplotlib

matplotlib.use('Agg')                       # 无显示环境防线,须在任何 pyplot 之前

import numpy as _numpy  # noqa: E402
import pytest  # noqa: E402

from vcstudio.external import native_charts as nc  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    """修复 test_kpoints 延迟依赖测试对 sys.modules 的污染(与既有图测试同款)。"""
    sys.modules.setdefault('numpy', _numpy)
    yield


def _assert_png(path):
    assert os.path.isfile(path), path
    with open(path, 'rb') as f:
        assert f.read(8) == b'\x89PNG\r\n\x1a\n', f'{path} 不是合法 PNG'
    assert os.path.getsize(path) > 1000


def _assert_pdf(path):
    assert os.path.isfile(path), path
    with open(path, 'rb') as f:
        assert f.read(5) == b'%PDF-', f'{path} 不是合法 PDF'


def test_convergence_plot_dual_export(tmp_path):
    pts = [(300, -10.50), (400, -10.510), (500, -10.512),
           (600, -10.5125), (700, -10.5126)]
    out = nc.convergence_plot(pts, str(tmp_path / 'conv.png'),
                              threshold_mev=1.0, xlabel='ENCUT (eV)')
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_convergence_plot_accepts_dict_and_unsorted(tmp_path):
    data = {'xs': [700, 300, 500, 400, 600], 'ys': [-10.5126, -10.50, -10.512,
                                                     -10.510, -10.5125]}
    out = nc.convergence_plot(data, str(tmp_path / 'c2.png'), threshold_mev=2.0,
                              xlabel='k-density', per_atom=True)
    _assert_png(out[0])


def test_convergence_plot_needs_two_points(tmp_path):
    with pytest.raises(ValueError):
        nc.convergence_plot([(300, -1.0)], str(tmp_path / 'x.png'), xlabel='ENCUT')
    with pytest.raises(ValueError):
        nc.convergence_plot({'xs': [1, 2], 'ys': [1.0]}, str(tmp_path / 'x.png'),
                            xlabel='ENCUT')


def test_energy_time_plot_dual_axis(tmp_path):
    steps = [{'energy': -100.0 - 0.01 * i, 'temperature': 300 + (i % 7)}
             for i in range(24)]
    out = nc.energy_time_plot(steps, str(tmp_path / 'aimd.png'), dt_fs=1.0)
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_energy_time_plot_energy_only(tmp_path):
    data = {'energy': [-50.0, -50.1, -50.05, -50.2]}     # 无温度 → 单轴
    out = nc.energy_time_plot(data, str(tmp_path / 'e.png'))
    _assert_png(out[0])


def test_energy_time_plot_validation(tmp_path):
    with pytest.raises(ValueError):
        nc.energy_time_plot([{'energy': -1.0}], str(tmp_path / 'x.png'))
    with pytest.raises(ValueError):
        nc.energy_time_plot({'energy': [-1.0, -2.0, -3.0],
                             'temperature': [300, 310]}, str(tmp_path / 'x.png'))
