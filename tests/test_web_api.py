"""Api 处理器纯逻辑测试:注入假模块,零网络零 keyring。

每个公开方法至少一个 happy + 一个错误路径;所有返回一律 JSON-safe dict,
异常绝不穿透到 JS(错误落 'error' 字段)。中文注释允许,英文标识符。
"""
import types

from vcstudio.gui_web.api import Api
from vcstudio.cluster.profiles import ClusterProfile


# ── 假件工厂 ────────────────────────────────────────────────────────────────
def _fake_profiles(store):
    m = types.SimpleNamespace()
    m.load_profiles = lambda: dict(store)
    m.save_profiles = lambda p: store.clear() or store.update(p)
    m.ClusterProfile = ClusterProfile
    return m


def _fake_ledger(entries, removed):
    """entries: [(job_dir, manifest|None)];removed 收集 unregister 调用。"""
    m = types.SimpleNamespace()
    m.load_all = lambda: list(entries)
    m.unregister = lambda d: removed.append(d) or True
    return m


# ── list_profiles ────────────────────────────────────────────────────────────
def test_list_profiles_roundtrip():
    store = {'c1': ClusterProfile(name='c1', hostname='h', scheduler='PBS')}
    api = Api(profiles_mod=_fake_profiles(store))
    out = api.list_profiles()
    assert out['profiles'][0]['name'] == 'c1'
    assert out['profiles'][0]['scheduler'] == 'PBS'
    assert out['error'] is None


def test_list_profiles_error_is_caught():
    boom = types.SimpleNamespace(
        load_profiles=lambda: (_ for _ in ()).throw(RuntimeError('坏配置')))
    api = Api(profiles_mod=boom)
    out = api.list_profiles()
    assert out['profiles'] == [] and '坏配置' in out['error']


# ── save_profile ─────────────────────────────────────────────────────────────
def test_save_profile_roundtrip_only_known_fields():
    store = {}
    api = Api(profiles_mod=_fake_profiles(store))
    out = api.save_profile({'name': 'c9', 'hostname': 'h9', 'scheduler': 'PBS',
                            'bogus': 'ignored'})
    assert out['ok'] is True and out['error'] is None
    assert store['c9'].hostname == 'h9' and store['c9'].scheduler == 'PBS'
    assert not hasattr(store['c9'], 'bogus')


def test_save_profile_rejects_blank_name():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.save_profile({'name': '  '})
    assert out['ok'] is False and '名称' in out['error']


# ── delete_profile ───────────────────────────────────────────────────────────
def test_delete_profile_removes_entry():
    store = {'c1': ClusterProfile(name='c1'), 'c2': ClusterProfile(name='c2')}
    api = Api(profiles_mod=_fake_profiles(store))
    out = api.delete_profile('c1')
    assert out['ok'] is True and 'c1' not in store and 'c2' in store


def test_delete_profile_missing_is_ok_false():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.delete_profile('nope')
    assert out['ok'] is False


# ── test_connection ──────────────────────────────────────────────────────────
def test_test_connection_saves_password_on_success():
    saved = {}
    secrets = types.SimpleNamespace(
        get_password=lambda n: None,
        set_password=lambda n, pw: saved.update({n: pw}))
    ssh = types.SimpleNamespace(check_connection=lambda prof, password, trust_new=False:
        types.SimpleNamespace(ok=True, message='ok', scheduler='PBS', needs_trust=False))
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets, ssh_test_mod=ssh)
    out = api.test_connection('c1', 'pw123', False)
    assert out['ok'] is True and saved == {'c1': 'pw123'}
    assert out['scheduler'] == 'PBS' and out['needs_trust'] is False


def test_test_connection_failure_does_not_save():
    saved = {}
    secrets = types.SimpleNamespace(
        get_password=lambda n: None,
        set_password=lambda n, pw: saved.update({n: pw}))
    ssh = types.SimpleNamespace(check_connection=lambda prof, password, trust_new=False:
        types.SimpleNamespace(ok=False, message='认证失败', scheduler='', needs_trust=False))
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets, ssh_test_mod=ssh)
    out = api.test_connection('c1', 'wrong', False)
    assert out['ok'] is False and saved == {}
    assert '认证失败' in out['message']


