"""Api 处理器纯逻辑测试:注入假模块,零网络零 keyring。

每个公开方法至少一个 happy + 一个错误路径;所有返回一律 JSON-safe dict,
异常绝不穿透到 JS(错误落 'error' 字段)。中文注释允许,英文标识符。
"""
import os
import sys
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
        'errors': [('demo_ads_bad', 'POTCAR 缺 Ta')],       # 坏构型 → warnings,不整体失败
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
    # 坏构型隔离:build 警告 + 构型错误都进 warnings(不整体失败)
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
             'delta_e': None, 'note': '构型未完成'},
        ],
    }
    ads = _fake_adsorption(proj_map={'/p': proj}, delta_ret=delta_ret)
    api = Api(adsorption_mod=ads, report_full_mod=_fake_report_full())
    out = api.proj_delta('/p')
    assert out['ok'] is True and out['error'] is None
    assert out['rows'][0]['delta_e'] == -2.5
    assert out['rows'][1]['delta_e'] is None and '构型未完成' in out['rows'][1]['note']
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


# ── proj_figures / proj_compare_figures(原生出图接线) ───────────────────────
def _fake_ncharts(calls):
    """native_charts 假件:记录每次调用与参数,返回假文件路径。"""
    m = types.SimpleNamespace()

    def _rec(kind):
        def _f(data, out_path, **kw):
            calls.setdefault(kind, []).append({'data': data, 'out': out_path, **kw})
            return [str(out_path)]
        return _f
    m.adsorption_bar = _rec('bar')
    m.energy_matrix_table = _rec('table')
    m.free_energy_ladder = _rec('ladder')
    m.heatmap_matrix = _rec('heatmap')
    m.volcano_plot = _rec('volcano')
    m.scaling_relation = lambda xs, ys, out_path, **kw: (
        calls.setdefault('scaling', []).append(
            {'xs': xs, 'ys': ys, 'out': out_path, **kw}) or [str(out_path)])
    return m


def _proj(name, root):
    return {'name': name, 'root': root,
            'members': {'clean_slab': '/s', 'gas_ref': None, 'configs': []}}


def _delta(names_to_de, slab=('DONE', -100.0)):
    rows = [{'name': n, 'state': 'DONE' if d is not None else 'RUNNING',
             'e_config': None, 'delta_e': d, 'note': ''}
            for n, d in names_to_de.items()]
    return {'slab': slab, 'ref': ('无', None), 'has_ref': False, 'rows': rows}


def test_proj_figures_bar_table_with_short_names(tmp_path):
    calls = {}
    proj = _proj('liS', str(tmp_path))
    ads = _fake_adsorption(proj_map={'/p/project.yaml': proj},
                           delta_ret=_delta({'liS_ads_Li2S4': -1.2,
                                             'liS_ads_Li2S2': None}))
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              config_mod=_fake_config())
    out = api.proj_figures('/p/project.yaml', ['bar', 'table'])
    assert out['ok'] is True and len(out['files']) == 2
    # 短名剥前缀 + 只收已完成 ΔE
    data = calls['bar'][0]['data']
    assert data['adsorbates'] == ['Li2S4']
    assert data['substrates'] == {'liS': [-1.2]}
    assert calls['bar'][0]['negative_up'] is True
    assert out['out_dir'] == str(tmp_path / 'figures')


def test_proj_figures_no_done_rows_all_skipped(tmp_path):
    calls = {}
    ads = _fake_adsorption(proj_map={'/p': _proj('x', str(tmp_path))},
                           delta_ret=_delta({'x_ads_a': None}))
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              config_mod=_fake_config())
    out = api.proj_figures('/p', ['bar', 'table'])
    assert out['ok'] is True and out['files'] == []
    assert {s['kind'] for s in out['skipped']} == {'bar', 'table'}
    assert 'bar' not in calls


def test_proj_figures_ladder_uses_fed_pds_index(tmp_path):
    calls = {}
    mol_dir = tmp_path / 'mols'
    mol_dir.mkdir()
    fed = {'steps': [{'label': 'S8*', 'G': 0.0}, {'label': 'Li2S*', 'G': -1.0}],
           'pds_index': 0, 'u_l': 1.5, 'mu_li': -1.65, 'per_electron': [0.5],
           'thermo_corrected': False}
    fe = types.SimpleNamespace(
        path_from_project_and_molecules=lambda rows, e_slab, molecules_dir: fed)
    ads = _fake_adsorption(proj_map={'/p': _proj('liS', str(tmp_path))},
                           delta_ret=_delta({'liS_ads_Li2S4': -1.2}))
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              freeenergy_mod=fe,
              config_mod=_fake_config(cfg={'lis_molecules_dir': str(mol_dir)}))
    out = api.proj_figures('/p', ['ladder'])
    assert out['ok'] is True and len(out['files']) == 1
    lad = calls['ladder'][0]
    assert lad['pds_index'] == 0                      # 逐电子权威口径透传
    assert lad['step_labels'] == ['S8*', 'Li2S*']
    assert lad['data'] == [{'name': 'liS', 'G': [0.0, -1.0]}]


def test_proj_figures_ladder_skipped_without_molecules_dir(tmp_path):
    calls = {}
    ads = _fake_adsorption(proj_map={'/p': _proj('liS', str(tmp_path))},
                           delta_ret=_delta({'liS_ads_a': -1.0}))
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              config_mod=_fake_config())          # 无 lis_molecules_dir
    out = api.proj_figures('/p', ['ladder'])
    assert out['ok'] is True and out['files'] == []
    assert out['skipped'][0]['kind'] == 'ladder'
    assert 'lis_molecules_dir' in out['skipped'][0]['reason']


def test_proj_compare_figures_heatmap_union_cols(tmp_path):
    calls = {}
    p1, p2 = _proj('A', str(tmp_path / 'a')), _proj('B', str(tmp_path / 'b'))
    deltas = {'/a': _delta({'A_ads_S8': -0.5, 'A_ads_Li2S': -2.0}),
              '/b': _delta({'B_ads_S8': -0.8})}
    ads = _fake_adsorption(proj_map={'/a': p1, '/b': p2})
    ads.delta_e_rows = lambda proj: deltas['/a' if proj['name'] == 'A' else '/b']
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              config_mod=_fake_config())
    out = api.proj_compare_figures(['/a', '/b'], ['heatmap'])
    assert out['ok'] is True and len(out['files']) == 1
    data = calls['heatmap'][0]['data']
    assert data['rows'] == ['A', 'B']
    assert data['cols'] == ['S8', 'Li2S']            # 首见序并集
    assert data['values'] == [[-0.5, -2.0], [-0.8, None]]


def test_proj_compare_figures_needs_two_projects():
    api = Api(adsorption_mod=_fake_adsorption(proj_map={}),
              native_charts_mod=_fake_ncharts({}), config_mod=_fake_config())
    out = api.proj_compare_figures(['/only'], ['heatmap'])
    assert out['ok'] is False and '2 个' in out['error']


