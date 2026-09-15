from __future__ import annotations

import json
import types

import pytest

from vcstudio.gui_web.api import Api


_REQUEST_FINGERPRINT = 'f' * 64
_SOURCE_HASH = 's' * 64


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
        self.cas_error = None

    def open(self, job_dir, job_id, kind=None):
        self.calls.append(('open', job_dir, job_id, kind))
        return {'ok': True, 'job_id': job_id, 'session_token': 'trajectory-token'}

    def steps(self, token, **kwargs):
        self.calls.append(('steps', token, kwargs))
        return {'ok': True, 'rows': []}

    def frame(self, token):
        self.calls.append(('frame', token))
        return {'ok': True, 'view': {'xyz': '0\nframe\n'}}

    def repair_preview(self, token, *, allow_round_limit_override=False):
        self.calls.append(('preview', token, allow_round_limit_override))
        return {
            'ok': True, 'default_decision': 'pause',
            'session_token': token, 'source_hash': _SOURCE_HASH,
            'request_fingerprint': _REQUEST_FINGERPRINT,
        }

    def prepare_repair(self, token, decision, key, *,
                       allow_round_limit_override=False,
                       request_fingerprint=None):
        self.calls.append((
            'prepare', token, decision, key,
            allow_round_limit_override, request_fingerprint))
        return {
            'job_id': 'opaque-job', 'job_dir': self.job_dir,
            'operation_key': key, 'plan_id': 'p' * 64,
            'correction_id': 'correction-id', 'intent_record_hash': 'i' * 64,
            'incar_sha256_before': 'a' * 64,
            'journal_binding': {'schema': 'correction-link'},
            'request_fingerprint': request_fingerprint,
        }

    def record_repair_outcome(self, prepared, outcome):
        self.outcomes.append((prepared, outcome))
        return {'record_id': 'correction-id', 'record_hash': 'o' * 64,
                'status': 'applied', 'method_compatibility': 'unchanged'}

    def assert_repair_cas(self, prepared, *, ledger_job_id, manifest=None):
        self.calls.append(('cas', prepared['job_id'], ledger_job_id, manifest))
        if self.cas_error:
            raise RuntimeError(self.cas_error)
        return {
            'schema': 'vcstudio.repair-cas/v1', 'ledger_job_id': ledger_job_id,
            'manifest_job_id': ledger_job_id, 'manifest_state': 'UNCONVERGED',
            'scheduler_job_id': '100', 'diagnosis_sha256': 'd' * 64,
            'diagnosis_evidence_file': None, 'source_hash': 's' * 64,
            'files': {},
        }


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
        ('frame', 'frame-token'), ('preview', 'trajectory-token', True),
    ]


def test_confirmed_repair_uses_existing_idempotent_continue_and_redacts_path(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)
    captured = {}

    def _continue(dirs, name, password, trust_new=False, idempotency_key=None,
                  expected_cas_by_job=None, expected_correction_by_job=None,
                  allow_round_limit_override=False):
        captured.update(dirs=dirs, name=name, password=password,
                        trust_new=trust_new, idempotency_key=idempotency_key,
                        expected_cas_by_job=expected_cas_by_job,
                        allow_round_limit_override=allow_round_limit_override,
                        expected_correction_by_job=expected_correction_by_job)
        return {
            'ok': True, 'idempotency_key': idempotency_key,
            'results': [(str(job), True, '已续算重投，新作业号 101')],
        }

    api.continue_jobs = _continue
    result = api.trajectory_confirm_repair(
        'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
        'trajectory-repair:123456', request_fingerprint=_REQUEST_FINGERPRINT)

    assert captured['dirs'] == [str(job)]
    assert captured['idempotency_key'] == 'trajectory-repair:123456'
    assert list(captured['expected_correction_by_job'].values()) == [
        {'schema': 'correction-link'}]
    assert list(captured['expected_cas_by_job'].values())[0]['source_hash'] == (
        _SOURCE_HASH)
    assert captured['allow_round_limit_override'] is True
    assert result['results'] == [
        {'job_id': 'opaque-job', 'ok': True,
         'message': '已续算重投，新作业号 101'}]
    assert result['correction_outcome']['method_compatibility'] == 'unchanged'
    assert len(service.outcomes) == 1
    assert any(call[0] == 'cas' for call in service.calls)
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
        'trajectory-repair:123456', request_fingerprint=_REQUEST_FINGERPRINT)
    assert result['needs_trust'] is True
    assert result['correction_intent']['status'] == 'prepared'
    assert 'correction_outcome' not in result
    assert service.outcomes == []


def test_confirm_rejects_changed_cas_before_continue_seam(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    service.cas_error = 'same-size content replacement detected'
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)
    called = []
    api.continue_jobs = lambda *_args, **_kwargs: called.append(True) or {'results': []}

    result = api.trajectory_confirm_repair(
        'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
        'trajectory-repair:123456', request_fingerprint=_REQUEST_FINGERPRINT)

    assert result['ok'] is False
    assert 'replacement detected' in result['error']
    assert called == []


def test_confirm_requires_request_fingerprint_before_preparing_intent(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)

    result = api.trajectory_confirm_repair(
        'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
        'trajectory-repair:123456')

    assert result['ok'] is False
    assert 'fingerprint' in result['error']
    assert not any(call[0] == 'prepare' for call in service.calls)


