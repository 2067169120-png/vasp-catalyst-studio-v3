import json
import os

import pytest

from vcstudio.gui_web.api import Api
from vcstudio.project import task_analysis
from vcstudio.shared import manifest


def _oszicar(energies):
    return ''.join(
        f' DAV:   4   -1.0  0.0\n{i} F= {energy:.8f} E0= {energy:.8f} d E =0\n'
        for i, energy in enumerate(energies, 1))


def test_capability_matrix_covers_exact_catalog():
    from vcstudio.generate import task_catalog

    catalog_keys = {row['key'] for row in task_catalog.CATALOG}
    matrix = task_analysis.capability_matrix()
    assert set(matrix) == catalog_keys
    assert len(matrix) == 23
    assert all(row['report_supported'] for row in matrix.values())
    assert matrix['bands']['analysis_status'] == 'integrated'
    assert matrix['bader']['analysis_status'] == 'evidence_only'
    assert matrix['surface_energy']['analysis_status'] == 'dedicated'


def test_api_exposes_full_capability_matrix():
    out = Api().task_capabilities()
    assert out['ok'] is True
    assert len(out['capabilities']) == 23
    assert out['capabilities']['adsorption_project']['next_action']


def test_generic_vasp_analysis_is_evidence_based(tmp_path):
    (tmp_path / 'OSZICAR').write_text(_oszicar([-10.0, -10.25]), encoding='utf-8')
    (tmp_path / 'OUTCAR').write_text(
        ' reached required accuracy - stopping structural energy minimisation\n'
        ' General timing and accounting informations for this job:\n', encoding='utf-8')
    out = Api().analyze_task(str(tmp_path), kind='relax')
    assert out['ok'] is True
    assert out['supported'] is True
    assert out['analysis_status'] == 'integrated'
    assert out['result']['final_energy_e0_ev'] == -10.25
    assert out['result']['clean_exit'] is True
    assert out['result']['ionic_converged_marker'] is True


def test_artifact_only_analysis_says_it_is_not_quantitative(tmp_path):
    (tmp_path / 'ELFCAR').write_text('real artifact', encoding='utf-8')
    out = Api().analyze_task(str(tmp_path), kind='elf')
    assert out['ok'] is True
    assert out['analysis_status'] == 'evidence_only'
    assert out['result']['present'] == ['ELFCAR']
    assert '未生成定量科学结论' in out['summary']


def test_bader_analysis_uses_real_zval_and_charge(tmp_path):
    (tmp_path / 'ACF.dat').write_text(
        '# X Y Z CHARGE MIN DIST ATOMIC VOL\n'
        ' 1 0.0 0.0 0.0 3.7500 0.5 10.0\n'
        ' --------------------------------------------------\n', encoding='utf-8')
    (tmp_path / 'POTCAR').write_text(
        ' POMASS = 28.085; ZVAL = 4.000 mass and valenz\n', encoding='utf-8')
    (tmp_path / 'POSCAR').write_text(
        'Si\n1.0\n2 0 0\n0 2 0\n0 0 2\nSi\n1\nDirect\n0 0 0\n',
        encoding='utf-8')
    out = Api().analyze_task(str(tmp_path), kind='bader')
    assert out['ok'] is True
    assert out['result']['delta_q'] == [0.25]
    assert out['result']['sum_delta_q'] == 0.25
    assert out['analysis_status'] == 'evidence_only'
    assert 'Henkelman Bader' in out['next_action']


def test_chgdiff_analysis_returns_plane_average_from_real_grid(tmp_path):
    (tmp_path / 'CHGDIFF.vasp').write_text(
        'diff\n1.0\n2 0 0\n0 2 0\n0 0 4\nH\n1\nDirect\n0 0 0\n\n'
        '1 1 2\n1.0 -1.0\n', encoding='utf-8')
    out = Api().analyze_task(str(tmp_path), kind='chgdiff')
    assert out['ok'] is True
    assert out['result']['n_points'] == 2
    assert out['result']['rho_e_a3'] == [0.0625, -0.0625]
    assert out['analysis_status'] == 'integrated'


