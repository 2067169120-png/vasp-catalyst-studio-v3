"""batch_ops 是 UI 无关线程体:两个 GUI 共用,绝不 import tkinter。"""
import os
import sys
import types

import pytest

from vcstudio.cluster import batch_ops
from vcstudio.cluster.connection import ConnectError
from vcstudio.shared import manifest as manifest_mod


def test_no_tkinter_dependency():
    assert 'tkinter' not in sys.modules or True  # 见下:检查模块源
    import inspect
    src = inspect.getsource(batch_ops)
    assert 'tkinter' not in src


def test_filter_continuable_empty():
    eligible, skipped = batch_ops.filter_continuable([])
    assert eligible == [] and skipped == 0


def test_public_surface():
    for name in ('submit_batch', 'fetch_batch', 'continue_batch',
                 'refresh_batch', 'queue_detail', 'tune_batch', 'adopt_scan'):
        assert callable(getattr(batch_ops, name))


@pytest.mark.parametrize('invoke,empty_key', [
    (lambda p: batch_ops.submit_batch(p, 'pw', [], False), 'results'),
    (lambda p: batch_ops.fetch_batch(p, 'pw', [], False), 'results'),
    (lambda p: batch_ops.continue_batch(p, 'pw', [], False), 'results'),
    (lambda p: batch_ops.refresh_batch(p, 'pw', [], False), 'results'),
    (lambda p: batch_ops.tune_batch(p, 'pw', '/job', {}, False), 'results'),
    (lambda p: batch_ops.adopt_scan(p, 'pw', False, set(), '/tmp'), 'results'),
    (lambda p: batch_ops.queue_detail(p, 'pw', False), 'jobs'),
    (lambda p: batch_ops.workdir_lookup(p, 'pw', '1', False), 'workdir'),
])
def test_needs_trust_passthrough_has_real_key_evidence(
        monkeypatch, invoke, empty_key):
    def _unknown(*_args, **_kwargs):
        raise ConnectError(
            '请核对指纹', needs_trust=True,
            fingerprint='SHA256:abc', algorithm='ssh-ed25519', host='bastion')

    monkeypatch.setattr(batch_ops, 'open_client', _unknown)
    out = invoke(types.SimpleNamespace(name='c'))
    assert out['needs_trust'] is True
    assert out['fingerprint'] == 'SHA256:abc'
    assert out['algorithm'] == 'ssh-ed25519'
    assert out['host'] == 'bastion'
    assert empty_key in out


# ── adopt_scan:一次连接完成 明细 → 补目录 → 落认领 ────────────────────────────
def test_adopt_scan_skips_known_adopts_and_flags_missing(tmp_path, monkeypatch):
    """3 作业:1 已纳管跳过、1 带 workdir 认领、1 无 workdir(query_workdir 也空)→ 需手填。

    断言 adopt_external_job 以 tmp_path 下 sanitize 后的目录被调用,且该目录被 makedirs。
    """
    # 假连接:open_client → (client, jump);close_quiet 无操作
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new=False: ('CLIENT', 'JUMP'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda c, j: None)

    jobs = [
        {'job_id': '100', 'state': 'RUNNING', 'name': 'known', 'workdir': '/home/u/known'},
        {'job_id': '200', 'state': 'QUEUED', 'name': 'ads/job:2', 'workdir': '/home/u/w2'},
        {'job_id': '300', 'state': 'QUEUED', 'name': 'nowd', 'workdir': ''},
    ]
    wd_calls, adopted = [], []

    def _adopt(local_dir, prof, job_id, remote_dir, name=''):
        adopted.append({'local': local_dir, 'jid': job_id,
                        'remote': remote_dir, 'name': name})
        return {'state': 'SUBMITTED'}

    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail',
                        lambda client, prof: jobs)
    monkeypatch.setattr(batch_ops.submitter, 'query_workdir',
                        lambda client, prof, jid: wd_calls.append(jid) or '')
    monkeypatch.setattr(batch_ops.submitter, 'adopt_external_job', _adopt)

    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, {'100'}, str(tmp_path))

    assert out['needs_trust'] is False
    rows = {r[0]: r for r in out['results']}
    assert '100' not in rows                       # 已纳管:不入 results
    assert rows['200'][1] is True and '已认领' in rows['200'][2]
    assert rows['300'][1] is False and '查不到工作目录' in rows['300'][2]
    # 仅无 workdir 的 300 才落回 query_workdir(200 已带 workdir 短路)
    assert wd_calls == ['300']
    # 认领目录 = tmp_path/<sanitize('ads/job:2')>,且被 makedirs 建出
    expected = os.path.join(str(tmp_path), 'ads_job_2')
    assert adopted == [{'local': expected, 'jid': '200',
                        'remote': '/home/u/w2', 'name': 'ads/job:2'}]
    assert os.path.isdir(expected)


