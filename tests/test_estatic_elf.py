"""estatic 'elf' kind 测试:LELF=.TRUE. 产出标志 + NPAR 兼容性 warning + purpose 校验。"""
import pytest

from vcstudio.generate import estatic
from vcstudio.generate.incar_builder import parse_incar

_CONTCAR = """slab Cu
1.0
 3.0 0.0 0.0
 0.0 3.0 0.0
 0.0 0.0 20.0
 Cu
 2
Direct
 0.0 0.0 0.10
 0.5 0.5 0.15
"""
_INCAR = "SYSTEM = relax\nENCUT = 450\nGGA = PE\nIBRION = 2\nNSW = 100\nISIF = 2\nEDIFF = 1E-04\n"
_KP = "Automatic\n0\nGamma\n3 3 1\n0 0 0\n"
_POT = "PAW_PBE Cu\n"


def _make_relax(tmp_path):
    d = tmp_path / 'relax'
    d.mkdir()
    (d / 'CONTCAR').write_text(_CONTCAR, encoding='utf-8')
    (d / 'INCAR').write_text(_INCAR, encoding='utf-8')
    (d / 'KPOINTS').write_text(_KP, encoding='utf-8')
    (d / 'POTCAR').write_text(_POT, encoding='utf-8')
    return d


def test_elf_kind_sets_lelf(tmp_path):
    relax = _make_relax(tmp_path)
    estatic.build_static_job(str(relax), str(tmp_path / 'elf'), purpose='elf')
    inc = parse_incar((tmp_path / 'elf' / 'INCAR').read_text(encoding='utf-8'))
    assert inc['LELF'] is True
    assert inc['NSW'] == 0 and inc['IBRION'] == -1          # 仍是静态单点


def test_elf_kind_npar_warning(tmp_path):
    relax = _make_relax(tmp_path)
    res = estatic.build_static_job(str(relax), str(tmp_path / 'elf'), purpose='elf')
    assert any('NPAR' in w and 'ELF' in w for w in res['warnings'])


def test_elf_in_purposes():
    assert 'elf' in estatic._PURPOSES


def test_other_purposes_no_lelf(tmp_path):
    relax = _make_relax(tmp_path)
    estatic.build_static_job(str(relax), str(tmp_path / 'pdos'), purpose='pdos')
    inc = parse_incar((tmp_path / 'pdos' / 'INCAR').read_text(encoding='utf-8'))
    assert 'LELF' not in inc                                # 只有 elf kind 才加


def test_bad_purpose_still_rejected(tmp_path):
    relax = _make_relax(tmp_path)
    with pytest.raises(ValueError, match='purpose'):
        estatic.build_static_job(str(relax), str(tmp_path / 'x'), purpose='wavefun')
