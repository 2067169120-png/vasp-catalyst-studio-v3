"""campaign.fingerprint 测试:抽取/稳定哈希/一致性比对。"""
from vcstudio.campaign import fingerprint as fp


INCAR = ('GGA = PE\nENCUT = 500\nISPIN = 2\nIVDW = 12\n'
         'EDIFF = 1E-5 ; EDIFFG = -0.02   # 力判据\n'
         'LDAU = .TRUE.\nLDAUU = 3.0 0.0\n')
KPOINTS = 'auto mesh\n0\nGamma\n3 3 1\n'
POTCAR = [{'element': 'C', 'titel': 'PAW_PBE C 08Apr2002'},
          {'element': 'O', 'titel': 'PAW_PBE O 08Apr2002'}]


def test_extract_maps_all_fields():
    f = fp.extract_from_inputs(INCAR, KPOINTS, POTCAR, reference_convention='gas-phase O2')
    assert f['functional'] == 'PBE'
    assert f['dispersion'] == 'DFT-D3(BJ)'
    assert f['encut'] == 500.0
    assert f['kpoints_scheme'] == 'Gamma 3x3x1'
    assert f['spin'] == 2
    assert f['ediff'] == 1e-5
    assert f['ediffg'] == -0.02
    assert f['u_values']['LDAU'] is True
    assert f['reference_convention'] == 'gas-phase O2'
    assert set(f) == set(fp.FINGERPRINT_FIELDS)


def test_potcar_ids_are_titel_hashes():
    f = fp.extract_from_inputs('ENCUT=400\n', KPOINTS, POTCAR)
    assert set(f['potcar_ids']) == {'C', 'O'}
    assert len(f['potcar_ids']['C']) == 12
    assert f['potcar_ids']['C'] != f['potcar_ids']['O']


def test_hash_is_stable_and_order_independent():
    f = fp.extract_from_inputs(INCAR, KPOINTS, POTCAR)
    h1 = fp.fingerprint_hash(f)
    h2 = fp.fingerprint_hash(dict(reversed(list(f.items()))))
    assert h1 == h2
    assert len(h1) == 12


def test_hash_numeric_normalization():
    """400 与 400.0、1e-4 与 0.0001 视为同一指纹。"""
    a = fp.new_fingerprint(encut=400, ediff=1e-4)
    b = fp.new_fingerprint(encut=400.0, ediff=0.0001)
    assert fp.fingerprint_hash(a) == fp.fingerprint_hash(b)


def test_hash_differs_on_encut():
    a = fp.new_fingerprint(functional='PBE', encut=500.0)
    b = fp.new_fingerprint(functional='PBE', encut=520.0)
    assert fp.fingerprint_hash(a) != fp.fingerprint_hash(b)


def test_group_consistent():
    f = fp.extract_from_inputs(INCAR, KPOINTS, POTCAR)
    res = fp.check_group_consistency([f, dict(f), dict(f)])
    assert res['consistent'] is True
    assert res['diffs'] == []


def test_group_inconsistent_names_field_and_who():
    a = fp.new_fingerprint(functional='PBE', encut=500.0)
    b = fp.new_fingerprint(functional='PBE', encut=450.0)
    res = fp.check_group_consistency([a, b], labels=['slab', 'ads'])
    assert res['consistent'] is False
    fields = [d['field'] for d in res['diffs']]
    assert 'encut' in fields
    diff = next(d for d in res['diffs'] if d['field'] == 'encut')
    assert diff['values'] == {'slab': 500.0, 'ads': 450.0}
    assert 'ENCUT' in diff['detail'] and 'slab' in diff['detail']


def test_group_single_is_trivially_consistent():
    assert fp.check_group_consistency([fp.new_fingerprint(encut=500.0)])['consistent'] is True
    assert fp.check_group_consistency([])['consistent'] is True