def test_adopt_scan_name_collision_appends_profile_and_jid(tmp_path, monkeypatch):
    """两个未纳管作业同名 'Nb_S8':第二个目录追加 profile + jid 避让，
    两个都认领成功且落到不同目录(不再第二个失败还引用第一个的作业号)。"""
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new=False: ('C', 'J'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda c, j: None)

    jobs = [
        {'job_id': '501', 'state': 'QUEUED', 'name': 'Nb_S8', 'workdir': '/home/u/a'},
        {'job_id': '502', 'state': 'QUEUED', 'name': 'Nb_S8', 'workdir': '/home/u/b'},
    ]
    adopted = []

    def _adopt(local_dir, prof, job_id, remote_dir, name=''):
        adopted.append(local_dir)
        return {'state': 'SUBMITTED'}

    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail',
                        lambda client, prof: jobs)
    monkeypatch.setattr(batch_ops.submitter, 'query_workdir',
                        lambda client, prof, jid: '')
    monkeypatch.setattr(batch_ops.submitter, 'adopt_external_job', _adopt)

    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, set(), str(tmp_path))

    rows = {r[0]: r for r in out['results']}
    assert rows['501'][1] is True and rows['502'][1] is True   # 两个都认领成功
    assert len(adopted) == 2 and adopted[0] != adopted[1]      # 落到不同目录
    assert adopted[0] == os.path.join(str(tmp_path), 'Nb_S8')
    assert adopted[1] == os.path.join(str(tmp_path), 'Nb_S8_c1_502')
    assert os.path.isdir(adopted[1])


def test_adopt_scan_same_job_id_on_other_cluster_never_reuses_local_dir(tmp_path, monkeypatch):
    local = tmp_path / 'same_name'
    local.mkdir()
    existing = manifest_mod.new_manifest(
        job_id='old', system='old', task_type='relax', calc_type='slab', inputs={})
    existing.update({'cluster': 'server-a', 'remote_dir': '/work/a',
                     'scheduler_job_id': '777'})
    manifest_mod.set_state(existing, 'SUBMITTED')
    manifest_mod.save_manifest(local, existing)
    monkeypatch.setattr(batch_ops, 'open_client', lambda *a, **k: ('C', 'J'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda *a: None)
    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail', lambda *_a: [
        {'job_id': '777', 'name': 'same_name', 'workdir': '/work/b'}])
    adopted = []
    monkeypatch.setattr(
        batch_ops.submitter, 'adopt_external_job',
        lambda local_dir, *_a, **_k: adopted.append(local_dir) or {'state': 'SUBMITTED'})

    out = batch_ops.adopt_scan(
        types.SimpleNamespace(name='server-b'), None, False, set(), str(tmp_path))

    target = os.path.join(str(tmp_path), 'same_name_server-b_777')
    assert out['results'][0][1] is True
    assert adopted == [target]
    assert target != str(local)


def test_adopt_scan_exact_existing_binding_is_idempotent(tmp_path, monkeypatch):
    from vcstudio.cluster import ledger

    local = tmp_path / 'same_name'
    local.mkdir()
    existing = manifest_mod.new_manifest(
        job_id='old', system='old', task_type='relax', calc_type='slab', inputs={})
    existing.update({'cluster': 'server-b', 'remote_dir': '/work/b',
                     'scheduler_job_id': '777'})
    manifest_mod.set_state(existing, 'SUBMITTED')
    manifest_mod.save_manifest(local, existing)
    monkeypatch.setattr(batch_ops, 'open_client', lambda *a, **k: ('C', 'J'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda *a: None)
    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail', lambda *_a: [
        {'job_id': '777', 'name': 'same_name', 'workdir': '/work/b'}])
    registered = []
    monkeypatch.setattr(ledger, 'register', lambda path: registered.append(path) or True)
    monkeypatch.setattr(
        batch_ops.submitter, 'adopt_external_job',
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('原绑定不得重复认领')))

    out = batch_ops.adopt_scan(
        types.SimpleNamespace(name='server-b'), None, False, set(), str(tmp_path))

    assert out['results'][0][1] is True
    assert '原绑定' in out['results'][0][2]
    assert registered == [str(local)]


