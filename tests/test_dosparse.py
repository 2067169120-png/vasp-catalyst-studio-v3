"""C4 vasprun.xml DOS 流式解析测试。"""
import io

import pytest

from vcstudio.project import dosparse


def _vasprun(spin2=False, with_dos=True):
    spin1 = ('<set comment="spin 1">\n'
             '<r>   -10.0000    0.0000    0.0000 </r>\n'
             '<r>    -5.0000    1.2500    0.5000 </r>\n'
             '<r>     0.0000    3.7500    2.0000 </r>\n'
             '</set>\n')
    s2 = ('<set comment="spin 2">\n'
          '<r>   -10.0000    0.0000    0.0000 </r>\n'
          '<r>    -5.0000    1.1000    0.4000 </r>\n'
          '<r>     0.0000    3.2000    1.8000 </r>\n'
          '</set>\n') if spin2 else ''
    dos = ('<dos>\n<i name="efermi">   -2.0340 </i>\n'
           '<total><array><dimension dim="1">gridpoints</dimension>\n'
           f'<set>\n{spin1}{s2}</set>\n</array></total>\n</dos>\n') if with_dos else ''
    return (f'<?xml version="1.0" encoding="ISO-8859-1"?>\n<modeling>\n'
            f'<calculation>\n{dos}<projected><big>ignored</big></projected>\n'
            f'</calculation>\n</modeling>\n')


def test_parse_single_spin():
    d = dosparse.parse_vasprun_dos(io.StringIO(_vasprun()))
    assert d['efermi'] == pytest.approx(-2.034)
    assert d['energies'] == pytest.approx([-10.0, -5.0, 0.0])
    assert d['spin_up'] == pytest.approx([0.0, 1.25, 3.75])
    assert d['spin_down'] is None


def test_parse_spin_polarized():
    d = dosparse.parse_vasprun_dos(io.StringIO(_vasprun(spin2=True)))
    assert d['spin_down'] == pytest.approx([0.0, 1.10, 3.20])
    assert len(d['energies']) == 3


def test_parse_no_dos_section_raises():
    with pytest.raises(ValueError):
        dosparse.parse_vasprun_dos(io.StringIO(_vasprun(with_dos=False)))


