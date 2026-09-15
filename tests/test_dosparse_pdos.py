"""F19 全分辨投影 DOS + d 带中心(梯形积分)测试:合成 vasprun.xml,数值手算对拍。

合成夹具:2 原子 × (s/p/d) × 双自旋,能量网格 [-3,-2,-1,0,1]。列布局 LORBIT=11
(E s py pz px dxy dyz dz2 dxz x2-y2,10 列);s 全放 col1,p 全放 py,d 全放 dxy。
手算基准(ion1):
  上自旋 d = [0,1,2,1,0];下自旋 d = [0,0,2,2,0];s↑=[0,.1,.2,.3,0];p↑=[0,.2,.1,0,0]
  ion2 上自旋 d = [0,2,0,0,0]。
"""
import io

import pytest

from vcstudio.project import dosparse


def _row(e, s, p, d):
    """一行 10 列:E s py pz px dxy dyz dz2 dxz x2-y2(p→py,d→dxy,其余 0)。"""
    return f'<r> {e} {s} {p} 0 0 {d} 0 0 0 0 </r>\n'


def _spin_block(sp, rows):
    return f'<set comment="spin {sp}">\n{rows}</set>\n'


# ion1:能量 [-3,-2,-1,0,1]
_I1_UP = (_row(-3.0, 0.0, 0.0, 0.0) + _row(-2.0, 0.1, 0.2, 1.0)
          + _row(-1.0, 0.2, 0.1, 2.0) + _row(0.0, 0.3, 0.0, 1.0)
          + _row(1.0, 0.0, 0.0, 0.0))
_I1_DN = (_row(-3.0, 0.0, 0.0, 0.0) + _row(-2.0, 0.0, 0.0, 0.0)
          + _row(-1.0, 0.0, 0.0, 2.0) + _row(0.0, 0.0, 0.0, 2.0)
          + _row(1.0, 0.0, 0.0, 0.0))
_I2_UP = (_row(-3.0, 0.0, 0.0, 0.0) + _row(-2.0, 0.0, 0.0, 2.0)
          + _row(-1.0, 0.0, 0.0, 0.0) + _row(0.0, 0.0, 0.0, 0.0)
          + _row(1.0, 0.0, 0.0, 0.0))
_I2_DN = _I2_UP  # ion2 两自旋等值


def _vasprun_pdos(spin2=True, efermi=0.0):
    fields = ('<field>energy</field><field>s</field><field>py</field>'
              '<field>pz</field><field>px</field><field>dxy</field>'
              '<field>dyz</field><field>dz2</field><field>dxz</field>'
              '<field>x2-y2</field>\n')

    def ion(n, up, dn):
        blk = _spin_block(1, up)
        if spin2:
            blk += _spin_block(2, dn)
        return f'<set comment="ion {n}">\n{blk}</set>\n'

    return ('<?xml version="1.0" encoding="ISO-8859-1"?>\n<modeling>\n<calculation>\n'
            f'<dos>\n<i name="efermi"> {efermi} </i>\n'
            f'<partial>\n<array>\n{fields}<set>\n'
            f'{ion(1, _I1_UP, _I1_DN)}{ion(2, _I2_UP, _I2_DN)}'
            '</set>\n</array>\n</partial>\n</dos>\n'
            '<projected><big>ignored huge block</big></projected>\n'
            '</calculation>\n</modeling>\n')


# ── parse_vasprun_pdos ───────────────────────────────────────────────────────

def test_pdos_parse_structure_and_orbitals():
    d = dosparse.parse_vasprun_pdos(_vasprun_pdos())
    assert d['efermi'] == pytest.approx(0.0)
    assert d['spin_polarized'] is True
    assert d['energies'] == pytest.approx([-3.0, -2.0, -1.0, 0.0, 1.0])
    assert [ion['index'] for ion in d['ions']] == [1, 2]
    o1 = d['ions'][0]['orbitals']
    assert o1['d']['up'] == pytest.approx([0, 1, 2, 1, 0])
    assert o1['d']['down'] == pytest.approx([0, 0, 2, 2, 0])
    assert o1['s']['up'] == pytest.approx([0, 0.1, 0.2, 0.3, 0])
    assert o1['p']['up'] == pytest.approx([0, 0.2, 0.1, 0, 0])
    assert d['ions'][1]['orbitals']['d']['up'] == pytest.approx([0, 2, 0, 0, 0])


def test_pdos_parse_accepts_stringio_and_text():
    txt = _vasprun_pdos()
    a = dosparse.parse_vasprun_pdos(io.StringIO(txt))
    b = dosparse.parse_vasprun_pdos(txt)              # 含 '<' 的 str 视为 XML 文本
    assert a['energies'] == pytest.approx(b['energies'])


def test_pdos_parse_from_file(tmp_path):
    p = tmp_path / 'vasprun.xml'
    p.write_text(_vasprun_pdos(), encoding='utf-8')
    d = dosparse.parse_vasprun_pdos(str(p))            # str 路径
    assert d['ions'][0]['orbitals']['d']['up'] == pytest.approx([0, 1, 2, 1, 0])
    d2 = dosparse.parse_vasprun_pdos(p)                # PathLike
    assert d2['spin_polarized'] is True


def test_pdos_non_spin_polarized():
    d = dosparse.parse_vasprun_pdos(_vasprun_pdos(spin2=False))
    assert d['spin_polarized'] is False
    assert 'down' not in d['ions'][0]['orbitals']['d']


