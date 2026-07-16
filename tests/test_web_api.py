"""Api 处理器纯逻辑测试:注入假模块,零网络零 keyring。

每个公开方法至少一个 happy + 一个错误路径;所有返回一律 JSON-safe dict,
异常绝不穿透到 JS(错误落 'error' 字段)。中文注释允许,英文标识符。
"""
import os
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


def test_test_connection_falls_back_to_keyring_password():
    # cluster.js 在 keyring 已存密码时故意传 None;test_connection 须回退取 keyring 密码,
    # 否则 check_connection 拿到 password=None 恒认证失败(retest 永远失败)。
    seen = {}
    secrets = types.SimpleNamespace(
        get_password=lambda n: 'kr-pw',
        set_password=lambda n, pw: None)

    def _check(prof, password, trust_new=False):
        seen['password'] = password
        return types.SimpleNamespace(ok=True, message='ok', scheduler='PBS', needs_trust=False)

    ssh = types.SimpleNamespace(check_connection=_check)
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets, ssh_test_mod=ssh)
    out = api.test_connection('c1', None, False)
    assert seen['password'] == 'kr-pw'
    assert out['ok'] is True


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


def test_list_jobs_injects_project_and_role():
    """任务行注入所属吸附能项目组:clean/gas/config 各归位,独立作业 project=None。"""
    def mk(st):
        return {'state': st, 'task_type': 'relax', 'calc_type': 'slab',
                'created_at': '2026-07-14T09:00:00', 'results': {}}
    entries = [('/jobs/demo_slab_clean', mk('DONE')),
               ('/jobs/demo_ads_S8', mk('RUNNING')),
               ('/jobs/demo_ref', mk('DONE')),
               ('/jobs/standalone', mk('DONE'))]
    proj = {'name': 'demo', 'members': {
        'clean_slab': '/jobs/demo_slab_clean',
        'gas_ref': '/jobs/demo_ref',
        'configs': ['/jobs/demo_ads_S8'],
    }}
    ads = _fake_adsorption(projects=['/p/project.yaml'],
                           proj_map={'/p/project.yaml': proj})
    api = Api(ledger_mod=_fake_ledger(entries, []), adsorption_mod=ads)
    out = api.list_jobs()
    rows = {r['dir']: r for r in out['jobs']}
    assert rows['/jobs/demo_slab_clean']['project'] == 'demo'
    assert rows['/jobs/demo_slab_clean']['role'] == 'clean'
    assert rows['/jobs/demo_ads_S8']['project'] == 'demo'
    assert rows['/jobs/demo_ads_S8']['role'] == 'config'
    assert rows['/jobs/demo_ref']['project'] == 'demo'
    assert rows['/jobs/demo_ref']['role'] == 'gas'
    assert rows['/jobs/standalone']['project'] is None
    assert rows['/jobs/standalone']['role'] is None


def test_list_jobs_project_map_failure_does_not_break_listing():
    """项目注册表崩坏(list_projects 抛)→ 全部作业不分组,list_jobs 契约不变。"""
    entries = [('/jobs/a', {'state': 'DONE', 'created_at': 'x', 'results': {}})]
    boom_ads = types.SimpleNamespace(
        list_projects=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('注册表坏了')),
        load_project=lambda p: None)
    api = Api(ledger_mod=_fake_ledger(entries, []), adsorption_mod=boom_ads)
    out = api.list_jobs()
    assert out.get('error') is None or 'error' not in out
    assert out['jobs'][0]['project'] is None and out['jobs'][0]['role'] is None


def test_list_jobs_single_bad_project_yaml_is_skipped():
    """单个 project.yaml load 抛异常 → 只影响该项目,别的项目照常归组。"""
    entries = [('/jobs/ok_slab', {'state': 'DONE', 'created_at': 'x', 'results': {}})]
    good = {'name': 'ok', 'members': {'clean_slab': '/jobs/ok_slab',
                                      'gas_ref': None, 'configs': []}}

    def _load(p):
        if p == '/bad/project.yaml':
            raise RuntimeError('yaml 畸形')
        return good
    ads = types.SimpleNamespace(
        list_projects=lambda *a, **k: ['/bad/project.yaml', '/good/project.yaml'],
        load_project=_load)
    api = Api(ledger_mod=_fake_ledger(entries, []), adsorption_mod=ads)
    out = api.list_jobs()
    assert out['jobs'][0]['project'] == 'ok' and out['jobs'][0]['role'] == 'clean'


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


def test_submit_jobs_resolve_load_error_caught():
    """load_profiles 崩溃(配置损坏/keyring 后端报错)也须兜成 error dict,绝不穿透 JS。"""
    boom = types.SimpleNamespace(
        load_profiles=lambda: (_ for _ in ()).throw(RuntimeError('yaml corrupt')))
    api = Api(profiles_mod=boom)
    out = api.submit_jobs(['/a'], 'c1', None, False)
    assert isinstance(out, dict) and 'yaml corrupt' in out.get('error', '')


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