def test_parse_truncated_xml_raises_valueerror():
    text = _vasprun()
    with pytest.raises(ValueError):
        dosparse.parse_vasprun_dos(io.StringIO(text[:len(text) // 3]))


# ── 投影 DOS(<partial>,LORBIT=11)──────────────────────────────────────────
# ion 1 spin 1:d 五列和 = [1.0, 2.0, 1.0, 0.0];s = [0.1, 0.3, 0.5, 0.7];
#              p 三列和 = [0.0, 0.4, 0.0, 0.0]
# ion 2 spin 1:d 五列和 = [0.5, 0.5, 0.0, 0.0]
_ION1_ROWS = ('<r> -2.0 0.10 0.00 0.00 0.00 0.20 0.20 0.20 0.20 0.20 </r>\n'
              '<r> -1.0 0.30 0.10 0.10 0.20 0.40 0.40 0.40 0.40 0.40 </r>\n'
              '<r>  0.0 0.50 0.00 0.00 0.00 0.20 0.20 0.20 0.20 0.20 </r>\n'
              '<r>  1.0 0.70 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 </r>\n')
_ION2_ROWS = ('<r> -2.0 0.00 0.00 0.00 0.00 0.10 0.10 0.10 0.10 0.10 </r>\n'
              '<r> -1.0 0.00 0.00 0.00 0.00 0.10 0.10 0.10 0.10 0.10 </r>\n'
              '<r>  0.0 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 </r>\n'
              '<r>  1.0 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 </r>\n')


def _vasprun_partial(spin2=False):
    def ion(rows):
        s = f'<set comment="spin 1">\n{rows}</set>\n'
        if spin2:
            s += f'<set comment="spin 2">\n{rows}</set>\n'
        return s
    fields = ('<field>energy</field><field>s</field><field>py</field><field>pz</field>'
              '<field>px</field><field>dxy</field><field>dyz</field><field>dz2</field>'
              '<field>dxz</field><field>x2-y2</field>\n')
    return ('<?xml version="1.0" encoding="ISO-8859-1"?>\n<modeling>\n<calculation>\n'
            '<dos>\n<i name="efermi">   0.0000 </i>\n'
            '<total><array><set><set comment="spin 1">\n'
            '<r> -2.0 9.9 0.0 </r>\n</set></set></array></total>\n'
            f'<partial>\n<array>\n{fields}<set>\n'
            f'<set comment="ion 1">\n{ion(_ION1_ROWS)}</set>\n'
            f'<set comment="ion 2">\n{ion(_ION2_ROWS)}</set>\n'
            '</set>\n</array>\n</partial>\n</dos>\n'
            '<projected><big>ignored</big></projected>\n'
            '</calculation>\n</modeling>\n')


def test_parse_partial_d_all_ions():
    d = dosparse.parse_vasprun_partial(io.StringIO(_vasprun_partial()))
    assert d['efermi'] == pytest.approx(0.0)
    assert d['energies'] == pytest.approx([-2.0, -1.0, 0.0, 1.0])
    assert d['dos'] == pytest.approx([1.5, 2.5, 1.0, 0.0])       # ion1+ion2 的 d 和
    # <total> 的行(9.9)绝不能混进投影
    assert all(abs(v - 9.9) > 1e-9 for v in d['dos'])


def test_parse_partial_ion_filter_and_orbitals():
    src = _vasprun_partial()
    d1 = dosparse.parse_vasprun_partial(io.StringIO(src), ions=(1,))
    assert d1['dos'] == pytest.approx([1.0, 2.0, 1.0, 0.0])      # 只算 ion 1 的 d
    s1 = dosparse.parse_vasprun_partial(io.StringIO(src), ions=(1,), orbitals=('s',))
    assert s1['dos'] == pytest.approx([0.1, 0.3, 0.5, 0.7])
    p1 = dosparse.parse_vasprun_partial(io.StringIO(src), ions=(1,), orbitals=('p',))
    assert p1['dos'] == pytest.approx([0.0, 0.4, 0.0, 0.0])
    sp = dosparse.parse_vasprun_partial(io.StringIO(src), ions=(1,), orbitals=('s', 'd'))
    assert sp['dos'] == pytest.approx([1.1, 2.3, 1.5, 0.7])      # s+d 逐点相加


def test_parse_partial_sums_both_spins():
    d = dosparse.parse_vasprun_partial(io.StringIO(_vasprun_partial(spin2=True)),
                                       ions=(1,))
    assert d['dos'] == pytest.approx([2.0, 4.0, 2.0, 0.0])       # 两自旋等值 → 翻倍


def test_parse_partial_missing_section_signals_no_projection():
    # LORBIT<10:vasprun 只有 <total> 无 <partial> → 明确"无投影数据"信号
    with pytest.raises(ValueError, match='无投影'):
        dosparse.parse_vasprun_partial(io.StringIO(_vasprun()))


def test_parse_partial_ion_out_of_range_named():
    with pytest.raises(ValueError, match='ion 1..2'):
        dosparse.parse_vasprun_partial(io.StringIO(_vasprun_partial()), ions=(99,))


def test_parse_partial_unknown_orbital_raises():
    with pytest.raises(ValueError, match='未知轨道'):
        dosparse.parse_vasprun_partial(io.StringIO(_vasprun_partial()), orbitals=('g',))


def test_parse_partial_lorbit10_layout():
    # LORBIT=10 汇总布局(E s p d):d 取第 3 列
    xml = ('<modeling><calculation><dos><i name="efermi"> 0.0 </i>\n'
           '<partial><array><set><set comment="ion 1"><set comment="spin 1">\n'
           '<r> -1.0 0.1 0.2 0.7 </r>\n<r> 0.0 0.1 0.2 0.3 </r>\n'
           '</set></set></set></array></partial></dos></calculation></modeling>')
    d = dosparse.parse_vasprun_partial(io.StringIO(xml))
    assert d['dos'] == pytest.approx([0.7, 0.3])
    # f 轨道在 4 列布局里不存在 → 显式报错,绝不取错列
    with pytest.raises(ValueError, match='列'):
        dosparse.parse_vasprun_partial(io.StringIO(xml), orbitals=('f',))


# ── d 带中心 ─────────────────────────────────────────────────────────────────
def test_d_band_center_hand_computed():
    # 手算:Σ(E−EF)ρ = (−2)(1)+(−1)(2)+0(1)+1(0) = −4;Σρ = 4 → ε_d = −1.0
    es, ds = [-2.0, -1.0, 0.0, 1.0], [1.0, 2.0, 1.0, 0.0]
    assert dosparse.d_band_center(es, ds, efermi=0.0) == pytest.approx(-1.0)
    # E_F 平移 −1 → 相对中心跟着平移:ε_d = −1 − (−1) = 0
    assert dosparse.d_band_center(es, ds, efermi=-1.0) == pytest.approx(0.0)
    # 二阶矩:m2 = (4·1+1·2)/4 = 1.5;w = sqrt(1.5 − 1²) = sqrt(0.5)
    c, w = dosparse.d_band_center(es, ds, efermi=0.0, return_width=True)
    assert c == pytest.approx(-1.0)
    assert w == pytest.approx(0.5 ** 0.5)


def test_d_band_center_from_parsed_partial():
    d = dosparse.parse_vasprun_partial(io.StringIO(_vasprun_partial()), ions=(1,))
    c = dosparse.d_band_center(d['energies'], d['dos'], d['efermi'])
    assert c == pytest.approx(-1.0)                   # 同一手算数据走全流程


def test_d_band_center_empty_or_zero_returns_none():
    assert dosparse.d_band_center([], [], efermi=0.0) is None
    assert dosparse.d_band_center([-1.0, 0.0], [0.0, 0.0], efermi=0.0) is None
    with pytest.raises(ValueError, match='长度'):
        dosparse.d_band_center([-1.0], [1.0, 2.0], efermi=0.0)
