"""batch_ops 后台批量函数测试(不建 Tk 窗口,只测线程体):连接关闭责任与单作业失败隔离。"""
import types

import pytest

from vcstudio.cluster import batch_ops
from vcstudio.shared import manifest as mm


def _mk_job(tmp_path, name, state, *, restartable=None, rounds=0):
    d = tmp_path / name
    d.mkdir()
    m = mm.new_manifest(job_id=name, system='s', task_type='relax', calc_type='slab', inputs={})
    m['scheduler_job_id'] = '100'
    if state != 'CREATED':
        mm.set_state(m, state)
    res = {}
    if restartable is not None:
        res['diagnosis'] = {'failure_class': 'NONCONVERGED', 'restartable': restartable}
    if rounds:
        res['continue_rounds'] = rounds
    m['results'] = res
    mm.save_manifest(d, m)
    return str(d)


def test_filter_continuable_partitions_selection(tmp_path):
    ok = _mk_job(tmp_path, 'ok', 'UNCONVERGED', restartable=True)
    running = _mk_job(tmp_path, 'run', 'RUNNING', restartable=True)      # 仍在跑 → 跳过
    hardfail = _mk_job(tmp_path, 'seg', 'FAILED', restartable=False)     # 不可续算 → 跳过
    capped = _mk_job(tmp_path, 'cap', 'UNCONVERGED', restartable=True, rounds=3)  # 到顶 → 跳过
    nodiag = _mk_job(tmp_path, 'done', 'DONE')                           # 无诊断 → 跳过
    eligible, skipped = batch_ops.filter_continuable([ok, running, hardfail, capped, nodiag])
    assert eligible == [ok] and skipped == 4


def test_filter_continuable_manual_override_keeps_other_guards(tmp_path):
    capped = _mk_job(
        tmp_path, 'cap', 'UNCONVERGED', restartable=True,
        rounds=batch_ops.submitter.CONTINUE_MAX_ROUNDS)
    running = _mk_job(
        tmp_path, 'run', 'RUNNING', restartable=True,
        rounds=batch_ops.submitter.CONTINUE_MAX_ROUNDS)
    hardfail = _mk_job(
        tmp_path, 'hard', 'FAILED', restartable=False,
        rounds=batch_ops.submitter.CONTINUE_MAX_ROUNDS)

    eligible, skipped = batch_ops.filter_continuable(
        [capped, running, hardfail], allow_round_limit_override=True)

    assert eligible == [capped]
    assert skipped == 2


def test_filter_continuable_rejects_created_even_with_restartable_diagnosis(tmp_path):
    created = _mk_job(
        tmp_path, 'created', 'CREATED', restartable=True,
        rounds=batch_ops.submitter.CONTINUE_MAX_ROUNDS)

    assert batch_ops.filter_continuable(
        [created], allow_round_limit_override=True) == ([], 1)


def test_filter_continuable_rejects_missing_source_scheduler_generation(tmp_path):
    job = _mk_job(tmp_path, 'missing-source', 'UNCONVERGED', restartable=True)
    data = mm.load_manifest(job)
    data['scheduler_job_id'] = None
    mm.save_manifest(job, data)

    assert batch_ops.filter_continuable(
        [job], allow_round_limit_override=True) == ([], 1)


def test_filter_continuable_excludes_neb_from_generic_restart(tmp_path):
    d = _mk_job(tmp_path, 'neb', 'UNCONVERGED', restartable=True)
    data = mm.load_manifest(d)
    data['task_type'] = 'neb'
    mm.save_manifest(d, data)
    assert batch_ops.filter_continuable([d]) == ([], 1)


class FakeSFTP:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self):
        self.closed = False
        self.sftp = FakeSFTP()

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


def _patch_open(monkeypatch):
    client, jump = FakeConn(), FakeConn()
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new: (client, jump))
    return client, jump


def test_submit_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(batch_ops.submitter, 'submit_job',
                        lambda c, s, p, d: {'scheduler_job_id': '1'})
    payload = batch_ops.submit_batch(object(), None, ['d1'], False)
    assert payload['results'] == [('d1', True, '已提交,作业号 1')]
    assert client.closed and jump.closed          # 跳板连接同样必须关