# ── 生成页假件 ────────────────────────────────────────────────────────────────
def _fake_config(cfg=None, ui=None, calls=None):
    """config 假件:load_config/get_ui_state 只读;set_* 落 calls 便于断言持久化。"""
    cfg = dict(cfg or {})
    ui = dict(ui or {})
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.load_config = lambda *a, **k: dict(cfg)
    m.get_ui_state = lambda c=None: dict(ui)
    m.set_ui_state = lambda **kv: calls.__setitem__('ui_state', dict(kv))
    m.set_potcar_lib_root = lambda p, *a, **k: calls.__setitem__('lib', p)
    return m


def _fake_logic(pos='POS摘要', inc='INC预览', errs=None):
    m = types.SimpleNamespace()
    m.poscar_preview = lambda path, calc='slab': pos
    m.incar_preview = lambda incar, poscar, lib, validate=True: inc
    m.validate_generate_inputs = lambda p, i, o, lib: list(errs or [])
    return m


def _fake_ledger_register(registered):
    m = types.SimpleNamespace()
    m.register = lambda d: registered.append(d) or True
    return m


def _fake_manifest(written):
    m = types.SimpleNamespace()
    m.create_from_build = lambda jd, payload, *, poscar_path, validate: written.update(
        jd=jd, poscar=poscar_path, validate=validate)
    return m


# ── gen_preview ──────────────────────────────────────────────────────────────
def test_gen_preview_returns_summary():
    api = Api(config_mod=_fake_config(cfg={'potcar_lib_root': '/lib'}),
              logic_mod=_fake_logic(pos='体系 A', inc='将补全 ENCUT'))
    out = api.gen_preview('/p/POSCAR', '/p/INCAR')
    assert out['ok'] is True and out['error'] is None
    assert out['summary']['poscar'] == '体系 A'
    assert out['summary']['incar'] == '将补全 ENCUT'


def test_gen_preview_error_is_caught():
    boom = _fake_logic()
    boom.poscar_preview = lambda path, calc='slab': (_ for _ in ()).throw(RuntimeError('解析炸了'))
    api = Api(config_mod=_fake_config(), logic_mod=boom)
    out = api.gen_preview('/p/POSCAR', '/p/INCAR')
    assert out['ok'] is False and '解析炸了' in out['error']


# ── gen_state ────────────────────────────────────────────────────────────────
def test_gen_state_backfills_from_config():
    cfg = {'potcar_lib_root': '/lib'}
    ui = {'last_poscar': '/last/POSCAR', 'last_incar': '/last/INCAR', 'last_out': '/last/out'}
    api = Api(config_mod=_fake_config(cfg=cfg, ui=ui))
    out = api.gen_state()
    assert out == {'poscar': '/last/POSCAR', 'incar': '/last/INCAR',
                   'out_dir': '/last/out', 'lib_root': '/lib'}


def test_gen_state_error_is_caught():
    boom = types.SimpleNamespace(
        load_config=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('config坏了')),
        get_ui_state=lambda c=None: {})
    api = Api(config_mod=boom)
    out = api.gen_state()
    assert out['poscar'] == '' and out['lib_root'] == '' and 'config坏了' in out.get('error', '')


# ── gen_run(build→manifest→ledger 全链) ─────────────────────────────────────
def test_gen_run_full_chain_writes_manifest_and_registers():
    calls, written, registered = {}, {}, []
    payload = {'ok': True, 'out_dir': '/out/job1', 'warnings': ['⚠ 建议偶极修正'],
               'kpoints': [5, 5, 1], 'elements': ['Mo', 'S']}
    jb = types.SimpleNamespace(
        build_job_dir=lambda poscar, incar, out, **k: dict(payload))
    api = Api(config_mod=_fake_config(calls=calls),
              logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb,
              manifest_mod=_fake_manifest(written),
              ledger_mod=_fake_ledger_register(registered))
    out = api.gen_run('/p/POSCAR', '/p/INCAR', '/out/job1', '/lib')
    assert out['ok'] is True and out['job_dir'] == '/out/job1'
    assert out['warnings'] == ['⚠ 建议偶极修正'] and out['error'] is None
    # 落档 + 台账登记 + 持久化 lib/ui_state
    assert written == {'jd': '/out/job1', 'poscar': '/p/POSCAR', 'validate': True}
    assert registered == ['/out/job1']
    assert calls['lib'] == '/lib'
    assert calls['ui_state'] == {'last_poscar': '/p/POSCAR',
                                 'last_incar': '/p/INCAR', 'last_out': '/out/job1'}