def test_dedicated_multi_job_task_does_not_fake_single_job_result(tmp_path):
    out = Api().analyze_task(str(tmp_path), kind='surface_energy')
    assert out['ok'] is False
    assert out['supported'] is False
    assert out['analysis_status'] == 'dedicated'
    assert '专用多作业流程' in out['error']
    assert '表面能计算器' in out['next_action']


def test_frequency_analysis_includes_imaginary_mode_gate(tmp_path):
    (tmp_path / 'OUTCAR').write_text(
        '   1 f  =   10.000000 THz   62.831853 2PI*THz  333.564095 cm-1    41.356676 meV\n'
        '   2 f/i=    0.500000 THz    3.141593 2PI*THz   16.678205 cm-1     2.067834 meV\n',
        encoding='utf-8')
    out = Api().analyze_task(str(tmp_path), kind='freq')
    assert out['ok'] is True
    assert out['result']['imaginary_gate']['verdict'] == 'noise'
    assert out['result']['g_corr_ev'] is not None


def test_trace_report_contains_analysis_and_file_hash(tmp_path):
    (tmp_path / 'OSZICAR').write_text(_oszicar([-1.0]), encoding='utf-8')
    report = tmp_path / 'reports' / 'static.json'
    out = Api().task_report(str(tmp_path), str(report), kind='static')
    assert out['ok'] is True and out['analysis_ok'] is True
    payload = json.loads(report.read_text(encoding='utf-8'))
    assert payload['schema'] == 'vcstudio.task-report.v1'
    assert payload['task_key'] == 'static'
    evidence = {row['name']: row for row in payload['evidence']['files']}
    assert len(evidence['OSZICAR']['sha256']) == 64
    assert payload['analysis']['result']['final_energy_e0_ev'] == -1.0


def test_trace_report_can_record_unsupported_analysis_honestly(tmp_path):
    report = tmp_path / 'surface.md'
    out = Api().task_report(str(tmp_path), str(report), kind='surface_energy')
    assert out['ok'] is True
    assert out['analysis_ok'] is False
    text = report.read_text(encoding='utf-8')
    assert 'dedicated' in text
    assert '不能从一个目录独立得出结论' in text


def test_unknown_task_is_not_silently_treated_as_vasp(tmp_path):
    (tmp_path / 'OSZICAR').write_text(_oszicar([-2.0]), encoding='utf-8')
    out = Api().analyze_task(str(tmp_path), kind='made_up')
    assert out['ok'] is False
    assert out['analysis_status'] == 'unsupported'
    assert out['report_supported'] is False


@pytest.mark.parametrize(
    ('engine', 'input_name', 'output_name', 'output_text', 'expected_ev'),
    [
        ('gaussian', 'mol.gjf', 'mol.log',
         ' SCF Done:  E(RPBE) =  -2.0000000000 A.U.\n'
         ' Normal termination of Gaussian 16\n', -2.0 * 27.211386),
        ('cp2k', 'cp2k.inp', 'cp2k.out',
         ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -3.0000000000\n'
         ' SCF run converged\n GEOMETRY OPTIMIZATION COMPLETED\n', -3.0 * 27.211386),
        ('castep', 'case.cell', 'case.castep',
         ' Final energy, E = -123.456 eV\n Total time = 4.2 s\n', -123.456),
    ],
)
def test_non_vasp_analysis_uses_declared_engine_output(
        tmp_path, engine, input_name, output_name, output_text, expected_ev):
    root = tmp_path / engine
    root.mkdir()
    (root / input_name).write_text('declared input\n', encoding='utf-8')
    (root / output_name).write_text(output_text, encoding='utf-8')
    m = manifest.new_manifest(
        job_id=f'{engine}-1', system='demo', task_type='relax', calc_type='molecule',
        inputs={'engine': engine, 'task': 'relax', 'files': [input_name],
                'output_files': [output_name]}, warnings=[])
    manifest.set_state(m, 'DONE', note='test completion evidence')
    m['results']['diagnosis'] = {
        'failure_class': 'CONVERGED', 'evidence': 'normal termination',
        'output_file': output_name, 'output_bytes': len(output_text.encode()),
    }
    manifest.save_manifest(root, m)

    out = Api().analyze_task(str(root))
    assert out['ok'] is True
    assert out['result']['engine'] == engine
    assert out['result']['energy_ev'] == pytest.approx(expected_ev)
    assert out['result']['converged'] is True
    assert out['result']['completion_evidence']['manifest_state'] == 'DONE'
    assert not (root / 'OSZICAR').exists()

    report = root / 'report.json'
    made = Api().task_report(str(root), str(report))
    assert made['ok'] is True and made['analysis_ok'] is True
    evidence = {row['name']: row for row in
                json.loads(report.read_text(encoding='utf-8'))['evidence']['files']}
    assert len(evidence[input_name]['sha256']) == 64
    assert len(evidence[output_name]['sha256']) == 64