def test_pdos_missing_partial_raises():
    xml = ('<modeling><calculation><dos><i name="efermi"> 0.0 </i>\n'
           '<total><array><set><set comment="spin 1">\n<r> -1 1 </r>\n'
           '</set></set></array></total></dos></calculation></modeling>')
    with pytest.raises(ValueError, match='无投影'):
        dosparse.parse_vasprun_pdos(xml)


# ── sum_pdos ─────────────────────────────────────────────────────────────────

def test_sum_pdos_ion_filter_both_spins():
    d = dosparse.parse_vasprun_pdos(_vasprun_pdos())
    r = dosparse.sum_pdos(d, ions=(1,), orbitals=('d',), spin='both')
    assert r['dos_up'] == pytest.approx([0, 1, 2, 1, 0])
    assert r['dos_down'] == pytest.approx([0, 0, 2, 2, 0])


def test_sum_pdos_all_ions_and_orbital_families():
    d = dosparse.parse_vasprun_pdos(_vasprun_pdos())
    r = dosparse.sum_pdos(d, ions=None, orbitals=('d',), spin='up')
    assert r['dos_up'] == pytest.approx([0, 3, 2, 1, 0])   # ion1+ion2 d↑
    assert r['dos_down'] is None                            # spin='up' 只给上自旋
    sp = dosparse.sum_pdos(d, ions=(1,), orbitals=('s', 'p'), spin='up')
    assert sp['dos_up'] == pytest.approx([0, 0.3, 0.3, 0.3, 0])   # s↑+p↑ 逐点


def test_sum_pdos_bad_inputs():
    d = dosparse.parse_vasprun_pdos(_vasprun_pdos())
    with pytest.raises(ValueError, match='spin'):
        dosparse.sum_pdos(d, spin='sideways')
    with pytest.raises(ValueError, match='无匹配'):
        dosparse.sum_pdos(d, ions=(99,))
    with pytest.raises(ValueError, match='不在投影'):
        dosparse.sum_pdos(d, orbitals=('f',))


# ── band_center(梯形积分,窗口可见)────────────────────────────────────────────

def test_band_center_trapz_hand_computed():
    # ion1 d↑ = [0,1,2,1,0] over E=[-3..1];梯形:∫ρ=4,∫Eρ=-4 → ε_d = -1.0
    e = [-3.0, -2.0, -1.0, 0.0, 1.0]
    d = [0.0, 1.0, 2.0, 1.0, 0.0]
    r = dosparse.band_center(e, d, efermi=0.0)
    assert r['center_eV'] == pytest.approx(-1.0)
    assert r['n_states'] == pytest.approx(4.0)
    assert r['window'] == (-10.0, 2.0)


def test_band_center_window_and_occupied():
    e = [-3.0, -2.0, -1.0, 0.0, 1.0]
    d = [0.0, 1.0, 2.0, 1.0, 0.0]
    # 窗口 (-1.5, 2):只取 E=-1,0,1 → ρ=[2,1,0];∫ρ=2,∫Eρ=-1 → -0.5
    w = dosparse.band_center(e, d, efermi=0.0, window=(-1.5, 2.0))
    assert w['center_eV'] == pytest.approx(-0.5)
    assert w['window'] == (-1.5, 2.0)
    # occupied_only:只取 E<=0 → E=[-3,-2,-1,0],ρ=[0,1,2,1];梯形 ∫ρ=3.5,∫Eρ=-4 → -8/7
    occ = dosparse.band_center(e, d, efermi=0.0, occupied_only=True)
    assert occ['center_eV'] == pytest.approx(-8 / 7)
    assert occ['occupied_only'] is True


def test_band_center_from_parsed_pdos():
    d = dosparse.parse_vasprun_pdos(_vasprun_pdos())
    up = dosparse.sum_pdos(d, ions=(1,), orbitals=('d',), spin='up')
    c = dosparse.band_center(up['energies'], up['dos_up'], efermi=d['efermi'])
    assert c['center_eV'] == pytest.approx(-1.0)       # 全流程手算对拍


def test_band_center_empty_or_zero_none():
    assert dosparse.band_center([], [], efermi=0.0)['center_eV'] is None
    z = dosparse.band_center([-1.0, 0.0], [0.0, 0.0], efermi=0.0)
    assert z['center_eV'] is None
    with pytest.raises(ValueError, match='长度'):
        dosparse.band_center([-1.0], [1.0, 2.0], efermi=0.0)


def test_spin_band_centers():
    e = [-3.0, -2.0, -1.0, 0.0, 1.0]
    up = [0.0, 1.0, 2.0, 1.0, 0.0]      # ε↑ = -1.0
    dn = [0.0, 0.0, 2.0, 2.0, 0.0]      # ∫ρ=4, ∫Eρ=-2 → ε↓ = -0.5
    r = dosparse.spin_band_centers(e, up, dn, efermi=0.0)
    assert r['up']['center_eV'] == pytest.approx(-1.0)
    assert r['down']['center_eV'] == pytest.approx(-0.5)
    r2 = dosparse.spin_band_centers(e, up, None, efermi=0.0)
    assert r2['down'] is None


def test_dp_overlap_hand_computed():
    e = [-3.0, -2.0, -1.0, 0.0, 1.0]
    dd = [0.0, 1.0, 2.0, 1.0, 0.0]      # ion1 d↑
    dp = [0.0, 0.2, 0.1, 0.0, 0.0]      # ion1 p↑;min=[0,.2,.1,0,0] → ∫=0.3
    assert dosparse.dp_overlap(e, dd, dp) == pytest.approx(0.3)
    with pytest.raises(ValueError, match='长度'):
        dosparse.dp_overlap(e, dd, [0.0, 0.1])