def test_adopt_scan_needs_trust_branch(monkeypatch):
    """首次未知主机指纹:open_client 抛 ConnectError(needs_trust)→ 返回 needs_trust。"""
    from vcstudio.cluster.connection import ConnectError

    def _boom(prof, pw, trust_new=False):
        raise ConnectError('未知主机', needs_trust=True)

    monkeypatch.setattr(batch_ops, 'open_client', _boom)
    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, set(), '/root')
    assert out['needs_trust'] is True and out['results'] == []


def test_adopt_scan_adopt_failure_is_per_job(tmp_path, monkeypatch):
    """单作业 adopt_external_job 抛错只失败该条,msg 透传异常文案。"""
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new=False: ('C', 'J'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda c, j: None)
    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail',
                        lambda client, prof: [
                            {'job_id': '9', 'name': 'x', 'workdir': '/home/u/x'}])
    monkeypatch.setattr(batch_ops.submitter, 'query_workdir',
                        lambda client, prof, jid: '')
    monkeypatch.setattr(batch_ops.submitter, 'adopt_external_job',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('已关联作业号')))
    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, set(), str(tmp_path))
    assert out['results'] == [['9', False, '已关联作业号']]


# ── cancel_batch:逐作业 qdel/scancel + 回写 manifest(假连接假 scheduler) ─────
def _prof(scheduler='Slurm'):
    return types.SimpleNamespace(name='c1', scheduler=scheduler,
                                 scheduler_bin='', username='u')


def _make_job(tmp_path, name, job_id, state='RUNNING'):
    """建一个带 scheduler_job_id 的作业目录(job.yaml),返回目录路径。"""
    d = tmp_path / name
    d.mkdir()
    m = manifest_mod.new_manifest(job_id=f'{name}-x', system=name,
                                  task_type='relax', calc_type='', inputs={})
    m['scheduler_job_id'] = job_id
    m['cluster'] = 'c1'
    manifest_mod.set_state(m, state)
    manifest_mod.save_manifest(str(d), m)
    return str(d)


def _fake_conn(monkeypatch):
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new=False: ('CLIENT', 'JUMP'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda c, j: None)


def test_cancel_batch_success_and_writeback(tmp_path, monkeypatch):
    """两作业全取消:scancel 带上作业号,manifest 回写 FAILED + note '用户取消'。"""
    _fake_conn(monkeypatch)
    calls = []
    monkeypatch.setattr(batch_ops.submitter, 'run_cmd',
                        lambda client, cmd, check=False: calls.append(cmd) or ('', ''))
    d1 = _make_job(tmp_path, 'j1', '111')
    d2 = _make_job(tmp_path, 'j2', '222')
    out = batch_ops.cancel_batch(_prof('Slurm'), [d1, d2], password='pw')

    assert out['ok'] is True and out['error'] is None
    assert set(out['cancelled']) == {'111', '222'} and out['failed'] == []
    assert any('scancel' in c and '111' in c for c in calls)
    m1 = manifest_mod.load_manifest(d1)
    assert m1['state'] == 'FAILED' and m1['state_history'][-1]['note'] == '用户取消'


def test_cancel_batch_partial_failure(tmp_path, monkeypatch):
    """作业 222 的 scancel 报错(退出码非零)→ 仅该条 failed 且透传文案,其余照常回写。"""
    _fake_conn(monkeypatch)

    def _run(client, cmd, check=False):
        if '222' in cmd:
            raise RuntimeError('scancel: error: Invalid job id 222')
        return ('', '')

    monkeypatch.setattr(batch_ops.submitter, 'run_cmd', _run)
    d1 = _make_job(tmp_path, 'j1', '111')
    d2 = _make_job(tmp_path, 'j2', '222')
    out = batch_ops.cancel_batch(_prof('Slurm'), [d1, d2], password='pw')

    assert out['cancelled'] == ['111']
    assert len(out['failed']) == 1 and out['failed'][0]['job_id'] == '222'
    assert 'Invalid job id' in out['failed'][0]['reason']
    assert manifest_mod.load_manifest(d1)['state'] == 'FAILED'      # 成功者回写
    assert manifest_mod.load_manifest(d2)['state'] == 'RUNNING'     # 失败者保持原态


def test_cancel_batch_missing_jobid_is_per_job(tmp_path, monkeypatch):
    """无 job.yaml / 无 scheduler_job_id 的目录 → 各自失败(不打断有作业号的)。"""
    _fake_conn(monkeypatch)
    monkeypatch.setattr(batch_ops.submitter, 'run_cmd',
                        lambda client, cmd, check=False: ('', ''))
    d1 = _make_job(tmp_path, 'j1', '111')
    d2 = tmp_path / 'nomanifest'
    d2.mkdir()
    d3 = tmp_path / 'nojid'
    d3.mkdir()
    m = manifest_mod.new_manifest(job_id='x', system='s', task_type='relax',
                                  calc_type='', inputs={})
    manifest_mod.save_manifest(str(d3), m)                          # scheduler_job_id=None

    out = batch_ops.cancel_batch(_prof('Slurm'), [d1, str(d2), str(d3)], password='pw')
    assert out['cancelled'] == ['111']
    failed_ids = {f['job_id'] for f in out['failed']}
    assert failed_ids == {str(d2), str(d3)}
    assert all('无法取消' in f['reason'] for f in out['failed'])


