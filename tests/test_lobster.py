"""LOBSTER/COHP 链测试:lobsterin 文本、前置检查、COHPCAR/ICOHPLIST/lobsterout 解析、
spilling 闸、cohp_plot 冒烟。纯函数离线测,subprocess 经 run= 注入;matplotlib 有则测文件非空。
"""
from __future__ import annotations

import os
import sys

import pytest

from vcstudio.external import lobster as lb

try:
    import matplotlib
    matplotlib.use('Agg')
    import numpy as _numpy
    _HAS_MPL = True
except Exception:                              # pragma: no cover
    _HAS_MPL = False


@pytest.fixture(autouse=True)
def _heal_numpy_sys_modules():
    """全套跑时 test_kpoints 会 pop 'numpy' 但留子模块,后续 fresh import 触发
    RecursionError;把收集期同一模块对象放回(零副作用)。"""
    if _HAS_MPL:
        sys.modules.setdefault('numpy', _numpy)
    yield


# ── lobsterin 文本 ───────────────────────────────────────────────────────────

def test_lobsterin_atom_pairs_emits_cohpbetween():
    t = lb.lobsterin_text(atom_pairs=[(1, 2), (1, 3)])
    assert 'cohpbetween atom 1 atom 2' in t
    assert 'cohpbetween atom 1 atom 3' in t
    assert 'cohpGenerator' not in t             # 给定对时不自动枚举
    assert 'basisSet pbeVaspFit2015' in t


def test_lobsterin_distance_generator_when_no_pairs():
    t = lb.lobsterin_text(distance_range=(0.5, 3.2))
    assert 'cohpGenerator from 0.5 to 3.2' in t
    assert 'cohpbetween' not in t


def test_lobsterin_energy_window_and_basis():
    t = lb.lobsterin_text(basis='bunge', e_window=(-8, 4))
    assert 'COHPstartEnergy -8' in t
    assert 'COHPendEnergy 4' in t
    assert 'basisSet bunge' in t
    assert 'gaussianSmearingWidth' in t         # 含展宽行


def test_lobsterin_spin_comment_toggle():
    on = lb.lobsterin_text(spin=True)
    off = lb.lobsterin_text(spin=False)
    assert 'ISPIN=2' in on
    assert 'ISPIN=1' in off or '非自旋' in off
    # 每行都有注释说明(以 # 开头的行 ≥ 若干)
    assert sum(1 for ln in on.splitlines() if ln.strip().startswith('#')) >= 5


# ── prepare_lobster_dir 前置检查各分支 ──────────────────────────────────────

def _write_incar(d, text):
    with open(os.path.join(d, 'INCAR'), 'w', encoding='utf-8') as f:
        f.write(text)


def _poscar_fe_s(d):
    with open(os.path.join(d, 'POSCAR'), 'w', encoding='utf-8') as f:
        f.write('FeS\n1.0\n5 0 0\n0 5 0\n0 0 5\nFe S\n1 1\nDirect\n'
                '0 0 0\n0.5 0.5 0.5\n')


def test_prepare_missing_files_listed(tmp_path):
    r = lb.prepare_lobster_dir(str(tmp_path))
    assert os.path.isfile(r['lobsterin_path'])   # lobsterin 无论如何写出
    assert not r['ok']
    reqs = ' '.join(r['requirements'])
    assert 'WAVECAR' in reqs and 'vasprun.xml' in reqs and 'POTCAR' in reqs


def test_prepare_all_ready_ok(tmp_path):
    d = str(tmp_path)
    for name in ('WAVECAR', 'vasprun.xml', 'POTCAR'):
        (tmp_path / name).write_text('x', encoding='utf-8')
    _poscar_fe_s(d)
    _write_incar(d, 'ISYM = -1\nNBANDS = 40\nLWAVE = .TRUE.\n')  # Fe9+S4=13 < 40
    r = lb.prepare_lobster_dir(d, atom_pairs=[(1, 2)])
    assert r['ok'] and not r['requirements'], r['requirements']


def test_prepare_isym_wrong_flagged(tmp_path):
    d = str(tmp_path)
    for name in ('WAVECAR', 'vasprun.xml', 'POTCAR'):
        (tmp_path / name).write_text('x', encoding='utf-8')
    _poscar_fe_s(d)
    _write_incar(d, 'ISYM = 2\nNBANDS = 40\n')
    r = lb.prepare_lobster_dir(d)
    assert not r['ok']
    assert any('ISYM' in x and '-1' in x for x in r['requirements'])


def test_prepare_isym_zero_accepted(tmp_path):
    """ISYM=0 亦为关对称,LOBSTER 可接受(与 -1 同判通过)。"""
    d = str(tmp_path)
    for name in ('WAVECAR', 'vasprun.xml', 'POTCAR'):
        (tmp_path / name).write_text('x', encoding='utf-8')
    _poscar_fe_s(d)
    _write_incar(d, 'ISYM = 0\nNBANDS = 40\n')
    r = lb.prepare_lobster_dir(d)
    assert r['ok'], r['requirements']


