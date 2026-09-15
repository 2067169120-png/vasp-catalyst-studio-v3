"""电子结构出图冒烟测试(native_charts.pdos_plot / charge_profile_plot,Agg 后端)。"""
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
    """与 test_native_charts 同款:修复 test_kpoints 延迟依赖测试对 sys.modules 的污染。"""
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


_E = [-8.0, -6.0, -4.0, -2.0, 0.0, 2.0, 4.0]


def test_pdos_plot_spin_mirror_and_band_center(tmp_path):
    series = [
        {'label': 'Co 3d', 'energies': _E,
         'dos_up': [0.1, 0.5, 1.2, 2.0, 0.6, 0.2, 0.0],
         'dos_down': [0.1, 0.4, 0.9, 1.1, 0.3, 0.1, 0.0], 'color': '#EE6677'},
        {'label': 'O 2p', 'energies': _E,
         'dos_up': [0.0, 0.2, 0.8, 0.3, 0.1, 0.0, 0.0]},
    ]
    out = nc.pdos_plot(series, tmp_path / 'pdos', efermi=0.0,
                       band_centers={'Co 3d': -1.8}, panel='a', title='PDOS')
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_pdos_plot_single_spin_no_mirror(tmp_path):
    series = [{'label': 'd', 'energies': _E,
               'dos_up': [0.0, 0.3, 1.0, 1.5, 0.4, 0.1, 0.0]}]
    out = nc.pdos_plot(series, tmp_path / 'pdos2.png', mirror_spin=False,
                       xlim=(-6, 3))
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_pdos_plot_bad_data():
    with pytest.raises(ValueError):                        # series 空
        nc.pdos_plot([], 'x')
    with pytest.raises(ValueError):                        # 缺 dos_up
        nc.pdos_plot([{'energies': _E}], 'x')
    with pytest.raises(ValueError):                        # 长度不一致
        nc.pdos_plot([{'energies': _E, 'dos_up': [1.0, 2.0]}], 'x')


def test_charge_profile_plot_with_regions(tmp_path):
    z = [round(0.5 * k, 2) for k in range(20)]
    rho = [((-1) ** k) * 0.01 * (k - 10) for k in range(20)]
    out = nc.charge_profile_plot(
        z, rho, tmp_path / 'chg', panel='b', title='planar avg',
        regions=[{'z0': 0.0, 'z1': 4.0, 'label': 'slab', 'color': '#E9E9E9'},
                 {'z0': 6.0, 'z1': 9.5, 'label': 'adsorbate', 'color': '#FDE9C8'}])
    assert len(out) == 2
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_charge_profile_plot_plain(tmp_path):
    out = nc.charge_profile_plot([0.0, 1.0, 2.0, 3.0], [0.0, 0.2, -0.1, 0.05],
                                 tmp_path / 'chg2.png')
    _assert_png(out[0])
    _assert_pdf(out[1])


def test_charge_profile_plot_bad_data():
    with pytest.raises(ValueError):                        # 长度不一致
        nc.charge_profile_plot([0.0, 1.0], [0.0], 'x')
    with pytest.raises(ValueError):                        # 点数 < 2
        nc.charge_profile_plot([0.0], [0.0], 'x')