def test_proj_compare_scaling_pair_and_volcano_skip(tmp_path):
    calls = {}
    projs = {f'/p{i}': _proj(f'M{i}', str(tmp_path / f'p{i}')) for i in range(3)}
    des = {'M0': {'M0_ads_a': -1.0, 'M0_ads_b': -2.0},
           'M1': {'M1_ads_a': -1.5, 'M1_ads_b': -2.6},
           'M2': {'M2_ads_a': -2.0, 'M2_ads_b': -3.1}}
    ads = _fake_adsorption(proj_map=projs)
    ads.delta_e_rows = lambda proj: _delta(des[proj['name']])
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              config_mod=_fake_config())          # 无分子库 → volcano 应 skip
    out = api.proj_compare_figures(list(projs), ['scaling', 'volcano'])
    assert out['ok'] is True
    sc = calls['scaling'][0]
    assert sc['xs'] == [-1.0, -1.5, -2.0] and sc['ys'] == [-2.0, -2.6, -3.1]
    assert [s['kind'] for s in out['skipped']] == ['volcano']
    assert 'U_L' in out['skipped'][0]['reason']


# ── 设置页 / 自动驾驶 假件 ────────────────────────────────────────────────────
def _fake_config_rw(backing):
    """可读写 config 假件:load_config/save_config/get_ui_state/set_ui_state 共享 backing。"""
    m = types.SimpleNamespace()
    m.load_config = lambda *a, **k: dict(backing)
    m.save_config = lambda cfg, *a, **k: (backing.clear() or backing.update(cfg))

    def _get_ui(c=None):
        src = c if c is not None else backing
        return dict(src.get('ui') or {})
    m.get_ui_state = _get_ui

    def _set_ui(**kv):
        ui = dict(backing.get('ui') or {})
        ui.update({k: v for k, v in kv.items() if v is not None})
        backing['ui'] = ui
    m.set_ui_state = _set_ui
    m.set_potcar_lib_root = lambda p, *a, **k: backing.__setitem__('potcar_lib_root', p)
    return m


def _fake_ai(calls=None, key_saved=False):
    """ai_analysis 假件:save/load key + probe(用注入 transport)+ 默认预设常量。"""
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.DEFAULT_PROMPT_PRESET = 'DEFAULT_PRESET_TEXT'
    m.save_api_key = lambda k: calls.__setitem__('saved_key', k)
    m.load_api_key = lambda: 'stored-key' if key_saved else None

    def _probe(*, api_key=None, base_url='', model='', transport=None, timeout=20):
        calls['probe'] = {'base_url': base_url, 'model': model}
        if transport is not None:
            st, _raw = transport(base_url or 'u', b'{}', {}, timeout)
            return {'ok': st == 200, 'error': None if st == 200 else f'HTTP {st}'}
        return {'ok': True, 'error': None}
    m.probe = _probe
    return m


def _fake_manifest_mod(states):
    """manifest 假件:load_manifest 按目录返回注入的 manifest dict。"""
    m = types.SimpleNamespace()
    m.load_manifest = lambda d: states.get(d)
    m.create_from_build = lambda *a, **k: None
    return m


# ── settings_get / llm_save / key 状态掩码 ───────────────────────────────────
def test_settings_get_aggregates_and_masks_key():
    backing = {'potcar_lib_root': '/lib', 'lis_molecules_dir': '/mols',
               'ideal_window': [-1.0, 0.5],
               'llm': {'base_url': 'u', 'model': 'm', 'allow_external': True},
               'ui': {'theme': 'paper', 'poll_interval': 5, 'autopilot_fetch': False}}
    api = Api(config_mod=_fake_config_rw(backing), ai_analysis_mod=_fake_ai(key_saved=True))
    out = api.settings_get()
    assert out['ok'] is True
    assert out['llm'] == {'base_url': 'u', 'model': 'm', 'allow_external': True,
                          'key_saved': True}
    assert 'api_key' not in out['llm'] and 'stored-key' not in str(out)   # 绝不回显密钥
    assert out['paths']['lis_molecules_dir'] == '/mols'
    assert out['paths']['ideal_window'] == [-1.0, 0.5]
    assert out['ui']['theme'] == 'paper' and out['ui']['poll_interval'] == 5
    assert out['ui']['autopilot_fetch'] is False and out['ui']['autopilot'] is True
    assert out['prompt']['is_default'] is True and 'DEFAULT_PRESET_TEXT' in out['prompt']['text']


def test_settings_get_defaults_when_unset():
    api = Api(config_mod=_fake_config_rw({}), ai_analysis_mod=_fake_ai(key_saved=False))
    out = api.settings_get()
    assert out['ui'] == {'theme': 'classic', 'autopilot': True, 'poll_interval': 10,
                         'autopilot_continue': True, 'autopilot_fetch': True,
                         'autopilot_report': True}
    assert out['llm']['key_saved'] is False and out['llm']['base_url'] == ''


def test_llm_save_roundtrip():
    backing = {}
    api = Api(config_mod=_fake_config_rw(backing))
    out = api.llm_save('https://api.openai.com/v1/chat/completions', 'gpt-4o', True)
    assert out['ok'] is True
    assert backing['llm'] == {'base_url': 'https://api.openai.com/v1/chat/completions',
                              'model': 'gpt-4o', 'allow_external': True}


def test_llm_key_save_and_status():
    calls = {}
    api = Api(ai_analysis_mod=_fake_ai(calls=calls, key_saved=False))
    out = api.llm_key_save('sk-123')
    assert out['ok'] is True and calls['saved_key'] == 'sk-123'
    assert api.llm_key_status() == {'saved': False}
    api2 = Api(ai_analysis_mod=_fake_ai(key_saved=True))
    assert api2.llm_key_status() == {'saved': True}


def test_llm_key_save_rejects_blank():
    api = Api(ai_analysis_mod=_fake_ai())
    out = api.llm_key_save('   ')
    assert out['ok'] is False and out['error']


# ── llm_test(注入假 transport) ──────────────────────────────────────────────
def test_llm_test_ok_with_injected_transport():
    calls = {}
    api = Api(ai_analysis_mod=_fake_ai(calls=calls))
    out = api.llm_test('http://api', 'gpt', transport=lambda u, b, h, t: (200, b'{}'))
    assert out['ok'] is True and calls['probe']['base_url'] == 'http://api'


def test_llm_test_http_error():
    api = Api(ai_analysis_mod=_fake_ai())
    out = api.llm_test('http://api', 'gpt', transport=lambda u, b, h, t: (401, b'no'))
    assert out['ok'] is False and '401' in out['error']


