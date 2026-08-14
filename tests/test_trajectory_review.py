from __future__ import annotations

import json
from pathlib import Path

import pytest

from vcstudio.project.trajectory_review import TrajectoryReviewService
from vcstudio.shared import manifest


_HEADER = (
    'trajectory\n1.0\n5 0 0\n0 5 0\n0 0 5\nH He\n1 1\n'
)


def _poscar(x=0.25) -> str:
    return _HEADER + f'Direct\n0 0 0\n{x} {x} {x}\n'


def _xdatcar(count: int, *, incomplete=False) -> str:
    chunks = [_HEADER]
    for step in range(1, count + 1):
        value = 0.20 + step * 0.001
        chunks.append(
            f'Direct configuration= {step}\n0 0 0\n{value:.6f} {value:.6f} {value:.6f}\n')
    if incomplete:
        chunks.append(f'Direct configuration= {count + 1}\n0 0 0\n0.4 0.4 0.4')
    return ''.join(chunks)


def _oszicar(count: int, *, newline=True) -> str:
    lines = [
        f' {step:4d} T= {300 + step:.1f} E= {-10 + step * .01:.6f} '
        f'F= {-10 + step * .01:.6f} E0= {-10 + step * .01:.6f} d E =0.0'
        for step in range(1, count + 1)
    ]
    return '\n'.join(lines) + ('\n' if newline else '')


def _outcar(count: int) -> str:
    block = (
        ' POSITION                                       TOTAL-FORCE (eV/Angst)\n'
        ' -----------------------------------------------------------------------------------\n'
        ' 0 0 0 0.010 0.000 0.000\n'
        ' 1 1 1 0.000 0.020 0.000\n'
        ' -----------------------------------------------------------------------------------\n'
    )
    return block * count + ' General timing and accounting informations for this job:\n'


def _job(tmp_path: Path, *, task='aimd', steps=3, incomplete=False) -> Path:
    root = tmp_path / task
    root.mkdir()
    value = manifest.new_manifest(
        job_id=f'{task}-job', system='HHe', task_type=task,
        calc_type='slab', inputs={'engine': 'vasp'},
    )
    manifest.save_manifest(root, value)
    (root / 'INCAR').write_text('ENCUT = 400\nALGO = Fast\n', encoding='utf-8')
    (root / 'POSCAR').write_text(_poscar(), encoding='utf-8')
    (root / 'CONTCAR').write_text(_poscar(.30), encoding='utf-8')
    (root / 'OSZICAR').write_text(
        _oszicar(steps, newline=not incomplete), encoding='utf-8')
    (root / 'OUTCAR').write_text(_outcar(steps), encoding='utf-8')
    (root / 'XDATCAR').write_text(
        _xdatcar(steps, incomplete=incomplete), encoding='utf-8')
    return root


def _assert_path_free(payload, root: Path):
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    assert str(root) not in encoded
    assert root.as_posix() not in encoded


def test_aimd_snapshot_pages_frames_and_authoritative_summary_are_path_free(tmp_path):
    root = _job(tmp_path)
    service = TrajectoryReviewService()

    opened = service.open(root, 'aimd-job')
    assert opened['ok'] is True
    assert opened['n_steps'] == opened['n_frames'] == 3
    assert opened['analysis']['source'].endswith('analyze_aimd')
    assert opened['analysis']['energy_drift_total_ev'] == pytest.approx(0.02)
    assert len(opened['source_hash']) == 64
    _assert_path_free(opened, root)

    page = service.steps(opened['session_token'], offset=0, limit=2)
    assert page['sampled_steps'] == 3 and page['next_offset'] == 2
    assert [row['step'] for row in page['rows']] == [1, 2]
    assert page['rows'][0]['energy_ev'] == -9.99
    assert page['rows'][0]['temperature_k'] == 301.0
    assert page['rows'][0]['fmax_ev_a'] == 0.02
    assert page['rows'][0]['frame_token'].startswith('frame-')
    _assert_path_free(page, root)

    frame = service.frame(page['rows'][0]['frame_token'])
    assert frame['ok'] is True and frame['view']['natoms'] == 2
    assert frame['view']['xyz'].splitlines()[0] == '2'
    assert frame['metrics']['distance_status'] == 'available'
    _assert_path_free(frame, root)


def test_large_trajectory_is_bounded_in_browser_dto_and_server_sampled(tmp_path):
    root = _job(tmp_path, steps=2501)
    service = TrajectoryReviewService()

    opened = service.open(root, 'aimd-job')
    assert opened['n_steps'] == 2501
    assert len(opened['plot']['points']) <= 1000
    assert opened['plot']['sampling_stride'] > 1

    page = service.steps(opened['session_token'], limit=200, stride=100)
    assert page['total_steps'] == 2501
    assert page['sampled_steps'] == 26
    assert len(page['rows']) == 26
    assert [row['step'] for row in page['rows'][:3]] == [1, 101, 201]
    encoded = json.dumps(page)
    assert 'Direct configuration' not in encoded
    assert len(encoded) < 100_000