def test_submit_batch_reports_cross_process_busy_as_structured_retryable_state(monkeypatch):
    client, jump = _patch_open(monkeypatch)

    def busy(*_args, **_kwargs):
        raise batch_ops.submitter.JobOperationBusy('job is owned by another process')

    monkeypatch.setattr(batch_ops.submitter, 'submit_job', busy)
    payload = batch_ops.submit_batch(
        object(), None, ['d1'], False, idempotency_key='jobop-busy-submit-001')

    assert payload['busy'] is True
    assert payload['code'] == 'job_busy' and payload['busy_count'] == 1
    assert payload['results'] == [('d1', False, 'job is owned by another process')]
    assert client.closed and jump.closed


def test_submit_batch_reports_unknown_remote_submission_without_path(monkeypatch):
    client, jump = _patch_open(monkeypatch)

    def unknown(*_args, **_kwargs):
        raise batch_ops.submitter.UnknownRemoteSubmission(
            'manual recovery required', recovery_status='remote_accepted',
            scheduler_job_id='991')

    monkeypatch.setattr(batch_ops.submitter, 'submit_job', unknown)
    payload = batch_ops.submit_batch(
        object(), None, ['C:/secret/job'], False,
        idempotency_key='jobop-recovery-submit-001')

    assert payload['requires_manual_recovery'] is True
    assert payload['code'] == 'unknown_remote_submission'
    assert payload['scheduler_job_ids'] == ['991']
    assert 'C:/secret/job' not in payload['results'][0][2]
    assert client.closed and jump.closed


def test_submit_batch_duplicate_selection_fails_whole_batch_before_network(tmp_path, monkeypatch):
    a = tmp_path / 'calc'
    a.mkdir()
    monkeypatch.setattr(
        batch_ops, 'open_client',
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('冲突批次不得联网')))
    profile = types.SimpleNamespace(name='c1', remote_root='/remote/jobs')

    out = batch_ops.submit_batch(profile, None, [str(a), str(a)], False)

    assert [row[1] for row in out['results']] == [False, False]
    assert all('同一远程目录' in row[2] for row in out['results'])


def test_submit_batch_same_basename_uses_hashed_remote_dirs(tmp_path, monkeypatch):
    a = tmp_path / 'left' / 'calc'
    b = tmp_path / 'right' / 'calc'
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    client, jump = _patch_open(monkeypatch)
    seen = []
    monkeypatch.setattr(
        batch_ops.submitter, 'submit_job',
        lambda _c, _s, _p, d: seen.append(d) or {'scheduler_job_id': str(len(seen))})

    out = batch_ops.submit_batch(
        types.SimpleNamespace(name='c1', remote_root='/remote/jobs'),
        None, [str(a), str(b)], False)

    assert [row[1] for row in out['results']] == [True, True]
    assert seen == [str(a), str(b)]
    assert client.closed and jump.closed


def test_refresh_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(batch_ops.submitter, 'query_scheduler', lambda c, p: ({}, {}))
    monkeypatch.setattr(batch_ops.submitter, 'assert_profile_binding', lambda *a, **k: {})
    monkeypatch.setattr(batch_ops.submitter, 'refresh_job',
                        lambda c, p, d, live_states, terminal_reasons=None: {'state': 'DONE', 'results': {}})
    payload = batch_ops.refresh_batch(object(), None, ['d1'], False)
    assert payload['results'] == [('d1', 'DONE')]
    assert client.closed and jump.closed