# ── prompt_get / prompt_save / prompt_reset ──────────────────────────────────
def test_prompt_save_get_reset_roundtrip():
    backing = {}
    api = Api(config_mod=_fake_config_rw(backing), ai_analysis_mod=_fake_ai())
    g0 = api.prompt_get()
    assert g0['is_default'] is True and g0['text'] == 'DEFAULT_PRESET_TEXT'
    assert api.prompt_save('MY CUSTOM PROMPT')['ok'] is True
    assert backing['llm']['prompt_preset'] == 'MY CUSTOM PROMPT'
    g1 = api.prompt_get()
    assert g1['is_default'] is False and g1['text'] == 'MY CUSTOM PROMPT'
    r = api.prompt_reset()
    assert r['ok'] is True and 'prompt_preset' not in backing.get('llm', {})
    assert api.prompt_get()['is_default'] is True


def test_prompt_save_rejects_blank():
    api = Api(config_mod=_fake_config_rw({}), ai_analysis_mod=_fake_ai())
    assert api.prompt_save('   ')['ok'] is False


# ── paths_save / theme_set / autopilot_save ──────────────────────────────────
def test_paths_save_roundtrip_and_ideal_window():
    backing = {}
    api = Api(config_mod=_fake_config_rw(backing))
    out = api.paths_save('/lib2', '/mols2', '-1.5', '0.5')
    assert out['ok'] is True
    assert backing['potcar_lib_root'] == '/lib2'
    assert backing['lis_molecules_dir'] == '/mols2'
    assert backing['ideal_window'] == [-1.5, 0.5]
    # 两个都留空 → 移除 ideal_window
    api.paths_save('/lib2', '/mols2', '', '')
    assert 'ideal_window' not in backing


def test_paths_save_partial_ideal_window_error():
    api = Api(config_mod=_fake_config_rw({}))
    out = api.paths_save('/lib', '/mols', '1.0', '')
    assert out['ok'] is False and 'ideal_window' in out['error']


def test_theme_set_persists_and_validates():
    backing = {}
    api = Api(config_mod=_fake_config_rw(backing))
    assert api.theme_set('deep')['theme'] == 'deep' and backing['ui']['theme'] == 'deep'
    assert api.theme_set('bogus')['theme'] == 'classic'   # 非法 → classic


def test_autopilot_save_persists_subswitches():
    backing = {}
    api = Api(config_mod=_fake_config_rw(backing))
    api.autopilot_save(True, 15, True, False, True)
    ui = backing['ui']
    assert ui['autopilot'] is True and ui['poll_interval'] == 15
    assert ui['autopilot_continue'] is True and ui['autopilot_fetch'] is False
    assert ui['autopilot_report'] is True
    # 非法间隔回落 10
    api.autopilot_save(poll_interval=999)
    assert backing['ui']['poll_interval'] == 10


# ── pipeline_tick(幂等:首拍 report_done + 写标记,次拍无重复) ──────────────────
def test_pipeline_tick_report_done_idempotent(tmp_path):
    calls, saved = {}, []
    proj = {'name': 'liS', 'root': str(tmp_path),
            'members': {'clean_slab': '/s', 'gas_ref': None, 'configs': ['/c1']}}
    ads = _fake_adsorption(projects=['/p/project.yaml'],
                           proj_map={'/p/project.yaml': proj},
                           delta_ret=_delta({'liS_ads_Li2S4': -1.2}))
    ads.save_project = lambda root, p: saved.append(root)
    manifest = _fake_manifest_mod({'/s': {'state': 'DONE', 'results': {}},
                                   '/c1': {'state': 'DONE', 'results': {}}})
    rf = _fake_report_full(member_dirs=['/s', '/c1'],
                           report_ret=str(tmp_path / 'report' / 'liS_report.html'))
    api = Api(profiles_mod=_fake_profiles({}), adsorption_mod=ads, manifest_mod=manifest,
              native_charts_mod=_fake_ncharts(calls), report_full_mod=rf,
              config_mod=_fake_config())
    out1 = api.pipeline_tick()
    assert out1['ok'] is True and out1['last_sync']
    rd = [e for e in out1['events'] if e['kind'] == 'report_done']
    assert len(rd) == 1 and rd[0]['project'] == 'liS'
    assert rd[0]['report'].endswith('liS_report.html')
    assert proj.get('autopilot_report_done') and saved == [str(tmp_path)]   # 标记已写
    # 次拍:标记已在 → 不再重复出报告
    out2 = api.pipeline_tick()
    assert [e for e in out2['events'] if e['kind'] == 'report_done'] == []


def test_pipeline_tick_no_profiles_no_errors():
    api = Api(profiles_mod=_fake_profiles({}),
              adsorption_mod=_fake_adsorption(projects=[]),
              config_mod=_fake_config())
    out = api.pipeline_tick()
    assert out['ok'] is True and out['events'] == [] and out['errors'] == []
    assert out['synced'] == 0


def test_pipeline_tick_skips_profile_without_credentials():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    secrets = types.SimpleNamespace(get_password=lambda n: None, set_password=lambda n, pw: None)
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets,
              adsorption_mod=_fake_adsorption(projects=[]),
              ledger_mod=_fake_ledger([], []), config_mod=_fake_config())
    out = api.pipeline_tick()
    assert any(e['kind'] == 'skip' and 'c1' in e['text'] for e in out['events'])
    assert out['synced'] == 0


# ── pipeline_status(阶段判定) ───────────────────────────────────────────────
def test_pipeline_status_stage_detection():
    projA = {'name': 'A', 'members': {'clean_slab': '/a/s', 'gas_ref': None,
                                      'configs': ['/a/c1', '/a/c2']}}
    projB = {'name': 'B', 'members': {'clean_slab': '/b/s', 'gas_ref': None,
                                      'configs': ['/b/c1']},
             'autopilot_report_done': '2026-07-16T00:00:00'}
    ads = _fake_adsorption(projects=['/a/project.yaml', '/b/project.yaml'],
                           proj_map={'/a/project.yaml': projA, '/b/project.yaml': projB})
    states = {
        '/a/s': {'state': 'DONE', 'results': {}},
        '/a/c1': {'state': 'CREATED', 'results': {}},     # 未提交 → 提交阶段
        '/a/c2': {'state': 'DONE', 'results': {}},
        '/b/s': {'state': 'DONE', 'results': {}},
        '/b/c1': {'state': 'DONE', 'results': {}},         # 全 DONE + 标记 → 报告完成
    }
    api = Api(adsorption_mod=ads, manifest_mod=_fake_manifest_mod(states))
    out = api.pipeline_status()
    by = {p['name']: p for p in out['projects']}
    assert by['A']['stage'] == 'submit' and by['A']['needs_human'] is False
    assert by['B']['stage'] == 'report_done'
    assert by['B']['done'] == 2 and by['B']['total'] == 2
    assert by['B']['stage_index'] == 5 and by['B']['stages'][5] == 'report_done'