def test_test_connection_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.test_connection('nope', 'pw', False)
    assert out['ok'] is False and '集群' in out['message']


# ── has_saved_password ───────────────────────────────────────────────────────
def test_has_saved_password_true_and_false():
    secrets = types.SimpleNamespace(
        get_password=lambda n: 'secret' if n == 'c1' else None,
        set_password=lambda n, pw: None)
    api = Api(secrets_mod=secrets)
    assert api.has_saved_password('c1') == {'saved': True}
    assert api.has_saved_password('c2') == {'saved': False}


# ── preview_script ───────────────────────────────────────────────────────────
def test_preview_script_returns_text():
    sub = types.SimpleNamespace(
        build_script_text=lambda prof, job_dir: '#!/bin/bash\necho hi')
    store = {'c1': ClusterProfile(name='c1', hostname='h')}
    api = Api(profiles_mod=_fake_profiles(store), submitter_mod=sub)
    out = api.preview_script('c1', '/job')
    assert out['ok'] is True and 'echo hi' in out['text']


def test_preview_script_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.preview_script('nope', '/job')
    assert out['ok'] is False and '集群' in out['error']


def test_preview_script_build_failure_caught():
    sub = types.SimpleNamespace(
        build_script_text=lambda prof, job_dir: (_ for _ in ()).throw(OSError('无模板')))
    store = {'c1': ClusterProfile(name='c1')}
    api = Api(profiles_mod=_fake_profiles(store), submitter_mod=sub)
    out = api.preview_script('c1', '/job')
    assert out['ok'] is False and '无模板' in out['error']


# ── list_jobs ────────────────────────────────────────────────────────────────
def test_list_jobs_row_assembly_and_stale():
    m_done = {
        'state': 'DONE', 'task_type': 'relax', 'calc_type': 'slab',
        'cluster': 'c1', 'scheduler_job_id': '12345',
        'state_history': [{'at': '2026-07-14T10:00:00'}],
        'results': {'energy_e0_eV': -12.3456},
    }
    m_run = {
        'state': 'RUNNING', 'task_type': 'relax', 'calc_type': 'slab',
        'cluster': 'c1', 'scheduler_job_id': '999',
        'created_at': '2026-07-14T09:00:00',
        'results': {'live': {'ionic_steps': 7, 'fmax': 0.03}},
    }
    entries = [('/jobs/a', m_done), ('/jobs/b', m_run), ('/jobs/gone', None)]
    api = Api(ledger_mod=_fake_ledger(entries, []))
    out = api.list_jobs()
    assert out['stale'] == ['/jobs/gone']
    rows = {r['dir']: r for r in out['jobs']}
    a = rows['/jobs/a']
    assert a['name'] == 'a' and a['state'] == 'DONE' and a['task'] == 'relax/slab'
    assert a['cluster'] == 'c1' and a['job_id'] == '12345'
    assert a['energy'] == '-12.3456' and a['updated'] == '2026-07-14T10:00:00'
    b = rows['/jobs/b']
    assert b['steps'] == 7 and b['fmax'] == 0.03 and b['state'] == 'RUNNING'
    assert b['updated'] == '2026-07-14T09:00:00'


def test_list_jobs_error_is_caught():
    boom = types.SimpleNamespace(
        load_all=lambda: (_ for _ in ()).throw(RuntimeError('台账坏了')))
    api = Api(ledger_mod=boom)
    out = api.list_jobs()
    assert out['jobs'] == [] and out['stale'] == [] and '台账坏了' in out.get('error', '')


# ── submit_jobs / _resolve ───────────────────────────────────────────────────
def test_submit_jobs_delegates_to_batch_ops():
    calls = {}
    bo = types.SimpleNamespace(submit_batch=lambda prof, pw, dirs, tn:
        calls.update(dirs=dirs) or {'needs_trust': False,
                                    'results': [(d, True, 'ok') for d in dirs]})
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo)
    out = api.submit_jobs(['/a', '/b'], 'c1', None, False)
    assert calls['dirs'] == ['/a', '/b'] and out['results'][0][1] is True


