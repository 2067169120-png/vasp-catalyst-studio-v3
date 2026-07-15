"""C3 Methods 段生成纯函数测试:facts 抽取 + 双语渲染 + BibTeX 按需。"""
from vcstudio.generate import methods_text as mt


_INCAR_RPBE = """\
SYSTEM = Ta_S8
GGA = RP
ENCUT = 400
ISMEAR = 0
SIGMA = 0.05
EDIFF = 1E-5
EDIFFG = -0.03
IBRION = 2
NSW = 500
IVDW = 11
ISPIN = 2
MAGMOM = 48*0.0 1*5.0
"""

_KPOINTS_GAMMA = """\
Automatic mesh
0
Gamma
3 3 1
0 0 0
"""

_POTCAR_HEAD = (
    ' PAW_PBE C 08Apr2002\n blah\n TITEL  = PAW_PBE C 08Apr2002\n more\n'
    ' PAW_PBE Ta_pv 07Sep2000\n TITEL  = PAW_PBE Ta_pv 07Sep2000\n'
    ' TITEL  = PAW_PBE S 06Sep2000\n'
)


def test_parse_potcar_titels():
    ts = mt.parse_potcar_titels(_POTCAR_HEAD)
    assert [t['variant'] for t in ts] == ['C', 'Ta_pv', 'S']
    assert ts[0]['flavor'] == 'PAW_PBE'
    assert ts[1]['element'] == 'Ta'


def test_parse_kpoints_scheme():
    k = mt.parse_kpoints_scheme(_KPOINTS_GAMMA)
    assert k['scheme'] == 'Gamma'
    assert k['grid'] == [3, 3, 1]


def test_extract_facts_rpbe_overrides_potcar_flavor():
    r = mt.extract_facts(_INCAR_RPBE, _KPOINTS_GAMMA, _POTCAR_HEAD)
    f = r['facts']
    assert f['functional'] == 'RPBE'          # GGA=RP 覆盖 PAW_PBE 味
    assert f['potcar_flavor'] == 'PAW_PBE'
    assert f['encut'] == 400
    assert f['ivdw'] == 'DFT-D3(zero)'
    assert f['ispin'] == 2
    assert f['smearing'].startswith('Gaussian')
    assert f['ediffg_force'] == 0.03          # EDIFFG=-0.03 → 力判据 eV/Å
    assert f['relax'] == 'CG'                 # IBRION=2
    assert f['kpoints']['grid'] == [3, 3, 1]
    assert r['warnings'] == []


def test_extract_facts_no_gga_falls_back_to_flavor():
    incar = 'ENCUT = 450\nISMEAR = 0\nSIGMA = 0.05\n'
    r = mt.extract_facts(incar, _KPOINTS_GAMMA, _POTCAR_HEAD)
    assert r['facts']['functional'] == 'PBE'  # PAW_PBE 味缺 GGA → PBE


def test_extract_facts_missing_pieces_degrade():
    r = mt.extract_facts('ENCUT = 400\n', None, None)
    assert r['facts']['kpoints'] is None
    assert r['facts']['potcars'] == []
    assert any('KPOINTS' in w for w in r['warnings'])
    assert any('POTCAR' in w for w in r['warnings'])


def test_render_bilingual_key_sentences():
    r = mt.extract_facts(_INCAR_RPBE, _KPOINTS_GAMMA, _POTCAR_HEAD)
    zh = mt.render_zh(r['facts'])
    en = mt.render_en(r['facts'])
    for text in (zh, en):
        assert 'RPBE' in text
        assert '400' in text
        assert 'D3' in text
        assert '0.03' in text
    assert 'VASP' in en and 'projector augmented-wave' in en.lower()
    assert '平面波' in zh and '赝势' in zh
    assert '3 × 3 × 1' in en or '3x3x1' in en or '3 × 3 × 1' in zh


def test_render_bibtex_only_used_refs():
    r = mt.extract_facts(_INCAR_RPBE, _KPOINTS_GAMMA, _POTCAR_HEAD)
    bib = mt.render_bibtex(r['facts'])
    assert 'Kresse' in bib and 'Hammer' in bib          # VASP + RPBE
    assert 'Grimme' in bib and '154104' in bib          # D3 zero
    assert '1456' not in bib                            # 无 BJ 阻尼不引 JCC 32,1456
    # 无色散时 Grimme 全不引
    r2 = mt.extract_facts('GGA = RP\nENCUT = 400\n', _KPOINTS_GAMMA, _POTCAR_HEAD)
    assert 'Grimme' not in mt.render_bibtex(r2['facts'])


def test_render_handles_none_kpoints():
    r = mt.extract_facts('ENCUT = 400\n', None, None)
    zh = mt.render_zh(r['facts'])
    en = mt.render_en(r['facts'])
    assert zh and en  # 不崩、有产出;缺什么在 warnings 已说
