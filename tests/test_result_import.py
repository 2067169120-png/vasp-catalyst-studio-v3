"""Local VASP-result import: multi-evidence convergence and read-only commit."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from vcstudio.project import adsorption
from vcstudio.project import result_import as ri
from vcstudio.shared import manifest


_CONTCAR = """CONTCAR_Li2S8
1.0
12.3 0 0
-6.15 10.65 0
0 0 20
Li S
2 8
Direct
0.1 0.1 0.1
0.2 0.2 0.2
0.3 0.3 0.3
0.4 0.4 0.4
0.5 0.5 0.5
0.6 0.6 0.6
0.7 0.7 0.7
0.8 0.8 0.8
0.9 0.9 0.9
0.05 0.05 0.05
"""


def _oszicar(*, final_scf=8, energy=-38.072771, trailing=False):
    lines = []
    for step, e in ((1, -37.9), (2, energy)):
        for i in range(1, final_scf + 1):
            lines.append(f'RMM: {i:3d} {e + 0.01 / i:16.9E} {-1e-4 / i:12.5E} 0 1 1e-3')
        lines.append(f'{step:4d} F= {e:14.8E} E0= {e:14.8E}  d E =-5.3E-06')
    if trailing:
        lines.extend(['DAV:   1 -3.8E+01 -1.0E-01 0 20 1E-1',
                      'DAV:   2 -3.8E+01 -5.0E-02 0 20 1E-1'])
    return '\n'.join(lines) + '\n'


def _outcar(*, marker=True, footer=True, electronic=True, nelm=100,
            fmax=0.016851, energy=-38.07277089, stopped=False):
    lines = [
        'vasp.6.4.3 18Apr24',
        f'NELM = {nelm}; NELMIN = 8',
        'EDIFF = 0.1E-04',
        'EDIFFG = -0.2E-01',
        'NSW = 500',
        'IBRION = 2',
        f'energy without entropy = {energy:.8f} energy(sigma->0) = {energy:.8f}',
    ]
    if electronic:
        lines.append('aborting loop because EDIFF is reached')
    lines.append(f'FORCES: max atom, RMS {fmax:.6f} 0.010308')
    if marker:
        lines.append('reached required accuracy - stopping structural energy minimisation')
    if stopped:
        lines.append('soft stop encountered!  aborting job')
    if footer:
        lines.extend(['General timing and accounting informations for this job:',
                      'Total CPU time used (sec): 244.647'])
    return '\n'.join(lines) + '\n'


def _vasprun(*, complete=True, static=True, e0=-0.0, ewo=-38.07277089,
             scf_steps=8, force=0.016, method=''):
    params = (
        '<i name="NELM" type="int">100</i>'
        f'<i name="NSW" type="int">{0 if static else 500}</i>'
        f'<i name="IBRION" type="int">{-1 if static else 2}</i>'
        '<i name="EDIFFG">-0.020</i>' + method
    )
    scf = ''.join(
        '<scstep><energy><i name="e_0_energy">-37.0</i></energy></scstep>'
        for _ in range(scf_steps))
    end = '</modeling>' if complete else ''
    return (
        '<?xml version="1.0"?><modeling><parameters>' + params + '</parameters>'
        '<calculation>' + scf
        + f'<varray name="forces"><v>{force} 0 0</v></varray>'
        + '<energy>'
        + f'<i name="e_fr_energy">{ewo}</i>'
        + f'<i name="e_wo_entrp">{ewo}</i>'
        + f'<i name="e_0_energy">{e0}</i>'
        + '</energy></calculation>' + end)


def _make_result(folder: Path, *, marker=True, footer=True, electronic=True,
                 nelm=100, final_scf=8, fmax=0.016851, trailing=False,
                 with_inputs=False, with_xml=True):
    folder.mkdir(parents=True)
    (folder / 'CONTCAR').write_text(_CONTCAR, encoding='utf-8')
    (folder / 'OSZICAR').write_text(
        _oszicar(final_scf=final_scf, trailing=trailing), encoding='utf-8')
    (folder / 'OUTCAR').write_text(
        _outcar(marker=marker, footer=footer, electronic=electronic,
                nelm=nelm, fmax=fmax), encoding='utf-8')
    if with_xml:
        (folder / 'vasprun.xml').write_text(_vasprun(static=False), encoding='utf-8')
    if with_inputs:
        (folder / 'INCAR').write_text('IBRION=2\nNSW=500\nNELM=100\nEDIFFG=-0.02\n', encoding='utf-8')
        (folder / 'POSCAR').write_text(_CONTCAR.replace('CONTCAR', 'POSCAR', 1), encoding='utf-8')
        (folder / 'KPOINTS').write_text('Gamma\n0\nGamma\n1 1 1\n0 0 0\n', encoding='utf-8')
        (folder / 'POTCAR').write_text('TITEL = PAW_PBE Li\nTITEL = PAW_PBE S\n', encoding='utf-8')
    return folder


def _candidate(root: Path):
    result = ri.scan_folder(root)
    assert result['summary']['total'] == 1
    return result['candidates'][0]


def _fake_services():
    registered_jobs, registered_projects = [], []
    ads = SimpleNamespace(
        save_project=adsorption.save_project,
        register_project=lambda path: registered_projects.append(str(path)) or True,
    )
    ledger = SimpleNamespace(register=lambda path: registered_jobs.append(str(path)) or True)
    return ads, ledger, registered_jobs, registered_projects


def test_real_style_relax_is_done_with_structured_evidence(tmp_path):
    """Regression for the user's Li2S8 sample, reduced to the decisive lines."""
    _make_result(tmp_path / 'mol_Li2S8')
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'DONE'
    assert c['task_type'] == 'relax'
    assert c['energy_e0_eV'] == pytest.approx(-38.072771)
    assert c['energy_source'] == 'OSZICAR:E0'
    assert c['species'] == 'Li2S8' and c['suggested_role'] == 'molecule_ref'
    ev = c['convergence_evidence']
    assert ev['electronic']['final_scf_steps'] == 8
    assert ev['electronic']['nelm'] == 100
    assert not ev['electronic']['nelm_saturated']
    assert ev['ionic']['converged_marker'] and ev['ionic']['force_gate_passed']
    assert ev['completion']['outcar_footer']
    assert ev['completion']['vasprun_checked'] and ev['completion']['vasprun_complete']
    assert ev['cross_file_energy']['consistent']
    assert ev['cross_file_energy']['spread_eV'] < 1e-6
    assert c['diagnosis']['code'] == 'converged_multi_evidence'


