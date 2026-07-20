"""Local VASP result import: discovery, scientific gates, and atomic commit."""
from __future__ import annotations

from pathlib import Path

import pytest

from vcstudio.cluster import ledger
from vcstudio.project import adsorption, freeenergy, result_import
from vcstudio.shared import manifest


IONIC_MARK = 'reached required accuracy'
ELECTRONIC_MARK = 'aborting loop because EDIFF is reached'


def _poscar(label='C slab'):
    return (
        f'{label}\n1.0\n10 0 0\n0 10 0\n0 0 15\nC\n1\n'
        'Direct\n0 0 0\n'
    )


def _quartet(path: Path, *, task='static') -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if task == 'relax':
        incar = 'ENCUT = 400\nNSW = 100\nIBRION = 2\nNELM = 60\n'
    elif task == 'freq':
        incar = 'ENCUT = 400\nNSW = 1\nIBRION = 5\nNELM = 60\n'
    else:
        incar = 'ENCUT = 400\nNSW = 0\nIBRION = -1\nNELM = 60\n'
    (path / 'INCAR').write_text(incar, encoding='utf-8')
    (path / 'POTCAR').write_text(
        'TITEL  = PAW_PBE C 08Apr2002\nENMAX = 273.214 eV\n', encoding='utf-8')
    (path / 'KPOINTS').write_text(
        'mesh\n0\nGamma\n1 1 1\n0 0 0\n', encoding='utf-8')
    (path / 'POSCAR').write_text(_poscar(path.name), encoding='utf-8')
    return path


def _result(path: Path, energy: float, *, marker: str | None,
            task='static', quartet=True, oszicar_extra='') -> Path:
    if quartet:
        _quartet(path, task=task)
    else:
        path.mkdir(parents=True, exist_ok=True)
        incar = 'NSW = 100\nIBRION = 2\n' if task == 'relax' else 'NSW = 0\n'
        (path / 'INCAR').write_text(incar, encoding='utf-8')
    (path / 'OSZICAR').write_text(
        oszicar_extra + f'  1 F= {energy: .8E} E0= {energy: .8E} d E = 0.0\n',
        encoding='utf-8')
    outcar = 'free  energy   TOTEN  = % .8f eV\n' % energy
    if marker:
        outcar += marker + '\n'
    outcar += 'General timing and accounting informations for this job\n'
    (path / 'OUTCAR').write_text(outcar, encoding='utf-8')
    return path


def _by_rel(scan):
    return {item['relative_path']: item for item in scan['preview']}


def test_scan_root_finds_only_leaves_and_keeps_reference_roles_conservative(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'slab_clean')
    _quartet(root / 'ads' / 'site_Li2S4')
    _result(root / 'Li2S6', -120.0, marker=ELECTRONIC_MARK, quartet=False)
    _result(root / 'refs' / 'molecules' / 'mol_S8', -80.0,
            marker=ELECTRONIC_MARK, quartet=False)

    # A VASP-looking group directory with a deeper calculation must not be
    # returned alongside that leaf.
    container = root / 'container'
    container.mkdir(parents=True)
    (container / 'POSCAR').write_text(_poscar('container'), encoding='utf-8')
    _quartet(container / 'real_job')

    scan = result_import.scan_root(root)
    assert scan['ok']
    found = _by_rel(scan)
    assert set(found) == {
        'Li2S6', 'ads/site_Li2S4', 'container/real_job',
        'refs/molecules/mol_S8', 'slab_clean',
    }
    assert found['slab_clean']['role_suggestion'] == 'clean'
    assert found['ads/site_Li2S4']['role_suggestion'] == 'config'
    assert found['ads/site_Li2S4']['species_suggestion'] == 'Li2S4'
    assert found['refs/molecules/mol_S8']['role_suggestion'] == 'molecule'
    assert found['refs/molecules/mol_S8']['species_suggestion'] == 'S8'

    # A Li-S formula alone is not enough evidence that the directory is a
    # molecular reference; the user must map this ambiguous result.
    assert found['Li2S6']['species_suggestion'] == 'Li2S6'
    assert found['Li2S6']['role_suggestion'] is None
    assert 'Li2S6' in scan['role_suggestions']['ambiguous']


