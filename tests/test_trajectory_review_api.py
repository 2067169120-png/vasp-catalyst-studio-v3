from __future__ import annotations

import json
import types

from vcstudio.gui_web.api import Api


def _ledger(job_dir, *, duplicate=False):
    value = {
        'job_id': 'opaque-job', 'job_uuid': '', 'task_type': 'aimd',
        'calc_type': 'slab', 'state': 'UNCONVERGED', 'inputs': {'engine': 'vasp'},
        'results': {},
    }
    entries = [(str(job_dir), value)]
    if duplicate:
        entries.append((str(job_dir) + '-duplicate', dict(value)))
    return types.SimpleNamespace(load_all=lambda: list(entries))


class _Trajectory:
    def __init__(self, job_dir):
        self.job_dir = job_dir
        self.calls = []
        self.outcomes = []

    def open(self, job_dir, job_id, kind=None):
        self.calls.append(('open', job_dir, job_id, kind))
        return {'ok': True, 'job_id': job_id, 'session_token': 'trajectory-token'}

    def steps(self, token, **kwargs):
        self.calls.append(('steps', token, kwargs))
        return {'ok': True, 'rows': []}

    def frame(self, token):
        self.calls.append(('frame', token))
        return {'ok': True, 'view': {'xyz': '0\nframe\n'}}

    def repair_preview(self, token):
        self.calls.append(('preview', token))
        return {'ok': True, 'default_decision': 'pause'}

    def prepare_repair(self, token, decision, key):
        self.calls.append(('prepare', token, decision, key))
        return {
            'job_id': 'opaque-job', 'job_dir': self.job_dir,
            'operation_key': key, 'plan_id': 'p' * 64,
            'correction_id': 'correction-id', 'intent_record_hash': 'i' * 64,
            'incar_sha256_before': 'a' * 64,
        }

    def record_repair_outcome(self, prepared, outcome):
        self.outcomes.append((prepared, outcome))
        return {'record_id': 'correction-id', 'record_hash': 'o' * 64,
                'status': 'applied', 'method_compatibility': 'unchanged'}


def test_trajectory_api_resolves_only_registered_opaque_job_ids(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)

    opened = api.trajectory_open('opaque-job')
    assert opened['ok'] is True
    assert service.calls == [('open', str(job), 'opaque-job', None)]

    rejected = api.trajectory_open(str(job))
    assert rejected['ok'] is False
    assert str(job) not in json.dumps(rejected)
    assert len(service.calls) == 1


def test_trajectory_api_fails_closed_on_duplicate_ledger_identity(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(
        ledger_mod=_ledger(job, duplicate=True),
        trajectory_review_service=service,
    )
    result = api.trajectory_open('opaque-job')
    assert result['ok'] is False
    assert 'ambiguous' in result['error']
    assert service.calls == []


def test_trajectory_api_pages_and_frames_forward_only_tokens(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)

    assert api.trajectory_steps('trajectory-token', 5, 20, 10)['ok'] is True
    assert api.trajectory_frame('frame-token')['ok'] is True
    assert api.trajectory_repair_preview('trajectory-token')['default_decision'] == 'pause'
    assert service.calls == [
        ('steps', 'trajectory-token', {'offset': 5, 'limit': 20, 'stride': 10}),
        ('frame', 'frame-token'), ('preview', 'trajectory-token'),
    ]


def test_confirmed_repair_uses_existing_idempotent_continue_and_redacts_path(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)
    captured = {}

    def _continue(dirs, name, password, trust_new=False, idempotency_key=None):
        captured.update(dirs=dirs, name=name, password=password,
                        trust_new=trust_new, idempotency_key=idempotency_key)
        return {
            'ok': True, 'idempotency_key': idempotency_key,
            'results': [(str(job), True, '已续算重投，新作业号 101')],
        }

    api.continue_jobs = _continue
    result = api.trajectory_confirm_repair(
        'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
        'trajectory-repair:123456')

    assert captured['dirs'] == [str(job)]
    assert captured['idempotency_key'] == 'trajectory-repair:123456'
    assert result['results'] == [
        {'job_id': 'opaque-job', 'ok': True,
         'message': '已续算重投，新作业号 101'}]
    assert result['correction_outcome']['method_compatibility'] == 'unchanged'
    assert len(service.outcomes) == 1
    assert str(job) not in json.dumps(result, ensure_ascii=False)
    assert 'secret' not in json.dumps(result, ensure_ascii=False)


def test_host_trust_retry_does_not_finalize_correction_outcome(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)
    api.continue_jobs = lambda *_args, **_kwargs: {
        'ok': False, 'needs_trust': True,
        'fingerprint': 'SHA256:host', 'algorithm': 'ssh-ed25519', 'host': 'cluster',
        'results': [],
    }
    result = api.trajectory_confirm_repair(
        'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
        'trajectory-repair:123456')
    assert result['needs_trust'] is True
    assert result['correction_intent']['status'] == 'prepared'
    assert 'correction_outcome' not in result
    assert service.outcomes == []