def test_vasprun_zero_e0_does_not_hide_valid_energy(tmp_path):
    d = tmp_path / 'static_only_xml'
    d.mkdir()
    (d / 'CONTCAR').write_text(_CONTCAR, encoding='utf-8')
    (d / 'vasprun.xml').write_text(_vasprun(e0=-0.0, ewo=-12.345), encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['task_type'] == 'static' and c['state_suggestion'] == 'DONE'
    assert c['energy_e0_eV'] == pytest.approx(-12.345)
    assert c['energy_source'] == 'vasprun.xml:derived_sigma0'
    assert any('e_0_energy' in w for w in c['warnings'])
    assert c['convergence_evidence']['completion']['vasprun_complete']


def test_relax_can_use_complete_vasprun_without_outcar_or_oszicar(tmp_path):
    d = tmp_path / 'xml_relax'
    d.mkdir()
    (d / 'CONTCAR').write_text(_CONTCAR, encoding='utf-8')
    (d / 'vasprun.xml').write_text(
        _vasprun(static=False, e0=-20.0, ewo=-20.0, force=0.015), encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['task_type'] == 'relax' and c['state_suggestion'] == 'DONE'
    assert c['energy_source'] == 'vasprun.xml:e_0_energy'
    assert c['convergence_evidence']['ionic']['force_gate_passed']
    assert c['convergence_evidence']['completion']['vasprun_complete']


def test_outcar_energy_is_fallback_when_oszicar_missing(tmp_path):
    d = tmp_path / 'outcar_only'
    d.mkdir()
    (d / 'CONTCAR').write_text(_CONTCAR, encoding='utf-8')
    (d / 'OUTCAR').write_text(_outcar(), encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'DONE'
    assert c['energy_source'] == 'OUTCAR:energy(sigma->0)'
    assert c['energy_e0_eV'] == pytest.approx(-38.07277089)


def test_truncated_vasprun_is_not_normal_completion(tmp_path):
    d = tmp_path / 'truncated_xml'
    d.mkdir()
    (d / 'CONTCAR').write_text(_CONTCAR, encoding='utf-8')
    (d / 'OSZICAR').write_text(_oszicar(), encoding='utf-8')
    (d / 'INCAR').write_text('NSW=0\nIBRION=-1\nNELM=100\n', encoding='utf-8')
    (d / 'vasprun.xml').write_text(_vasprun(complete=False), encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'NEEDS_HUMAN'
    assert not c['confirmation_eligible']
    assert not c['convergence_evidence']['completion']['vasprun_complete']
    assert any('不完整' in w for w in c['warnings'])


def test_nelm_saturation_is_hard_block_even_with_markers(tmp_path):
    _make_result(tmp_path / 'saturated', nelm=8, final_scf=8)
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'NEEDS_HUMAN'
    assert not c['confirmation_eligible']
    assert c['convergence_evidence']['electronic']['nelm_saturated']
    assert any('NELM=8' in b for b in c['diagnosis']['blockers'])


def test_output_parameters_override_an_incar_edited_after_run(tmp_path):
    d = _make_result(tmp_path / 'edited_input', with_inputs=True, nelm=100, final_scf=8)
    (d / 'INCAR').write_text('IBRION=5\nNSW=1\nNELM=8\nEDIFFG=-0.001\n', encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['task_type'] == 'relax'
    assert c['convergence_evidence']['electronic']['nelm'] == 100
    assert c['state_suggestion'] == 'DONE'


def test_trailing_scf_after_last_summary_is_hard_block(tmp_path):
    _make_result(tmp_path / 'truncated_oszicar', trailing=True)
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'NEEDS_HUMAN'
    assert not c['confirmation_eligible']
    assert c['convergence_evidence']['electronic']['trailing_incomplete_scf']


def test_force_gate_can_prove_relax_without_wording_marker(tmp_path):
    _make_result(tmp_path / 'wording_variant', marker=False, footer=True, fmax=0.019)
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'DONE'
    assert not c['convergence_evidence']['ionic']['converged_marker']
    assert c['convergence_evidence']['ionic']['force_gate_passed']


def test_markers_are_case_insensitive_and_accept_short_wording(tmp_path):
    d = _make_result(tmp_path / 'case_variant')
    text = (d / 'OUTCAR').read_text(encoding='utf-8')
    text = text.replace(
        'reached required accuracy - stopping structural energy minimisation',
        'REACHED REQUIRED ACCURACY')
    text = text.replace('General timing and accounting informations for this job:',
                        'GENERAL TIMING AND ACCOUNTING INFORMATION FOR THIS JOB:')
    (d / 'OUTCAR').write_text(text, encoding='utf-8')
    assert _candidate(tmp_path)['state_suggestion'] == 'DONE'


def test_frequency_uses_normal_end_and_electronic_convergence(tmp_path):
    d = _make_result(tmp_path / 'freq', marker=False)
    text = (d / 'OUTCAR').read_text(encoding='utf-8')
    text = text.replace('IBRION = 2', 'IBRION = 5').replace('NSW = 500', 'NSW = 1')
    (d / 'OUTCAR').write_text(text, encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['task_type'] == 'freq' and c['state_suggestion'] == 'DONE'
    assert not c['convergence_evidence']['ionic']['converged_marker']


def test_fatal_error_is_not_manually_confirmable(tmp_path):
    d = _make_result(tmp_path / 'fatal', marker=False, footer=False)
    with open(d / 'OUTCAR', 'a', encoding='utf-8') as f:
        f.write('ZBRENT: fatal error in bracketing\n')
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'NEEDS_HUMAN'
    assert not c['confirmation_eligible']
    assert 'ZBRENT' in c['convergence_evidence']['fatal_error']


def test_old_converged_segment_cannot_mask_incomplete_restart(tmp_path):
    d = _make_result(tmp_path / 'restart')
    old = (d / 'OUTCAR').read_text(encoding='utf-8')
    latest = _outcar(marker=False, footer=False, electronic=True)
    (d / 'OUTCAR').write_text(old + '\n' + latest, encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'NEEDS_HUMAN'
    assert not c['convergence_evidence']['ionic']['converged_marker']
    assert not c['convergence_evidence']['completion']['outcar_footer']


def test_ambiguous_energy_can_be_manually_confirmed_with_audit(tmp_path):
    source = tmp_path / 'source'
    # Normal footer + terminal electronic evidence are present, but the relax
    # lacks both the wording marker and the EDIFFG force gate.
    d = _make_result(source / 'ads_Li2S8', marker=False, footer=True, fmax=0.03)
    c = _candidate(source)
    assert c['state_suggestion'] == 'NEEDS_HUMAN' and c['confirmation_eligible']
    ads, ledger, jobs, projects = _fake_services()
    result = ri.commit_import(
        source, tmp_path / 'managed', '人工核对项目',
        [{'path': c['path'], 'role': 'config', 'manual_confirm': True,
          'confirmation_reason': '已对照原始作业日志和末结构', 'task_type': 'relax'}],
        adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    assert result['summary']['manual_confirmed'] == 1
    imported = Path(result['imported'][0]['path'])
    m = manifest.load_manifest(imported)
    assert m['state'] == 'DONE'
    assert m['results']['import_confirmation']['manual'] is True
    assert '原始作业日志' in m['results']['import_confirmation']['reason']
    assert jobs == [str(imported)] and projects == [result['project_path']]
    assert any('clean_slab' in w for w in result['project']['warnings'])
    assert d.is_dir()  # source tree was not moved


def test_manual_confirmation_cannot_override_nelm(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'bad', nelm=8, final_scf=8)
    c = _candidate(source)
    ads, ledger, *_ = _fake_services()
    with pytest.raises(ValueError, match='不能人工确认为 DONE'):
        ri.commit_import(
            source, tmp_path / 'managed', 'bad-project',
            [{'path': c['path'], 'role': 'config', 'manual_confirm': True}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    assert not (tmp_path / 'managed' / 'bad-project').exists()


def test_manual_confirmation_requires_an_audit_reason(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'ambiguous', marker=False, footer=True, fmax=0.03)
    c = _candidate(source)
    assert c['confirmation_eligible']
    ads, ledger, *_ = _fake_services()
    with pytest.raises(ValueError, match='核对理由'):
        ri.commit_import(
            source, tmp_path / 'managed', 'missing-reason',
            [{'path': c['path'], 'role': 'config', 'manual_confirm': True,
              'task_type': 'relax'}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)


def test_cross_file_energy_mismatch_is_a_hard_gate(tmp_path):
    d = _make_result(tmp_path / 'mixed_restart')
    (d / 'OSZICAR').write_text(_oszicar(energy=-37.0), encoding='utf-8')
    c = _candidate(tmp_path)
    assert c['state_suggestion'] == 'NEEDS_HUMAN'
    assert not c['confirmation_eligible']
    cross = c['convergence_evidence']['cross_file_energy']
    assert not cross['consistent'] and cross['spread_eV'] > 1.0
    assert any('跨文件不一致' in reason for reason in c['diagnosis']['blockers'])


def test_task_override_recomputes_convergence_gate(tmp_path):
    source = tmp_path / 'source'
    d = source / 'static_result'
    d.mkdir(parents=True)
    (d / 'CONTCAR').write_text(_CONTCAR, encoding='utf-8')
    (d / 'OSZICAR').write_text(_oszicar(), encoding='utf-8')
    out = _outcar(marker=False).replace('NSW = 500', 'NSW = 0').replace(
        'IBRION = 2', 'IBRION = -1')
    out = '\n'.join(line for line in out.splitlines() if 'FORCES: max atom' not in line) + '\n'
    (d / 'OUTCAR').write_text(out, encoding='utf-8')
    c = _candidate(source)
    assert c['task_type'] == 'static' and c['state_suggestion'] == 'DONE'
    ads, ledger, *_ = _fake_services()
    result = ri.commit_import(
        source, tmp_path / 'managed', 'override-gate',
        [{'path': c['path'], 'role': 'standalone', 'task_type': 'relax'}],
        adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    imported = manifest.load_manifest(result['imported'][0]['path'])
    assert imported['task_type'] == 'relax'
    assert imported['state'] == 'NEEDS_HUMAN'


def test_relax_task_override_cannot_bypass_ionic_convergence(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'unfinished_relax', marker=False, footer=True, fmax=0.5)
    candidate = _candidate(source)
    assert candidate['task_type'] == 'relax' and candidate['state_suggestion'] == 'NEEDS_HUMAN'
    ads, ledger, *_ = _fake_services()
    with pytest.raises(ValueError, match='不能改为 static'):
        ri.commit_import(
            source, tmp_path / 'managed', 'no-bypass',
            [{'path': candidate['path'], 'role': 'molecule_ref',
              'species': 'Li2S8', 'task_type': 'static'}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)


def test_reference_method_signature_is_parsed_and_persisted(tmp_path):
    source = tmp_path / 'source'
    folder = _make_result(source / 'mol_Li2S8')
    original = (folder / 'OUTCAR').read_text(encoding='utf-8')
    method = (
        ' GGA = RP\n LEXCH = 9\n IVDW = 11\n ISPIN = 1\n ENCUT = 400.0 eV\n'
        ' LDAU = F\n LHFCALC = T\n AEXX = 0.25\n HFSCREEN = 0.2\n'
        ' TITEL = PAW_PBE Li 17Jan2003\n'
        ' TITEL = PAW_PBE S 06Sep2000\n')
    (folder / 'OUTCAR').write_text(original + method, encoding='utf-8')
    candidate = _candidate(source)
    signature = candidate['reference_method_signature']
    assert signature['functional'] == 'RPBE'
    assert signature['ivdw'] == 11 and signature['ispin'] == 1
    assert signature['encut'] == pytest.approx(400.0)
    assert signature['aexx'] == pytest.approx(0.25)
    assert signature['potcar_titel'] == [
        'PAW_PBE Li 17Jan2003', 'PAW_PBE S 06Sep2000']

    ads, ledger, *_ = _fake_services()
    result = ri.commit_import(
        source, tmp_path / 'managed', 'refs',
        [{'path': candidate['path'], 'role': 'molecule_ref', 'species': 'Li2S8'}],
        adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    imported = manifest.load_manifest(result['project']['species_ref_jobs']['Li2S8'])
    assert imported['results']['reference_method_signature']['functional'] == 'RPBE'
    assert imported['inputs']['reference_method_signature']['ivdw'] == 11


def test_dft_u_method_vectors_are_parsed_with_potcar_order_and_persisted(tmp_path):
    source = tmp_path / 'source'
    folder = _make_result(source / 'mol_Li2S8')
    original = (folder / 'OUTCAR').read_text(encoding='utf-8')
    method = (
        ' LDAU = T\n LDAUTYPE = 2\n LDAUL = 0 -1\n'
        ' LDAUU = 3.000000 0.000000\n LDAUJ = 2*0.000000\n'
        ' TITEL = PAW_PBE Li 17Jan2003\n'
        ' TITEL = PAW_PBE S 06Sep2000\n')
    (folder / 'OUTCAR').write_text(original + method, encoding='utf-8')

    candidate = _candidate(source)
    signature = candidate['reference_method_signature']
    assert signature['ldau'] == 'T' and signature['ldautype'] == 2
    assert signature['ldaul'] == [0, -1]
    assert signature['ldauu'] == [3.0, 0.0]
    assert signature['ldauj'] == [0.0, 0.0]
    assert signature['potcar_elements'] == ['Li', 'S']

    ads, ledger, *_ = _fake_services()
    result = ri.commit_import(
        source, tmp_path / 'managed', 'refs-u',
        [{'path': candidate['path'], 'role': 'molecule_ref', 'species': 'Li2S8'}],
        adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    imported = manifest.load_manifest(result['project']['species_ref_jobs']['Li2S8'])
    persisted = imported['results']['reference_method_signature']
    assert persisted['ldautype'] == 2 and persisted['ldauu'] == [3.0, 0.0]
    assert imported['inputs']['reference_method_signature']['potcar_elements'] == ['Li', 'S']


def test_vasprun_explicit_ldau_false_completes_outcar_method_signature(tmp_path):
    source = tmp_path / 'source'
    folder = _make_result(source / 'mol_Li2S8')
    (folder / 'vasprun.xml').write_text(
        _vasprun(static=False, method='<i type="logical" name="LDAU">F</i>'),
        encoding='utf-8')

    signature = _candidate(source)['reference_method_signature']

    assert signature['ldau'] == 'F'


def test_molecule_reference_species_must_match_structure_composition(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'mislabelled_Li2S8')
    candidate = _candidate(source)
    ads, ledger, *_ = _fake_services()
    with pytest.raises(ValueError, match='结构组成 Li2S8 不一致'):
        ri.commit_import(
            source, tmp_path / 'managed', 'bad-label',
            [{'path': candidate['path'], 'role': 'molecule_ref', 'species': 'Li2S6'}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)


def test_commit_molecule_library_copies_full_provenance_without_clean(tmp_path):
    source = tmp_path / 'source'
    d = _make_result(source / 'mol_Li2S8', with_inputs=True)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}
    c = _candidate(source)
    ads, ledger, jobs, _projects = _fake_services()
    result = ri.commit_import(
        source, tmp_path / 'managed', 'Li-S分子库',
        [{'path': c['path'], 'role': 'molecule_ref', 'species': 'Li2S8'}],
        adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    project = result['project']
    assert project['members']['clean_slab'] is None
    assert project['species_refs']['Li2S8'] == pytest.approx(-38.072771)
    imported = Path(project['species_ref_jobs']['Li2S8'])
    for name in ('INCAR', 'KPOINTS', 'CONTCAR', 'POSCAR', 'OUTCAR',
                 'OSZICAR', 'vasprun.xml', 'POTCAR', 'job.yaml'):
        assert (imported / name).is_file()
    m = manifest.load_manifest(imported)
    assert set(m['inputs']['source_sha256']) >= {
        'INCAR', 'KPOINTS', 'CONTCAR', 'POSCAR', 'OUTCAR', 'OSZICAR', 'vasprun.xml', 'POTCAR'}
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}
    assert after == before
    assert jobs == [str(imported)]
    loaded = yaml.safe_load(Path(result['project_path']).read_text(encoding='utf-8'))
    assert loaded['molecules_dir'] == str(imported.parent)


def test_unconfirmed_molecule_cannot_enter_reference_library(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'mol_Li2S8', marker=False, footer=False)
    c = _candidate(source)
    ads, ledger, *_ = _fake_services()
    with pytest.raises(ValueError, match='不能写入分子参考库'):
        ri.commit_import(
            source, tmp_path / 'managed', 'refs',
            [{'path': c['path'], 'role': 'molecule_ref', 'species': 'Li2S8',
              'manual_confirm': True}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)


def test_recursive_scan_includes_root_and_selected_false_is_ignored(tmp_path):
    source = tmp_path / 'root_result'
    _make_result(source)
    nested = _make_result(source / 'nested' / 'ads_CO')
    result = ri.scan_folder(source)
    assert [c['relative_path'] for c in result['candidates']] == ['.', 'nested/ads_CO']
    assert result['candidates'][1]['suggested_role'] == 'config'
    assert result['suggested_name'] == 'root_result_导入项目'
    assert result['suggested_out_root'] == str(tmp_path)
    root_c, nested_c = result['candidates']
    ads, ledger, _jobs, _projects = _fake_services()
    committed = ri.commit_import(
        source, tmp_path / 'managed', 'selected-only',
        [{'path': root_c['path'], 'role': 'standalone'},
         {'path': nested_c['path'], 'role': 'config', 'selected': False}],
        adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    assert committed['summary']['total'] == 1
    assert committed['imported'][0]['source'] == str(source.resolve())
    assert nested.is_dir()


def test_commit_rejects_unknown_task_type_and_existing_destination(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'one')
    c = _candidate(source)
    ads, ledger, *_ = _fake_services()
    with pytest.raises(ValueError, match='任务类型'):
        ri.commit_import(
            source, tmp_path / 'managed', 'invalid-task',
            [{'path': c['path'], 'role': 'standalone', 'task_type': 'neb'}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    existing = tmp_path / 'managed' / 'exists'
    existing.mkdir(parents=True)
    with pytest.raises(FileExistsError, match='避免覆盖'):
        ri.commit_import(
            source, tmp_path / 'managed', 'exists',
            [{'path': c['path'], 'role': 'standalone'}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    with pytest.raises(ValueError, match='源目录内部'):
        ri.commit_import(
            source, source / 'managed', 'nested-destination',
            [{'path': c['path'], 'role': 'standalone'}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)


def test_commit_rolls_back_renamed_tree_and_partial_ledger_on_register_failure(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'one')
    _make_result(source / 'two')
    candidates = ri.scan_folder(source)['candidates']
    registered = []

    def register(path):
        if registered:
            raise RuntimeError('注入的第二条台账失败')
        registered.append(str(path))
        return True

    def unregister(path):
        registered.remove(str(path))
        return True

    ledger = SimpleNamespace(register=register, unregister=unregister)
    ads = SimpleNamespace(
        save_project=adsorption.save_project,
        register_project=lambda _path: True,
        unregister_project=lambda _path: True,
    )
    target = tmp_path / 'managed' / 'rollback-ledger'
    with pytest.raises(RuntimeError, match='第二条台账'):
        ri.commit_import(
            source, tmp_path / 'managed', 'rollback-ledger',
            [{'path': row['path'], 'role': 'standalone'} for row in candidates],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    assert registered == []
    assert not target.exists()
    assert (source / 'one').is_dir() and (source / 'two').is_dir()


def test_commit_rolls_back_all_jobs_and_tree_when_project_register_fails(tmp_path):
    source = tmp_path / 'source'
    _make_result(source / 'one')
    candidate = _candidate(source)
    registered = []
    ledger = SimpleNamespace(
        register=lambda path: registered.append(str(path)) or True,
        unregister=lambda path: registered.remove(str(path)) or True,
    )
    ads = SimpleNamespace(
        save_project=adsorption.save_project,
        register_project=lambda _path: (_ for _ in ()).throw(
            RuntimeError('注入的项目注册失败')),
        unregister_project=lambda _path: True,
    )
    with pytest.raises(RuntimeError, match='项目注册失败'):
        ri.commit_import(
            source, tmp_path / 'managed', 'rollback-project',
            [{'path': candidate['path'], 'role': 'standalone'}],
            adsorption_mod=ads, ledger_mod=ledger, manifest_mod=manifest)
    assert registered == []
    assert not (tmp_path / 'managed' / 'rollback-project').exists()
    assert (source / 'one').is_dir()