def test_gen_run_passes_calc_type_through():
    """修复:web 生成页 calc_type 不再硬编码 slab,前端选择直达 build_job_dir。"""
    seen = {}
    payload = {'ok': True, 'out_dir': '/out/j', 'warnings': [], 'kpoints': [4, 4, 4],
               'elements': ['Si']}

    def _build(poscar, incar, out, **k):
        seen['calc_type'] = k.get('calc_type')
        return dict(payload)

    jb = types.SimpleNamespace(build_job_dir=_build)
    api = Api(config_mod=_fake_config(), logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb, manifest_mod=_fake_manifest({}),
              ledger_mod=_fake_ledger_register([]))
    api.gen_run('/p/POSCAR', '/p/INCAR', '/out/j', '/lib', 'bulk')
    assert seen['calc_type'] == 'bulk'


def test_gen_run_invalid_calc_type_falls_back_to_slab():
    seen = {}

    def _build(poscar, incar, out, **k):
        seen['calc_type'] = k.get('calc_type')
        return {'ok': True, 'out_dir': '/out/j', 'warnings': [], 'kpoints': [1, 1, 1],
                'elements': ['Si']}

    jb = types.SimpleNamespace(build_job_dir=_build)
    api = Api(config_mod=_fake_config(), logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb, manifest_mod=_fake_manifest({}),
              ledger_mod=_fake_ledger_register([]))
    api.gen_run('/p/POSCAR', '/p/INCAR', '/out/j', '/lib', 'nonsense')
    assert seen['calc_type'] == 'slab'


def test_gen_run_validation_error_short_circuits():
    jb = types.SimpleNamespace(
        build_job_dir=lambda *a, **k: (_ for _ in ()).throw(AssertionError('不应生成')))
    api = Api(config_mod=_fake_config(),
              logic_mod=_fake_logic(errs=['POSCAR 文件不存在或未选择']),
              job_builder_mod=jb)
    out = api.gen_run('', '/p/INCAR', '/out', '/lib')
    assert out['ok'] is False and out['job_dir'] is None
    assert 'POSCAR' in out['error']


def test_gen_run_build_exception_is_caught():
    jb = types.SimpleNamespace(
        build_job_dir=lambda poscar, incar, out, **k:
        (_ for _ in ()).throw(ValueError('POSCAR 缺元素符号行')))
    api = Api(config_mod=_fake_config(), logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb)
    out = api.gen_run('/p/POSCAR', '/p/INCAR', '/out', '/lib')
    assert out['ok'] is False and out['job_dir'] is None
    assert '缺元素符号行' in out['error']


def test_gen_run_manifest_failure_only_warns():
    """job.yaml/台账写失败不撤销已生成的四件套,只追加警告。"""
    registered = []
    payload = {'ok': True, 'out_dir': '/out/j', 'warnings': []}
    jb = types.SimpleNamespace(build_job_dir=lambda poscar, incar, out, **k: dict(payload))
    manifest = types.SimpleNamespace(
        create_from_build=lambda *a, **k: (_ for _ in ()).throw(OSError('磁盘满')))
    api = Api(config_mod=_fake_config(), logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb, manifest_mod=manifest,
              ledger_mod=_fake_ledger_register(registered))
    out = api.gen_run('/p/POSCAR', '/p/INCAR', '/out/j', '/lib')
    assert out['ok'] is True and out['job_dir'] == '/out/j'
    assert any('磁盘满' in w for w in out['warnings'])


# ── pick_file / pick_dir(注入 dialog_fn,零 webview) ────────────────────────
def test_pick_file_uses_injected_dialog():
    seen = {}
    api = Api(dialog_fn=lambda kind: seen.update(kind=kind) or '/chosen/POSCAR')
    out = api.pick_file('poscar')
    assert out['path'] == '/chosen/POSCAR' and seen['kind'] == 'poscar'


def test_pick_file_cancel_returns_none():
    api = Api(dialog_fn=lambda kind: None)
    out = api.pick_file('incar')
    assert out['path'] is None


def test_pick_dir_uses_injected_dialog():
    seen = {}
    api = Api(dialog_fn=lambda kind: seen.update(kind=kind) or '/chosen/dir')
    out = api.pick_dir()
    assert out['path'] == '/chosen/dir' and seen['kind'] == 'dir'


def test_pick_dir_cancel_returns_none():
    api = Api(dialog_fn=lambda kind: None)
    out = api.pick_dir()
    assert out['path'] is None


# ── 项目页假件 ────────────────────────────────────────────────────────────────
def _fake_adsorption(*, projects=None, proj_map=None, create_ret=None,
                     delta_ret=None, csv_ret=None, calls=None):
    """adsorption 假件:list/load/create/delta/export 全可注入;calls 收参数便于断言。"""
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.list_projects = lambda *a, **k: list(projects or [])
    m.load_project = lambda p: (proj_map or {}).get(p)

    def _create(root, name, **kw):
        calls['create'] = {'root': root, 'name': name, **kw}
        return dict(create_ret or {})
    m.create_project = _create
    m.delta_e_rows = lambda proj: dict(delta_ret or {})

    def _export(proj, summary, out):
        calls['export'] = {'out': out, 'summary': summary}
        return csv_ret if csv_ret is not None else out
    m.export_csv = _export
    return m


