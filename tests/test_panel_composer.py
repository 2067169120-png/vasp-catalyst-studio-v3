"""panel_composer 多面板拼版器测试:suggest_layout + 真实拼版(2 图/蜂窝 3 图)+ 校验。"""
from __future__ import annotations

import os
import struct
import sys

import matplotlib

matplotlib.use('Agg')

import numpy as _numpy  # noqa: E402
import pytest  # noqa: E402

from vcstudio.external import native_charts as nc  # noqa: E402
from vcstudio.project import panel_composer as pc  # noqa: E402


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    sys.modules.setdefault('numpy', _numpy)
    yield


def _png_size(path):
    with open(path, 'rb') as f:
        f.read(16)
        return struct.unpack('>II', f.read(8))       # (width_px, height_px)


def _make_two(tmp_path):
    f1 = nc.adsorption_bar({'adsorbates': ['S8', 'Li2S8'],
                            'substrates': {'CoO': [-1.2, -0.8]}},
                           str(tmp_path / 'a.png'))[0]
    f2 = nc.scaling_relation([1.0, 2.0, 3.0], [1.1, 2.0, 2.9],
                             str(tmp_path / 'b.png'), xlabel='x', ylabel='y')[0]
    return f1, f2


# ── suggest_layout ────────────────────────────────────────────────────────────

def test_suggest_layout_values():
    assert pc.suggest_layout(1) == (1, 1)
    assert pc.suggest_layout(2) == (1, 2)
    assert pc.suggest_layout(3) == (2, 2)         # 蜂窝:2 上 1 下
    assert pc.suggest_layout(4) == (2, 2)
    assert pc.suggest_layout(5) == (2, 3)
    assert pc.suggest_layout(6) == (2, 3)
    assert pc.suggest_layout(9) == (3, 3)


def test_suggest_layout_rejects_nonpositive():
    with pytest.raises(ValueError):
        pc.suggest_layout(0)


# ── compose ───────────────────────────────────────────────────────────────────

def test_compose_two_panels_dual_export(tmp_path):
    f1, f2 = _make_two(tmp_path)
    out = pc.compose([{'file': f1, 'label': 'a'}, {'file': f2, 'label': 'b'}],
                     str(tmp_path / 'panel.png'), cols=2, journal='nature',
                     width='double')
    assert len(out) == 2
    assert out[0].endswith('.png') and os.path.getsize(out[0]) > 2000
    assert out[1].endswith('.pdf')
    with open(out[1], 'rb') as f:
        assert f.read(5) == b'%PDF-'
    w, h = _png_size(out[0])
    # 目标双栏 ≈ 7.2 in @ ≥300 dpi;bbox='tight' 略有裁剪,给足容差
    assert w >= int(6.5 * 300), (w, h)


def test_compose_single_narrower_than_double(tmp_path):
    f1, f2 = _make_two(tmp_path)
    single = pc.compose([{'file': f1}, {'file': f2}], str(tmp_path / 's.png'),
                        width='single')
    double = pc.compose([{'file': f1}, {'file': f2}], str(tmp_path / 'd.png'),
                        width='double')
    assert _png_size(single[0])[0] < _png_size(double[0])[0]


def test_compose_honeycomb_three(tmp_path):
    f1, f2 = _make_two(tmp_path)
    out = pc.compose([{'file': f1}, {'file': f2}, {'file': f1}],
                     str(tmp_path / 'p3.png'), cols=2)          # 3 图 → 2 + 1(末行居中)
    assert os.path.isfile(out[0]) and os.path.getsize(out[0]) > 2000


def test_compose_empty_raises(tmp_path):
    with pytest.raises(ValueError):
        pc.compose([], str(tmp_path / 'x.png'))


def test_compose_missing_source_raises(tmp_path):
    with pytest.raises(ValueError):
        pc.compose([{'file': str(tmp_path / 'nope.png')}], str(tmp_path / 'x.png'))


def test_compose_auto_labels_when_absent(tmp_path):
    f1, f2 = _make_two(tmp_path)
    out = pc.compose([{'file': f1}, {'file': f2}], str(tmp_path / 'auto.png'))
    assert os.path.isfile(out[0])                    # 无 label 也能自动补 a/b