def test_pipeline_status_monitor_recover_and_needs_human():
    projM = {'name': 'M', 'members': {'clean_slab': '/m/s', 'gas_ref': None,
                                      'configs': ['/m/c1']}}
    projR = {'name': 'R', 'members': {'clean_slab': '/r/s', 'gas_ref': None,
                                      'configs': ['/r/c1', '/r/c2']}}
    ads = _fake_adsorption(projects=['/m/p', '/r/p'],
                           proj_map={'/m/p': projM, '/r/p': projR})
    states = {
        '/m/s': {'state': 'DONE', 'results': {}},
        '/m/c1': {'state': 'RUNNING', 'results': {}},      # 在跑 → 监控
        '/r/s': {'state': 'DONE', 'results': {}},
        '/r/c1': {'state': 'UNCONVERGED',
                  'results': {'diagnosis': {'restartable': True}, 'continue_rounds': 2}},
        '/r/c2': {'state': 'NEEDS_HUMAN', 'results': {}},  # 红旗
    }
    api = Api(adsorption_mod=ads, manifest_mod=_fake_manifest_mod(states))
    out = api.pipeline_status()
    by = {p['name']: p for p in out['projects']}
    assert by['M']['stage'] == 'monitor'
    assert by['R']['stage'] == 'recover' and by['R']['recover_round'] == 2
    assert by['R']['needs_human'] is True


# ── open_dir 文件路径 → 打开所在目录 ─────────────────────────────────────────
def test_open_dir_file_opens_parent(tmp_path, monkeypatch):
    import subprocess
    f = tmp_path / 'report.html'
    f.write_text('x', encoding='utf-8')
    opened = {}
    monkeypatch.setattr(subprocess, 'Popen', lambda args, *a, **k: opened.update(args=args))
    monkeypatch.setattr(sys, 'platform', 'linux')
    api = Api()
    out = api.open_dir(str(f))
    assert out['ok'] is True and opened['args'] == ['xdg-open', str(tmp_path)]


def test_open_dir_missing_still_errors():
    api = Api()
    out = api.open_dir('/definitely/not/a/real/path/xyz')
    assert out['ok'] is False


# ══════════════════════════════════════════════════════════════════════════════
# Phase A 新引擎接线(派生计算 / SAC 矩阵 / 多自旋 / 通用反应 / campaign)
# 全部走构造注入的假件,零真实 matplotlib/ssh/campaign 磁盘依赖。
# ══════════════════════════════════════════════════════════════════════════════

# ── 假件工厂 ────────────────────────────────────────────────────────────────
def _fake_freq(*, changes=None, warnings=None, calls=None, boom=None):
    """freq_builder 假件:build_freq_job 回显 out_dir + 逐条改动 dict。"""
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(relax_dir, out_dir, **kw):
        calls['build'] = {'relax_dir': relax_dir, 'out_dir': out_dir, **kw}
        if boom:
            raise boom
        return {'out_dir': out_dir, 'free_atoms': [0, 1],
                'changes': list(changes if changes is not None else
                                [{'key': 'IBRION', 'action': 'replace', 'old': '2',
                                  'new': '5', 'reason': '有限差分频率'}]),
                'warnings': list(warnings or [])}
    m.build_freq_job = _build
    return m


def _fake_estatic(*, calls=None, boom_kinds=None):
    """estatic 假件:build_static_job 回显 out_dir + 字符串 changes。"""
    calls = calls if calls is not None else {}
    boom_kinds = boom_kinds or {}
    m = types.SimpleNamespace()

    def _build(relax_dir, out_dir, *, purpose='pdos', **kw):
        calls.setdefault('purposes', []).append(purpose)
        if purpose in boom_kinds:
            raise boom_kinds[purpose]
        return {'out_dir': out_dir, 'changes': [f'{purpose} 改动 A', f'{purpose} 改动 B'],
                'warnings': []}
    m.build_static_job = _build
    return m


_SAC_POSCAR = ('demo\n1.0\n8 0 0\n0 8 0\n0 0 20\nFe N C\n1 4 26\nCartesian\n'
               + '0 0 0\n' * 31)


def _fake_sac(*, sites_list=None, ads_texts=None, place_boom=None, build_boom=None,
              calls=None):
    """sac 束假件:sac_builder.build_sac / sites.enumerate+place / molecules。"""
    calls = calls if calls is not None else {}
    sb = types.SimpleNamespace()

    def _build_sac(template, metal, **kw):
        calls.setdefault('built', []).append((metal, template))
        if build_boom and template in build_boom:
            raise build_boom[template]
        return {'poscar': _SAC_POSCAR, 'description': f'{metal}@{template}',
                'site_indices': {'metal': 0, 'coord': [1, 2, 3, 4],
                                 'metal_element': metal}}
    sb.build_sac = _build_sac

    _sites = sites_list if sites_list is not None else [
        {'name': 'top_metal', 'position': [0.5, 0.5, 0.4], 'kind': 'top_metal'},
        {'name': 'hollow', 'position': [0.5, 0.5, 0.4], 'kind': 'hollow'}]
    st = types.SimpleNamespace()
    st.enumerate_sac_sites = lambda pos, si: [dict(s) for s in _sites]

    def _place(pos, mol, site, *, rotations=(0,), **kw):
        calls.setdefault('placed', []).append((mol, site['name'], tuple(rotations)))
        if place_boom:
            raise place_boom
        return list(ads_texts if ads_texts is not None
                    else [f'{mol}@{site["name"]}_r{d}\nPOSCAR\n' for d in rotations])
    st.place_adsorbate = _place

    mo = types.SimpleNamespace()
    mo.list_molecules = lambda: ['S8', 'Li2S4', 'O2']
    mo.molecule_info = lambda n: {'formula': n, 'spin_hint': None, 'source_note': 'x'}
    return types.SimpleNamespace(sac_builder=sb, sites=st, molecules=mo)


def _fake_jb_text(built=None):
    """job_builder 假件:build_job_dir 回显 out_dir(供 _job_from_text 链)。"""
    built = built if built is not None else []
    m = types.SimpleNamespace()

    def _build(poscar_path, incar, out, **kw):
        built.append(out)
        return {'ok': True, 'out_dir': out, 'warnings': [], 'kpoints': [3, 3, 1],
                'elements': ['Fe', 'N', 'C']}
    m.build_job_dir = _build
    return m


def _fake_ads_projects(saved=None):
    """adsorption 假件:save_project/register_project 收参(不碰真实注册表)。"""
    saved = saved if saved is not None else []
    m = types.SimpleNamespace()
    m.save_project = lambda root, proj: saved.append((root, proj))
    m.register_project = lambda pp: True
    m.list_projects = lambda *a, **k: []
    return m


