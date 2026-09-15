"""Role-aware scientific policy for adsorption-energy method plans."""

import pytest

from vcstudio.project.method_policy import compare_plans


def _plan(elements, *, titles=None, **changes):
    defaults = {
        'functional': 'PBE',
        'ivdw': 12,
        'ispin': 2,
        'ldau': False,
        'ldautype': None,
        'ldaul': None,
        'ldauu': None,
        'ldauj': None,
        'encut': 500.0,
        'metagga': 'F',
        'lhfcalc': False,
        'aexx': None,
        'hfscreen': None,
        'potcar_titel': list(titles or [f'PAW_PBE {element}' for element in elements]),
        'element_orders': [list(elements)],
        'kpoints_scheme': {'scheme': 'Gamma', 'grid': [3, 3, 1],
                           'shift': [0.0, 0.0, 0.0]},
    }
    defaults.update(changes)
    return defaults


def _compare(left, right, relation='periodic_delta'):
    return compare_plans(
        left, right, left_label='left', right_label='right', relation=relation)


def test_molecule_ldau_off_is_compatible_with_catalyst_only_fe_u():
    molecule = _plan(['Li', 'S'], ispin=1)
    periodic = _plan(
        ['Fe', 'Li', 'S'],
        ldau=True, ldautype=2,
        ldaul='2 -1 -1', ldauu='4.0 0 0', ldauj='0 0 0')

    result = _compare(molecule, periodic, relation='molecular_reference')

    assert not result['issues']
    assert not any('DFT+U' in warning for warning in result['warnings'])


def test_shared_s_u_conflicts_with_molecular_ldau_off():
    molecule = _plan(['Li', 'S'], ispin=1)
    periodic = _plan(
        ['Fe', 'Li', 'S'],
        ldau=True, ldautype=2,
        ldaul='2 -1 2', ldauu='4.0 0 3.0', ldauj='0 0 0')

    result = _compare(molecule, periodic, relation='molecular_reference')

    assert any('S 的 DFT+U 有效参数不一致' in issue for issue in result['issues'])
    assert not any('Fe' in issue and 'DFT+U' in issue for issue in result['issues'])


@pytest.mark.parametrize('overrides', [
    {'ldautype': 1},
    {'ldaul': '-1 1'},
    {'ldauu': '0 4.0'},
    {'ldauj': '0 0.5'},
])
def test_every_shared_effective_u_component_is_compared(overrides):
    baseline = _plan(
        ['Fe', 'S'], ldau=True, ldautype=2,
        ldaul='-1 2', ldauu='0 3.0', ldauj='0 0')
    changed_values = {
        'ldau': True, 'ldautype': 2,
        'ldaul': '-1 2', 'ldauu': '0 3.0', 'ldauj': '0 0',
        **overrides,
    }
    changed = _plan(['Fe', 'S'], **changed_values)

    result = _compare(baseline, changed)

    assert any('S 的 DFT+U 有效参数不一致' in issue for issue in result['issues'])


def test_u_vectors_are_compared_by_element_not_raw_order():
    left = _plan(
        ['Fe', 'S'], titles=['PAW_PBE Fe', 'PAW_PBE S'],
        ldau=True, ldautype=2,
        ldaul='-1 2', ldauu='0 3.0', ldauj='0 0')
    right = _plan(
        ['S', 'Fe'], titles=['PAW_PBE S', 'PAW_PBE Fe'],
        ldau=True, ldautype=2,
        ldaul='2 -1', ldauu='3.0 0', ldauj='0 0')

    result = _compare(left, right)

    assert result['issues'] == []
    assert not any('DFT+U' in warning for warning in result['warnings'])


@pytest.mark.parametrize('relation, expected', [
    ('periodic_delta', '吸附诱导磁性'),
    ('molecular_reference', '独立体系'),
])
def test_legal_spin_difference_is_note_only(relation, expected):
    left = _plan(['Fe'], ispin=1)
    right = _plan(['Fe'], ispin=2)

    result = _compare(left, right, relation=relation)

    assert not result['issues']
    assert not any('ISPIN' in warning for warning in result['warnings'])
    assert any('ISPIN=1' in note and expected in note for note in result['notes'])


def test_runtime_controls_and_magmom_are_not_compared():
    left = _plan(['Fe'], nsw=40, ibrion=2, magmom='1*4', ismear=1, sigma=0.2)
    right = _plan(['Fe'], nsw=200, ibrion=1, magmom='1*-4', ismear=0, sigma=0.05)

    result = _compare(left, right)

    assert result['issues'] == []
    assert result['warnings'] == []
    assert not any(key in ' '.join(result['notes']) for key in ('NSW', 'IBRION', 'MAGMOM'))


def test_periodic_kpoints_mismatch_blocks_but_molecular_relation_ignores_it():
    left = _plan(['Fe'])
    right = _plan(
        ['Fe'], kpoints_scheme={'scheme': 'Gamma', 'grid': [5, 5, 1],
                                'shift': [0.0, 0.0, 0.0]})

    periodic = _compare(left, right, relation='periodic_delta')
    molecular = _compare(left, right, relation='molecular_reference')

    assert any('KPOINTS 不一致' in issue for issue in periodic['issues'])
    assert not any('KPOINTS' in issue for issue in molecular['issues'])