def test_refresh_batch_preserves_scheduler_map_for_every_job(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    scheduler_states = {'101': 'RUNNING', '102': 'QUEUED'}
    seen = []
    monkeypatch.setattr(
        batch_ops.submitter, 'query_scheduler', lambda _c, _p: (scheduler_states, {}))
    monkeypatch.setattr(batch_ops.submitter, 'assert_profile_binding', lambda *a, **k: {})

    def _refresh(_client, _profile, job_dir, live_states, terminal_reasons=None):
        seen.append((job_dir, live_states))
        return {'state': 'RUNNING',
                'results': {'live': {'ionic_steps': 1, 'warning': ''}}}

    monkeypatch.setattr(batch_ops.submitter, 'refresh_job', _refresh)

    payload = batch_ops.refresh_batch(object(), None, ['first', 'second'], False)

    assert [row[0] for row in payload['results']] == ['first', 'second']
    assert seen == [('first', scheduler_states), ('second', scheduler_states)]
    assert client.closed and jump.closed


def test_fetch_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    seen = []
    monkeypatch.setattr(batch_ops.submitter, 'assert_profile_binding',
                        lambda *args, **kwargs: {})
    monkeypatch.setattr(batch_ops.submitter, 'fetch_results',
                        lambda c, s, d, files=None, profile=None:
                        seen.append((d, files, profile)) or
                        (['CONTCAR'], []))
    profile = object()
    payload = batch_ops.fetch_batch(profile, None, ['d1', 'd2'], False)
    assert payload['results'][0][1] is True
    assert seen == [('d1', None, profile), ('d2', None, profile)]
    # 每个 manifest 自己决定默认结果包，并将已校验 profile 透传到下载层复核。
    assert client.closed and jump.closed


def test_fetch_batch_rejects_job_bound_to_another_cluster(tmp_path, monkeypatch):
    d = _mk_job(tmp_path, 'foreign', 'DONE')
    data = mm.load_manifest(d)
    data.update({'cluster': 'server-a', 'remote_dir': '/remote/foreign',
                 'scheduler_job_id': '42'})
    mm.save_manifest(d, data)
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(
        batch_ops.submitter, 'fetch_results',
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('不得从错误服务器下载')))

    out = batch_ops.fetch_batch(types.SimpleNamespace(name='server-b'), None, [d], False)

    assert out['results'][0][1] is False
    assert '属于服务器「server-a」' in out['results'][0][2]
    assert client.closed and jump.closed


# ── 单作业失败隔离:一条连接抖动(SSHException)不许打断整批 ──
def test_submit_batch_survives_ssh_exception(monkeypatch):
    from paramiko.ssh_exception import SSHException
    client, jump = _patch_open(monkeypatch)

    def flaky(c, s, p, d):
        if d == 'bad':
            raise SSHException('channel closed')
        return {'scheduler_job_id': '2'}

    monkeypatch.setattr(batch_ops.submitter, 'submit_job', flaky)
    payload = batch_ops.submit_batch(object(), None, ['bad', 'good'], False)
    assert [r[1] for r in payload['results']] == [False, True]   # bad 失败,good 照常
    assert 'channel closed' in payload['results'][0][2]
    assert client.closed and jump.closed


def test_refresh_batch_survives_ssh_exception(monkeypatch):
    from paramiko.ssh_exception import SSHException
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(batch_ops.submitter, 'query_scheduler', lambda c, p: ({}, {}))
    monkeypatch.setattr(batch_ops.submitter, 'assert_profile_binding', lambda *a, **k: {})

    def flaky(c, p, d, live_states, terminal_reasons=None):
        if d == 'bad':
            raise SSHException('boom')
        return {'state': 'DONE', 'results': {}}

    monkeypatch.setattr(batch_ops.submitter, 'refresh_job', flaky)
    payload = batch_ops.refresh_batch(object(), None, ['bad', 'good'], False)
    assert '查询失败' in payload['results'][0][1]
    assert payload['results'][1][1] == 'DONE'
    assert client.closed and jump.closed


def test_fetch_batch_survives_ssh_exception(monkeypatch):
    from paramiko.ssh_exception import SSHException
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(batch_ops.submitter, 'assert_profile_binding',
                        lambda *args, **kwargs: {})

    def flaky(c, s, d, files=None, profile=None):
        if d == 'bad':
            raise SSHException('boom')
        return (['CONTCAR'], [])

    monkeypatch.setattr(batch_ops.submitter, 'fetch_results', flaky)
    payload = batch_ops.fetch_batch(object(), None, ['bad', 'good'], False)
    assert [r[1] for r in payload['results']] == [False, True]
    assert client.closed and jump.closed


# ── 续算批量:关闭责任 + 单作业失败隔离(不可续算的自失败不打断整批) ──
def test_continue_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(batch_ops.submitter, 'continue_from_contcar',
                        lambda c, p, d: {'scheduler_job_id': '9'})
    payload = batch_ops.continue_batch(object(), None, ['d1'], False)
    assert payload['results'] == [('d1', True, '已续算重投,新作业号 9')]
    assert client.closed and jump.closed


def test_continue_batch_survives_error(monkeypatch):
    client, jump = _patch_open(monkeypatch)

    def flaky(c, p, d):
        if d == 'bad':
            raise RuntimeError('不可自动续算')
        return {'scheduler_job_id': '9'}

    monkeypatch.setattr(batch_ops.submitter, 'continue_from_contcar', flaky)
    payload = batch_ops.continue_batch(object(), None, ['bad', 'good'], False)
    assert [r[1] for r in payload['results']] == [False, True]
    assert client.closed and jump.closed