def test_unknown_profile_is_error_not_crash():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.submit_jobs(['/a'], 'nope', None, False)
    assert out.get('error') and '集群' in out['error']


def test_submit_jobs_need_password_token():
    secrets = types.SimpleNamespace(get_password=lambda n: None, set_password=lambda n, pw: None)
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets)
    out = api.submit_jobs(['/a'], 'c1', None, False)
    assert out == {'error': 'NEED_PASSWORD'}


def test_submit_jobs_uses_saved_password():
    got = {}
    bo = types.SimpleNamespace(submit_batch=lambda prof, pw, dirs, tn:
        got.update(pw=pw) or {'needs_trust': False, 'results': []})
    secrets = types.SimpleNamespace(get_password=lambda n: 'kr-pw', set_password=lambda n, pw: None)
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets, batch_ops_mod=bo)
    out = api.submit_jobs(['/a'], 'c1', None, False)
    assert got['pw'] == 'kr-pw' and 'error' not in out


def test_submit_jobs_batch_ops_exception_caught():
    bo = types.SimpleNamespace(
        submit_batch=lambda prof, pw, dirs, tn: (_ for _ in ()).throw(RuntimeError('连不上')))
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo)
    out = api.submit_jobs(['/a'], 'c1', None, False)
    assert '连不上' in out['error']


# ── fetch_jobs / continue_jobs / queue_detail ────────────────────────────────
def test_fetch_jobs_delegates_with_files():
    calls = {}
    bo = types.SimpleNamespace(fetch_batch=lambda prof, pw, dirs, tn, files:
        calls.update(files=files, dirs=dirs) or {'needs_trust': False, 'results': []})
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo)
    out = api.fetch_jobs(['/a'], 'c1', None, False, ['CONTCAR', 'OUTCAR'])
    assert calls['files'] == ['CONTCAR', 'OUTCAR'] and 'error' not in out


def test_fetch_jobs_default_files():
    """files=None 时 api 不传 files,由 batch_ops 自身默认(FETCH_FILES)兜底。"""
    from vcstudio.cluster import submitter
    calls = {}
    bo = types.SimpleNamespace(fetch_batch=lambda prof, pw, dirs, tn, files=submitter.FETCH_FILES:
        calls.update(files=files) or {'needs_trust': False, 'results': []})
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo)
    api.fetch_jobs(['/a'], 'c1', None, False)
    assert tuple(calls['files']) == tuple(submitter.FETCH_FILES)


def test_fetch_jobs_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.fetch_jobs(['/a'], 'nope', None, False)
    assert out.get('error') and '集群' in out['error']


def test_continue_jobs_delegates():
    calls = {}
    bo = types.SimpleNamespace(continue_batch=lambda prof, pw, dirs, tn:
        calls.update(dirs=dirs) or {'needs_trust': False, 'results': []})
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo)
    api.continue_jobs(['/a', '/b'], 'c1', None, False)
    assert calls['dirs'] == ['/a', '/b']


def test_continue_jobs_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.continue_jobs(['/a'], 'nope', None, False)
    assert out.get('error') and '集群' in out['error']


def test_queue_detail_delegates():
    bo = types.SimpleNamespace(queue_detail=lambda prof, pw, tn:
        {'needs_trust': False, 'jobs': [{'job_id': '1'}]})
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo)
    out = api.queue_detail('c1', None, False)
    assert out['jobs'][0]['job_id'] == '1'


def test_queue_detail_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.queue_detail('nope', None, False)
    assert out.get('error') and '集群' in out['error']