def _fake_spin(*, variants=None, ground=None, audit=None, calls=None, build_boom=None):
    """spin_scan 假件:build_spin_variants / pick_ground_state / audit_magmom。"""
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _bsv(job_dir, out_root, **kw):
        calls['bsv'] = {'job_dir': job_dir, 'out_root': out_root}
        if build_boom:
            raise build_boom
        return list(variants if variants is not None else [
            {'name': 'nm', 'out_dir': out_root + '/j_spin_nm', 'magmom': None,
             'changes': [{'key': 'ISPIN', 'old': None, 'new': 2}], 'warnings': []},
            {'name': 'hs', 'out_dir': out_root + '/j_spin_hs', 'magmom': '4 0',
             'changes': [{'key': 'MAGMOM', 'old': None, 'new': '4 0'}], 'warnings': []}])
    m.build_spin_variants = _bsv
    m.pick_ground_state = lambda dirs: (ground if ground is not None
                                        else {'winner': 'hs', 'energies': {},
                                              'de_meV': {}, 'warning': None})

    def _audit(outcar, init):
        calls.setdefault('audits', []).append({'outcar': outcar, 'init': init})
        return audit if audit is not None else {
            'final_magnetization': 3.9, 'initial_magmom': init, 'collapsed': False,
            'flipped': False, 'audited': True, 'warning': None}
    m.audit_magmom = _audit
    return m


def _fake_reactions(presets=None):
    p = presets if presets is not None else {
        'ORR_4E': {'name': 'ORR_4E', 'description': '4 电子氧还原 ORR',
                   'electrode': 'RHE', 'direction': 'reduction', 'steps': []},
        'HER': {'name': 'HER', 'description': '析氢 HER', 'electrode': 'RHE',
                'direction': 'reduction', 'steps': []}}
    m = types.SimpleNamespace()
    m.list_presets = lambda: dict(p)
    m.get_preset = lambda k: dict(p)[k]
    return m