def test_explicit_kpoints_digest_is_compared_for_periodic_energy():
    left = _plan(
        ['Fe'], kpoints_scheme={'scheme': 'explicit', 'grid': None,
                                'raw': '2 Recip ...', 'raw_sha256': 'a' * 64})
    right = _plan(
        ['Fe'], kpoints_scheme={'scheme': 'explicit', 'grid': None,
                                'raw': '2 Recip ...', 'raw_sha256': 'b' * 64})

    result = _compare(left, right)

    assert any('KPOINTS 不一致' in issue for issue in result['issues'])


def test_omitted_metagga_equals_explicit_disabled_default():
    omitted = _plan(['Fe'], metagga=None)
    explicit = _plan(['Fe'], metagga='FALSE')

    result = _compare(omitted, explicit)

    assert not any('METAGGA' in issue for issue in result['issues'])
    assert not any('METAGGA' in warning for warning in result['warnings'])


def test_potcar_compares_shared_elements_only():
    clean = _plan(['Fe'], titles=['PAW_PBE Fe 06Sep2000'])
    ads_ok = _plan(
        ['Fe', 'S'], titles=['PAW_PBE Fe 06Sep2000', 'PAW_PBE S 06Sep2000'])
    ads_bad = _plan(
        ['Fe', 'S'], titles=['PAW_PBE Fe_pv 02Aug2007', 'PAW_PBE S 06Sep2000'])

    ok = _compare(clean, ads_ok)
    bad = _compare(clean, ads_bad)

    assert not ok['issues']
    assert any('Fe 的 POTCAR TITEL 不一致' in issue for issue in bad['issues'])
    assert not any('S 的 POTCAR' in issue for issue in bad['issues'])


def test_missing_evidence_warns_without_inventing_conflict():
    left = _plan(['Fe'])
    right = _plan(
        ['Fe'], functional=None, potcar_titel=[], element_orders=[],
        ldau=True, ldautype=2, ldaul='not-a-vector', ldauu=None, ldauj=None)

    result = _compare(left, right)

    assert not any('泛函' in issue for issue in result['issues'])
    assert any('泛函 证据不完整' in warning for warning in result['warnings'])
    assert any('POTCAR' in warning for warning in result['warnings'])
    assert any('DFT+U' in warning for warning in result['warnings'])


def test_known_electronic_method_differences_are_issues():
    left = _plan(['Fe'], functional='PBE', ivdw=12, encut=500,
                 metagga='F', lhfcalc=True, aexx=0.25, hfscreen=0.2)
    right = _plan(['Fe'], functional='RPBE', ivdw=11, encut=450,
                  metagga='R2SCAN', lhfcalc=True, aexx=0.30, hfscreen=0.3)

    result = _compare(left, right)
    text = '\n'.join(result['issues'])

    for expected in ('泛函', 'IVDW', 'ENCUT', 'METAGGA', 'AEXX', 'HFSCREEN'):
        assert expected in text


def test_no_shared_elements_is_not_a_hard_conflict():
    left = _plan(['Fe'])
    right = _plan(['Li', 'S'])

    result = _compare(left, right, relation='molecular_reference')

    assert result['issues'] == []
    assert any('没有共享元素' in note for note in result['notes'])


def test_incomplete_shared_u_mapping_is_warning():
    left = _plan(['Fe', 'S'])
    right = _plan(
        ['Fe', 'S'], ldau=True, ldautype=2,
        ldaul='2 2', ldauu='4.0', ldauj='0 0')

    result = _compare(left, right)

    assert not any('DFT+U' in issue for issue in result['issues'])
    assert any('DFT+U 有效参数映射不完整' in warning
               for warning in result['warnings'])


def test_omitted_ldau_vectors_use_vasp_effective_defaults():
    globally_off = _plan(['Fe', 'S'], ldau=False)
    defaults = _plan(
        ['Fe', 'S'], ldau=True, ldautype=None,
        ldaul=None, ldauu=None, ldauj=None)

    result = _compare(globally_off, defaults)

    assert not any('DFT+U' in issue for issue in result['issues'])
    assert not any('DFT+U' in warning for warning in result['warnings'])


def test_dudarev_compares_u_minus_j_not_raw_pair():
    left = _plan(
        ['Fe'], ldau=True, ldautype=2,
        ldaul='2', ldauu='4', ldauj='1')
    right = _plan(
        ['Fe'], ldau=True, ldautype=2,
        ldaul='2', ldauu='3', ldauj='0')

    result = _compare(left, right)

    assert not any('DFT+U' in issue for issue in result['issues'])
    assert not any('DFT+U' in warning for warning in result['warnings'])


def test_invalid_relation_is_rejected():
    with pytest.raises(ValueError, match='relation'):
        _compare(_plan(['Fe']), _plan(['Fe']), relation='unknown')