def test_prepare_nbands_too_low(tmp_path):
    d = str(tmp_path)
    for name in ('WAVECAR', 'vasprun.xml', 'POTCAR'):
        (tmp_path / name).write_text('x', encoding='utf-8')
    _poscar_fe_s(d)
    _write_incar(d, 'ISYM = -1\nNBANDS = 5\n')   # 5 < 13
    r = lb.prepare_lobster_dir(d)
    assert not r['ok']
    assert any('NBANDS' in x and '13' in x for x in r['requirements'])


def test_prepare_nbands_missing_recommends(tmp_path):
    d = str(tmp_path)
    for name in ('WAVECAR', 'vasprun.xml', 'POTCAR'):
        (tmp_path / name).write_text('x', encoding='utf-8')
    _poscar_fe_s(d)
    _write_incar(d, 'ISYM = -1\n')               # 无 NBANDS
    r = lb.prepare_lobster_dir(d)
    assert any('NBANDS' in x for x in r['requirements'])


def test_prepare_nonexistent_dir_errors(tmp_path):
    r = lb.prepare_lobster_dir(str(tmp_path / 'nope'))
    assert not r['ok'] and '不存在' in r['error']


# ── 探测与调用(subprocess 注入) ────────────────────────────────────────────

def test_find_lobster_configured(tmp_path):
    exe = tmp_path / 'lobster'
    exe.write_text('#!/bin/sh\n', encoding='utf-8')
    assert lb.find_lobster(str(exe)) == str(exe)


def test_find_lobster_none(monkeypatch):
    monkeypatch.setattr(lb.shutil, 'which', lambda name: None)
    monkeypatch.setattr(lb, '_DEFAULT_LOBSTER_DIRS', ())
    assert lb.find_lobster() is None


def test_run_lobster_no_exe_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, 'find_lobster', lambda configured='': None)
    r = lb.run_lobster(str(tmp_path))
    assert not r['ok'] and 'lobster 可执行文件' in r['error']


def test_run_lobster_fake_success(tmp_path):
    (tmp_path / 'lobsterin').write_text('x', encoding='utf-8')
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        with open(os.path.join(kw['cwd'], 'lobsterout'), 'w') as f:
            f.write('done')

        class P:
            returncode = 0
            stdout = b'LOBSTER finished normally\n'
        return P()

    r = lb.run_lobster(str(tmp_path), exe='lob.exe', run=fake_run)
    assert r['ok'] and 'finished' in r['stdout_tail']
    assert calls[0] == ['lob.exe']


def test_run_lobster_missing_lobsterin(tmp_path):
    r = lb.run_lobster(str(tmp_path), exe='lob.exe', run=lambda *a, **k: None)
    assert not r['ok'] and 'lobsterin' in r['error']


def test_run_lobster_nonzero_exit(tmp_path):
    (tmp_path / 'lobsterin').write_text('x', encoding='utf-8')

    def fail_run(cmd, **kw):
        class P:
            returncode = 7
            stdout = b'boom'
        return P()

    r = lb.run_lobster(str(tmp_path), exe='lob.exe', run=fail_run)
    assert not r['ok'] and '退出码 7' in r['error']


# ── COHPCAR.lobster 解析(自旋/非自旋) ──────────────────────────────────────

_COHPCAR_NONSPIN = """COHPCAR.lobster
3 1 3 -2.0 2.0 0.00000
No.1:Fe1->O2(2.05)
No.2:Fe1->O3(2.06)
 -2.0  -0.10 -0.50  -0.20 -0.60  -0.05 -0.40
  0.0  -0.30 -0.80  -0.40 -0.90  -0.10 -0.60
  2.0   0.20  0.00   0.30  0.00   0.15  0.00
"""