def _fake_report_full(*, member_dirs=None, report_ret='/out/报告.html', calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m._member_dirs = lambda proj: list(
        member_dirs if member_dirs is not None else [])

    def _gen(proj, out, *, config=None):
        calls['report'] = {'out': out, 'config': config, 'proj': proj}
        return report_ret
    m.generate_project_report = _gen
    return m


# ── proj_list ────────────────────────────────────────────────────────────────
def test_proj_list_assembles_path_name_members():
    proj = {'name': 'demo', 'members': {'clean_slab': '/s', 'gas_ref': None,
                                        'configs': ['/c1', '/c2']}}
    ads = _fake_adsorption(projects=['/p/project.yaml'],
                           proj_map={'/p/project.yaml': proj})
    rf = _fake_report_full(member_dirs=['/s', '/c1', '/c2'])
    api = Api(adsorption_mod=ads, report_full_mod=rf)
    out = api.proj_list()
    assert out['error'] is None
    assert out['projects'] == [{'path': '/p/project.yaml', 'name': 'demo',
                                'n_members': 3}]


def test_proj_list_counts_members_inline_without_report_full():
    """成员计数内联,绝不触碰 report_full(避免为渲染列表而拖入 matplotlib):
    注入一个 _member_dirs 必炸的 report_full 假件,proj_list 仍应内联算出 n_members=3。"""
    proj = {'name': 'demo', 'members': {'clean_slab': '/s', 'gas_ref': None,
                                        'configs': ['/c1', '/c2']}}
    ads = _fake_adsorption(projects=['/p/project.yaml'],
                           proj_map={'/p/project.yaml': proj})
    boom_rf = types.SimpleNamespace(
        _member_dirs=lambda proj: (_ for _ in ()).throw(
            AssertionError('must not be called')))
    api = Api(adsorption_mod=ads, report_full_mod=boom_rf)
    out = api.proj_list()
    assert out['error'] is None
    assert out['projects'] == [{'path': '/p/project.yaml', 'name': 'demo',
                                'n_members': 3}]


def test_proj_list_skips_unloadable_and_catches_error():
    boom = types.SimpleNamespace(
        list_projects=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('注册表坏了')))
    api = Api(adsorption_mod=boom, report_full_mod=_fake_report_full())
    out = api.proj_list()
    assert out['projects'] == [] and '注册表坏了' in out['error']


# ── proj_create ──────────────────────────────────────────────────────────────
def test_proj_create_mirrors_on_generate_params_and_transforms(tmp_path):
    slab = tmp_path / 'POSCAR_slab'
    slab.write_text('slab')
    incar = tmp_path / 'INCAR'
    incar.write_text('incar')
    calls = {}
    create_ret = {
        'ok': True, 'project_path': str(tmp_path / 'demo' / 'project.yaml'),
        'generated': [('demo_ads_x', '/o/x', ['偶极建议'])],
        'errors': [('demo_ads_bad', 'POTCAR 缺 Ta')],       # 坏组态 → warnings,不整体失败
        'advisories': [('P1', 'ENCUT', '建议统一 ENCUT=400')],
    }
    ads = _fake_adsorption(create_ret=create_ret, calls=calls)
    api = Api(adsorption_mod=ads, report_full_mod=_fake_report_full(),
              config_mod=_fake_config(cfg={'potcar_lib_root': '/lib'}))
    out = api.proj_create('demo', str(slab), ['/c1', '/c2'], str(incar), '',
                          str(tmp_path))
    assert out['ok'] is True
    assert out['project_path'] == str(tmp_path / 'demo' / 'project.yaml')
    assert out['error'] is None
    # 参数顺序/键忠实镜像 _on_generate → create_project
    c = calls['create']
    assert c['root'] == os.path.join(str(tmp_path), 'demo') and c['name'] == 'demo'
    assert c['clean_poscar'] == str(slab)
    assert c['config_poscars'] == ['/c1', '/c2']
    assert c['incar_path'] == str(incar)
    assert c['ref_poscar'] is None       # 空气相 → None(镜像可选气相参考)
    assert c['lib_root'] == '/lib'
    # advisories → "[级别] 文案" 字符串列表
    assert out['advisories'] == ['[P1·ENCUT] 建议统一 ENCUT=400']
    # 坏组态隔离:build 警告 + 组态错误都进 warnings(不整体失败)
    assert 'demo_ads_x:偶极建议' in out['warnings']
    assert 'demo_ads_bad:POTCAR 缺 Ta' in out['warnings']


def test_proj_create_validation_error_short_circuits(tmp_path):
    incar = tmp_path / 'INCAR'
    incar.write_text('incar')
    ads = _fake_adsorption(create_ret={'ok': True})
    api = Api(adsorption_mod=ads, config_mod=_fake_config())
    # 清洁表面缺失 → 校验拦截,不触碰 create_project
    ads.create_project = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('不应生成'))
    out = api.proj_create('demo', '/no/such/slab', ['/c1'], str(incar), '',
                          str(tmp_path))
    assert out['ok'] is False and out['project_path'] is None
    assert '清洁表面' in out['error']


