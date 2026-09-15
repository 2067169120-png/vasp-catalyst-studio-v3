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


def test_explicit_kpoints_identity_covers_all_effective_lines_but_not_comment():
    first = mt.parse_kpoints_scheme(
        'comment one\n2\nReciprocal\n0 0 0 1\n0.5 0 0 1\n')
    same = mt.parse_kpoints_scheme(
        'different comment\n2\nReciprocal\n0 0 0 1\n0.5 0 0 1\n')
    changed = mt.parse_kpoints_scheme(
        'comment one\n2\nReciprocal\n0 0 0 1\n0.25 0 0 1\n')

    assert first['raw_sha256'] == same['raw_sha256']
    assert first['raw_sha256'] != changed['raw_sha256']


def test_explicit_kpoints_identity_normalises_case_numbers_and_signed_zero():
    first = mt.parse_kpoints_scheme(
        'comment\n2\nReciprocal\n0 0 0 1\n0.5 -0 0 1\n')
    same = mt.parse_kpoints_scheme(
        'other\n2\nreciprocal\n0.0 0.000 0 1.0\n0.500000 0 0.0 1\n')

    assert first['raw'] == same['raw']
    assert first['raw_sha256'] == same['raw_sha256']


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


def test_extract_facts_hse06_is_not_mislabelled_as_pbe():
    incar = (
        'GGA = PE\nENCUT = 500\nLHFCALC = .TRUE.\n'
        'AEXX = 0.25\nHFSCREEN = 0.2\nLASPH = .TRUE.\n'
    )
    result = mt.extract_facts(incar, _KPOINTS_GAMMA, _POTCAR_HEAD)
    facts = result['facts']
    assert facts['functional'] == 'HSE06'
    assert facts['functional_class'] == 'screened hybrid'
    assert facts['base_functional'] == 'PBE'
    assert facts['lhfcalc'] is True
    assert facts['aexx'] == 0.25 and facts['hfscreen'] == 0.2
    assert 'HSE06' in mt.render_zh(facts)
    assert 'HSE06' in mt.render_en(facts)
    assert 'Heyd2003' in mt.render_bibtex(facts)


def test_extract_facts_r2scan_is_not_mislabelled_as_pbe():
    incar = 'METAGGA = R2SCAN\nLASPH = .TRUE.\nENCUT = 500\n'
    result = mt.extract_facts(incar, _KPOINTS_GAMMA, _POTCAR_HEAD)
    facts = result['facts']
    assert facts['functional'] == 'r2SCAN'
    assert facts['functional_class'] == 'meta-GGA'
    assert facts['metagga'] == 'R2SCAN'
    assert facts['functional_known'] is True
    assert 'meta-GGA r2SCAN' in mt.render_zh(facts)
    assert 'meta-GGA r2SCAN' in mt.render_en(facts)
    assert 'Furness2020' in mt.render_bibtex(facts)


def test_malformed_hybrid_settings_are_not_reported_as_verified_pbe():
    result = mt.extract_facts(
        'GGA = PE\nLHFCALC = .TRUE.\nAEXX = not-a-number\nHFSCREEN = 0.2\n',
        _KPOINTS_GAMMA, _POTCAR_HEAD)
    assert result['facts']['functional_known'] is False
    assert result['facts']['lhfcalc'] is True
    assert any('AEXX' in warning for warning in result['warnings'])


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


# ── 新增泛函/色散 BibTeX 条目按需接线 ──────────────────────────────────────────
import pytest  # noqa: E402


@pytest.mark.parametrize('gga,func,cite,uncited', [
    ('91', 'PW91', 'Perdew1992', 'Perdew1996'),      # PW91 独立引,不引 PBE
    ('PS', 'PBEsol', 'Perdew2008', 'Hammer1999'),    # PBEsol 引 PBE+Perdew2008
    ('RE', 'revPBE', 'Zhang1998', 'Hammer1999'),     # revPBE 引 PBE+Zhang1998
    ('AM', 'AM05', 'Armiento2005', 'Perdew1996'),    # AM05 独立引
])
def test_functional_bibtex_wiring(gga, func, cite, uncited):
    r = mt.extract_facts(f'GGA = {gga}\nENCUT = 400\n', _KPOINTS_GAMMA, _POTCAR_HEAD)
    assert r['facts']['functional'] == func
    bib = mt.render_bibtex(r['facts'])
    assert cite in bib
    assert uncited not in bib
    # PBEsol/revPBE 作为 PBE 修订,惯例连引 PBE 原文
    if func in ('PBEsol', 'revPBE'):
        assert 'Perdew1996' in bib


@pytest.mark.parametrize('ivdw,cite,uncited', [
    ('1', 'Grimme2006', 'Grimme2010'),      # D2 → Grimme2006,不引 D3
    ('2', 'Tkatchenko2009', 'Grimme'),      # TS → Tkatchenko-Scheffler,无 Grimme
    ('21', 'Tkatchenko2009', 'Grimme'),     # TS+SCS 亦引 TS 原文
])
def test_dispersion_bibtex_wiring(ivdw, cite, uncited):
    r = mt.extract_facts(f'GGA = PE\nENCUT = 400\nIVDW = {ivdw}\n',
                         _KPOINTS_GAMMA, _POTCAR_HEAD)
    bib = mt.render_bibtex(r['facts'])
    assert cite in bib and uncited not in bib


_INCAR_LDAU = """\
GGA = PE
ENCUT = 500
ISMEAR = 0
SIGMA = 0.05
LDAU = .TRUE.
LDAUL = -1 -1 2 -1
LDAUU = 0 0 3.9 0
LDAUJ = 0 0 0 0
"""


def test_ldau_sentence_carries_u_value_and_cites_dudarev():
    r = mt.extract_facts(_INCAR_LDAU, _KPOINTS_GAMMA, _POTCAR_HEAD)
    f = r['facts']
    assert f['ldau'] is True and f['ldauu'] == '0 0 3.9 0'
    zh, en = mt.render_zh(f), mt.render_en(f)
    for text in (zh, en):
        assert 'DFT+U' in text
        assert '0 0 3.9 0' in text        # Methods 文本明示 U 值
        assert 'Dudarev' in text
    bib = mt.render_bibtex(f)
    assert 'Dudarev1998' in bib and '1505' in bib


def test_ldau_off_no_u_sentence_no_dudarev():
    r = mt.extract_facts('GGA = PE\nENCUT = 500\n', _KPOINTS_GAMMA, _POTCAR_HEAD)
    assert r['facts']['ldau'] is False and r['facts']['ldauu'] is None
    assert 'Dudarev' not in mt.render_bibtex(r['facts'])
    assert 'DFT+U' not in mt.render_zh(r['facts'])