_COHPCAR_SPIN = """COHPCAR.lobster
3 2 3 -2.0 2.0 0.00000
No.1:Fe1->O2(2.05)
No.2:Fe1->O3(2.06)
 -2.0  -0.10 -0.50 -0.20 -0.60 -0.05 -0.40  -0.11 -0.51 -0.21 -0.61 -0.06 -0.41
  0.0  -0.30 -0.80 -0.40 -0.90 -0.10 -0.60  -0.31 -0.81 -0.41 -0.91 -0.11 -0.61
  2.0   0.20  0.00  0.30  0.00  0.15  0.00   0.21  0.00  0.31  0.00  0.16  0.00
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding='utf-8')
    return str(p)


def test_parse_cohpcar_nonspin_values(tmp_path):
    parsed = lb.parse_cohpcar(_write(tmp_path, 'COHPCAR.lobster', _COHPCAR_NONSPIN))
    assert parsed['energies'] == [-2.0, 0.0, 2.0]
    assert not parsed['spin_polarized'] and parsed['efermi_zeroed']
    assert parsed['pairs'][0]['label'] == 'average'
    assert parsed['pairs'][0]['cohp'] == [-0.10, -0.30, 0.20]
    b1 = parsed['pairs'][1]
    assert b1['label'] == 'Fe1->O2' and b1['distance'] == 2.05
    assert b1['cohp'] == [-0.20, -0.40, 0.30]        # 第 3 列
    assert b1['icohp'] == [-0.60, -0.90, 0.00]       # 第 4 列
    assert b1['cohp_up'] is None and b1['cohp_down'] is None


def test_parse_cohpcar_spin_up_down_and_sum(tmp_path):
    parsed = lb.parse_cohpcar(_write(tmp_path, 'COHPCAR.lobster', _COHPCAR_SPIN))
    assert parsed['spin_polarized']
    b1 = parsed['pairs'][1]
    assert b1['cohp_up'] == [-0.20, -0.40, 0.30]
    assert b1['cohp_down'] == [-0.21, -0.41, 0.31]
    # cohp 合计 = 上 + 下
    assert b1['cohp'][0] == pytest.approx(-0.41)
    assert b1['icohp'][1] == pytest.approx(-0.90 + -0.91)


def test_parse_cohpcar_bad_and_short(tmp_path):
    with pytest.raises(ValueError):
        lb.parse_cohpcar(_write(tmp_path, 'c1', 'COHPCAR.lobster\n'))
    # 列数不足
    bad = ('COHPCAR.lobster\n3 1 3 0.0\nNo.1:A->B(2.0)\nNo.2:A->C(2.1)\n'
           ' -2 -0.1 -0.5\n')
    with pytest.raises(ValueError, match='列数|损坏|格式'):
        lb.parse_cohpcar(_write(tmp_path, 'c2', bad))


def test_parse_cohpcar_average_only(tmp_path):
    """num_bonds=0(仅平均)也能解析。"""
    text = ('COHPCAR.lobster\n1 1 2 0.0\n -1.0 -0.2 -0.4\n  1.0 0.1 0.0\n')
    parsed = lb.parse_cohpcar(_write(tmp_path, 'c3', text))
    assert len(parsed['pairs']) == 1 and parsed['pairs'][0]['label'] == 'average'


# ── ICOHPLIST.lobster 解析 ───────────────────────────────────────────────────

_ICOHP_NONSPIN = """COHPLIST header line
     1  Fe1  O2  2.05000  0 0 0  -1.234
     2  Fe1  O3  2.06000  0 0 0  -1.100
"""

_ICOHP_SPIN = """header
     1  Fe1  O2  2.05000  0 0 0  -0.600
     2  Fe1  O3  2.06000  0 0 0  -0.550
     1  Fe1  O2  2.05000  0 0 0  -0.634
     2  Fe1  O3  2.06000  0 0 0  -0.550