def test_proj_create_exception_is_caught(tmp_path):
    slab = tmp_path / 'POSCAR'
    slab.write_text('s')
    incar = tmp_path / 'INCAR'
    incar.write_text('i')
    ads = _fake_adsorption()
    ads.create_project = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('赝势缺失'))
    api = Api(adsorption_mod=ads, config_mod=_fake_config())
    out = api.proj_create('demo', str(slab), ['/c1'], str(incar), '', str(tmp_path))
    assert out['ok'] is False and out['project_path'] is None
    assert '赝势缺失' in out['error']


# ── proj_delta ───────────────────────────────────────────────────────────────
def test_proj_delta_passes_through_rows_and_gating():
    proj = {'name': 'demo', 'members': {}}
    delta_ret = {
        'slab': ('DONE', -12.5), 'ref': ('无', None), 'has_ref': False,
        'rows': [
            {'name': 'demo_ads_a', 'state': 'DONE', 'e_config': -20.0,
             'delta_e': -2.5, 'note': ''},
            {'name': 'demo_ads_b', 'state': 'RUNNING', 'e_config': None,
             'delta_e': None, 'note': '组态未完成'},
        ],
    }
    ads = _fake_adsorption(proj_map={'/p': proj}, delta_ret=delta_ret)
    api = Api(adsorption_mod=ads, report_full_mod=_fake_report_full())
    out = api.proj_delta('/p')
    assert out['ok'] is True and out['error'] is None
    assert out['rows'][0]['delta_e'] == -2.5
    assert out['rows'][1]['delta_e'] is None and '组态未完成' in out['rows'][1]['note']
    assert '清洁表面' in out['note']


def test_proj_delta_missing_project_error():
    ads = _fake_adsorption(proj_map={})
    api = Api(adsorption_mod=ads, report_full_mod=_fake_report_full())
    out = api.proj_delta('/gone')
    assert out['ok'] is False and out['rows'] == []
    assert '项目' in out['error']


# ── proj_export_csv ──────────────────────────────────────────────────────────
def test_proj_export_csv_delegates():
    proj = {'name': 'demo', 'members': {}}
    calls = {}
    ads = _fake_adsorption(proj_map={'/p': proj}, delta_ret={'rows': []},
                           csv_ret='/save/demo.csv', calls=calls)
    api = Api(adsorption_mod=ads, report_full_mod=_fake_report_full())
    out = api.proj_export_csv('/p', '/save/demo.csv')
    assert out['ok'] is True and out['file'] == '/save/demo.csv'
    assert out['error'] is None and calls['export']['out'] == '/save/demo.csv'


def test_proj_export_csv_missing_project_error():
    ads = _fake_adsorption(proj_map={})
    api = Api(adsorption_mod=ads, report_full_mod=_fake_report_full())
    out = api.proj_export_csv('/gone', '/save/x.csv')
    assert out['ok'] is False and out['file'] is None and '项目' in out['error']


# ── proj_report ──────────────────────────────────────────────────────────────
def test_proj_report_generates_via_report_full():
    proj = {'name': 'demo', 'members': {}}
    calls = {}
    ads = _fake_adsorption(proj_map={'/p': proj})
    rf = _fake_report_full(member_dirs=['/s', '/c1'],
                           report_ret='/save/报告.html', calls=calls)
    api = Api(adsorption_mod=ads, report_full_mod=rf,
              config_mod=_fake_config(cfg={'k': 'v'}))
    out = api.proj_report('/p', '/save/报告.html')
    assert out['ok'] is True and out['file'] == '/save/报告.html'
    assert calls['report']['out'] == '/save/报告.html'
    assert calls['report']['config'] == {'k': 'v'} and calls['report']['proj'] is proj


def test_proj_report_no_members_error():
    proj = {'name': 'demo', 'members': {}}
    ads = _fake_adsorption(proj_map={'/p': proj})
    rf = _fake_report_full(member_dirs=[])       # 无成员作业
    api = Api(adsorption_mod=ads, report_full_mod=rf, config_mod=_fake_config())
    out = api.proj_report('/p', '/save/x.html')
    assert out['ok'] is False and out['file'] is None and '成员' in out['error']


# ── adopt_root_get / adopt_root_set(认领本地根目录配置) ──────────────────────
def test_adopt_root_get_default_when_unset():
    api = Api(config_mod=_fake_config(ui={}))
    out = api.adopt_root_get()
    assert out['root'] == os.path.join(os.path.expanduser('~'), 'vcstudio_jobs')


def test_adopt_root_get_returns_configured():
    api = Api(config_mod=_fake_config(ui={'adopt_root': 'E:\\claimed'}))
    out = api.adopt_root_get()
    assert out['root'] == 'E:\\claimed'


