"""能带解析/带隙/出图测试(project.bands):EIGENVAL & vasprun 解析、直接/间接/金属判定、band_plot。"""
from __future__ import annotations

import os
import sys

import matplotlib

matplotlib.use('Agg')

import numpy as _numpy  # noqa: E402
import pytest  # noqa: E402

from vcstudio.project import bands  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    sys.modules.setdefault('numpy', _numpy)
    yield


def _eigenval(nelect, kpt_bands, ispin=1):
    """kpt_bands: 每个 k 点的能量列表(ISPIN=1),合成 EIGENVAL 文本。"""
    nk = len(kpt_bands)
    nb = len(kpt_bands[0])
    lines = [f'   2   2   1   {ispin}', '  1.0 1.0 1.0 1.0 1e-4', '  0.0', '  CAR', '  synthetic',
             f'   {nelect}   {nk}   {nb}']
    for k, energies in enumerate(kpt_bands):
        lines.append('')
        lines.append(f'  {0.1 * k:.3f} 0.0 0.0 {1.0 if k == 0 else 0.0}')
        for b, e in enumerate(energies):
            lines.append(f'  {b + 1}  {e:.6f}  {1.0 if b < nelect // 2 else 0.0}')
    return '\n'.join(lines) + '\n'


# 1 占据带(NELECT=2);valence=band0,conduction=band1
_DIRECT = _eigenval(2, [[-3.0, 1.0], [-2.0, 0.5], [-3.2, 1.5]])     # VBM/CBM 都在 k=1
_INDIRECT = _eigenval(2, [[-3.0, 1.5], [-2.0, 1.0], [-3.2, 0.5]])   # VBM@k1,CBM@k2
_METAL = _eigenval(2, [[-1.0, 0.3], [0.5, 0.6], [-1.0, 0.4]])       # VBM=0.5>CBM=0.3


_VASPRUN = """<?xml version="1.0"?>
<modeling>
 <calculation>
  <dos><i name="efermi">0.0</i></dos>
  <eigenvalues><array><set>
   <set comment="spin 1">
    <set comment="kpoint 1"><r> -3.0 1.0 </r><r>  1.0 0.0 </r></set>
    <set comment="kpoint 2"><r> -2.0 1.0 </r><r>  0.5 0.0 </r></set>
   </set>
  </set></array></eigenvalues>
  <kpoints><varray name="kpointlist">
   <v> 0 0 0 </v><v> 0.5 0 0 </v>
  </varray></kpoints>
 </calculation>
</modeling>
"""


# ── EIGENVAL 解析 ────────────────────────────────────────────────────────────────
def test_parse_eigenval_shape():
    d = bands.parse_eigenval(_DIRECT)
    assert d['ispin'] == 1 and d['nkpts'] == 3 and d['nbands'] == 2
    assert d['nelect'] == 2.0 and d['efermi'] is None
    assert d['bands'][0][0] == pytest.approx([-3.0, -2.0, -3.2])   # band0 across k
    assert len(d['kpath']) == 3


def test_parse_eigenval_ispin2():
    ev = ('   2   2   1   2\n  1 1 1 1 1e-4\n  0\n  CAR\n  s\n   4   1   2\n\n'
          '  0 0 0 1.0\n  1  -3.0  -2.9  1 1\n  2   1.0   1.1  0 0\n')
    d = bands.parse_eigenval(ev)
    assert d['ispin'] == 2
    assert d['bands'][0][0][0] == pytest.approx(-3.0)   # spin up band0 k0
    assert d['bands'][1][0][0] == pytest.approx(-2.9)   # spin dn band0 k0


# ── 带隙:直接 / 间接 / 金属 ──────────────────────────────────────────────────────
def test_gap_direct():
    d = bands.parse_bands(_DIRECT)
    g = d['gap']
    assert g['value'] == pytest.approx(2.5) and g['direct'] is True
    assert g['vbm']['k'] == 1 and g['cbm']['k'] == 1 and not g['metal']


def test_gap_indirect():
    g = bands.parse_bands(_INDIRECT)['gap']
    assert g['value'] == pytest.approx(2.5) and g['direct'] is False
    assert g['vbm']['k'] == 1 and g['cbm']['k'] == 2


def test_gap_metal():
    g = bands.parse_bands(_METAL)['gap']
    assert g['metal'] is True and g['value'] == 0.0


def test_gap_via_efermi_override():
    # 用 efermi 覆盖(EIGENVAL 无 efermi),按费米阈判
    g = bands.band_gap(bands.parse_eigenval(_DIRECT)['bands'], efermi=-1.0)
    assert g['value'] == pytest.approx(2.5) and g['direct'] is True


def test_gap_ispin2_no_efermi_note():
    d = bands.parse_eigenval(
        '   2   2   1   2\n  1 1 1 1 1e-4\n  0\n  CAR\n  s\n   4   1   2\n\n'
        '  0 0 0 1.0\n  1  -3.0  -2.9  1 1\n  2   1.0   1.1  0 0\n')
    g = bands.band_gap(d['bands'], efermi=None, nelect=4, ispin=2)
    assert g['value'] is None and 'ISPIN=2' in g['note']


# ── vasprun 解析 ────────────────────────────────────────────────────────────────
def test_parse_vasprun_efermi_and_gap():
    d = bands.parse_bands(_VASPRUN)
    assert d['efermi'] == pytest.approx(0.0)
    assert d['gap']['value'] == pytest.approx(2.5) and d['gap']['direct'] is True
    assert len(d['kpath']) == 2


def test_parse_bands_dispatch_xml_vs_eigenval():
    assert bands.parse_bands(_VASPRUN)['efermi'] == 0.0     # XML 路径
    assert bands.parse_bands(_DIRECT)['efermi'] is None     # EIGENVAL 路径


# ── band_plot 出图 ──────────────────────────────────────────────────────────────
def test_band_plot_outputs(tmp_path):
    d = bands.parse_bands(_DIRECT)
    out = bands.band_plot(d, tmp_path / 'band', ticks=[(0, 'Γ'), (1, 'X'), (2, 'M')],
                          ylim=(-5, 3), title='Si')
    assert len(out) == 2
    with open(out[0], 'rb') as f:
        assert f.read(8) == b'\x89PNG\r\n\x1a\n'
    assert os.path.getsize(out[1]) > 500


def test_band_plot_ispin2_two_colors(tmp_path):
    d = bands.parse_eigenval(
        '   2   2   1   2\n  1 1 1 1 1e-4\n  0\n  CAR\n  s\n   4   2   2\n\n'
        '  0 0 0 1.0\n  1  -3.0  -2.9  1 1\n  2   1.0   1.1  0 0\n\n'
        '  0.5 0 0 0.0\n  1  -2.8  -2.7  1 1\n  2   1.2   1.3  0 0\n')
    out = bands.band_plot(d, tmp_path / 'band2', efermi=0.0)
    assert len(out) == 2 and os.path.isfile(out[0])