def test_manual_continue_batch_disables_only_the_round_cap(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    calls = []

    def continued(c, p, d, *, max_rounds=batch_ops.submitter.CONTINUE_MAX_ROUNDS):
        calls.append((d, max_rounds))
        return {'scheduler_job_id': '10'}

    monkeypatch.setattr(batch_ops.submitter, 'continue_from_contcar', continued)
    payload = batch_ops.continue_batch(
        object(), None, ['capped'], False,
        allow_round_limit_override=True)

    assert payload['results'][0][1] is True
    assert calls == [('capped', None)]
    assert client.closed and jump.closed


def test_continue_batch_rejects_cas_before_opening_connection(monkeypatch):
    opened = []
    monkeypatch.setattr(
        batch_ops, 'open_client',
        lambda *_args, **_kwargs: opened.append(True) or (_ for _ in ()).throw(
            AssertionError('connection seam must not be reached')))
    monkeypatch.setattr(
        batch_ops.submitter, 'assert_repair_content_cas',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError('same-size replacement')))
    payload = batch_ops.continue_batch(
        object(), None, ['d1'], False,
        idempotency_key='trajectory-repair:cas-preflight',
        expected_cas_by_job={'d1': {'schema': 'vcstudio.repair-cas/v1'}})
    assert payload['ok'] is False
    assert '未建立远程连接' in payload['error']
    assert opened == []


@pytest.mark.parametrize('guard_name', [
    'expected_cas_by_job',
    'expected_correction_by_job',
])
def test_continue_batch_old_adapter_cannot_drop_repair_guards(
        monkeypatch, guard_name):
    opened = []

    def legacy_adapter(_client, _profile, _job_dir, *, idempotency_key=None):
        raise AssertionError('legacy adapter must not be called')

    monkeypatch.setattr(batch_ops.submitter, 'continue_from_contcar', legacy_adapter)
    monkeypatch.setattr(
        batch_ops, 'open_client',
        lambda *_args, **_kwargs: opened.append(True) or (_ for _ in ()).throw(
            AssertionError('connection seam must not be reached')))
    payload = batch_ops.continue_batch(
        object(), None, ['d1'], False,
        idempotency_key='trajectory-repair:legacy-adapter',
        **{guard_name: {'d1': {'schema': 'guard'}}})

    assert payload['ok'] is False
    assert '适配器不支持' in payload['error']
    assert opened == []


def test_continue_batch_forwards_exact_correction_journal_binding(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(
        batch_ops.submitter, 'assert_repair_content_cas', lambda *_args: None)
    monkeypatch.setattr(
        batch_ops.submitter, 'assert_repair_correction_binding',
        lambda *_args: None)
    captured = {}

    def continued(_client, _profile, directory, **kwargs):
        captured.update(directory=directory, **kwargs)
        return {'scheduler_job_id': '101', '_continue_replayed': True}

    monkeypatch.setattr(batch_ops.submitter, 'continue_from_contcar', continued)
    correction = {'schema': 'vcstudio.correction-journal-link/v1'}
    payload = batch_ops.continue_batch(
        object(), None, ['d1'], False,
        idempotency_key='trajectory-repair:batch-binding',
        allow_round_limit_override=True,
        expected_cas_by_job={'d1': {'schema': 'vcstudio.repair-cas/v1'}},
        expected_correction_by_job={'d1': correction})
    assert payload['results'] == [('d1', True, '已确认续算,新作业号 101')]
    assert captured['correction_binding'] is correction
    assert captured['idempotency_key'] == 'trajectory-repair:batch-binding'
    assert captured['max_rounds'] is None
    assert client.closed and jump.closed


def test_continue_batch_rejects_correction_binding_before_connection(monkeypatch):
    opened = []
    monkeypatch.setattr(
        batch_ops, 'open_client',
        lambda *_args, **_kwargs: opened.append(True) or (_ for _ in ()).throw(
            AssertionError('connection seam must not be reached')))
    payload = batch_ops.continue_batch(
        object(), None, ['d1'], False,
        idempotency_key='trajectory-repair:bad-binding',
        expected_correction_by_job={'d1': {'schema': 'invalid'}})
    assert payload['ok'] is False
    assert '未建立远程连接' in payload['error']
    assert opened == []
