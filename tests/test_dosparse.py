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