def test_adopt_root_get_error_falls_back_to_default():
    boom = types.SimpleNamespace(
        get_ui_state=lambda c=None: (_ for _ in ()).throw(RuntimeError('config坏了')))
    api = Api(config_mod=boom)
    out = api.adopt_root_get()
    assert out['root'] == os.path.join(os.path.expanduser('~'), 'vcstudio_jobs')


def test_adopt_root_set_persists_via_ui_state():
    calls = {}
    api = Api(config_mod=_fake_config(calls=calls))
    out = api.adopt_root_set('E:\\myroot')
    assert out['ok'] is True and out['error'] is None
    assert calls['ui_state'] == {'adopt_root': 'E:\\myroot'}


def test_adopt_root_set_rejects_blank():
    api = Api(config_mod=_fake_config())
    out = api.adopt_root_set('   ')
    assert out['ok'] is False and out['error']


def test_adopt_root_set_error_is_caught():
    boom = types.SimpleNamespace(
        set_ui_state=lambda **kv: (_ for _ in ()).throw(OSError('磁盘满')))
    api = Api(config_mod=boom)
    out = api.adopt_root_set('E:\\x')
    assert out['ok'] is False and '磁盘满' in out['error']


# ── adopt_all(委托 batch_ops.adopt_scan,known_ids 从台账,root 从配置) ────────
def test_adopt_all_delegates_with_known_ids_and_root():
    calls = {}
    bo = types.SimpleNamespace(adopt_scan=lambda prof, pw, tn, known, root:
        calls.update(known=known, root=root, tn=tn) or
        {'needs_trust': False, 'results': [['200', True, '已认领 → X']]})
    entries = [
        ('/a', {'scheduler_job_id': '100'}),
        ('/b', {'scheduler_job_id': '200'}),
        ('/c', {'state': 'DONE'}),                       # 无作业号 → 不入 known
        ('/gone', None),                                 # 失效条目 → 不入 known
    ]
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo,
              ledger_mod=_fake_ledger(entries, []),
              config_mod=_fake_config(ui={'adopt_root': 'E:\\root'}))
    out = api.adopt_all('c1', None, True)
    assert calls['known'] == {'100', '200'} and calls['tn'] is True
    assert calls['root'] == 'E:\\root'
    assert out['results'][0][1] is True


def test_adopt_all_unknown_profile_error():
    api = Api(profiles_mod=_fake_profiles({}), ledger_mod=_fake_ledger([], []),
              config_mod=_fake_config())
    out = api.adopt_all('nope', None, False)
    assert out.get('error') and '集群' in out['error']