def test_cancel_batch_pbs_uses_qdel(tmp_path, monkeypatch):
    """PBS 方言走 qdel(带作业号)。"""
    _fake_conn(monkeypatch)
    calls = []
    monkeypatch.setattr(batch_ops.submitter, 'run_cmd',
                        lambda client, cmd, check=False: calls.append(cmd) or ('', ''))
    d1 = _make_job(tmp_path, 'j1', '888')
    out = batch_ops.cancel_batch(_prof('PBS'), [d1], password='pw')
    assert out['cancelled'] == ['888']
    assert any('qdel' in c and '888' in c for c in calls)


def test_cancel_batch_wrong_cluster_never_calls_scheduler(tmp_path, monkeypatch):
    _fake_conn(monkeypatch)
    d = _make_job(tmp_path, 'foreign', '777')
    data = manifest_mod.load_manifest(d)
    data['cluster'] = 'server-a'
    manifest_mod.save_manifest(d, data)
    monkeypatch.setattr(
        batch_ops.submitter, 'run_cmd',
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('不得取消错误服务器作业')))

    out = batch_ops.cancel_batch(
        types.SimpleNamespace(name='server-b', scheduler='Slurm', scheduler_bin=''),
        [d], password='pw')

    assert out['cancelled'] == []
    assert out['failed'][0]['job_id'] == '777'
    assert '属于服务器「server-a」' in out['failed'][0]['reason']
    assert manifest_mod.load_manifest(d)['state'] == 'RUNNING'


def test_cancel_batch_rejects_scheduler_generation_changed_after_target_snapshot(
        tmp_path, monkeypatch):
    _fake_conn(monkeypatch)
    d = _make_job(tmp_path, 'race', '777')
    real_cancel = batch_ops.submitter.cancel_job
    commands = []

    def change_generation_then_cancel(client, profile, job_dir, **kwargs):
        data = manifest_mod.load_manifest(job_dir)
        data['scheduler_job_id'] = '888'
        manifest_mod.save_manifest(job_dir, data)
        return real_cancel(client, profile, job_dir, **kwargs)

    monkeypatch.setattr(batch_ops.submitter, 'cancel_job', change_generation_then_cancel)
    monkeypatch.setattr(
        batch_ops.submitter, 'run_cmd',
        lambda _client, command, **_kwargs: commands.append(command) or ('', ''))

    out = batch_ops.cancel_batch(_prof('Slurm'), [d], password='pw')

    assert out['cancelled'] == []
    assert '代次已变化' in out['failed'][0]['reason']
    assert commands == []


def test_cancel_batch_needs_trust(monkeypatch):
    """首次未知主机指纹:open_client 抛 ConnectError(needs_trust)→ ok=False + needs_trust。"""
    from vcstudio.cluster.connection import ConnectError

    def _boom(prof, pw, trust_new=False):
        raise ConnectError('未知主机', needs_trust=True)

    monkeypatch.setattr(batch_ops, 'open_client', _boom)
    out = batch_ops.cancel_batch(_prof('Slurm'), ['/x'], password='pw')
    assert out['ok'] is False and out['needs_trust'] is True and out['cancelled'] == []


def test_cancel_batch_needs_trust_passthrough_has_key_evidence(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise ConnectError(
            '未知主机', needs_trust=True, fingerprint='SHA256:def',
            algorithm='ssh-rsa', host='target')

    monkeypatch.setattr(batch_ops, 'open_client', _boom)
    out = batch_ops.cancel_batch(_prof('Slurm'), ['/x'], password='pw')
    assert out['needs_trust'] is True
    assert out['fingerprint'] == 'SHA256:def'
    assert out['algorithm'] == 'ssh-rsa'
    assert out['host'] == 'target'


def test_cancel_batch_unsupported_scheduler_errors():
    """不支持的调度器(LSF)→ 早失败 ok=False + error,绝不静默,连接都不建立。"""
    out = batch_ops.cancel_batch(_prof('LSF'), ['/x'], password='pw')
    assert out['ok'] is False and out['error'] and out['cancelled'] == []