def test_non_vasp_analysis_refuses_undeclared_stale_output(tmp_path):
    (tmp_path / 'old.out').write_text(
        ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -9.0\n'
        ' SCF run converged\n', encoding='utf-8')
    m = manifest.new_manifest(
        job_id='cp2k-declared', system='demo', task_type='static', calc_type='bulk',
        inputs={'engine': 'cp2k', 'files': ['cp2k.inp'],
                'output_files': ['cp2k.out']}, warnings=[])
    manifest.save_manifest(tmp_path, m)
    out = Api().analyze_task(str(tmp_path))
    assert out['ok'] is False
    assert '清单声明的主输出尚未下载' in out['error']


def test_unknown_manifest_engine_is_not_treated_as_vasp(tmp_path):
    (tmp_path / 'OSZICAR').write_text(_oszicar([-9.0]), encoding='utf-8')
    m = manifest.new_manifest(
        job_id='unknown-engine', system='demo', task_type='static', calc_type='bulk',
        inputs={'engine': 'orca', 'files': ['calc.inp']}, warnings=[])
    manifest.save_manifest(tmp_path, m)
    out = Api().analyze_task(str(tmp_path))
    assert out['ok'] is False and out['supported'] is False
    assert '未接入计算引擎' in out['error']


def _poscar(comment='H2'):
    return (f'{comment}\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n2\nDirect\n'
            '0.10 0.5 0.5\n0.30 0.5 0.5\n')


def test_engine_generate_creates_standard_manifest_and_ledger_for_all_file_engines(tmp_path):
    source = tmp_path / 'POSCAR'
    source.write_text(_poscar('molecule'), encoding='utf-8')
    registered = []
    ledger = type('Ledger', (), {'register': staticmethod(
        lambda job_dir: registered.append(job_dir) or True)})
    api = Api(ledger_mod=ledger)
    cases = {
        'gaussian': {'periodic': False, 'task': 'static',
                     'functional': 'PBE', 'extras': {'basis': 'def2-SVP'}},
        'cp2k': {'periodic': True, 'task': 'relax', 'functional': 'PBE',
                 'cutoff_ev': 500, 'kpoints': [1, 1, 1],
                 'extras': {'cutoff_ry': 400}},
        'castep': {'periodic': True, 'task': 'static', 'functional': 'PBE',
                   'cutoff_ev': 500, 'kpoints': [1, 1, 1]},
    }
    for engine, params in cases.items():
        out_dir = tmp_path / f'{engine}_job'
        out = api.engine_generate(engine, {'poscar': str(source), **params}, str(out_dir))
        assert out['ok'] is True and out['registered'] is True
        m = manifest.load_manifest(out_dir)
        assert m['state'] == 'CREATED'
        assert m['inputs']['engine'] == engine
        assert m['inputs']['files']
        assert m['inputs']['output_files']
        assert set(m['inputs']['sha256']) == set(m['inputs']['files'])
        assert registered[-1] == str(out_dir)