def test_adopt_all_batch_ops_exception_caught():
    bo = types.SimpleNamespace(
        adopt_scan=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('连不上')))
    store = {'c1': ClusterProfile(name='c1', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo,
              ledger_mod=_fake_ledger([], []), config_mod=_fake_config())
    out = api.adopt_all('c1', None, False)
    assert '连不上' in out['error']


def test_query_workdir_delegates_to_batch_ops():
    import types
    calls = {}
    bo = types.SimpleNamespace(workdir_lookup=lambda prof, pw, jid, tn:
        calls.update(jid=jid) or {'needs_trust': False, 'workdir': '/home/u/dir with space'})
    from vcstudio.cluster.profiles import ClusterProfile
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key', key_path='/k')}

    def _fake_profiles_mod():
        m = types.SimpleNamespace()
        m.load_profiles = lambda: dict(store)
        m.save_profiles = lambda p: None
        m.ClusterProfile = ClusterProfile
        return m

    from vcstudio.gui_web.api import Api
    api = Api(profiles_mod=_fake_profiles_mod(), batch_ops_mod=bo)
    out = api.query_workdir(205690, 'c1', None, False)
    assert calls['jid'] == '205690'
    assert out['workdir'] == '/home/u/dir with space'


# ── conv_series (C1 收敛过程可视化) ──────────────────────────────────────────
_OSZICAR_2 = (
    "DAV:   1     0.11600000E+03   0.116E+03   0.116E+03   864   0.5\n"
    "DAV:   2    -0.84943000E+02  -0.200E+03  -0.200E+03   912   0.1\n"
    "   1 F= -.85018175E+02 E0= -.85018175E+02  d E =-.850182E+02\n"
    "DAV:   1    -0.85000000E+02  -0.100E-01  -0.100E-01   700   0.01\n"
    "   2 F= -.85120000E+02 E0= -.85120000E+02  d E =-.101825E+00\n"
)
_OUTCAR_2 = (
    " POSITION                                       TOTAL-FORCE (eV/Angst)\n"
    " -----------------------------------------------------------------------\n"
    "      0.0      0.0      0.0         0.300000     0.400000     0.000000\n"
    " -----------------------------------------------------------------------\n"
    "    total drift:                   0.0 0.0 0.0\n"
    " POSITION                                       TOTAL-FORCE (eV/Angst)\n"
    " -----------------------------------------------------------------------\n"
    "      0.0      0.0      0.0         0.000000     0.030000     0.040000\n"
    " -----------------------------------------------------------------------\n"
)


def test_conv_series_reads_local_oszicar_and_outcar(tmp_path):
    (tmp_path / 'OSZICAR').write_text(_OSZICAR_2, encoding='utf-8')
    (tmp_path / 'OUTCAR').write_text(_OUTCAR_2, encoding='utf-8')
    api = Api()  # 用真实 convergence 纯模块
    out = api.conv_series(str(tmp_path))
    assert out['ok'] is True
    s = out['series']
    assert s['steps'] == [1, 2]
    assert s['E0'][0] == -85.018175
    assert s['fmax'][0] == 0.5
    assert s['have_forces'] is True


def test_conv_series_missing_oszicar_gives_error(tmp_path):
    api = Api()
    out = api.conv_series(str(tmp_path))
    assert out['ok'] is False
    assert 'OSZICAR' in out['error']


def test_conv_series_no_outcar_degrades(tmp_path):
    (tmp_path / 'OSZICAR').write_text(_OSZICAR_2, encoding='utf-8')
    api = Api()
    out = api.conv_series(str(tmp_path))
    assert out['ok'] is True
    assert out['series']['have_forces'] is False
    assert out['series']['fmax'] == [None, None]


def test_conv_series_injects_conv_mod(tmp_path):
    (tmp_path / 'OSZICAR').write_text('whatever', encoding='utf-8')
    seen = {}

    def _fake_series(osz, outc=None):
        seen['osz'] = osz
        seen['outc'] = outc
        return {'steps': [1], 'E0': [-1.0], 'dE': [None], 'fmax': [None],
                'scf_iters': [1], 'have_forces': False, 'notes': []}

    fake = types.SimpleNamespace(convergence_series=_fake_series)
    api = Api(conv_mod=fake)
    out = api.conv_series(str(tmp_path))
    assert out['ok'] is True
    assert seen['osz'] == 'whatever'
    assert seen['outc'] is None  # 无 OUTCAR → 传 None


def test_conv_series_error_is_caught(tmp_path):
    (tmp_path / 'OSZICAR').write_text('x', encoding='utf-8')

    def _boom(osz, outc=None):
        raise RuntimeError('parse boom')

    api = Api(conv_mod=types.SimpleNamespace(convergence_series=_boom))
    out = api.conv_series(str(tmp_path))
    assert out['ok'] is False
    assert 'parse boom' in out['error']


# ── struct_view (C2 结构 3D 预览) ────────────────────────────────────────────
_POSCAR_MIN = (
    'test\n1.0\n10 0 0\n0 10 0\n0 0 30\nC S\n1 1\nDirect\n'
    '0.05 0.05 0.333333333333\n0.05 0.05 0.433333333333\n'
)


def test_struct_view_reads_file_directly(tmp_path):
    f = tmp_path / 'POSCAR'
    f.write_text(_POSCAR_MIN, encoding='utf-8')
    api = Api()  # 真 structure_view 纯模块
    out = api.struct_view(str(f))
    assert out['ok'] is True
    assert out['view']['natoms'] == 2
    assert out['view']['gap']['level'] == 'ok'
    assert out['used'] == 'POSCAR'


def test_struct_view_auto_prefers_contcar(tmp_path):
    (tmp_path / 'POSCAR').write_text(_POSCAR_MIN, encoding='utf-8')
    (tmp_path / 'CONTCAR').write_text(_POSCAR_MIN, encoding='utf-8')
    api = Api()
    out = api.struct_view(str(tmp_path), 'AUTO')
    assert out['ok'] is True and out['used'] == 'CONTCAR'
    # 只有 POSCAR 时回退
    import os as _os
    _os.remove(str(tmp_path / 'CONTCAR'))
    out2 = api.struct_view(str(tmp_path), 'AUTO')
    assert out2['ok'] is True and out2['used'] == 'POSCAR'


def test_struct_view_missing_file_error(tmp_path):
    api = Api()
    out = api.struct_view(str(tmp_path), 'AUTO')
    assert out['ok'] is False
    assert 'CONTCAR' in out['error'] or 'POSCAR' in out['error']


def test_struct_view_auto_falls_back_on_unparseable_contcar(tmp_path):
    # CONTCAR 非空但解析失败(只有换行)→ 回退用旁边合法的 POSCAR
    (tmp_path / 'CONTCAR').write_text('\n', encoding='utf-8')
    (tmp_path / 'POSCAR').write_text(_POSCAR_MIN, encoding='utf-8')
    api = Api()
    out = api.struct_view(str(tmp_path), 'AUTO')
    assert out['ok'] is True and out['used'] == 'POSCAR'


def test_struct_view_parse_error_is_caught(tmp_path):
    f = tmp_path / 'POSCAR'
    f.write_text('garbage\n', encoding='utf-8')
    api = Api()
    out = api.struct_view(str(f))
    assert out['ok'] is False and out['error']


# ── dos_view (C4 DOS 出图) ───────────────────────────────────────────────────
_VASPRUN_MIN = (
    '<?xml version="1.0"?>\n<modeling><calculation><dos>'
    '<i name="efermi"> -2.0 </i><total><array><set>'
    '<set comment="spin 1"><r> -5.0 1.0 0.5 </r><r> 0.0 2.0 1.0 </r></set>'
    '</set></array></total></dos></calculation></modeling>\n'
)


def test_dos_view_renders_and_saves(tmp_path):
    (tmp_path / 'vasprun.xml').write_text(_VASPRUN_MIN, encoding='utf-8')
    api = Api()
    out = api.dos_view(str(tmp_path))
    assert out['ok'] is True
    assert out['svg'].startswith('<svg')
    assert out['saved'] and out['saved'].endswith('dos.svg')
    import os as _os
    assert _os.path.isfile(out['saved'])


def test_dos_view_missing_vasprun_error(tmp_path):
    api = Api()
    out = api.dos_view(str(tmp_path))
    assert out['ok'] is False and 'vasprun' in out['error']


def test_dos_view_bad_xml_error(tmp_path):
    (tmp_path / 'vasprun.xml').write_text('<broken', encoding='utf-8')
    api = Api()
    out = api.dos_view(str(tmp_path))
    assert out['ok'] is False and out['error']


def test_dos_view_save_failure_only_warns(tmp_path, monkeypatch):
    (tmp_path / 'vasprun.xml').write_text(_VASPRUN_MIN, encoding='utf-8')
    api = Api()
    import builtins
    real_open = builtins.open

    def deny_write(path, mode='r', *a, **kw):
        if 'w' in mode and str(path).endswith('dos.svg'):
            raise PermissionError('denied')
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, 'open', deny_write)
    out = api.dos_view(str(tmp_path))
    assert out['ok'] is True and out['svg'].startswith('<svg')
    assert out['saved'] is None
    assert any('保存' in w or 'denied' in w for w in out['warnings'])