def test_kpoints_schemes_variants():
    assert fp._kpoints_scheme('c\n0\nMonkhorst\n5 5 5\n') == 'Monkhorst 5x5x5'
    explicit = fp._kpoints_scheme('c\n4\nCartesian\n0 0 0 1\n')
    assert explicit.startswith('explicit:4:sha256=')
    assert fp._kpoints_scheme('', {'KSPACING': '0.25'}) == 'KSPACING 0.25'
    assert fp._kpoints_scheme('') is None


def test_kpoints_fingerprint_tracks_shift_and_explicit_coordinates_not_comment():
    gamma = 'first comment\n0\nGamma\n3 3 1\n0 0 0\n'
    shifted = 'first comment\n0\nGamma\n3 3 1\n0.5 0 0\n'
    explicit_a = 'comment A\n2\nReciprocal\n0 0 0 1\n0.5 0 0 1\n'
    explicit_b = 'comment B\n2\nReciprocal\n0 0 0 1\n0.25 0 0 1\n'
    explicit_same = explicit_a.replace('comment A', 'irrelevant comment')

    assert fp._kpoints_scheme(gamma) != fp._kpoints_scheme(shifted)
    assert fp._kpoints_scheme(explicit_a) != fp._kpoints_scheme(explicit_b)
    assert fp._kpoints_scheme(explicit_a) == fp._kpoints_scheme(explicit_same)


def test_explicit_kpoints_fingerprint_ignores_equivalent_numeric_formatting():
    first = 'comment\n2\nReciprocal\n0 0 0 1\n0.5 -0.0 0 1\n'
    same = ('different comment\n2\nreciprocal\n0.000 0 0.0 1.0\n'
            '0.500000 0 0.000 1\n')

    assert fp._kpoints_scheme(first) == fp._kpoints_scheme(same)


def test_automatic_kpoints_identity_ignores_equivalent_formatting_and_comments():
    assert fp._kpoints_scheme('c\n0\nAuto\n20\n') == \
        fp._kpoints_scheme('other\n0\nauto\n20.0 ! length\n')
    assert fp._kpoints_scheme('c\n0\nGamma\n03 03 01\n-0 0.0 0\n') == \
        fp._kpoints_scheme('other\n0\ngamma\n3 3 1\n0 0 0\n')


def test_dispersion_and_functional_variants():
    assert fp.extract_from_inputs('GGA=RP\n', '', [])['functional'] == 'RPBE'
    assert fp.extract_from_inputs('METAGGA=R2SCAN\n', '', [])['functional'] == 'metagga:R2SCAN'
    assert fp.extract_from_inputs('GGA=PE\nMETAGGA=F\n', '', [])['functional'] == 'PBE'
    assert fp.extract_from_inputs(
        'GGA=PE\nLHFCALC=T\n', '', []
    )['functional'] == 'hybrid:base=PBE;metagga=F;AEXX=0.25;HFSCREEN=0'
    assert fp.extract_from_inputs(
        'GGA=RP\nLHFCALC=T\nAEXX=0.30\nHFSCREEN=0.2\n', '', []
    )['functional'] == 'hybrid:base=RPBE;metagga=F;AEXX=0.3;HFSCREEN=0.2'
    assert fp.canonical_functional(base='hse06') == \
        'hybrid:base=PBE;metagga=F;AEXX=0.25;HFSCREEN=0.2'
    assert fp.canonical_functional(base='HSE06', lhfcalc=False) is None
    assert fp.canonical_functional(base='pbe') == 'PBE'
    assert fp.extract_from_inputs(
        'LHFCALC=T\nAEXX=not-a-number\n', '',
        [{'element': 'C', 'titel': 'PAW_PBE C 08Apr2002'}]
    )['functional'] is None
    assert fp.extract_from_inputs('IVDW=11\n', '', [])['dispersion'] == 'DFT-D3(zero)'
    assert fp.extract_from_inputs('ENCUT=400\n', '', [])['dispersion'] is None
    # ISPIN 缺省为 1
    assert fp.extract_from_inputs('ENCUT=400\n', '', [])['spin'] == 1