"""


def test_parse_icohplist_nonspin(tmp_path):
    recs = lb.parse_icohplist(_write(tmp_path, 'ICOHPLIST.lobster', _ICOHP_NONSPIN))
    assert len(recs) == 2
    assert recs[0]['pair'] == 'Fe1->O2' and recs[0]['icohp'] == -1.234
    assert recs[0]['distance'] == 2.05 and recs[0]['icohp_up'] is None


def test_parse_icohplist_spin_pairs_up_down(tmp_path):
    recs = lb.parse_icohplist(_write(tmp_path, 'ICOHPLIST.lobster', _ICOHP_SPIN))
    assert len(recs) == 2                          # 两块合并为每对一条
    assert recs[0]['icohp_up'] == pytest.approx(-0.600)
    assert recs[0]['icohp_down'] == pytest.approx(-0.634)
    assert recs[0]['icohp'] == pytest.approx(-1.234)


def test_parse_icohplist_empty_raises(tmp_path):
    with pytest.raises(ValueError):
        lb.parse_icohplist(_write(tmp_path, 'ic', 'only header\n'))


# ── spilling 解析 + 质量闸 ───────────────────────────────────────────────────

def test_parse_spilling_fractions(tmp_path):
    out = (' SpillingFactor:\n abs. total spilling:  1.23 %\n'
           ' abs. charge spilling:  0.85 %\n')
    sp = lb.parse_spilling(_write(tmp_path, 'lobsterout', out))
    assert sp['abs_total'] == pytest.approx(0.0123)
    assert sp['charge_spilling'] == pytest.approx(0.0085)


def test_parse_spilling_spin_per_channel(tmp_path):
    out = (' spilling for spin channel 1\n total spilling: 1.20 %\n'
           ' charge spilling: 0.80 %\n'
           ' spilling for spin channel 2\n total spilling: 1.60 %\n'
           ' charge spilling: 0.90 %\n')
    sp = lb.parse_spilling(_write(tmp_path, 'lobsterout', out))
    assert len(sp['per_spin']) == 2
    assert sp['abs_total'] == pytest.approx(0.016)    # 取最差自旋道
    assert sp['per_spin'][0]['total'] == pytest.approx(0.012)


def test_parse_spilling_none_raises(tmp_path):
    with pytest.raises(ValueError):
        lb.parse_spilling(_write(tmp_path, 'lobsterout', 'no spilling here\n'))


def test_spilling_gate_pass_and_fail():
    good = lb.spilling_gate({'abs_total': 0.012, 'charge_spilling': 0.008})
    assert good['ok'] and '可接受' in good['note']
    bad = lb.spilling_gate({'abs_total': 0.065, 'charge_spilling': 0.031})
    assert not bad['ok'] and 'COHP 不可信' in bad['note']


def test_spilling_gate_custom_threshold():
    r = lb.spilling_gate({'abs_total': 0.03, 'charge_spilling': 0.02},
                         threshold=0.02)
    assert not r['ok']                              # 3% > 2% 阈


# ── cohp_plot 冒烟(matplotlib 有则文件非空) ────────────────────────────────

def _parsed_nonspin():
    return {'energies': [-2.0, -1.0, 0.0, 1.0, 2.0], 'efermi': 0.0,
            'efermi_zeroed': True, 'spin_polarized': False,
            'pairs': [
                {'label': 'average', 'distance': None,
                 'cohp': [-0.1, -0.2, -0.3, 0.1, 0.2],
                 'icohp': [-0.5, -0.7, -0.9, -0.8, -0.6],
                 'cohp_up': None, 'cohp_down': None},
                {'label': 'Fe1->S2', 'distance': 2.3,
                 'cohp': [-0.2, -0.4, -0.5, 0.2, 0.3],
                 'icohp': [-0.6, -1.0, -1.4, -1.2, -1.0],
                 'cohp_up': None, 'cohp_down': None}]}


def _assert_png_pdf(paths):
    assert len(paths) == 2
    with open(paths[0], 'rb') as f:
        assert f.read(8) == b'\x89PNG\r\n\x1a\n'
    with open(paths[1], 'rb') as f:
        assert f.read(5) == b'%PDF-'
    assert all(os.path.getsize(p) > 1000 for p in paths)


@pytest.mark.skipif(not _HAS_MPL, reason='无 matplotlib')
def test_cohp_plot_smoke(tmp_path):
    paths = lb.cohp_plot(_parsed_nonspin(), str(tmp_path / 'cohp'))
    _assert_png_pdf(paths)


@pytest.mark.skipif(not _HAS_MPL, reason='无 matplotlib')
def test_cohp_plot_spin_smoke(tmp_path):
    parsed = {'energies': [-2.0, -1.0, 0.0, 1.0, 2.0], 'efermi': 0.0,
              'efermi_zeroed': True, 'spin_polarized': True,
              'pairs': [{'label': 'Co1->S3', 'distance': 2.2,
                         'cohp': [-0.4, -0.6, -0.8, 0.2, 0.3],
                         'icohp': [-0.5, -0.9, -1.3, -1.1, -0.9],
                         'cohp_up': [-0.2, -0.3, -0.4, 0.1, 0.15],
                         'cohp_down': [-0.2, -0.3, -0.4, 0.1, 0.15],
                         'icohp_up': [-0.25] * 5, 'icohp_down': [-0.25] * 5}]}
    paths = lb.cohp_plot(parsed, str(tmp_path / 'cohp_spin'))
    _assert_png_pdf(paths)


@pytest.mark.skipif(not _HAS_MPL, reason='无 matplotlib')
def test_cohp_plot_pair_labels_filter(tmp_path):
    paths = lb.cohp_plot(_parsed_nonspin(), str(tmp_path / 'c'),
                         pair_labels=['Fe1->S2'], flip=False)
    _assert_png_pdf(paths)


def test_cohp_plot_bad_parsed_raises(tmp_path):
    with pytest.raises(ValueError):
        lb.cohp_plot({'energies': [], 'pairs': []}, str(tmp_path / 'x'))


@pytest.mark.skipif(not _HAS_MPL, reason='无 matplotlib')
def test_cohp_plot_unknown_pair_label_raises(tmp_path):
    with pytest.raises(ValueError, match='未匹配'):
        lb.cohp_plot(_parsed_nonspin(), str(tmp_path / 'x'),
                     pair_labels=['NoSuch->Pair'])


@pytest.mark.skipif(lb.find_lobster() is None, reason='本机无 lobster')
def test_real_lobster_present_smoke():                # pragma: no cover
    assert os.path.isfile(lb.find_lobster())