# ── refresh_status(目标 dirs 从台账筛) ──────────────────────────────────────
def test_refresh_status_targets_only_this_cluster_active():
    entries = [
        ('/a', {'scheduler_job_id': '1', 'cluster': 'c1', 'state': 'RUNNING'}),
        ('/b', {'scheduler_job_id': '2', 'cluster': 'c1', 'state': 'QUEUED'}),
        ('/c', {'scheduler_job_id': '3', 'cluster': 'c1', 'state': 'DONE'}),      # 终态排除
        ('/d', {'scheduler_job_id': '4', 'cluster': 'other', 'state': 'RUNNING'}),  # 别的集群排除
        ('/e', {'cluster': 'c1', 'state': 'RUNNING'}),                            # 无作业号排除
    ]
    calls = {}
    bo = types.SimpleNamespace(refresh_batch=lambda prof, pw, dirs, tn:
        calls.update(dirs=dirs) or {'needs_trust': False, 'results': []})
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo,
              ledger_mod=_fake_ledger(entries, []))
    api.refresh_status('c1', None, False)
    assert calls['dirs'] == ['/a', '/b']


def test_refresh_status_no_targets_short_circuits():
    entries = [('/c', {'scheduler_job_id': '3', 'cluster': 'c1', 'state': 'DONE'})]
    bo = types.SimpleNamespace(
        refresh_batch=lambda *a: (_ for _ in ()).throw(AssertionError('不应连接')))
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo,
              ledger_mod=_fake_ledger(entries, []))
    out = api.refresh_status('c1', None, False)
    assert out['results'] == [] and out['needs_trust'] is False


def test_refresh_status_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}), ledger_mod=_fake_ledger([], []))
    out = api.refresh_status('nope', None, False)
    assert out.get('error') and '集群' in out['error']


# ── adopt_job ────────────────────────────────────────────────────────────────
def test_adopt_job_delegates():
    calls = {}
    sub = types.SimpleNamespace(adopt_external_job=lambda local, prof, jid, remote, name='':
        calls.update(local=local, jid=jid, remote=remote, name=name) or {'state': 'SUBMITTED'})
    store = {'c1': ClusterProfile(name='c1')}
    api = Api(profiles_mod=_fake_profiles(store), submitter_mod=sub)
    out = api.adopt_job('/local', 'c1', '777', '/remote/dir', 'myjob')
    assert out['ok'] is True
    assert calls == {'local': '/local', 'jid': '777', 'remote': '/remote/dir', 'name': 'myjob'}


def test_adopt_job_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.adopt_job('/local', 'nope', '777', '/remote', 'n')
    assert out.get('error') and '集群' in out['error']


def test_adopt_job_failure_caught():
    sub = types.SimpleNamespace(adopt_external_job=lambda *a, **k:
        (_ for _ in ()).throw(ValueError('远程目录需为绝对路径')))
    store = {'c1': ClusterProfile(name='c1')}
    api = Api(profiles_mod=_fake_profiles(store), submitter_mod=sub)
    out = api.adopt_job('/local', 'c1', '777', 'relative', 'n')
    assert '绝对路径' in out['error']


# ── remove_jobs / clean_stale ────────────────────────────────────────────────
def test_remove_jobs_unregisters_each():
    removed = []
    api = Api(ledger_mod=_fake_ledger([], removed))
    out = api.remove_jobs(['/a', '/b'])
    assert out['ok'] is True and removed == ['/a', '/b']


def test_clean_stale_removes_only_missing_manifest():
    removed = []
    entries = [('/a', {'state': 'DONE'}), ('/gone', None), ('/gone2', None)]
    api = Api(ledger_mod=_fake_ledger(entries, removed))
    out = api.clean_stale()
    assert out['ok'] is True and out['removed'] == 2
    assert removed == ['/gone', '/gone2']


def test_remove_jobs_error_caught():
    boom = types.SimpleNamespace(
        unregister=lambda d: (_ for _ in ()).throw(RuntimeError('写失败')))
    api = Api(ledger_mod=boom)
    out = api.remove_jobs(['/a'])
    assert out['ok'] is False and '写失败' in out.get('error', '')


# ── open_dir ─────────────────────────────────────────────────────────────────
def test_open_dir_missing_path_error():
    api = Api()
    out = api.open_dir('/definitely/not/a/real/path/xyz')
    assert out['ok'] is False


# ── ping 保留(前端桥活性探测) ──────────────────────────────────────────────
def test_ping_still_pong():
    assert Api().ping() == 'pong'