# ── methods_text (C3 Methods 段生成) ─────────────────────────────────────────
def test_methods_text_full_job_dir(tmp_path):
    (tmp_path / 'INCAR').write_text(
        'GGA = RP\nENCUT = 400\nISMEAR = 0\nSIGMA = 0.05\nIVDW = 11\n',
        encoding='utf-8')
    (tmp_path / 'KPOINTS').write_text(
        'mesh\n0\nGamma\n3 3 1\n0 0 0\n', encoding='utf-8')
    (tmp_path / 'POTCAR').write_text(
        ' TITEL  = PAW_PBE C 08Apr2002\n TITEL  = PAW_PBE S 06Sep2000\n',
        encoding='utf-8')
    api = Api()
    out = api.methods_text(str(tmp_path))
    assert out['ok'] is True
    assert 'RPBE' in out['zh'] and 'RPBE' in out['en']
    assert 'Kresse' in out['bibtex']
    assert out['warnings'] == []


def test_methods_text_missing_incar_is_error(tmp_path):
    api = Api()
    out = api.methods_text(str(tmp_path))
    assert out['ok'] is False and 'INCAR' in out['error']


def test_methods_text_missing_optional_files_degrade(tmp_path):
    (tmp_path / 'INCAR').write_text('ENCUT = 400\n', encoding='utf-8')
    api = Api()
    out = api.methods_text(str(tmp_path))
    assert out['ok'] is True
    assert any('KPOINTS' in w for w in out['warnings'])
    assert any('POTCAR' in w for w in out['warnings'])


def test_methods_text_injects_methods_mod(tmp_path):
    (tmp_path / 'INCAR').write_text('x', encoding='utf-8')
    seen = {}
    fake = types.SimpleNamespace(
        extract_facts=lambda i, k, p: (seen.update(i=i, k=k, p=p) or
                                       {'facts': {'f': 1}, 'warnings': []}),
        render_zh=lambda f: 'ZH',
        render_en=lambda f: 'EN',
        render_bibtex=lambda f: 'BIB')
    api = Api(methods_mod=fake)
    out = api.methods_text(str(tmp_path))
    assert out['ok'] is True and out['zh'] == 'ZH' and out['bibtex'] == 'BIB'
    assert seen['i'] == 'x' and seen['k'] is None and seen['p'] is None


def test_struct_view_injects_sview_mod(tmp_path):
    f = tmp_path / 'POSCAR'
    f.write_text('whatever', encoding='utf-8')
    seen = {}

    def _fake(content):
        seen['content'] = content
        return {'xyz': '0\nx\n', 'natoms': 0, 'formula': '', 'gap': {}, 'notes': []}

    api = Api(sview_mod=types.SimpleNamespace(structure_view=_fake))
    out = api.struct_view(str(f))
    assert out['ok'] is True
    assert seen['content'] == 'whatever'