def test_derive_neb_copies_endpoint_evidence_and_parser_merges_manifest_fallback(tmp_path):
    start, end = tmp_path / 'start', tmp_path / 'end'
    start.mkdir()
    end.mkdir()
    start_text = _poscar('H2 start')
    end_text = start_text.replace('0.30 0.5 0.5', '0.55 0.5 0.5')
    (start / 'CONTCAR').write_text(start_text, encoding='utf-8')
    (end / 'CONTCAR').write_text(end_text, encoding='utf-8')
    (start / 'INCAR').write_text('ENCUT = 400\nISMEAR = 0\n', encoding='utf-8')
    (start / 'POTCAR').write_text('H POTCAR\n', encoding='utf-8')
    (start / 'OSZICAR').write_text(_oszicar([-10.0]), encoding='utf-8')
    (end / 'OSZICAR').write_text(_oszicar([-9.8]), encoding='utf-8')
    (start / 'OUTCAR').write_text('start evidence\n', encoding='utf-8')
    (end / 'OUTCAR').write_text('end evidence\n', encoding='utf-8')
    (start / 'vasprun.xml').write_text('<start/>\n', encoding='utf-8')
    (end / 'vasprun.xml').write_text('<end/>\n', encoding='utf-8')
    registered = []
    ledger = type('Ledger', (), {'register': staticmethod(
        lambda job_dir: registered.append(job_dir) or True)})
    made = Api(ledger_mod=ledger).derive_neb(str(start), str(end), nimages=3)
    assert made['ok'] is True
    root = tmp_path / 'start_neb'
    assert (root / '00' / 'OSZICAR').is_file()
    assert (root / '04' / 'OUTCAR').is_file()
    assert (root / '04' / 'vasprun.xml').is_file()
    m = manifest.load_manifest(root)
    endpoints = m['inputs']['neb_endpoints']
    assert endpoints['start']['energy_e0_eV'] == -10.0
    assert endpoints['end']['energy_e0_eV'] == -9.8
    assert endpoints['start']['trusted'] is True
    assert len(endpoints['start']['files'][0]['sha256']) == 64

    for frame, energy in zip(('01', '02', '03'), (-9.7, -9.4, -9.65)):
        (root / frame / 'OSZICAR').write_text(_oszicar([energy]), encoding='utf-8')
    # 模拟回收时远端未返回端点输出：只允许用生成阶段已哈希的 manifest 证据。
    os.unlink(root / '00' / 'OSZICAR')
    os.unlink(root / '00' / 'OUTCAR')
    os.unlink(root / '04' / 'OSZICAR')
    os.unlink(root / '04' / 'OUTCAR')
    analyzed = Api().analyze_task(str(root), kind='neb')
    assert analyzed['ok'] is True
    assert analyzed['result']['energies'] == [-10.0, -9.7, -9.4, -9.65, -9.8]
    assert analyzed['result']['barrier_f'] == pytest.approx(0.6)
    assert analyzed['result']['energy_sources'][0].startswith('copied:')
    assert any('job.yaml' in warning for warning in analyzed['result']['warnings'])


def test_neb_parser_rejects_untrusted_manifest_energy(tmp_path):
    from vcstudio.project import neb

    for frame in ('00', '01', '02'):
        (tmp_path / frame).mkdir()
    (tmp_path / '01' / 'OSZICAR').write_text(_oszicar([-1.0]), encoding='utf-8')
    (tmp_path / '02' / 'OSZICAR').write_text(_oszicar([-1.2]), encoding='utf-8')
    m = manifest.new_manifest(
        job_id='neb-untrusted', system='demo', task_type='neb', calc_type='slab',
        inputs={'n_images': 1, 'neb_endpoints': {
            'start': {'target_frame': '00', 'energy_e0_eV': -2.0,
                      'energy_source': 'copied:OSZICAR:E0', 'trusted': False,
                      'files': [{'name': 'OSZICAR', 'sha256': '0' * 64}]}}},
        warnings=[])
    manifest.save_manifest(tmp_path, m)
    with pytest.raises(ValueError, match='无法读取初态'):
        neb.parse_neb_energies(tmp_path)