def test_scan_keeps_strong_root_clean_beside_child_config(tmp_path):
    root = tmp_path / 'source'
    _quartet(root)
    _quartet(root / 'ads_top')

    found = _by_rel(result_import.scan_root(root))
    assert set(found) == {'.', 'ads_top'}
    assert found['.']['input_complete'] is True
    assert found['ads_top']['role_suggestion'] == 'config'


def test_role_hints_do_not_inherit_clean_or_species_from_root_name(tmp_path):
    clean_named_root = tmp_path / 'clean_project'
    _quartet(clean_named_root / 'ads_top')
    candidate = result_import.scan_root(clean_named_root)['preview'][0]
    assert candidate['role_suggestion'] == 'config'

    species_named_root = tmp_path / 'Li2S4_project'
    _quartet(species_named_root / 'slab_clean')
    _quartet(species_named_root / 'ads_top')
    candidates = _by_rel(result_import.scan_root(species_named_root))
    assert candidates['slab_clean']['species_suggestion'] is None
    assert candidates['ads_top']['species_suggestion'] is None


def test_unicode_project_name_keeps_safe_distinct_directory(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _quartet(root / 'ads_top')
    plan = result_import.preview_project(
        root, '锂硫吸附_第一组', tmp_path / 'managed',
        {'clean': 'clean', 'ads_top': 'config'})
    assert plan['ok'], plan['errors']
    assert plan['directory_name'] == '锂硫吸附_第一组'
    assert Path(plan['destination']).name == '锂硫吸附_第一组'


@pytest.mark.parametrize(
    ('mutate', 'message'),
    [
        (lambda path: (path / 'INCAR').write_text(
            'ENCUT = 500\nNSW = 0\nIBRION = -1\n', encoding='utf-8'), 'ENCUT'),
        (lambda path: (path / 'KPOINTS').write_text(
            'mesh\n0\nGamma\n2 2 1\n0 0 0\n', encoding='utf-8'), 'KPOINTS'),
        (lambda path: (path / 'POSCAR').write_text(
            _poscar(path.name).replace('10 0 0', '12 0 0', 1), encoding='utf-8'),
         '晶胞不一致'),
        (lambda path: (path / 'INCAR').write_text(
            'ENCUT = 400\nNSW = 0\nLHFCALC = .TRUE.\nAEXX = 0.25\n'
            'HFSCREEN = 0.2\nLASPH = .TRUE.\n', encoding='utf-8'), 'HSE06'),
        (lambda path: (path / 'INCAR').write_text(
            'ENCUT = 400\nNSW = 0\nMETAGGA = R2SCAN\nLASPH = .TRUE.\n',
            encoding='utf-8'), 'r2SCAN'),
    ],
    ids=('encut', 'kpoints', 'lattice', 'hse', 'r2scan'),
)
def test_adsorption_consistency_blocks_incomparable_inputs(tmp_path, mutate, message):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    config = _quartet(root / 'ads_top')
    mutate(config)

    plan = result_import.preview_project(
        root, 'inconsistent', tmp_path / 'managed',
        {'clean': 'clean', 'ads_top': 'config'})
    assert not plan['ok']
    assert any(message in error for error in plan['errors']), plan['errors']


def test_adsorption_consistency_compares_kpoint_shift_not_comment(tmp_path):
    root = tmp_path / 'source'
    clean = _quartet(root / 'clean')
    config = _quartet(root / 'ads_top')
    (clean / 'KPOINTS').write_text(
        'clean comment\n0\nGamma\n1 1 1\n0 0 0\n', encoding='utf-8')
    (config / 'KPOINTS').write_text(
        'different harmless comment\n0\nGamma\n1 1 1\n0.5 0 0\n', encoding='utf-8')
    plan = result_import.preview_project(
        root, 'shifted-kmesh', tmp_path / 'managed',
        {'clean': 'clean', 'ads_top': 'config'})
    assert not plan['ok']
    assert any('KPOINTS 不同' in error for error in plan['errors'])


def test_config_may_add_adsorbate_atoms_but_not_remove_substrate(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    added = _quartet(root / 'ads_added')
    (added / 'POSCAR').write_text(
        'C plus S\n1\n10 0 0\n0 10 0\n0 0 15\nC S\n1 1\nDirect\n'
        '0 0 0\n0.5 0.5 0.5\n', encoding='utf-8')
    (added / 'POTCAR').write_text(
        'TITEL = PAW_PBE C 08Apr2002\nENMAX = 273.214 eV\n'
        'TITEL = PAW_PBE S 06Sep2000\nENMAX = 260.000 eV\n', encoding='utf-8')
    accepted = result_import.preview_project(
        root, 'adsorbate-added', tmp_path / 'managed',
        {'clean': 'clean', 'ads_added': 'config'})
    assert accepted['ok'], accepted['errors']

    _quartet(root / 'ads_removed')
    (root / 'clean' / 'POSCAR').write_text(
        'C2 slab\n1\n10 0 0\n0 10 0\n0 0 15\nC\n2\nDirect\n'
        '0 0 0\n0.5 0.5 0.5\n', encoding='utf-8')
    rejected = result_import.preview_project(
        root, 'substrate-removed', tmp_path / 'managed2',
        {'clean': 'clean', 'ads_removed': 'config'})
    assert not rejected['ok']
    assert any('基底组成不相容' in error for error in rejected['errors'])


def test_duplicate_config_names_keep_species_as_terminal_token(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _quartet(root / 'site_a' / 'top_Li2S4')
    _quartet(root / 'site_b' / 'top_Li2S4')
    plan = result_import.preview_project(
        root, 'duplicates', tmp_path / 'managed', [
            {'path': 'clean', 'role': 'clean'},
            {'path': 'site_a/top_Li2S4', 'role': 'config'},
            {'path': 'site_b/top_Li2S4', 'role': 'config'},
        ])
    assert plan['ok']
    names = [member['name'] for member in plan['members'] if member['role'] == 'config']
    assert len(set(names)) == 2
    assert all(name.endswith('_Li2S4') for name in names)


def test_task_specific_convergence_energy_gate_and_manual_confirmation(tmp_path):
    root = tmp_path / 'source'
    _result(root / 'static_ok', -10.0, marker=ELECTRONIC_MARK)
    _result(root / 'relax_ok', -20.0, marker=IONIC_MARK, task='relax')
    _result(root / 'relax_wrong_marker', -21.0,
            marker=ELECTRONIC_MARK, task='relax')
    _quartet(root / 'inputs_only', task='relax')
    _result(root / 'bad_energy', 2.5, marker=ELECTRONIC_MARK)

    scan = _by_rel(result_import.scan_root(root))
    assert scan['static_ok']['task_type'] == 'static'
    assert scan['static_ok']['state_suggestion'] == 'DONE'
    assert scan['static_ok']['energy_e0_eV'] == pytest.approx(-10.0)
    assert scan['relax_ok']['task_type'] == 'relax'
    assert scan['relax_ok']['state_suggestion'] == 'DONE'
    assert scan['relax_wrong_marker']['state_suggestion'] == 'NEEDS_HUMAN'
    assert scan['relax_wrong_marker']['raw_energy_e0_eV'] == pytest.approx(-21.0)
    assert scan['inputs_only']['state_suggestion'] == 'CREATED'
    assert scan['bad_energy']['state_suggestion'] == 'NEEDS_HUMAN'
    assert scan['bad_energy']['energy_e0_eV'] is None
    assert scan['bad_energy']['raw_energy_e0_eV'] == pytest.approx(2.5)

    plan = result_import.preview_project(
        root, 'manual', tmp_path / 'managed', [
            {'path': 'inputs_only', 'role': 'clean'},
            {'path': 'relax_wrong_marker', 'role': 'config', 'manual_confirm': True},
            {'path': 'bad_energy', 'role': 'config', 'manual_confirm': True},
        ])
    assert plan['ok']
    by_source = {Path(item['source']).name: item for item in plan['members']}
    assert by_source['relax_wrong_marker']['state'] == 'DONE'
    assert by_source['relax_wrong_marker']['manual_confirmed'] is True
    # Human confirmation may acknowledge a missing marker, but never legalises
    # an impossible positive total energy.
    assert by_source['bad_energy']['state'] == 'NEEDS_HUMAN'
    assert by_source['bad_energy']['manual_confirmed'] is False


def test_toten_or_vasprun_free_energy_is_not_relabelled_as_e0(tmp_path):
    root = tmp_path / 'source'
    outcar_only = _quartet(root / 'outcar_only')
    (outcar_only / 'OUTCAR').write_text(
        'free  energy   TOTEN = -42.5 eV\n' + ELECTRONIC_MARK + '\n',
        encoding='utf-8')
    efr_only = _quartet(root / 'efr_only')
    (efr_only / 'vasprun.xml').write_text(
        '<modeling><calculation><energy>'
        '<i name="e_fr_energy">-51.2</i>'
        '</energy></calculation></modeling>', encoding='utf-8')

    candidates = _by_rel(result_import.scan_root(root))
    assert candidates['outcar_only']['state_suggestion'] == 'NEEDS_HUMAN'
    assert candidates['outcar_only']['energy_e0_eV'] is None
    assert candidates['outcar_only']['observed_energy_source'] == 'OUTCAR:TOTEN'
    assert candidates['efr_only']['energy_e0_eV'] is None
    assert candidates['efr_only']['observed_energy_source'] == 'vasprun:e_fr_energy'

    plan = result_import.preview_project(
        root, 'energy-kind', tmp_path / 'managed', [
            {'path': 'outcar_only', 'role': 'clean', 'manual_confirm': True},
            {'path': 'efr_only', 'role': 'config', 'manual_confirm': True},
        ])
    assert plan['ok']
    assert all(member['state'] == 'NEEDS_HUMAN' for member in plan['members'])
    assert all(member['manual_confirmed'] is False for member in plan['members'])


def test_missing_incar_never_turns_relax_electronic_marker_into_static_done(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    result_dir = root / 'unknown_task'
    result_dir.mkdir(parents=True)
    (result_dir / 'OSZICAR').write_text(
        '1 F= -20 E0= -20 d E = 0\n', encoding='utf-8')
    # A non-converged relaxation can contain this marker after an electronic
    # step.  Without INCAR it must not be interpreted as a converged static job.
    (result_dir / 'OUTCAR').write_text(
        ELECTRONIC_MARK + '\nfree energy TOTEN = -20 eV\n', encoding='utf-8')
    candidate = _by_rel(result_import.scan_root(root))['unknown_task']
    assert candidate['task_type'] == 'unknown'
    assert candidate['state_suggestion'] == 'NEEDS_HUMAN'
    assert candidate['energy_e0_eV'] is None

    explicit = result_import.preview_project(
        root, 'typed', tmp_path / 'managed', [
            {'path': 'clean', 'role': 'clean'},
            {'path': 'unknown_task', 'role': 'config', 'task_type': 'static'},
        ])
    # An explicit task type can classify the result, but cannot invent the
    # missing quartet/method evidence needed for an adsorption-energy ΔE.
    assert not explicit['ok']
    assert any('科学一致性无法核验' in error for error in explicit['errors'])
    config = next(member for member in explicit['members'] if member['role'] == 'config')
    assert config['state'] == 'DONE'
    assert config['task_type_source'] == 'explicit'


def test_nelm_saturation_remains_blocked_even_with_manual_confirmation(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    scf_lines = ''.join(
        f'DAV: {index:3d} -0.100E+02 -0.5E-01 -0.1E+02 100 0.1E+00\n'
        for index in range(1, 61)
    )
    config_source = _result(root / 'config', -11.0, marker=None)
    # In a real OSZICAR the electronic block precedes the ionic F=/E0 summary.
    (config_source / 'OSZICAR').write_text(
        scf_lines + '  1 F= -0.11000000E+02 E0= -0.11000000E+02 d E = 0.0\n',
        encoding='utf-8')

    plan = result_import.preview_project(
        root, 'nelm', tmp_path / 'managed', [
            {'path': 'clean', 'role': 'clean'},
            {'path': 'config', 'role': 'config', 'manual_confirm': True},
        ])
    assert plan['ok']
    config = next(item for item in plan['members'] if item['role'] == 'config')
    assert config['state'] == 'NEEDS_HUMAN'
    assert config['diagnosis']['failure_class'] == 'SCF_SLOSHING'
    assert config['energy_e0_eV'] is None


def test_preview_is_zero_write_and_blocks_conflict_or_nested_destination(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _result(root / 'ads_config', -20.0, marker=ELECTRONIC_MARK)
    assignments = {'clean': 'clean', 'ads_config': 'config'}

    output = tmp_path / 'not-created-by-preview'
    plan = result_import.preview_project(root, 'demo', output, assignments)
    assert plan['ok']
    assert not output.exists()
    assert not ledger.list_dirs()
    assert not adsorption.list_projects()

    destination = output / 'demo'
    destination.mkdir(parents=True)
    conflict = result_import.preview_project(root, 'demo', output, assignments)
    assert not conflict['ok']
    assert any('目标已存在' in error for error in conflict['errors'])

    nested = result_import.preview_project(
        root, 'inside', root / 'managed', assignments)
    assert not nested['ok']
    assert any('嵌套' in error for error in nested['errors'])


def test_preview_fingerprint_binds_inputs_but_ignores_discarded_stale_outputs(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    config = _result(root / 'config', -20.0, marker=ELECTRONIC_MARK)
    assignments = [
        {'path': 'clean', 'role': 'clean'},
        {'path': 'config', 'role': 'config', 'force_created': True},
    ]
    plan = result_import.preview_project(
        root, 'recalculate', tmp_path / 'managed', assignments)
    assert plan['ok']
    config_plan = next(item for item in plan['members'] if item['role'] == 'config')
    assert config_plan['state'] == 'CREATED'

    # This output is explicitly outside the approved/copy set in recalculation
    # mode, so rotating it must not invalidate an unchanged quartet.
    (config / 'OUTCAR').write_text('archived stale output\n', encoding='utf-8')
    imported = result_import.import_project(
        root, 'recalculate', tmp_path / 'managed', assignments,
        expected_fingerprints=plan['fingerprints'])
    assert imported['ok'], imported.get('error')
    managed_config = Path(imported['project']['members']['configs'][0])
    assert not (managed_config / 'OUTCAR').exists()
    assert manifest.load_manifest(managed_config)['state'] == 'CREATED'


def test_preview_fingerprint_rejects_changed_quartet(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    config = _quartet(root / 'config')
    assignments = {'clean': 'clean', 'config': 'config'}
    plan = result_import.preview_project(
        root, 'changed-source', tmp_path / 'managed', assignments)
    assert plan['ok']

    # A comment-only POSCAR change keeps the recomputed scientific plan valid,
    # so refusal here specifically exercises preview/commit snapshot binding.
    (config / 'POSCAR').write_text(
        _poscar('edited after preview'), encoding='utf-8')
    rejected = result_import.import_project(
        root, 'changed-source', tmp_path / 'managed', assignments,
        expected_fingerprints=plan['fingerprints'])
    assert not rejected['ok']
    assert '预检后发生变化' in rejected['error']
    assert not Path(rejected['destination']).exists()


def test_copy_rejects_quartet_changed_after_internal_preflight(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _quartet(root / 'config')
    real_copy = result_import.shutil.copy2
    changed = {'done': False}

    def mutate_then_copy(source, target):
        source = Path(source)
        if source.name == 'POSCAR' and not changed['done']:
            source.write_text(_poscar('changed during copy'), encoding='utf-8')
            changed['done'] = True
        return real_copy(source, target)

    monkeypatch.setattr(result_import.shutil, 'copy2', mutate_then_copy)
    rejected = result_import.import_project(
        root, 'copy-race', tmp_path / 'managed',
        {'clean': 'clean', 'config': 'config'})
    assert not rejected['ok']
    assert '预检/复制期间发生变化' in rejected['error']
    assert not Path(rejected['destination']).exists()


def test_preview_rejects_string_booleans_and_species_path_traversal(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _result(root / 'config', -20.0, marker=None)
    output = tmp_path / 'managed'

    bad_bool = result_import.preview_project(
        root, 'bools', output, [
            {'path': 'clean', 'role': 'clean'},
            {'path': 'config', 'role': 'config', 'manual_confirm': 'false'},
        ], include_large_outputs='false')
    assert not bad_bool['ok']
    assert any('JSON boolean' in error for error in bad_bool['errors'])
    config = next(member for member in bad_bool['members'] if member['role'] == 'config')
    assert config['state'] == 'NEEDS_HUMAN'
    assert config['manual_confirmed'] is False

    traversal = result_import.preview_project(
        root, 'safe', output, [
            {'path': 'clean', 'role': 'clean'},
            {'path': 'config', 'role': 'config', 'species': '../../../escape'},
        ])
    assert not traversal['ok']
    assert any('合法化学式' in error for error in traversal['errors'])
    assert not output.exists()
    assert not (tmp_path / 'escape').exists()


def test_symlinked_evidence_is_never_trusted_or_copied(tmp_path):
    outside = tmp_path / 'outside'
    _result(outside, -42.0, marker=ELECTRONIC_MARK)
    root = tmp_path / 'source'
    job = _quartet(root / 'clean')
    (job / 'OUTCAR').symlink_to(outside / 'OUTCAR')
    (job / 'OSZICAR').symlink_to(outside / 'OSZICAR')
    _result(root / 'config', -50.0, marker=ELECTRONIC_MARK)

    scan = _by_rel(result_import.scan_root(root))
    assert scan['clean']['state_suggestion'] == 'CREATED'
    assert scan['clean']['energy_e0_eV'] is None
    result = result_import.import_project(
        root, 'links', tmp_path / 'managed',
        {'clean': 'clean', 'config': 'config'})
    assert result['ok'], result.get('error')
    imported_clean = Path(result['project']['members']['clean_slab'])
    assert manifest.load_manifest(imported_clean)['state'] == 'CREATED'
    assert not (imported_clean / 'OUTCAR').exists()
    assert not (imported_clean / 'OSZICAR').exists()


def test_invalid_or_mismatched_quartet_is_not_created(tmp_path):
    root = tmp_path / 'source'
    broken = root / 'broken'
    broken.mkdir(parents=True)
    for name in ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR'):
        (broken / name).write_text('', encoding='utf-8')

    mismatched = root / 'mismatched'
    _quartet(mismatched)
    (mismatched / 'POSCAR').write_text(
        'C O\n1\n10 0 0\n0 10 0\n0 0 10\nC O\n1 1\nDirect\n0 0 0\n0.5 0.5 0.5\n',
        encoding='utf-8')
    (mismatched / 'POTCAR').write_text(
        'TITEL = PAW_PBE O 08Apr2002\nENMAX = 400\n'
        'TITEL = PAW_PBE C 08Apr2002\nENMAX = 273\n', encoding='utf-8')
    candidates = _by_rel(result_import.scan_root(root))
    assert candidates['broken']['state_suggestion'] == 'NEEDS_HUMAN'
    assert candidates['mismatched']['state_suggestion'] == 'NEEDS_HUMAN'
    assert any('POTCAR 元素顺序' in issue
               for issue in candidates['mismatched']['files']['input_issues'])


def test_import_copies_allowlist_registers_project_and_local_molecule_refs(tmp_path):
    root = tmp_path / 'source'
    clean = _quartet(root / 'clean')
    config = _result(root / 'ads_Li2S4_top', -130.0, marker=IONIC_MARK, task='relax')
    gas_ref = _result(root / 'refs' / 'gas_ref', -12.0, marker=ELECTRONIC_MARK)
    mol_s8 = _result(root / 'refs' / 'molecules' / 'mol_S8', -80.0,
                     marker=ELECTRONIC_MARK)
    mol_li2s = _result(root / 'refs' / 'molecules' / 'mol_Li2S', -25.0,
                       marker=ELECTRONIC_MARK)
    (config / 'vasprun.xml').write_text(
        '<modeling><calculation><energy><i name="e_0_energy">-130.0</i>'
        '</energy></calculation></modeling>', encoding='utf-8')
    (config / 'CHGCAR').write_text('large density placeholder', encoding='utf-8')
    (config / 'run.sh').write_text('#!/bin/sh\n', encoding='utf-8')

    assignments = [
        {'path': clean, 'role': 'clean'},
        {'path': config, 'role': 'config'},
        {'path': gas_ref, 'role': 'ref'},
        {'path': mol_s8, 'role': 'molecule'},
        {'path': mol_li2s, 'role': 'molecule'},
    ]
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*') if path.is_file()
    }
    result = result_import.import_project(
        root, 'LiS imported', tmp_path / 'managed', assignments)
    assert result['ok'], result.get('error')

    destination = Path(result['destination'])
    project = adsorption.load_project(result['project_path'])
    assert project['root'] == str(destination)
    assert project['members']['clean_slab']
    assert len(project['members']['configs']) == 1
    assert project['members']['gas_ref']
    assert project['species_refs'] == pytest.approx({'S8': -80.0, 'Li2S': -25.0})
    assert Path(project['molecules_dir']).is_dir()
    assert (Path(project['molecules_dir']) / 'mol_S8' / 'OSZICAR').is_file()
    method_records = project['method_fingerprints']['members']
    assert len(method_records) == 5
    assert all(record['fingerprint']['complete'] for record in method_records)

    clean_manifest = manifest.load_manifest(project['members']['clean_slab'])
    config_manifest = manifest.load_manifest(project['members']['configs'][0])
    assert clean_manifest['state'] == 'CREATED'
    assert config_manifest['state'] == 'DONE'
    assert config_manifest['results']['energy_e0_eV'] == pytest.approx(-130.0)
    assert config_manifest['inputs']['elements'] == ['C']
    assert config_manifest['inputs']['potcar_provenance'][0]['variant'] == 'C'

    imported_config = Path(project['members']['configs'][0])
    assert (imported_config / 'OUTCAR').is_file()
    assert not (imported_config / 'vasprun.xml').exists()
    assert not (imported_config / 'CHGCAR').exists()
    assert not (imported_config / 'run.sh').exists()  # not in the allowlist
    config_preview = next(item for item in result['members'] if item['role'] == 'config')
    assert set(config_preview['files']['skipped_large']) == {'vasprun.xml', 'CHGCAR'}
    assert any('默认跳过大文件' in warning for warning in config_preview['warnings'])

    assert set(ledger.list_dirs()) == set(result['registered_jobs'])
    assert len(result['registered_jobs']) == 5
    assert adsorption.list_projects() == [result['project_path']]

    # Reading/copying did not alter any source file or add files under the source.
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*') if path.is_file()
    }
    assert after == before


def test_include_large_outputs_is_opt_in(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    config = _result(root / 'ads_config', -22.0, marker=ELECTRONIC_MARK)
    (config / 'vasprun.xml').write_text(
        '<modeling><calculation><energy><i name="e_0_energy">-22</i>'
        '</energy></calculation></modeling>', encoding='utf-8')
    (config / 'CHGCAR').write_bytes(b'density')
    result = result_import.import_project(
        root, 'with-large', tmp_path / 'managed',
        {'clean': 'clean', 'ads_config': 'config'},
        include_large_outputs=True)
    assert result['ok'], result.get('error')
    config_dir = Path(result['project']['members']['configs'][0])
    assert (config_dir / 'vasprun.xml').read_text(encoding='utf-8').startswith('<modeling>')
    assert (config_dir / 'CHGCAR').read_bytes() == b'density'


def test_vasprun_only_e0_requires_evidence_to_be_copied(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    config = _quartet(root / 'config')
    (config / 'OUTCAR').write_text(ELECTRONIC_MARK + '\n', encoding='utf-8')
    (config / 'vasprun.xml').write_text(
        '<modeling><calculation><energy><i name="e_0_energy">-44.0</i>'
        '</energy></calculation></modeling>', encoding='utf-8')
    assignments = {'clean': 'clean', 'config': 'config'}

    blocked = result_import.preview_project(
        root, 'xml-evidence', tmp_path / 'managed', assignments)
    assert not blocked['ok']
    assert any('必须启用 include_large_outputs' in error for error in blocked['errors'])
    assert not (tmp_path / 'managed').exists()

    imported = result_import.import_project(
        root, 'xml-evidence', tmp_path / 'managed', assignments,
        include_large_outputs=True)
    assert imported['ok'], imported.get('error')
    config_dir = Path(imported['project']['members']['configs'][0])
    config_manifest = manifest.load_manifest(config_dir)
    assert config_manifest['state'] == 'DONE'
    assert config_manifest['results']['energy_e0_eV'] == pytest.approx(-44.0)
    assert (config_dir / 'vasprun.xml').is_file()


def test_unconfirmed_molecule_is_quarantined_from_freeenergy_loader(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _result(root / 'ads_S8', -90.0, marker=ELECTRONIC_MARK)
    # A sane energy without a convergence marker remains untrusted until the
    # user explicitly confirms it.
    molecule = _result(
        root / 'refs' / 'molecules' / 'mol_S8', -80.0, marker=None)
    result = result_import.import_project(
        root, 'quarantine', tmp_path / 'managed', [
            {'path': 'clean', 'role': 'clean'},
            {'path': 'ads_S8', 'role': 'config'},
            {'path': molecule, 'role': 'molecule'},
        ])
    assert result['ok'], result.get('error')
    project = result['project']
    assert project['species_refs'] == {'S8': None}
    pending = next(item for item in result['members'] if item['role'] == 'molecule')
    assert pending['destination'].replace('\\', '/').endswith('/molecules/mol_S8')
    assert freeenergy.load_molecule_energies(project['molecules_dir']) == {}


def test_created_molecule_reference_becomes_available_after_done(tmp_path):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _result(root / 'ads_S8', -90.0, marker=ELECTRONIC_MARK)
    molecule = _quartet(root / 'refs' / 'molecules' / 'mol_S8')
    result = result_import.import_project(
        root, 'pending-ref', tmp_path / 'managed', [
            {'path': 'clean', 'role': 'clean'},
            {'path': 'ads_S8', 'role': 'config'},
            {'path': molecule, 'role': 'molecule'},
        ])
    assert result['ok'], result.get('error')
    project = result['project']
    managed_molecule = Path(project['species_ref_jobs']['S8'])
    assert managed_molecule.parent == Path(project['molecules_dir'])
    assert manifest.load_manifest(managed_molecule)['state'] == 'CREATED'
    assert freeenergy.load_molecule_energies(project['molecules_dir']) == {}

    item = manifest.load_manifest(managed_molecule)
    manifest.set_state(item, 'DONE')
    item['results']['energy_e0_eV'] = -80.0
    manifest.save_manifest(managed_molecule, item)
    assert freeenergy.load_molecule_energies(project['molecules_dir']) == {'S8': -80.0}


def test_copy_failure_cleans_staging_and_leaves_no_target(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _result(root / 'ads_config', -30.0, marker=ELECTRONIC_MARK)
    output = tmp_path / 'managed'

    real_copy = result_import.shutil.copy2
    calls = {'n': 0}

    def fail_after_one(source, target):
        calls['n'] += 1
        if calls['n'] == 2:
            raise OSError('simulated copy failure')
        return real_copy(source, target)

    monkeypatch.setattr(result_import.shutil, 'copy2', fail_after_one)
    result = result_import.import_project(
        root, 'atomic', output, {'clean': 'clean', 'ads_config': 'config'})
    assert not result['ok']
    assert 'simulated copy failure' in result['error']
    assert not (output / 'atomic').exists()
    assert list(output.glob('.atomic.import-*')) == []
    assert not ledger.list_dirs()
    assert not adsorption.list_projects()


def test_ledger_failure_rolls_back_committed_tree_and_partial_registration(
        tmp_path, monkeypatch):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _result(root / 'config', -30.0, marker=ELECTRONIC_MARK)
    output = tmp_path / 'managed'
    real_register = result_import.ledger.register
    calls = {'n': 0}

    def fail_second(job_dir, path=None):
        calls['n'] += 1
        if calls['n'] == 2:
            raise OSError('simulated ledger failure')
        return real_register(job_dir, path=path)

    monkeypatch.setattr(result_import.ledger, 'register', fail_second)
    result = result_import.import_project(
        root, 'ledger-atomic', output, {'clean': 'clean', 'config': 'config'})
    assert not result['ok']
    assert 'simulated ledger failure' in result['error']
    assert not Path(result['destination']).exists()
    assert ledger.list_dirs() == []
    assert adsorption.list_projects() == []


def test_registry_failure_after_write_is_fully_rolled_back(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    _quartet(root / 'clean')
    _result(root / 'config', -30.0, marker=ELECTRONIC_MARK)
    output = tmp_path / 'managed'
    real_register = result_import.adsorption.register_project

    def write_then_fail(project_path, path=None):
        real_register(project_path, path=path)
        raise OSError('simulated registry failure')

    monkeypatch.setattr(result_import.adsorption, 'register_project', write_then_fail)
    result = result_import.import_project(
        root, 'registry-atomic', output, {'clean': 'clean', 'config': 'config'})
    assert not result['ok']
    assert 'simulated registry failure' in result['error']
    assert not Path(result['destination']).exists()
    assert ledger.list_dirs() == []
    assert adsorption.list_projects() == []
