"""Structure-backed adsorbate identity and grouping tests."""
import pytest

from vcstudio.project import structure_identity as identity


def _poscar(species, counts, *, cell=10):
    natoms = sum(counts)
    return (
        'structure\n1.0\n'
        f'{cell} 0 0\n0 {cell} 0\n0 0 20\n'
        + ' '.join(species) + '\n'
        + ' '.join(str(value) for value in counts) + '\nDirect\n'
        + '0 0 0\n' * natoms
    )


def test_config_minus_clean_identifies_species_and_overrides_bad_name(tmp_path):
    clean = tmp_path / 'clean' / 'POSCAR'
    config = tmp_path / 'wrong_Li2S8' / 'POSCAR'
    clean.parent.mkdir()
    config.parent.mkdir()
    clean.write_text(_poscar(['C'], [24]), encoding='utf-8')
    config.write_text(_poscar(['C', 'Li', 'S'], [24, 2, 6]), encoding='utf-8')

    assignment = identity.classify_against_clean(
        clean, config, name_hint='Li2S8')

    assert assignment['species'] == 'Li2S6'
    assert assignment['composition'] == {'Li': 2, 'S': 6}
    assert assignment['confidence'] == 'exact' and assignment['confirmed'] is True
    assert any('实际为 Li2S6' in warning for warning in assignment['warnings'])


def test_different_cell_never_claims_exact_adsorbate_identity(tmp_path):
    clean = tmp_path / 'clean.vasp'
    config = tmp_path / 'Li2S8.vasp'
    clean.write_text(_poscar(['C'], [24], cell=10), encoding='utf-8')
    config.write_text(_poscar(['C', 'Li', 'S'], [24, 2, 8], cell=11), encoding='utf-8')

    assignment = identity.classify_against_clean(
        clean, config, name_hint='Li2S8')

    assert assignment['species'] == 'Li2S8'
    assert assignment['confidence'] == 'hint' and assignment['confirmed'] is False
    assert any('晶格不同' in warning for warning in assignment['warnings'])


def test_unique_composition_subset_finds_generic_clean_and_groups_configs(tmp_path):
    paths = []
    for name, species, counts in (
            ('000', ['C'], [24]),
            ('001', ['C', 'Li', 'S'], [24, 2, 8]),
            ('002', ['C', 'Li', 'S'], [24, 2, 8])):
        path = tmp_path / name / 'POSCAR'
        path.parent.mkdir()
        path.write_text(_poscar(species, counts), encoding='utf-8')
        paths.append(path)
    items = [
        {'path': str(path), 'name': path.parent.name,
         'structure': identity.poscar_facts(path)}
        for path in paths
    ]

    inferred = identity.infer_clean_candidate(items)
    assignments = [
        {'path': str(path), 'name': path.parent.name, **{
            'species': assignment['species'],
            'species_confidence': assignment['confidence'],
        }}
        for path in paths[1:]
        for assignment in [identity.classify_against_clean(paths[0], path)]
    ]
    groups = identity.species_groups(assignments)

    assert inferred['path'] == str(paths[0]) and inferred['confidence'] == 'exact'
    assert len(groups) == 1
    assert groups[0] == {
        'key': 'Li:2|S:8', 'species': 'Li2S8', 'label': 'Li2S8',
        'composition': {'Li': 2, 'S': 8}, 'count': 2,
        'paths': [str(paths[1]), str(paths[2])], 'names': ['001', '002'],
        'all_exact': True,
    }


def test_two_generic_structures_do_not_auto_assign_clean_slab(tmp_path):
    clean = tmp_path / '000' / 'POSCAR'
    config = tmp_path / '001' / 'POSCAR'
    clean.parent.mkdir()
    config.parent.mkdir()
    clean.write_text(_poscar(['C'], [24]), encoding='utf-8')
    config.write_text(_poscar(['C', 'Li', 'S'], [24, 2, 8]), encoding='utf-8')

    inferred = identity.infer_clean_candidate([
        {'path': str(clean), 'structure': identity.poscar_facts(clean)},
        {'path': str(config), 'structure': identity.poscar_facts(config)},
    ])

    assert inferred['path'] == ''
    assert inferred['confidence'] == 'insufficient'
    assert '至少需要 2 个构型' in inferred['reason']


def test_duplicate_reference_compositions_are_ambiguous_not_auto_confirmed(tmp_path):
    clean = tmp_path / 'clean' / 'POSCAR'
    config = tmp_path / 'ads' / 'POSCAR'
    clean.parent.mkdir()
    config.parent.mkdir()
    clean.write_text(_poscar(['C'], [24]), encoding='utf-8')
    config.write_text(_poscar(['C', 'Li', 'S'], [24, 2, 8]), encoding='utf-8')

    assignment = identity.classify_against_clean(
        clean, config, reference_species=['Li2S8', 'S8Li2'])

    assert assignment['species'] == 'Li2S8'
    assert assignment['reference_matches'] == ['Li2S8', 'S8Li2']
    assert assignment['confidence'] == 'ambiguous'
    assert assignment['confirmed'] is False
    assert 'ambiguous_reference_library' in assignment['source']
    assert any('多个参考标签' in warning for warning in assignment['warnings'])


def test_dataset_groups_merge_formula_aliases_by_exact_composition():
    groups = identity.build_dataset_groups(
        ['/jobs/a', '/jobs/b', '/jobs/c'],
        {'/jobs/a': 'Li2S8', '/jobs/b': 'S8Li2', '/jobs/c': 'Li2S6'},
    )

    assert groups == [
        {
            'group_id': 'composition:Li:2|S:6', 'species': 'Li2S6',
            'configs': ['/jobs/c'], 'n_configs': 1, 'reference_job': None,
        },
        {
            'group_id': 'composition:Li:2|S:8', 'species': 'Li2S8',
            'configs': ['/jobs/a', '/jobs/b'], 'n_configs': 2,
            'reference_job': None,
        },
    ]


def test_dataset_groups_reject_duplicate_reference_compositions():
    with pytest.raises(ValueError, match='具有相同原子组成'):
        identity.build_dataset_groups(
            ['/jobs/config'], {'/jobs/config': 'Li2S8'},
            {'Li2S8': '/refs/a', 'S8Li2': '/refs/b'},
        )