def test_legacy_continue_adapter_missing_cas_or_correction_fails_before_prepare(
        tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    service = _Trajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)
    called = []

    def legacy_continue(dirs, name, password, trust_new=False,
                        idempotency_key=None):
        called.append((dirs, name, password, trust_new, idempotency_key))
        return {'ok': True, 'results': []}

    api.continue_jobs = legacy_continue
    result = api.trajectory_confirm_repair(
        'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
        'trajectory-repair:123456', request_fingerprint=_REQUEST_FINGERPRINT)

    assert result['ok'] is False
    assert 'required server-owned repair bindings' in result['error']
    assert called == []
    assert not any(call[0] == 'prepare' for call in service.calls)


def test_confirm_reconciles_durable_outcome_before_credentials_or_remote_seam(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()

    class ReplayingTrajectory(_Trajectory):
        def reconcile_repair_outcome(self, job_dir, job_id, token, key):
            self.calls.append(('reconcile', job_dir, job_id, token, key))
            return {
                'prepared': {
                    'job_id': job_id, 'intent_record_hash': 'i' * 64,
                    'request_fingerprint': _REQUEST_FINGERPRINT,
                },
                'operation': {
                    'ok': True, 'replayed': True,
                    'results': [[job_dir, True, '已确认续算，新作业号 101']],
                },
                'correction_outcome': {
                    'record_id': 'a' * 24, 'record_hash': 'o' * 64,
                    'status': 'applied', 'method_compatibility': 'unchanged',
                },
            }

        def prepare_repair(self, *_args):
            raise AssertionError('durable replay must precede live plan/CAS')

    service = ReplayingTrajectory(str(job))
    api = Api(ledger_mod=_ledger(job), trajectory_review_service=service)
    api.continue_jobs = lambda *_args, **_kwargs: pytest.fail(
        'durable replay must not reach remote continuation')
    result = api.trajectory_confirm_repair(
        'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
        'trajectory-repair:123456', 'opaque-job', _REQUEST_FINGERPRINT)
    assert result['replayed'] is True
    assert result['correction_outcome']['status'] == 'applied'
    assert result['results'][0]['job_id'] == 'opaque-job'
    assert str(job) not in json.dumps(result, ensure_ascii=False)


def test_every_success_dto_is_recursively_public_projected(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()

    class LeakyTrajectory(_Trajectory):
        def open(self, job_dir, job_id, kind=None):
            return {
                'ok': True, 'job_id': job_id, 'session_token': 'trajectory-token',
                'warnings': [f'qsub {job_dir} --token=sk-very-secret'],
                'analysis': {'quality_gate': {'issues': [f'failed at {job_dir}']}},
                'path': job_dir,
                'nested': {
                    'token': 'raw-provider-token',
                    'attempt_token': 'remote-attempt-capability',
                    'private_key': 'private-key-material',
                    'command': f'qsub {job_dir}',
                    'safe': 'password=hunter2',
                },
            }

        def repair_preview(self, token, *, allow_round_limit_override=False):
            return {
                'ok': True, 'detected_evidence': f'ssh {job} password=hunter2',
                'default_decision': 'pause',
                'session_token': token, 'source_hash': _SOURCE_HASH,
                'request_fingerprint': _REQUEST_FINGERPRINT,
            }

    api = Api(
        ledger_mod=_ledger(job), trajectory_review_service=LeakyTrajectory(str(job)))
    opened = api.trajectory_open('opaque-job')
    preview = api.trajectory_repair_preview('trajectory-token')
    frame = api.trajectory_frame('frame-token')
    encoded = json.dumps([opened, preview, frame], ensure_ascii=False)

    assert str(job) not in encoded
    assert 'very-secret' not in encoded
    assert 'hunter2' not in encoded
    assert 'raw-provider-token' not in encoded
    assert 'remote-attempt-capability' not in encoded
    assert 'private-key-material' not in encoded
    assert 'qsub' not in encoded and 'ssh ' not in encoded
    assert 'path' not in opened
    assert frame['view']['xyz'] == '0\nframe\n'


def test_every_trajectory_exception_uses_the_same_recursive_public_sanitizer(
        tmp_path):
    job = tmp_path / 'private-job'
    job.mkdir()
    leak = (
        f'failed at {job} password=hunter2 token=provider-token; '
        f'qsub {job} --auth sk-super-secret; '
        '-----BEGIN OPENSSH PRIVATE KEY----- keybits '
        '-----END OPENSSH PRIVATE KEY-----')

    class FailingTrajectory(_Trajectory):
        def open(self, *_args, **_kwargs):
            raise RuntimeError(leak)

        def steps(self, *_args, **_kwargs):
            raise RuntimeError(leak)

        def frame(self, *_args, **_kwargs):
            raise RuntimeError(leak)

        def repair_preview(self, *_args, **_kwargs):
            raise RuntimeError(leak)

        def prepare_repair(self, *_args, **_kwargs):
            raise RuntimeError(leak)

    api = Api(
        ledger_mod=_ledger(job), trajectory_review_service=FailingTrajectory(str(job)))
    api.continue_jobs = lambda *_args, **_kwargs: {'ok': True, 'results': []}
    failures = [
        api.trajectory_open('opaque-job'),
        api.trajectory_steps('trajectory-token'),
        api.trajectory_frame('frame-token'),
        api.trajectory_repair_preview('trajectory-token'),
        api.trajectory_confirm_repair(
            'repair-token', 'continue_frozen_incar', 'cluster-a', 'secret', False,
            'trajectory-repair:123456',
            request_fingerprint=_REQUEST_FINGERPRINT),
    ]
    encoded = json.dumps(failures, ensure_ascii=False)

    assert all(item['ok'] is False for item in failures)
    for forbidden in (
            str(job), 'hunter2', 'provider-token', 'super-secret',
            'keybits', 'OPENSSH PRIVATE KEY', 'qsub'):
        assert forbidden not in encoded