def _reactions_orr_spec():
    return {'name': 'ORR_4E', 'description': '4 电子氧还原 ORR', 'electrode': 'RHE',
            'direction': 'reduction', 'steps': [
                {'label': 'O2', 'species': '*', 'n_electrons_cumulative': 0,
                 'coadsorbates_or_gas': [{'name': 'O2', 'coef': 1}]},
                {'label': '*OOH', 'species': 'OOH*', 'n_electrons_cumulative': 1,
                 'coadsorbates_or_gas': []},
                {'label': '*OH', 'species': 'OH*', 'n_electrons_cumulative': 2,
                 'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 1}]}]}


def _fake_reactions_orr():
    spec = _reactions_orr_spec()
    m = types.SimpleNamespace()
    m.list_presets = lambda: {'ORR_4E': spec}
    m.get_preset = lambda k: {'ORR_4E': spec}[k]
    return m


def _fake_fe_preset(*, mol_e=None, fed=None, path_calls=None):
    """freeenergy 假件(通用预设路径):load_molecule_energies + free_energy_path。"""
    path_calls = path_calls if path_calls is not None else {}
    m = types.SimpleNamespace()
    m.load_molecule_energies = lambda d: dict(mol_e or {})

    def _fep(spec, energies, **kw):
        path_calls['energies'] = dict(energies)
        path_calls['spec'] = spec
        return fed if fed is not None else {
            'steps': [{'label': s['label'], 'G': 0.0} for s in spec['steps']],
            'pds_index': 0, 'u_l': 0.7, 'u_eq': 1.23, 'eta': 0.5,
            'per_electron': [], 'warnings': []}
    m.free_energy_path = _fep
    return m


def _fake_campaign(*, campaigns=None, calls=None):
    """campaign 束假件:new_task/init_campaign/estimate/record + load/summary/budget。"""
    calls = calls if calls is not None else {}
    campaigns = campaigns or {}
    m = types.SimpleNamespace()
    m.new_task = lambda tid, kind, *, job_dir=None, **kw: {
        'id': tid, 'kind': kind, 'job_dir': job_dir}

    def _init(base, cid, *, tasks=None, title='', **kw):
        cdir = os.path.join(str(base), '.camp', str(cid))
        calls['init'] = {'base': str(base), 'cid': cid, 'title': title,
                         'tasks': list(tasks or [])}
        return {'dir': cdir, 'meta': {'id': cid, 'title': title,
                                      'budget_core_hours': None},
                'tasks': list(tasks or [])}
    m.init_campaign = _init
    m.estimate_job = lambda natoms, nkpts, kind, cores, **kw: round((natoms or 1) * 0.01, 3)
    m.record_estimate = lambda cdir, tid, ch: calls.setdefault(
        'estimates', {}).__setitem__(tid, ch)
    m.load_campaign = lambda cdir: campaigns.get(cdir)
    m.progress_summary = lambda camp: camp.get('_summary', {}) if isinstance(camp, dict) else {}
    m.load_budget = lambda cdir: (campaigns.get(cdir) or {}).get(
        '_budget', {'estimates': {}, 'actuals': {}})
    return m


# ── derive_freq ──────────────────────────────────────────────────────────────
def test_derive_freq_derives_and_registers(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    registered, calls = [], {}
    api = Api(freq_builder_mod=_fake_freq(calls=calls),
              ledger_mod=_fake_ledger_register(registered))
    out = api.derive_freq(str(tmp_path))
    assert out['ok'] is True and out['error'] is None
    assert out['job_dir'].endswith(os.path.basename(str(tmp_path)) + '_freq')
    assert out['changes'][0]['key'] == 'IBRION'          # 逐条派生改动
    assert registered == [out['job_dir']]                # 入台账
    assert calls['build']['out_dir'] == out['job_dir']   # 命名 {原名}_freq


def test_derive_freq_missing_dir_error():
    api = Api(freq_builder_mod=_fake_freq())
    out = api.derive_freq('/no/such/dir/xyz')
    assert out['ok'] is False and out['job_dir'] is None and '目录' in out['error']


def test_derive_freq_engine_exception_caught(tmp_path):
    api = Api(freq_builder_mod=_fake_freq(boom=ValueError('缺 CONTCAR')),
              ledger_mod=_fake_ledger_register([]))
    out = api.derive_freq(str(tmp_path))
    assert out['ok'] is False and '缺 CONTCAR' in out['error']


# ── derive_estatic ───────────────────────────────────────────────────────────
def test_derive_estatic_multi_kind_registers(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    registered, calls = [], {}
    api = Api(estatic_mod=_fake_estatic(calls=calls),
              ledger_mod=_fake_ledger_register(registered))
    out = api.derive_estatic(str(tmp_path), ['pdos', 'bader', 'chgdiff'])
    assert out['ok'] is True and len(out['jobs']) == 3
    assert calls['purposes'] == ['pdos', 'bader', 'chgdiff']
    assert [j['kind'] for j in out['jobs']] == ['pdos', 'bader', 'chgdiff']
    assert out['jobs'][0]['job_dir'].endswith('_st_pdos')   # 命名 {原名}_st_{kind}
    assert len(registered) == 3
    assert out['jobs'][0]['changes'] == ['pdos 改动 A', 'pdos 改动 B']


def test_derive_estatic_skips_invalid_kind(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api(estatic_mod=_fake_estatic(), ledger_mod=_fake_ledger_register([]))
    out = api.derive_estatic(str(tmp_path), ['pdos', 'bogus'])
    assert out['ok'] is True and len(out['jobs']) == 1
    assert out['skipped'] == [{'kind': 'bogus',
                               'reason': '不支持的静态类型(仅 pdos/bader/chgdiff)'}]


def test_derive_estatic_no_valid_kinds_error(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api(estatic_mod=_fake_estatic())
    out = api.derive_estatic(str(tmp_path), ['nope'])
    assert out['ok'] is False and out['jobs'] == [] and '静态类型' in out['error']


def test_derive_estatic_engine_failure_isolated(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    est = _fake_estatic(boom_kinds={'bader': RuntimeError('母 KPOINTS 缺')})
    api = Api(estatic_mod=est, ledger_mod=_fake_ledger_register([]))
    out = api.derive_estatic(str(tmp_path), ['pdos', 'bader'])
    assert out['ok'] is True and len(out['jobs']) == 1
    assert out['jobs'][0]['kind'] == 'pdos'
    assert any(s['kind'] == 'bader' and '母 KPOINTS' in s['reason'] for s in out['skipped'])


# ── molecule_list ────────────────────────────────────────────────────────────
def test_molecule_list_from_engine():
    api = Api(sac_mods=_fake_sac())
    out = api.molecule_list()
    assert out['ok'] is True
    assert [m['name'] for m in out['molecules']] == ['S8', 'Li2S4', 'O2']
    assert out['molecules'][0]['formula'] == 'S8'


def test_molecule_list_error_caught():
    boom = types.SimpleNamespace(molecules=types.SimpleNamespace(
        list_molecules=lambda: (_ for _ in ()).throw(RuntimeError('库坏'))))
    api = Api(sac_mods=boom)
    out = api.molecule_list()
    assert out['ok'] is False and '库坏' in out['error']


# ── sac_matrix_preview ───────────────────────────────────────────────────────
def test_sac_matrix_preview_counts_and_estimate():
    api = Api(sac_mods=_fake_sac())
    out = api.sac_matrix_preview(['Fe', 'Co'], ['MN4'], ['S8', 'O2'], 'metal_top', 2)
    assert out['ok'] is True
    assert out['n_slabs'] == 2                        # 2 金属 × 1 模板
    assert out['n_configs'] == 8                      # 每 slab 1 顶位 × 2 吸附质 × 2 取向 ×2 slab
    assert out['n_total_jobs'] == 10
    assert '粗估' in out['estimate_note']
    assert out['names'][0] == 'Fe@MN4_clean'


def test_sac_matrix_preview_all_sites_uses_all_kinds():
    api = Api(sac_mods=_fake_sac())                   # 默认 2 位点(top_metal + hollow)
    out = api.sac_matrix_preview(['Fe'], ['MN4'], ['S8'], 'all', 1)
    assert out['n_configs'] == 2                      # 2 位点 × 1 吸附质 × 1 取向


def test_sac_matrix_preview_requires_metal_and_template():
    api = Api(sac_mods=_fake_sac())
    out = api.sac_matrix_preview([], ['MN4'], ['S8'], 'all', 1)
    assert out['ok'] is False and '金属' in out['error']


# ── sac_matrix_generate ──────────────────────────────────────────────────────
def test_sac_matrix_generate_creates_jobs_project_and_campaign(tmp_path):
    incar = tmp_path / 'INCAR'
    incar.write_text('ENCUT=500\n', encoding='utf-8')
    registered, saved, cc = [], [], {}
    api = Api(sac_mods=_fake_sac(), job_builder_mod=_fake_jb_text(),
              manifest_mod=_fake_manifest({}),
              ledger_mod=_fake_ledger_register(registered),
              adsorption_mod=_fake_ads_projects(saved),
              campaign_mods=_fake_campaign(calls=cc),
              config_mod=_fake_config(cfg={'potcar_lib_root': '/lib'}))
    out = api.sac_matrix_generate(['Fe'], ['MN4'], ['S8'], 'metal_top', 1,
                                  str(incar), str(tmp_path))
    assert out['ok'] is True and out['error'] is None
    assert out['created'] == 2                        # 1 清洁面 + 1 构型
    assert len(registered) == 2                       # 全部入台账
    # 每 slab 一个吸附能项目(清洁面 + 构型族)
    assert len(out['project_paths']) == 1 and len(saved) == 1
    assert saved[0][1]['members']['clean_slab'].endswith('_clean')
    assert len(saved[0][1]['members']['configs']) == 1
    # 同步注册 campaign(2 任务节点 + 逐任务记预估机时)
    assert out['campaign'].endswith(cc['init']['cid'])
    assert len(cc['init']['tasks']) == 2 and len(cc['estimates']) == 2


def test_sac_matrix_generate_place_rejection_skipped(tmp_path):
    incar = tmp_path / 'INCAR'
    incar.write_text('E\n', encoding='utf-8')
    sac = _fake_sac(place_boom=ValueError('分子-表面最近距离过近,全部拒绝'))
    api = Api(sac_mods=sac, job_builder_mod=_fake_jb_text(),
              manifest_mod=_fake_manifest({}), ledger_mod=_fake_ledger_register([]),
              adsorption_mod=_fake_ads_projects(), campaign_mods=_fake_campaign(),
              config_mod=_fake_config())
    out = api.sac_matrix_generate(['Fe'], ['MN4'], ['S8'], 'metal_top', 1,
                                  str(incar), str(tmp_path))
    assert out['ok'] is True and out['created'] == 1          # 仅清洁面
    assert any('拒绝' in s['reason'] for s in out['skipped'])
    assert out['project_paths'] == []                         # 无构型 → 不建项目


def test_sac_matrix_generate_clean_only_no_project(tmp_path):
    incar = tmp_path / 'INCAR'
    incar.write_text('E\n', encoding='utf-8')
    saved = []
    api = Api(sac_mods=_fake_sac(), job_builder_mod=_fake_jb_text(),
              manifest_mod=_fake_manifest({}), ledger_mod=_fake_ledger_register([]),
              adsorption_mod=_fake_ads_projects(saved), campaign_mods=_fake_campaign(),
              config_mod=_fake_config())
    out = api.sac_matrix_generate(['Fe'], ['MN4'], [], 'all', 1,
                                  str(incar), str(tmp_path))
    assert out['ok'] is True and out['created'] == 1
    assert out['project_paths'] == [] and saved == []


def test_sac_matrix_generate_missing_incar_error(tmp_path):
    api = Api(sac_mods=_fake_sac(), config_mod=_fake_config())
    out = api.sac_matrix_generate(['Fe'], ['MN4'], ['S8'], 'all', 1,
                                  '/no/incar', str(tmp_path))
    assert out['ok'] is False and 'INCAR' in out['error']


def test_sac_matrix_generate_build_failure_isolated(tmp_path):
    incar = tmp_path / 'INCAR'
    incar.write_text('E\n', encoding='utf-8')
    sac = _fake_sac(build_boom={'MN4': ValueError('未知模板')})
    api = Api(sac_mods=sac, job_builder_mod=_fake_jb_text(),
              manifest_mod=_fake_manifest({}), ledger_mod=_fake_ledger_register([]),
              adsorption_mod=_fake_ads_projects(), campaign_mods=_fake_campaign(),
              config_mod=_fake_config())
    out = api.sac_matrix_generate(['Fe'], ['MN4', 'MN3'], ['S8'], 'metal_top', 1,
                                  str(incar), str(tmp_path))
    assert out['ok'] is True
    assert any('MN4' in s['name'] and '建模失败' in s['reason'] for s in out['skipped'])
    assert out['created'] >= 1                        # MN3 正常生成


# ── spin_family_generate ─────────────────────────────────────────────────────
def test_spin_family_generate_creates_family(tmp_path):
    pos = tmp_path / 'POSCAR'
    pos.write_text('p', encoding='utf-8')
    incar = tmp_path / 'INCAR'
    incar.write_text('i', encoding='utf-8')
    registered = []
    api = Api(spin_mod=_fake_spin(), job_builder_mod=_fake_jb_text(),
              manifest_mod=_fake_manifest({}),
              ledger_mod=_fake_ledger_register(registered), config_mod=_fake_config())
    out = api.spin_family_generate(str(pos), str(incar), str(tmp_path))
    assert out['ok'] is True and len(out['variants']) == 2
    assert [v['name'] for v in out['variants']] == ['nm', 'hs']
    assert len(registered) == 2                       # 家族全部入台账
    assert out['variants'][1]['magmom'] == '4 0'


def test_spin_family_generate_missing_files_error(tmp_path):
    api = Api(spin_mod=_fake_spin(), config_mod=_fake_config())
    out = api.spin_family_generate('/no/POSCAR', '/no/INCAR', str(tmp_path))
    assert out['ok'] is False and 'POSCAR' in out['error']


def test_spin_family_generate_engine_exception_caught(tmp_path):
    pos = tmp_path / 'POSCAR'
    pos.write_text('p', encoding='utf-8')
    incar = tmp_path / 'INCAR'
    incar.write_text('i', encoding='utf-8')
    spin = _fake_spin(build_boom=ValueError('作业目录缺 POSCAR'))
    api = Api(spin_mod=spin, job_builder_mod=_fake_jb_text(),
              manifest_mod=_fake_manifest({}), ledger_mod=_fake_ledger_register([]),
              config_mod=_fake_config())
    out = api.spin_family_generate(str(pos), str(incar), str(tmp_path))
    assert out['ok'] is False and '缺 POSCAR' in out['error']


# ── spin_family_compare ──────────────────────────────────────────────────────
def test_spin_family_compare_ground_and_audit(tmp_path):
    d1 = tmp_path / 'j_nm'
    d1.mkdir()
    (d1 / 'OUTCAR').write_text('magnetization 0.0', encoding='utf-8')
    d2 = tmp_path / 'j_hs'
    d2.mkdir()
    (d2 / 'OUTCAR').write_text('magnetization 3.9', encoding='utf-8')
    calls = {}
    manifest = _fake_manifest_mod({str(d1): {'inputs': {'spin_magmom': '0'}},
                                   str(d2): {'inputs': {'spin_magmom': '4 0'}}})
    api = Api(spin_mod=_fake_spin(calls=calls), manifest_mod=manifest)
    out = api.spin_family_compare([str(d1), str(d2)])
    assert out['ok'] is True and out['ground']['winner'] == 'hs'
    assert len(out['audits']) == 2 and all(a['audited'] for a in out['audits'])
    # audit_magmom 收到 OUTCAR 文本 + 各自初猜磁矩(从 manifest 溯源)
    assert any(a['init'] == '4 0' for a in calls['audits'])
    assert any('3.9' in a['outcar'] for a in calls['audits'])


def test_spin_family_compare_pending_passthrough_and_missing_outcar(tmp_path):
    d1 = tmp_path / 'j_nm'
    d1.mkdir()                                        # 无 OUTCAR
    spin = _fake_spin(ground={'pending': ['nm']})
    api = Api(spin_mod=spin, manifest_mod=_fake_manifest_mod({}))
    out = api.spin_family_compare([str(d1)])
    assert out['ok'] is True and out['ground'] == {'pending': ['nm']}
    assert out['audits'][0]['audited'] is False
    assert 'OUTCAR' in out['audits'][0]['warning']


def test_spin_family_compare_empty_dirs_error():
    api = Api(spin_mod=_fake_spin())
    out = api.spin_family_compare([])
    assert out['ok'] is False and '作业目录' in out['error']


def test_spin_family_compare_error_caught(tmp_path):
    spin = _fake_spin()
    spin.pick_ground_state = lambda dirs: (_ for _ in ()).throw(RuntimeError('读能量失败'))
    api = Api(spin_mod=spin, manifest_mod=_fake_manifest_mod({}))
    out = api.spin_family_compare([str(tmp_path)])
    assert out['ok'] is False and '读能量失败' in out['error']


# ── reaction_presets ─────────────────────────────────────────────────────────
def test_reaction_presets_shape():
    api = Api(reactions_mod=_fake_reactions())
    out = api.reaction_presets()
    assert out['ok'] is True
    assert {p['key'] for p in out['presets']} == {'ORR_4E', 'HER'}
    orr = next(p for p in out['presets'] if p['key'] == 'ORR_4E')
    assert orr['name'] == 'ORR_4E' and '氧还原' in orr['description']


def test_reaction_presets_error_caught():
    boom = types.SimpleNamespace(
        list_presets=lambda: (_ for _ in ()).throw(RuntimeError('预设坏')))
    api = Api(reactions_mod=boom)
    out = api.reaction_presets()
    assert out['ok'] is False and '预设坏' in out['error']


# ── proj_figures(通用反应预设 ladder;默认 Li-S 向后兼容) ─────────────────────
def test_proj_figures_preset_ladder_maps_species(tmp_path):
    calls, path_calls = {}, {}
    proj = _proj('PtN4', str(tmp_path))
    delta_ret = {'slab': ('DONE', -100.0), 'ref': ('无', None), 'has_ref': False,
                 'rows': [
                     {'name': 'PtN4_ads_OOH', 'state': 'DONE', 'e_config': -110.0,
                      'delta_e': -1.0, 'note': ''},
                     {'name': 'PtN4_ads_OH', 'state': 'DONE', 'e_config': -104.5,
                      'delta_e': -1.0, 'note': ''}]}
    ads = _fake_adsorption(proj_map={'/p': proj}, delta_ret=delta_ret)
    mol = tmp_path / 'mols'
    mol.mkdir()
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              reactions_mod=_fake_reactions_orr(),
              freeenergy_mod=_fake_fe_preset(
                  mol_e={'O2': -9.8, 'H2O': -14.2, 'H2': -6.8}, path_calls=path_calls),
              config_mod=_fake_config(cfg={'lis_molecules_dir': str(mol)}))
    out = api.proj_figures('/p', ['ladder'], str(tmp_path), 'ORR_4E')
    assert out['ok'] is True and len(out['files']) == 1
    e = path_calls['energies']
    assert e['*'] == -100.0                            # 干净基底 '*' → 清洁表面能量
    assert e['OOH*'] == -110.0 and e['OH*'] == -104.5  # 构型名 → 物种能量
    assert e['O2'] == -9.8 and e['H2O'] == -14.2 and e['H2'] == -6.8  # 分子库 + RHE 定标
    assert '氧还原' in calls['ladder'][0]['title']      # 标题随预设


def test_proj_figures_preset_ladder_missing_species_skipped(tmp_path):
    proj = _proj('PtN4', str(tmp_path))
    delta_ret = {'slab': ('DONE', -100.0), 'ref': ('无', None), 'has_ref': False,
                 'rows': [{'name': 'PtN4_ads_OH', 'state': 'DONE', 'e_config': -104.5,
                           'delta_e': -1.0, 'note': ''}]}    # 缺 OOH
    ads = _fake_adsorption(proj_map={'/p': proj}, delta_ret=delta_ret)
    mol = tmp_path / 'mols'
    mol.mkdir()
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts({}),
              reactions_mod=_fake_reactions_orr(),
              freeenergy_mod=_fake_fe_preset(mol_e={'O2': -9.8, 'H2O': -14.2, 'H2': -6.8}),
              config_mod=_fake_config(cfg={'lis_molecules_dir': str(mol)}))
    out = api.proj_figures('/p', ['ladder'], str(tmp_path), 'ORR_4E')
    assert out['ok'] is True and out['files'] == []
    assert out['skipped'][0]['kind'] == 'ladder'
    assert 'OOH*' in out['skipped'][0]['reason'] and '缺' in out['skipped'][0]['reason']


def test_proj_figures_default_ladder_unchanged_when_no_preset(tmp_path):
    # 不传 preset_key → 走既有 Li-S path_from_project_and_molecules,行为完全不变
    calls = {}
    mol_dir = tmp_path / 'mols'
    mol_dir.mkdir()
    fed = {'steps': [{'label': 'S8*', 'G': 0.0}, {'label': 'Li2S*', 'G': -1.0}],
           'pds_index': 0, 'u_l': 1.5}
    seen = {}

    def _path(rows, e_slab, molecules_dir):
        seen['called'] = True
        return fed
    fe = types.SimpleNamespace(path_from_project_and_molecules=_path)
    ads = _fake_adsorption(proj_map={'/p': _proj('liS', str(tmp_path))},
                           delta_ret=_delta({'liS_ads_Li2S4': -1.2}))
    api = Api(adsorption_mod=ads, native_charts_mod=_fake_ncharts(calls),
              freeenergy_mod=fe,
              config_mod=_fake_config(cfg={'lis_molecules_dir': str(mol_dir)}))
    out = api.proj_figures('/p', ['ladder'])          # 无 preset_key
    assert out['ok'] is True and len(out['files']) == 1
    assert seen.get('called') is True                 # 仍走 Li-S 便捷入口
    assert 'Li-S discharge path' in calls['ladder'][0]['title']


# ── campaign_list ────────────────────────────────────────────────────────────
def test_campaign_list_aggregates_states_and_budget():
    cdir = '/base/.camp/sac-1'
    campaigns = {cdir: {
        'meta': {'id': 'sac-1', 'title': 'SAC 批 3 作业', 'budget_core_hours': 50.0},
        'tasks': [],
        '_summary': {'total': 3, 'pending': 0, 'running': 0, 'failed': 0,
                     'completed': 1, 'validated': 1, 'accepted': 1},
        '_budget': {'estimates': {'a': 0.3, 'b': 0.5}, 'actuals': {}}}}
    api = Api(campaign_mods=_fake_campaign(campaigns=campaigns),
              config_mod=_fake_config(ui={'campaign_dirs': [cdir]}))
    out = api.campaign_list()
    assert out['ok'] is True and out['available'] is True and len(out['campaigns']) == 1
    c = out['campaigns'][0]
    assert c['name'] == 'SAC 批 3 作业' and c['n_tasks'] == 3
    # 累计口径:completed ⊇ validated ⊇ accepted(三态嵌套小条形)
    assert c['states'] == {'completed': 3, 'validated': 2, 'accepted': 1}
    assert c['budget'] == {'estimated': 0.8, 'cap': 50.0}


def test_campaign_list_no_campaigns_hidden():
    api = Api(campaign_mods=_fake_campaign(), config_mod=_fake_config(ui={}))
    out = api.campaign_list()
    assert out['ok'] is True and out['available'] is True and out['campaigns'] == []


def test_campaign_list_module_unavailable(monkeypatch):
    # campaign 模块不可用(ImportError)→ available=False,前端据此隐藏区块
    api = Api(config_mod=_fake_config(ui={'campaign_dirs': ['/x']}))
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == 'vcstudio.campaign':
            raise ImportError('campaign 不可用')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    out = api.campaign_list()
    assert out['ok'] is True and out['available'] is False and out['campaigns'] == []


def test_campaign_list_skips_unloadable_campaign():
    good = '/base/.camp/good'
    campaigns = {good: {
        'meta': {'id': 'good'}, 'tasks': [],
        '_summary': {'total': 1, 'completed': 1, 'validated': 0, 'accepted': 0},
        '_budget': {'estimates': {}, 'actuals': {}}}}
    api = Api(campaign_mods=_fake_campaign(campaigns=campaigns),
              config_mod=_fake_config(ui={'campaign_dirs': ['/base/.camp/bad', good]}))
    out = api.campaign_list()
    assert len(out['campaigns']) == 1 and out['campaigns'][0]['name'] == 'good'


def test_campaign_list_bad_campaign_does_not_break():
    cdir = '/b/c'
    fc = _fake_campaign(campaigns={cdir: {'meta': {'id': 'c'}, 'tasks': []}})
    fc.progress_summary = lambda camp: (_ for _ in ()).throw(RuntimeError('summary 坏'))
    api = Api(campaign_mods=fc, config_mod=_fake_config(ui={'campaign_dirs': [cdir]}))
    out = api.campaign_list()
    assert out['ok'] is True and out['campaigns'] == []   # 坏批次跳过,绝不拖垮仪表盘