def test_partial_write_withholds_incomplete_frame_and_stale_is_explicit(tmp_path):
    root = _job(tmp_path, steps=3, incomplete=True)
    service = TrajectoryReviewService()
    opened = service.open(root, 'aimd-job')
    assert opened['partial_write'] is True
    assert opened['n_steps'] == 2
    assert opened['n_frames'] == 3
    assert any('末帧' in warning for warning in opened['warnings'])

    with (root / 'OSZICAR').open('a', encoding='utf-8') as handle:
        handle.write('\n')
    stale = service.steps(opened['session_token'])
    assert stale['ok'] is False and stale['stale'] is True
    assert stale['source_hash'] == opened['source_hash']


def test_neb_navigation_consumes_existing_parser_without_second_barrier_formula(tmp_path):
    root = tmp_path / 'neb'
    root.mkdir()
    value = manifest.new_manifest(
        job_id='neb-job', system='HHe', task_type='neb', calc_type='slab',
        inputs={'engine': 'vasp'},
    )
    manifest.save_manifest(root, value)
    (root / 'INCAR').write_text('IMAGES = 1\n', encoding='utf-8')
    energies = (-10.0, -9.5, -9.8)
    for index, energy in enumerate(energies):
        frame = root / f'{index:02d}'
        frame.mkdir()
        (frame / 'POSCAR').write_text(_poscar(.2 + index * .03), encoding='utf-8')
        (frame / 'OSZICAR').write_text(
            f' 1 F= {energy} E0= {energy} d E =0.0\n', encoding='utf-8')
        (frame / 'OUTCAR').write_text(_outcar(1), encoding='utf-8')

    service = TrajectoryReviewService()
    opened = service.open(root, 'neb-job')
    assert opened['task_kind'] == 'neb'
    assert opened['analysis']['source'].endswith('parse_neb_energies')
    assert opened['analysis']['barrier_forward_ev'] == 0.5
    page = service.steps(opened['session_token'])
    assert [row['image'] for row in page['rows']] == ['00', '01', '02']
    assert [row['relative_energy_ev'] for row in page['rows']] == pytest.approx([0.0, 0.5, 0.2])


def test_repair_preview_defaults_pause_and_only_authorizes_frozen_incar_seam(tmp_path):
    root = _job(tmp_path, task='relax')
    value = manifest.load_manifest(root)
    value['state'] = 'UNCONVERGED'
    value['cluster'] = 'cluster-a'
    value['remote_dir'] = '/remote/job'
    value['scheduler_job_id'] = '100'
    value['results']['diagnosis'] = {
        'failure_class': 'NONCONVERGED', 'restartable': True,
        'evidence': '有输出但未见收敛标志',
    }
    manifest.save_manifest(root, value)

    service = TrajectoryReviewService()
    opened = service.open(root, 'relax-job')
    preview = service.repair_preview(opened['session_token'])
    assert preview['default_decision'] == 'pause'
    assert preview['execution_action'] == 'continue_frozen_incar'
    assert preview['execution_allowed'] is True
    assert preview['method_diff'] == []
    assert preview['estimated_cost']['exact_machine_time'] is None

    prepared = service.prepare_repair(
        preview['plan_token'], 'continue_frozen_incar', 'trajectory-repair:123456')
    assert prepared['intent_record_hash']
    before = (root / 'INCAR').read_bytes()
    outcome = service.record_repair_outcome(prepared, {
        'ok': True, 'results': [[str(root), True, 'continued']],
    })
    assert outcome['status'] == 'applied'
    assert outcome['method_compatibility'] == 'unchanged'
    assert (root / 'INCAR').read_bytes() == before

    reopened = service.open(root, 'relax-job')
    compatibility = reopened['method_compatibility']
    assert compatibility['status'] == 'verified_unchanged'
    assert compatibility['record_count'] == 2
    records = list((root / '.vcstudio-corrections').glob('*.json'))
    assert len(records) == 2


def test_unknown_and_manual_parameter_guidance_never_authorize_execution(tmp_path):
    root = _job(tmp_path, task='relax')
    value = manifest.load_manifest(root)
    value['state'] = 'NEEDS_HUMAN'
    value['results']['diagnosis'] = {
        'failure_class': 'SCF_SLOSHING', 'restartable': False,
        'evidence': 'SCF dE 不下降',
    }
    manifest.save_manifest(root, value)

    service = TrajectoryReviewService()
    opened = service.open(root, 'relax-job')
    preview = service.repair_preview(opened['session_token'])
    assert preview['execution_allowed'] is False
    assert preview['execution_action'] == 'pause'
    assert preview['method_diff']
    assert all(row['execution'] == 'manual_only' for row in preview['method_diff'])

    value['results']['diagnosis'] = {
        'failure_class': 'UNKNOWN', 'restartable': True, 'evidence': '规则未覆盖'}
    manifest.save_manifest(root, value)
    opened = service.open(root, 'relax-job')
    unknown = service.repair_preview(opened['session_token'])
    assert unknown['unknown_pauses'] is True
    assert unknown['execution_allowed'] is False
