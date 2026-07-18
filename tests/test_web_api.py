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


# ═══════════════════════════════════════════════════════════════════════════════
# v3.1 GUI 总集成:研究场景 / i18n / 图表预设 / 成稿包 / AI 助手 / 多引擎 / 结构编辑器
# ═══════════════════════════════════════════════════════════════════════════════
def _fake_scenarios(calls=None, reg=None, active=None):
    """scenarios 假件:list/get/active/set 全可注入;set 落 calls 便于断言持久化。"""
    calls = calls if calls is not None else {}
    full = {'key': 'full', 'name': '通用', 'description': '兜底',
            'pages': ['dashboard', 'generate', 'project', 'jobs', 'cluster', 'settings'],
            'cards': {}, 'figure_preset_order': ['bar', 'ladder'],
            'reaction_presets': [], 'engines': ['vasp'],
            'defaults': {'calc_type': 'slab'}, 'ai_context': 'x'}
    lis = {'key': 'lis', 'name': '锂硫', 'description': 'Li-S',
           'pages': ['dashboard', 'generate', 'project', 'jobs', 'cluster', 'settings'],
           'cards': {}, 'figure_preset_order': ['ladder', 'volcano'],
           'reaction_presets': ['LIS_16E'], 'engines': ['vasp'],
           'defaults': {'calc_type': 'slab'}, 'ai_context': 'y'}
    reg = reg if reg is not None else {'full': full, 'lis': lis}
    m = types.SimpleNamespace()
    m.list_scenarios = lambda: [dict(v) for v in reg.values()]
    m.get_scenario = lambda k: dict(reg.get(k, full))
    m.active_scenario = lambda cfg=None: dict(active if active is not None else full)
    m.set_scenario = lambda k, *a, **kw: calls.__setitem__('set', k)
    return m


def _fake_i18n(calls=None, lang='zh', avail=None, dict_ret=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.current_lang = lambda cfg=None: lang
    m.available_langs = lambda: list(avail if avail is not None else ['zh', 'en'])
    m.set_lang = lambda lg, *a, **kw: calls.__setitem__('lang', lg)
    m.export_for_js = lambda lg: dict(dict_ret if dict_ret is not None else (
        {'nav.dashboard': '仪表盘'} if lg == 'zh' else {'nav.dashboard': 'Dashboard'}))
    return m


def _fake_figpresets(calls=None, reg=None):
    calls = calls if calls is not None else {}
    reg = reg if reg is not None else {
        'adsorption_bar': {'key': 'adsorption_bar', 'name': '吸附能柱状图',
                           'category': '能量学', 'description': '柱状',
                           'required_data': 'adsorbates+substrates',
                           'thumbnail_svg': '<svg id="bar"/>',
                           'params_schema': {'negative_up': False, 'title': ''}},
        'delta_e_heatmap': {'key': 'delta_e_heatmap', 'name': 'ΔE 热图',
                            'category': '能量学', 'description': '热图',
                            'required_data': 'rows+cols+values',
                            'thumbnail_svg': '<svg id="hm"/>',
                            'params_schema': {'annotate': True}},
        'free_energy_ladder': {'key': 'free_energy_ladder', 'name': 'ΔG 台阶图',
                               'category': '电池', 'description': '台阶',
                               'required_data': 'paths',
                               'thumbnail_svg': '<svg id="ld"/>',
                               'params_schema': {'mark_pds': True, 'title': ''}},
        'pdos': {'key': 'pdos', 'name': 'PDOS', 'category': '电子结构',
                 'description': 'pdos', 'required_data': 'series',
                 'thumbnail_svg': '<svg id="pd"/>', 'params_schema': {}},
        'volcano': {'key': 'volcano', 'name': '火山图', 'category': '能量学',
                    'description': '火山', 'required_data': 'points',
                    'thumbnail_svg': '<svg id="vo"/>', 'params_schema': {}},
    }
    m = types.SimpleNamespace()
    m.list_presets = lambda category=None: [
        {k: v for k, v in p.items() if k != 'thumbnail_svg'}
        for p in reg.values() if category is None or p['category'] == category]
    m.get_preset = lambda k: dict(reg[k])   # 未知 key → KeyError
    m.categories = lambda: list(dict.fromkeys(p['category'] for p in reg.values()))

    def _render(k, data, out_path, **params):
        calls.setdefault('render', []).append(
            {'key': k, 'data': data, 'out': out_path, 'params': params})
        return [str(out_path)]
    m.render_preset = _render
    m.preset_provenance = lambda k, ds, params=None: {
        'preset': k, 'data_source': ds, 'params': dict(params or {})}
    return m


def _fake_draftpack(ret=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _dr(proj, out, **kw):
        calls['draft'] = {'out': out, 'proj': proj, 'kw': kw}
        if ret is not None:
            return dict(ret)
        return {'ok': True, 'summary': '✅ Draft-Ready 通过', 'issues': [],
                'issues_total': 0, 'summary_path': out + '/DRAFT_READY.md',
                'report': {
                    'si_package': {'zip_path': out + '/x_SI.zip', 'ok': True,
                                   'contents': ['members/a']},
                    'tables': {'files': [out + '/t.csv', out + '/t.html'], 'issues': []},
                    'methods': {'files': [out + '/methods_zh.md'], 'issues': []}}}
    m.draft_ready = _dr
    return m


def _fake_ai_paper(*, extract_ret=None, plan_ret=None, inst_ret=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _extract(source, transport=None, config=None):
        calls['extract'] = {'source': source, 'transport': transport}
        return extract_ret if extract_ret is not None else {
            'ok': True, 'spec': {'systems': [], 'adsorbates': []},
            'issues': ['未知泛函「XPB」'], 'error': None}
    m.extract_spec = _extract
    m.plan_campaign = lambda spec, **kw: (plan_ret if plan_ret is not None else {
        'ok': True, 'plan': {'jobs_estimate': 3, 'warnings': ['需用户提供 INCAR'],
                             'tasks': [{'id': 'a'}], 'nk': 9}})

    def _inst(plan, out_root, **kw):
        calls['inst'] = {'out_root': out_root, 'kw': dict(kw)}
        if kw.get('dry_run'):
            return {'ok': True, 'created': ['a'], 'campaign_dir': '(dry)',
                    'gates': {'budget': {'estimated_core_hours': 12.5}},
                    'dry_run': True}
        return inst_ret if inst_ret is not None else {
            'ok': True, 'created': ['a', 'b'],
            'campaign_dir': out_root + '/.camp/ai-paper',
            'gates': {'pilot': {'status': 'awaiting'}},
            'pilot': {'id': 'a'}, 'awaiting': 'pilot_validation'}
    m.instantiate = _inst
    return m


def _fake_engines(calls=None, gen_ret=None, validate_issues=None, nonequiv=None):
    calls = calls if calls is not None else {}

    class _Spec:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            calls['spec'] = dict(kw)

    class _Backend:
        def generate_inputs(self, spec, out):
            calls['gen'] = {'out': out, 'spec': spec}
            return gen_ret if gen_ret is not None else {
                'files': [out + '/cp2k.inp'], 'warnings': ['CP2K 需自备 GTH 赝势库']}

    m = types.SimpleNamespace()
    m.CalcSpec = _Spec
    m.validate = lambda spec: list(validate_issues or [])
    m.available_engines = lambda: ['vasp', 'cp2k', 'gaussian', 'castep']
    m.get_backend = lambda name: (calls.__setitem__('engine', name) or _Backend())
    m.nonequivalence_report = lambda s, d: list(
        nonequiv if nonequiv is not None else [f'[cutoff] {s}→{d} 截断能不可换算'])
    return m


# 供结构编辑器往返测试的最小 slab POSCAR:2 层(底层两 Fe,顶层一 O),c 向 20 Å 真空 18 Å
_EDITOR_POSCAR = (
    'slab test\n1.0\n3.0 0.0 0.0\n0.0 3.0 0.0\n0.0 0.0 20.0\n'
    'Fe O\n2 1\nCartesian\n'
    '0.0 0.0 5.0\n1.5 1.5 5.0\n0.75 0.75 7.0\n')


# ── scenario_* ───────────────────────────────────────────────────────────────
def test_scenario_list_shape():
    api = Api(scenarios_mod=_fake_scenarios())
    out = api.scenario_list()
    assert out['ok'] is True
    assert {s['key'] for s in out['scenarios']} == {'full', 'lis'}
    lis = next(s for s in out['scenarios'] if s['key'] == 'lis')
    assert 'project' in lis['pages'] and lis['reaction_presets'] == ['LIS_16E']


def test_scenario_get_configured_true_reads_active():
    calls = {}
    api = Api(scenarios_mod=_fake_scenarios(active={'key': 'lis', 'name': '锂硫',
              'pages': ['dashboard'], 'cards': {}, 'figure_preset_order': [],
              'reaction_presets': [], 'engines': ['vasp'], 'defaults': {},
              'ai_context': ''}, calls=calls),
              config_mod=_fake_config(ui={'scenario': 'lis'}))
    out = api.scenario_get()
    assert out['ok'] is True and out['configured'] is True
    assert out['scenario']['key'] == 'lis'


def test_scenario_get_unconfigured_first_launch():
    # config 无 ui.scenario → configured=False(前端据此弹首启场景选择模态)
    api = Api(scenarios_mod=_fake_scenarios(), config_mod=_fake_config(ui={}))
    out = api.scenario_get()
    assert out['ok'] is True and out['configured'] is False


def test_scenario_set_persists_and_returns_view():
    calls = {}
    api = Api(scenarios_mod=_fake_scenarios(calls=calls))
    out = api.scenario_set('lis')
    assert out['ok'] is True and out['key'] == 'lis' and calls['set'] == 'lis'
    assert out['scenario']['reaction_presets'] == ['LIS_16E']


def test_scenario_get_error_caught():
    boom = _fake_scenarios()
    boom.active_scenario = lambda cfg=None: (_ for _ in ()).throw(RuntimeError('场景坏'))
    api = Api(scenarios_mod=boom, config_mod=_fake_config())
    out = api.scenario_get()
    assert out['ok'] is False and '场景坏' in out['error']


# ── lang_* / i18n_dict ───────────────────────────────────────────────────────
def test_lang_get_returns_lang_and_available():
    api = Api(i18n_mod=_fake_i18n(lang='en', avail=['zh', 'en']),
              config_mod=_fake_config(ui={'lang': 'en'}))
    out = api.lang_get()
    assert out['ok'] is True and out['lang'] == 'en'
    assert out['available'] == ['zh', 'en']


def test_lang_set_persists_active_language():
    calls = {}
    api = Api(i18n_mod=_fake_i18n(calls=calls))
    out = api.lang_set('en')
    assert out['ok'] is True and out['lang'] == 'en' and calls['lang'] == 'en'


def test_i18n_dict_returns_full_table():
    api = Api(i18n_mod=_fake_i18n())
    out = api.i18n_dict('en')
    assert out['ok'] is True and out['lang'] == 'en'
    assert out['dict']['nav.dashboard'] == 'Dashboard'


def test_lang_get_error_caught():
    boom = _fake_i18n()
    boom.current_lang = lambda cfg=None: (_ for _ in ()).throw(RuntimeError('语言坏'))
    api = Api(i18n_mod=boom, config_mod=_fake_config())
    out = api.lang_get()
    assert out['ok'] is False and '语言坏' in out['error']


# ── figure_presets / render_figure_preset ────────────────────────────────────
def test_figure_presets_gallery_with_thumbnails():
    api = Api(figure_presets_mod=_fake_figpresets())
    out = api.figure_presets()
    assert out['ok'] is True and len(out['presets']) == 5
    bar = next(p for p in out['presets'] if p['key'] == 'adsorption_bar')
    assert bar['thumbnail_svg'] == '<svg id="bar"/>'
    assert bar['params_schema'] == {'negative_up': False, 'title': ''}
    assert '能量学' in out['categories']


def test_render_figure_preset_bar_assembles_from_delta(tmp_path):
    calls = {}
    ads = _fake_adsorption(proj_map={'/p': _proj('demo', str(tmp_path))},
                           delta_ret=_delta({'O': -1.0, 'OH': -2.0}))
    api = Api(figure_presets_mod=_fake_figpresets(calls=calls), adsorption_mod=ads)
    out = api.render_figure_preset('adsorption_bar', '/p', {'save_to': str(tmp_path)})
    assert out['ok'] is True and len(out['files']) == 1 and out['skipped'] == []
    data = calls['render'][0]['data']
    assert data['adsorbates'] == ['O', 'OH']
    assert data['substrates']['demo'] == [-1.0, -2.0]
    assert out['provenance']['preset'] == 'adsorption_bar'


def test_render_figure_preset_heatmap_rows_cols_values(tmp_path):
    calls = {}
    ads = _fake_adsorption(proj_map={'/p': _proj('demo', str(tmp_path))},
                           delta_ret=_delta({'O': -1.0, 'OH': -2.0}))
    api = Api(figure_presets_mod=_fake_figpresets(calls=calls), adsorption_mod=ads)
    out = api.render_figure_preset('delta_e_heatmap', '/p', {'save_to': str(tmp_path)})
    assert out['ok'] is True
    data = calls['render'][0]['data']
    assert data['rows'] == ['demo'] and data['cols'] == ['O', 'OH']
    assert data['values'] == [[-1.0, -2.0]]


def test_render_figure_preset_ladder_lis_default(tmp_path):
    calls = {}
    mol = tmp_path / 'mols'
    mol.mkdir()
    fed = {'steps': [{'label': 'S8*', 'G': 0.0}, {'label': 'Li2S*', 'G': -1.0}],
           'pds_index': 0, 'u_l': 1.5}
    fe = types.SimpleNamespace(
        path_from_project_and_molecules=lambda rows, e_slab, molecules_dir: fed)
    ads = _fake_adsorption(proj_map={'/p': _proj('liS', str(tmp_path))},
                           delta_ret=_delta({'liS_ads_Li2S4': -1.2}))
    api = Api(figure_presets_mod=_fake_figpresets(calls=calls), adsorption_mod=ads,
              freeenergy_mod=fe,
              config_mod=_fake_config(cfg={'lis_molecules_dir': str(mol)}))
    out = api.render_figure_preset('free_energy_ladder', '/p', {'save_to': str(tmp_path)})
    assert out['ok'] is True and len(out['files']) == 1
    data = calls['render'][0]['data']
    assert data['paths'][0]['G'] == [0.0, -1.0] and data['pds_index'] == 0
    assert calls['render'][0]['params']['title'] == 'Li-S discharge path'


def test_render_figure_preset_pdos_skipped_needs_parse(tmp_path):
    api = Api(figure_presets_mod=_fake_figpresets(), adsorption_mod=_fake_adsorption())
    out = api.render_figure_preset('pdos', '/p', {})
    assert out['ok'] is True and out['files'] == []
    assert out['skipped'][0]['kind'] == 'pdos' and 'PDOS' in out['skipped'][0]['reason']


def test_render_figure_preset_volcano_skipped_multi(tmp_path):
    api = Api(figure_presets_mod=_fake_figpresets(), adsorption_mod=_fake_adsorption())
    out = api.render_figure_preset('volcano', '/p', {})
    assert out['ok'] is True and out['files'] == []
    assert '多催化剂' in out['skipped'][0]['reason']


def test_render_figure_preset_no_done_skipped(tmp_path):
    ads = _fake_adsorption(proj_map={'/p': _proj('demo', str(tmp_path))},
                           delta_ret={'slab': ('RUNNING', None), 'ref': ('无', None),
                                      'has_ref': False,
                                      'rows': [{'name': 'O', 'state': 'RUNNING',
                                                'e_config': None, 'delta_e': None,
                                                'note': ''}]})
    api = Api(figure_presets_mod=_fake_figpresets(), adsorption_mod=ads)
    out = api.render_figure_preset('adsorption_bar', '/p', {})
    assert out['ok'] is True and out['files'] == []
    assert 'ΔE' in out['skipped'][0]['reason']


def test_render_figure_preset_unknown_key_error():
    api = Api(figure_presets_mod=_fake_figpresets(), adsorption_mod=_fake_adsorption())
    out = api.render_figure_preset('nope', '/p', {})
    assert out['ok'] is False and '未知图表预设' in out['error']


# ── draft_ready ──────────────────────────────────────────────────────────────
def test_draft_ready_aggregates_products(tmp_path):
    calls = {}
    ads = _fake_adsorption(proj_map={'/p': _proj('demo', str(tmp_path))})
    api = Api(adsorption_mod=ads, draftpack_mod=_fake_draftpack(calls=calls))
    out = api.draft_ready('/p', str(tmp_path))
    assert out['ok'] is True and out['error'] is None
    assert out['out_dir'] == str(tmp_path)
    # products 汇总 SI + 表格 + 方法学 + 总结
    assert any(p.endswith('_SI.zip') for p in out['products'])
    assert any(p.endswith('methods_zh.md') for p in out['products'])
    assert any(p.endswith('DRAFT_READY.md') for p in out['products'])
    assert calls['draft']['out'] == str(tmp_path)


def test_draft_ready_not_ok_passthrough(tmp_path):
    ret = {'ok': False, 'summary': '⚠️ 口径稽核未通过', 'issues': ['[待确认:X 无 ΔE]'],
           'issues_total': 1, 'summary_path': '/o/DRAFT_READY.md',
           'report': {'si_package': {'zip_path': '/o/x.zip', 'ok': False},
                      'tables': {'files': []}, 'methods': {'files': []}}}
    ads = _fake_adsorption(proj_map={'/p': _proj('demo', str(tmp_path))})
    api = Api(adsorption_mod=ads, draftpack_mod=_fake_draftpack(ret=ret))
    out = api.draft_ready('/p', str(tmp_path))
    assert out['ok'] is False and out['issues_total'] == 1
    assert out['issues'] == ['[待确认:X 无 ΔE]']


def test_draft_ready_missing_project_error():
    api = Api(adsorption_mod=_fake_adsorption(), draftpack_mod=_fake_draftpack())
    out = api.draft_ready('/nope', '/out')
    assert out['ok'] is False and '项目不存在' in out['error']


# ── ai_extract / ai_plan / ai_instantiate ────────────────────────────────────
def test_ai_extract_forwards_and_returns_spec():
    calls = {}
    api = Api(ai_paper_mod=_fake_ai_paper(calls=calls))
    out = api.ai_extract('论文文本 ... VASP ENCUT 500 eV', transport='T')
    assert out['ok'] is True and out['spec']['systems'] == []
    assert '未知泛函「XPB」' in out['issues']
    assert calls['extract']['transport'] == 'T'


def test_ai_extract_external_off_passthrough():
    ret = {'ok': False, 'spec': None, 'issues': [],
           'error': '未开启联网抽取:请在设置页开启「允许将文本发送到外部 LLM」'}
    api = Api(ai_paper_mod=_fake_ai_paper(extract_ret=ret))
    out = api.ai_extract('text')
    assert out['ok'] is False and '未开启联网抽取' in out['error']


def test_ai_plan_returns_plan_and_estimate():
    api = Api(ai_paper_mod=_fake_ai_paper())
    out = api.ai_plan({'systems': [], 'adsorbates': []})
    assert out['ok'] is True and out['jobs_estimate'] == 3
    assert out['est_core_hours'] == 12.5           # 经 dry-run 机时预算闸估值
    assert '需用户提供 INCAR' in out['warnings']


def test_ai_instantiate_registers_campaign_and_returns_gates(tmp_path):
    cc = {}
    api = Api(ai_paper_mod=_fake_ai_paper(),
              config_mod=_fake_config(ui={}, calls=cc))
    out = api.ai_instantiate({'tasks': [{'id': 'a'}]}, str(tmp_path), {})
    assert out['ok'] is True and out['created'] == ['a', 'b']
    assert out['awaiting'] == 'pilot_validation'
    assert out['gates']['pilot']['status'] == 'awaiting'
    # campaign 目录记入仪表盘发现表(config ui.campaign_dirs)
    assert out['campaign_dir'] in cc['ui_state']['campaign_dirs']


def test_ai_instantiate_missing_out_root_error():
    api = Api(ai_paper_mod=_fake_ai_paper())
    out = api.ai_instantiate({'tasks': []}, '', {})
    assert out['ok'] is False and '输出根目录' in out['error']


# ── engine_list / engine_generate / engine_nonequiv ──────────────────────────
def test_engine_list_all_and_scenario_visibility():
    api = Api(engines_mod=_fake_engines())      # 用真实 scenarios 判可见
    out = api.engine_list()
    assert out['ok'] is True and out['default'] == 'vasp'
    assert {e['key'] for e in out['engines']} == {'vasp', 'cp2k', 'gaussian', 'castep'}
    vasp = next(e for e in out['engines'] if e['key'] == 'vasp')
    assert vasp['experimental'] is False and vasp['visible'] is True
    # 分子化学场景:Gaussian 可见,CP2K/CASTEP 不在场景引擎白名单
    out2 = api.engine_list('molecular')
    vis = {e['key']: e['visible'] for e in out2['engines']}
    assert vis['vasp'] is True and vis['gaussian'] is True
    assert vis['cp2k'] is False and vis['castep'] is False


def test_engine_generate_builds_calcspec_and_writes(tmp_path):
    calls = {}
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_EDITOR_POSCAR, encoding='utf-8')
    api = Api(engines_mod=_fake_engines(calls=calls))
    out = api.engine_generate('cp2k', {
        'poscar': str(poscar), 'task': 'relax', 'functional': 'PBE',
        'cutoff_ev': 500, 'kpoints': [3, 3, 1], 'spin': True, 'charge': 0,
        'periodic': True}, str(tmp_path / 'cp2k_out'))
    assert out['ok'] is True and out['files'] == [str(tmp_path / 'cp2k_out') + '/cp2k.inp']
    assert calls['engine'] == 'cp2k'
    assert calls['spec']['cutoff_ev'] == 500.0 and calls['spec']['kpoints'] == (3, 3, 1)
    assert calls['spec']['spin'] is True


def test_engine_generate_surfaces_validate_issues(tmp_path):
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_EDITOR_POSCAR, encoding='utf-8')
    api = Api(engines_mod=_fake_engines(
        validate_issues=['周期性计算必须指定 cutoff_ev(平面波截断能,eV);缺失或非正值。']))
    out = api.engine_generate('cp2k', {'poscar': str(poscar), 'periodic': True},
                              str(tmp_path / 'o'))
    assert out['ok'] is True and out['issues'] and 'cutoff_ev' in out['issues'][0]


def test_engine_generate_missing_poscar_error(tmp_path):
    api = Api(engines_mod=_fake_engines())
    out = api.engine_generate('cp2k', {'poscar': '/nope/POSCAR'}, str(tmp_path))
    assert out['ok'] is False and '结构文件' in out['error']


def test_engine_nonequiv_report():
    api = Api(engines_mod=_fake_engines(
        nonequiv=['[cutoff] ENCUT 与 CUTOFF 不可换算', '[basis] 平面波 vs 高斯基组']))
    out = api.engine_nonequiv('vasp', 'cp2k')
    assert out['ok'] is True and len(out['report']) == 2
    assert 'cutoff' in out['report'][0]


# ── struct_load / struct_save / struct_fix_layers / struct_vacuum(真实往返) ───
def test_struct_load_parses_elements_coords_lattice(tmp_path):
    p = tmp_path / 'POSCAR'
    p.write_text(_EDITOR_POSCAR, encoding='utf-8')
    api = Api()
    out = api.struct_load(str(p))
    assert out['ok'] is True
    st = out['struct']
    assert st['elements'] == ['Fe', 'Fe', 'O'] and st['natoms'] == 3
    assert st['lattice'][2] == [0.0, 0.0, 20.0]
    assert st['formula'] == 'Fe2 O1'
    assert out['vacuum'] == 18.0                       # |c| 20 − z 跨度 2


def test_struct_load_missing_file_error():
    api = Api()
    out = api.struct_load('/nope/POSCAR')
    assert out['ok'] is False and '不存在' in out['error']


def test_struct_save_roundtrip(tmp_path):
    api = Api()
    state = {'elements': ['Fe', 'Fe', 'O'],
             'coords': [[0.0, 0.0, 5.0], [1.5, 1.5, 5.0], [0.75, 0.75, 7.0]],
             'lattice': [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 20.0]]}
    dest = tmp_path / 'out' / 'POSCAR'
    out = api.struct_save(state, str(dest))
    assert out['ok'] is True and os.path.isfile(str(dest))
    # 写出的 POSCAR 应可被 struct_load 再解析回同样的元素/原子数(往返)
    back = api.struct_load(str(dest))
    assert back['ok'] is True and back['struct']['elements'] == ['Fe', 'Fe', 'O']
    assert back['struct']['natoms'] == 3


def test_struct_fix_layers_maps_flags_back_to_state_order():
    # 状态原子序为 O(顶) 在前、Fe(底) 在后 —— 写出按物种分块会重排,须正确映射回状态序
    api = Api()
    state = {'elements': ['O', 'Fe', 'Fe'],
             'coords': [[0.75, 0.75, 7.0], [0.0, 0.0, 5.0], [1.5, 1.5, 5.0]],
             'lattice': [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 20.0]]}
    out = api.struct_fix_layers(state, 1)
    assert out['ok'] is True
    # 底层是两个 Fe(状态 idx 1、2)→ 冻结;O(状态 idx 0,顶层)→ 不冻结
    assert out['fixed'] == [False, True, True] and out['fixed_count'] == 2
    assert out['vacuum'] == 18.0


def test_struct_fix_layers_too_many_layers_error():
    api = Api()
    state = {'elements': ['Fe', 'Fe', 'O'],
             'coords': [[0.0, 0.0, 5.0], [1.5, 1.5, 5.0], [0.75, 0.75, 7.0]],
             'lattice': [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 20.0]]}
    out = api.struct_fix_layers(state, 5)          # 5 ≥ 总层数 2
    assert out['ok'] is False and out['fixed'] is None


def test_struct_save_with_fixed_emits_selective_dynamics(tmp_path):
    api = Api()
    state = {'elements': ['Fe', 'Fe', 'O'],
             'coords': [[0.0, 0.0, 5.0], [1.5, 1.5, 5.0], [0.75, 0.75, 7.0]],
             'lattice': [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 20.0]],
             'fixed': [True, True, False]}
    dest = tmp_path / 'POSCAR'
    out = api.struct_save(state, str(dest))
    assert out['ok'] is True
    text = dest.read_text(encoding='utf-8')
    assert 'Selective dynamics' in text
    assert text.count('F F F') == 2 and text.count('T T T') == 1


def test_struct_vacuum_from_state():
    api = Api()
    state = {'elements': ['Fe', 'O'],
             'coords': [[0.0, 0.0, 5.0], [0.75, 0.75, 7.0]],
             'lattice': [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 20.0]]}
    out = api.struct_vacuum(state)
    assert out['ok'] is True and out['vacuum'] == 18.0


def test_struct_vacuum_invalid_state_error():
    api = Api()
    out = api.struct_vacuum({'elements': ['Fe'], 'coords': [], 'lattice': []})
    assert out['ok'] is False and out['vacuum'] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 分子计算全流程总装(结构建模分子区 / ②Gaussian 面板 / ③本机运行·文件管理 /
# ⑤波函数分析 / ④AIMD 派生)—— 全部注入假件,零 rdkit/paramiko/Multiwfn/VMD。
# ═══════════════════════════════════════════════════════════════════════════════
def _fake_molbuild(*, ocsr_ret=None, svg_ret=None, s3d_ret=None, info_ret=None,
                   export_ret=None, open_ret=None, check_ret=None, reimport_ret=None,
                   probe_avail=True, calls=None):
    """molbuild 束假件:ocsr/smiles3d/molinfo/external_editor 四子模块。"""
    calls = calls if calls is not None else {}
    ocsr = types.SimpleNamespace()
    ocsr.probe = lambda: {'available': probe_avail,
                          'detail': 'DECIMER 可用' if probe_avail else '未安装 DECIMER'}
    ocsr.image_to_smiles = lambda p: (calls.__setitem__('img', p) or (
        ocsr_ret if ocsr_ret is not None else
        {'ok': True, 'smiles': 'c1ccccc1', 'elapsed_ms': 12.3, 'error': ''}))
    ocsr.smiles_svg = lambda s, width=400, height=300: (
        calls.__setitem__('svg', {'s': s, 'w': width, 'h': height}) or (
            svg_ret if svg_ret is not None else
            {'ok': True, 'svg': '<svg>ok</svg>', 'error': ''}))
    smiles3d = types.SimpleNamespace()
    smiles3d.smiles_to_3d = lambda s, forcefield='auto', **k: (
        calls.__setitem__('s3d', {'s': s, 'ff': forcefield}) or (
            s3d_ret if s3d_ret is not None else
            {'ok': True, 'elements': ['C', 'O'], 'coords': [[0, 0, 0], [1.2, 0, 0]],
             'formula': 'CO', 'n_atoms': 2, 'charge': 0, 'multiplicity_hint': 1,
             'warnings': ['实际所用力场:MMFF'], 'error': ''}))
    molinfo = types.SimpleNamespace()
    molinfo.mol_summary = lambda els, coords=None, charge=0: (
        info_ret if info_ret is not None else
        {'formula': 'CO', 'n_atoms': len(els), 'n_electrons': 14, 'mass_amu': 28.01,
         'charge': charge, 'suggested_multiplicity': 1, 'multiplicity_note': '按奇偶初猜'})
    molinfo.formula = lambda els: '+'.join(sorted(set(els))) or 'X'
    ee = types.SimpleNamespace()
    ee.export_for_editor = lambda els, cds, fmt='xyz', workdir=None: (
        calls.__setitem__('export', {'fmt': fmt, 'workdir': workdir}) or (
            export_ret if export_ret is not None else
            {'ok': True, 'path': '/tmp/vcstudio_external_edit.' + fmt,
             'mtime': 111.0, 'error': ''}))
    ee.open_with = lambda p, editor_exe=None: (
        calls.__setitem__('open', {'p': p, 'exe': editor_exe}) or (
            open_ret if open_ret is not None else {'ok': True, 'error': ''}))
    ee.check_reimport = lambda p, last: (
        check_ret if check_ret is not None else {'changed': True, 'mtime': 222.0})
    ee.reimport = lambda p: (
        reimport_ret if reimport_ret is not None else
        {'ok': True, 'elements': ['C', 'O'], 'coords': [[0, 0, 0], [1.2, 0, 0]], 'error': ''})
    return types.SimpleNamespace(ocsr=ocsr, smiles3d=smiles3d, molinfo=molinfo,
                                 external_editor=ee)


def _fake_gaussian(*, preview_text='#P B3LYP def2-SVP opt\n\n0 1\nC 0 0 0\n', calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.GAUSSIAN_TASKS = {
        'opt': {'name_zh': '结构优化', 'note': 'Opt'},
        'freq': {'name_zh': '频率分析', 'note': 'Freq'},
        'td': {'name_zh': '激发态 TD-DFT', 'note': 'TD'},
    }
    m.PERIODIC_TABLE_GROUPS = {
        'note': '基组建议仅供起点',
        'categories': [{'key': 'transition_metal', 'label': '过渡金属',
                        'basis_suggestion': ['LANL2DZ', 'SDD'], 'needs_ecp': True}],
        'elements': [{'z': 1, 'symbol': 'H', 'category': 'main_group',
                      'basis_suggestion': ['6-31G(d)']},
                     {'z': 26, 'symbol': 'Fe', 'category': 'transition_metal',
                      'basis_suggestion': ['LANL2DZ', 'SDD']}],
    }
    m.preview = lambda spec: (calls.__setitem__('spec', spec) or preview_text)
    return m


# ── mol_ocsr_probe ───────────────────────────────────────────────────────────
def test_mol_ocsr_probe_available():
    api = Api(molbuild_mods=_fake_molbuild(probe_avail=True))
    out = api.mol_ocsr_probe()
    assert out['ok'] is True and out['available'] is True and 'DECIMER' in out['detail']


def test_mol_ocsr_probe_missing_reports_unavailable():
    api = Api(molbuild_mods=_fake_molbuild(probe_avail=False))
    out = api.mol_ocsr_probe()
    assert out['ok'] is True and out['available'] is False


def test_mol_ocsr_probe_exception_caught():
    boom = types.SimpleNamespace(ocsr=types.SimpleNamespace(
        probe=lambda: (_ for _ in ()).throw(RuntimeError('炸'))))
    out = Api(molbuild_mods=boom).mol_ocsr_probe()
    assert out['ok'] is False and '炸' in out['error']


# ── mol_image_to_smiles ──────────────────────────────────────────────────────
def test_mol_image_to_smiles_ok():
    calls = {}
    api = Api(molbuild_mods=_fake_molbuild(calls=calls))
    out = api.mol_image_to_smiles('/tmp/mol.png')
    assert out['ok'] is True and out['smiles'] == 'c1ccccc1' and out['elapsed_ms'] == 12.3
    assert calls['img'] == '/tmp/mol.png'


def test_mol_image_to_smiles_empty_path_error():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_image_to_smiles('   ')
    assert out['ok'] is False and '图片' in out['error']


def test_mol_image_to_smiles_driver_missing_passthrough():
    api = Api(molbuild_mods=_fake_molbuild(
        ocsr_ret={'ok': False, 'smiles': '', 'elapsed_ms': 0.0, 'error': '未安装 DECIMER'}))
    out = api.mol_image_to_smiles('/tmp/x.png')
    assert out['ok'] is False and 'DECIMER' in out['error']


# ── mol_smiles_svg ───────────────────────────────────────────────────────────
def test_mol_smiles_svg_ok():
    calls = {}
    api = Api(molbuild_mods=_fake_molbuild(calls=calls))
    out = api.mol_smiles_svg('CCO', width=500, height=350)
    assert out['ok'] is True and '<svg>' in out['svg']
    assert calls['svg'] == {'s': 'CCO', 'w': 500, 'h': 350}


def test_mol_smiles_svg_empty_error():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_smiles_svg('')
    assert out['ok'] is False and 'SMILES' in out['error']


# ── mol_smiles_to_3d ─────────────────────────────────────────────────────────
def test_mol_smiles_to_3d_builds_struct():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_smiles_to_3d('CO', 'mmff')
    assert out['ok'] is True
    st = out['struct']
    assert st['elements'] == ['C', 'O'] and st['natoms'] == 2 and st['formula'] == 'CO'
    assert st['lattice'][0][0] == 15.0                 # 立方盒边长
    assert out['charge'] == 0 and out['multiplicity_hint'] == 1
    assert any('MMFF' in w for w in out['warnings'])


def test_mol_smiles_to_3d_empty_error():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_smiles_to_3d('')
    assert out['ok'] is False and out['struct'] is None


def test_mol_smiles_to_3d_rdkit_missing():
    api = Api(molbuild_mods=_fake_molbuild(
        s3d_ret={'ok': False, 'elements': [], 'coords': [], 'formula': '', 'n_atoms': 0,
                 'charge': 0, 'multiplicity_hint': None, 'warnings': [],
                 'error': '未安装 rdkit'}))
    out = api.mol_smiles_to_3d('CCO')
    assert out['ok'] is False and 'rdkit' in out['error'] and out['struct'] is None


# ── mol_info ─────────────────────────────────────────────────────────────────
def test_mol_info_ok():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_info(['C', 'O'], [[0, 0, 0], [1.2, 0, 0]], 0)
    assert out['ok'] is True and out['n_electrons'] == 14
    assert out['suggested_multiplicity'] == 1 and out['formula'] == 'CO'


def test_mol_info_empty_elements_error():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_info([])
    assert out['ok'] is False and '原子' in out['error']


def test_mol_info_unregistered_element_caught():
    boom = types.SimpleNamespace(molinfo=types.SimpleNamespace(
        mol_summary=lambda els, coords=None, charge=0:
        (_ for _ in ()).throw(ValueError('未登记元素'))))
    out = Api(molbuild_mods=boom).mol_info(['Xx'])
    assert out['ok'] is False and '未登记' in out['error']


# ── mol_export_editor / mol_open_with ────────────────────────────────────────
def test_mol_export_editor_ok():
    calls = {}
    api = Api(molbuild_mods=_fake_molbuild(calls=calls))
    out = api.mol_export_editor(['C', 'O'], [[0, 0, 0], [1.2, 0, 0]], 'mol', '/wd')
    assert out['ok'] is True and out['path'].endswith('.mol') and out['mtime'] == 111.0
    assert calls['export'] == {'fmt': 'mol', 'workdir': '/wd'}


def test_mol_export_editor_bad_fmt_passthrough():
    api = Api(molbuild_mods=_fake_molbuild(
        export_ret={'ok': False, 'path': None, 'mtime': None, 'error': '未知导出格式'}))
    out = api.mol_export_editor(['C'], [[0, 0, 0]], 'pdb')
    assert out['ok'] is False and '格式' in out['error']


def test_mol_open_with_ok():
    calls = {}
    api = Api(molbuild_mods=_fake_molbuild(calls=calls))
    out = api.mol_open_with('/tmp/x.xyz', '/usr/bin/avogadro')
    assert out['ok'] is True and calls['open']['exe'] == '/usr/bin/avogadro'


def test_mol_open_with_empty_path_error():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_open_with('')
    assert out['ok'] is False and '路径' in out['error']


# ── mol_check_reimport / mol_reimport ────────────────────────────────────────
def test_mol_check_reimport_changed():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_check_reimport('/tmp/edit.xyz', 100.0)
    assert out['ok'] is True and out['changed'] is True and out['mtime'] == 222.0


def test_mol_check_reimport_empty_error():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_check_reimport('')
    assert out['ok'] is False and out['changed'] is False


def test_mol_reimport_builds_struct(tmp_path):
    f = tmp_path / 'edit.xyz'
    f.write_text('2\n\nC 0 0 0\nO 1.2 0 0\n', encoding='utf-8')
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_reimport(str(f))
    assert out['ok'] is True and out['struct']['elements'] == ['C', 'O']
    assert out['struct']['natoms'] == 2


def test_mol_reimport_missing_file_error():
    api = Api(molbuild_mods=_fake_molbuild())
    out = api.mol_reimport('/nope/edit.xyz')
    assert out['ok'] is False and '不存在' in out['error']


# ── gauss_tasks / gauss_periodic_table ───────────────────────────────────────
def test_gauss_tasks_shape():
    api = Api(gaussian_mod=_fake_gaussian())
    out = api.gauss_tasks()
    assert out['ok'] is True
    keys = {t['key'] for t in out['tasks']}
    assert 'td' in keys
    td = next(t for t in out['tasks'] if t['key'] == 'td')
    assert td['name'] == '激发态 TD-DFT'


def test_gauss_periodic_table_shape():
    api = Api(gaussian_mod=_fake_gaussian())
    out = api.gauss_periodic_table()
    assert out['ok'] is True
    syms = {e['symbol'] for e in out['table']['elements']}
    assert 'Fe' in syms
    fe = next(e for e in out['table']['elements'] if e['symbol'] == 'Fe')
    assert 'LANL2DZ' in fe['basis_suggestion']


def test_gauss_tasks_exception_caught():
    class _Boom:
        @property
        def GAUSSIAN_TASKS(self):
            raise RuntimeError('炸')
    out = Api(gaussian_mod=_Boom()).gauss_tasks()
    assert out['ok'] is False and '炸' in out['error']


# ── engine_preview ───────────────────────────────────────────────────────────
def test_engine_preview_gaussian_text_and_chars(tmp_path):
    calls = {}
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_EDITOR_POSCAR, encoding='utf-8')
    api = Api(engines_mod=_fake_engines(), gaussian_mod=_fake_gaussian(calls=calls))
    out = api.engine_preview('gaussian', {
        'poscar': str(poscar), 'periodic': False, 'functional': 'B3LYP',
        'extras': {'gaussian_task': 'opt', 'basis': 'def2-SVP'}})
    assert out['ok'] is True and out['chars'] == len(out['text'])
    assert 'B3LYP' in out['text']
    assert calls['spec'].extras['gaussian_task'] == 'opt'   # extras 透传进 CalcSpec


def test_engine_preview_generic_roundtrip(tmp_path):
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_EDITOR_POSCAR, encoding='utf-8')

    def _writer(spec, out):
        p = os.path.join(out, 'cp2k.inp')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('&GLOBAL\n  RUN_TYPE GEO_OPT\n&END\n')
        return {'files': [p], 'warnings': ['需自备 GTH 赝势']}
    eng = _fake_engines()
    eng.get_backend = lambda name: types.SimpleNamespace(generate_inputs=_writer)
    api = Api(engines_mod=eng)
    out = api.engine_preview('cp2k', {'poscar': str(poscar), 'periodic': True,
                                      'cutoff_ev': 500})
    assert out['ok'] is True and 'RUN_TYPE' in out['text'] and out['chars'] > 0
    assert '需自备 GTH 赝势' in out['warnings']


def test_engine_preview_missing_structure_error():
    api = Api(engines_mod=_fake_engines(), gaussian_mod=_fake_gaussian())
    out = api.engine_preview('gaussian', {'poscar': '/nope/POSCAR'})
    assert out['ok'] is False and '结构文件' in out['error']


def test_engine_generate_passes_extras(tmp_path):
    calls = {}
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_EDITOR_POSCAR, encoding='utf-8')
    api = Api(engines_mod=_fake_engines(calls=calls))
    out = api.engine_generate('gaussian', {
        'poscar': str(poscar), 'periodic': False,
        'extras': {'gaussian_task': 'freq', 'nproc': 8}}, str(tmp_path / 'g_out'))
    assert out['ok'] is True
    assert calls['spec']['extras'] == {'gaussian_task': 'freq', 'nproc': 8}


# ── quick_submit_build ───────────────────────────────────────────────────────
def _fake_quick_submit(*, jobs=None, skipped=None, ok=True, error=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(files, out_root, job_prefix=''):
        calls['build'] = {'files': list(files), 'out_root': out_root, 'prefix': job_prefix}
        return {'ok': ok, 'jobs': list(jobs if jobs is not None else
                [{'dir': out_root + '/benzene', 'name': 'benzene', 'engine': 'gaussian',
                  'files': ['benzene.gjf']}]),
                'skipped': list(skipped or []), 'error': error}
    m.build_quick_jobs = _build
    m.submit_hint = lambda engine: f'{engine}-cmd-hint'
    return m


def test_quick_submit_build_registers_and_hints(tmp_path):
    registered = []
    api = Api(quick_submit_mod=_fake_quick_submit(),
              ledger_mod=_fake_ledger_register(registered))
    out = api.quick_submit_build(['/x/benzene.gjf'], str(tmp_path))
    assert out['ok'] is True and len(out['jobs']) == 1
    j = out['jobs'][0]
    assert j['engine'] == 'gaussian' and j['hint'] == 'gaussian-cmd-hint'
    assert j['registered'] is True and registered == [j['dir']]


def test_quick_submit_build_reports_skipped(tmp_path):
    api = Api(quick_submit_mod=_fake_quick_submit(
        jobs=[], skipped=[{'file': '/x/foo.txt', 'reason': '无法识别引擎'}]),
        ledger_mod=_fake_ledger_register([]))
    out = api.quick_submit_build(['/x/foo.txt'], str(tmp_path))
    assert out['ok'] is True and out['jobs'] == []
    assert out['skipped'][0]['reason'] == '无法识别引擎'


def test_quick_submit_build_no_files_error():
    api = Api(quick_submit_mod=_fake_quick_submit())
    out = api.quick_submit_build([], '/root')
    assert out['ok'] is False and '输入文件' in out['error']


def test_quick_submit_build_no_root_error():
    api = Api(quick_submit_mod=_fake_quick_submit())
    out = api.quick_submit_build(['/x/a.gjf'], '')
    assert out['ok'] is False and '输出根目录' in out['error']


# ── jobs_cancel_batch ────────────────────────────────────────────────────────
def _fake_batch_cancel(*, ok=True, cancelled=None, failed=None, error=None,
                       needs_trust=False, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _cancel(profile, jobs, *, password=None, trust_new=False):
        calls['cancel'] = {'jobs': list(jobs), 'password': password, 'trust_new': trust_new}
        return {'ok': ok, 'cancelled': list(cancelled or ['12345']),
                'failed': list(failed or []), 'error': error, 'needs_trust': needs_trust}
    m.cancel_batch = _cancel
    return m


def test_jobs_cancel_batch_ok():
    calls = {}
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store),
              batch_ops_mod=_fake_batch_cancel(calls=calls))
    out = api.jobs_cancel_batch(['/j/a', '/j/b'], 'c1', None, False)
    assert out['ok'] is True and out['cancelled'] == ['12345']
    assert calls['cancel']['jobs'] == ['/j/a', '/j/b']


def test_jobs_cancel_batch_no_dirs_error():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=_fake_batch_cancel())
    out = api.jobs_cancel_batch([], 'c1', None)
    assert out['ok'] is False and '取消' in out['error']


def test_jobs_cancel_batch_needs_password():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    secrets = types.SimpleNamespace(get_password=lambda n: None, set_password=lambda n, p: None)
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets,
              batch_ops_mod=_fake_batch_cancel())
    out = api.jobs_cancel_batch(['/j/a'], 'c1', None)
    assert out['error'] == 'NEED_PASSWORD'


# ── local_run_start / status / cancel ────────────────────────────────────────
def _fake_local_runner(*, start_ret=None, status_ret=None, cancel_ret=None, calls=None):
    calls = calls if calls is not None else {}

    class _LocalJob:
        def __init__(self, cmd, cwd, log_file, env=None):
            self.cmd, self.cwd, self.log_file, self.env = cmd, cwd, log_file, env
            calls['job'] = {'cmd': cmd, 'cwd': cwd, 'log_file': log_file}
    m = types.SimpleNamespace()
    m.LocalJob = _LocalJob
    m.start = lambda job: (start_ret if start_ret is not None else
                           {'ok': True, 'pid': 4242, 'error': ''})
    m.status = lambda d: (status_ret if status_ret is not None else
                          {'state': 'RUNNING', 'pid': 4242, 'exit_code': None,
                           'log_tail': '... running ...'})
    m.cancel = lambda d: (cancel_ret if cancel_ret is not None else {'ok': True, 'error': ''})
    return m


def test_local_run_start_builds_cmd_from_input(tmp_path):
    (tmp_path / 'benzene.gjf').write_text('#opt', encoding='utf-8')
    calls = {}
    api = Api(local_runner_mod=_fake_local_runner(calls=calls))
    out = api.local_run_start(str(tmp_path), 'g16')
    assert out['ok'] is True and out['pid'] == 4242
    assert out['cmd'] == ['g16', 'benzene.gjf']          # 末尾追加识别到的输入文件
    assert calls['job']['cwd'] == str(tmp_path)


def test_local_run_start_placeholder_template(tmp_path):
    (tmp_path / 'mol.com').write_text('#sp', encoding='utf-8')
    api = Api(local_runner_mod=_fake_local_runner())
    out = api.local_run_start(str(tmp_path), 'g09 {input} {output}')
    assert out['ok'] is True and out['cmd'] == ['g09', 'mol.com', 'mol.log']


def test_local_run_start_missing_dir_error():
    api = Api(local_runner_mod=_fake_local_runner())
    out = api.local_run_start('/no/such/dir', 'g16')
    assert out['ok'] is False and '目录' in out['error']


def test_local_run_start_missing_template_error(tmp_path):
    api = Api(local_runner_mod=_fake_local_runner())
    out = api.local_run_start(str(tmp_path), '  ')
    assert out['ok'] is False and '命令模板' in out['error']


def test_local_run_status_ok(tmp_path):
    api = Api(local_runner_mod=_fake_local_runner())
    out = api.local_run_status(str(tmp_path))
    assert out['ok'] is True and out['state'] == 'RUNNING' and out['pid'] == 4242


def test_local_run_status_missing_dir_error():
    api = Api(local_runner_mod=_fake_local_runner())
    out = api.local_run_status('')
    assert out['ok'] is False and out['state'] == 'NOT_STARTED'


def test_local_run_cancel_ok(tmp_path):
    api = Api(local_runner_mod=_fake_local_runner())
    out = api.local_run_cancel(str(tmp_path))
    assert out['ok'] is True


def test_local_run_cancel_missing_dir_error():
    api = Api(local_runner_mod=_fake_local_runner())
    out = api.local_run_cancel('')
    assert out['ok'] is False


# ── remote_ls / remote_fetch_file ────────────────────────────────────────────
class _FakeConnectError(Exception):
    def __init__(self, message, needs_trust=False):
        super().__init__(message)
        self.needs_trust = needs_trust


def _fake_connection(*, entries=None, connect_boom=None, calls=None, exec_ret=None):
    """connection 假件:open_client → (client, jump);client.open_sftp/exec_command。"""
    calls = calls if calls is not None else {}

    class _SFTP:
        def listdir_attr(self, path):
            calls['ls_path'] = path
            attrs = []
            for e in (entries if entries is not None else
                      [('run.log', 2048, 1700000000, 0o100644),
                       ('OUTCAR', 4096, 1700000100, 0o100644),
                       ('scratch', 0, 1700000200, 0o040755)]):
                attrs.append(types.SimpleNamespace(
                    filename=e[0], st_size=e[1], st_mtime=e[2], st_mode=e[3]))
            return attrs

        def get(self, remote, local):
            calls['get'] = {'remote': remote, 'local': local}
            with open(local, 'w', encoding='utf-8') as f:
                f.write('fetched')

        def put(self, local, remote):
            calls.setdefault('put', []).append({'local': local, 'remote': remote})

        def open(self, path, mode):
            calls.setdefault('scripts', []).append(path)
            return _SFTPFile()

        def close(self):
            calls['sftp_closed'] = True

    class _SFTPFile:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, data):
            calls.setdefault('script_text', []).append(data)

    class _Chan:
        def recv_exit_status(self):
            return (exec_ret or {}).get('code', 0)

    class _Out:
        channel = _Chan()

        def read(self):
            return (exec_ret or {}).get('stdout', b' minima found\n')

    class _Client:
        def open_sftp(self):
            return _SFTP()

        def exec_command(self, cmd, timeout=None):
            calls.setdefault('exec', []).append(cmd)
            return None, _Out(), None

    m = types.SimpleNamespace()
    m.ConnectError = _FakeConnectError

    def _open(prof, pw, trust_new=False):
        calls['open'] = {'prof': prof.name, 'trust_new': trust_new}
        if connect_boom:
            raise connect_boom
        return _Client(), None
    m.open_client = _open
    m.close_quiet = lambda *cs: calls.__setitem__('closed', True)
    return m


def test_remote_ls_lists_entries(tmp_path):
    calls = {}
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store),
              connection_mod=_fake_connection(calls=calls))
    out = api.remote_ls('c1', None, '/home/me/run')
    assert out['ok'] is True and calls['ls_path'] == '/home/me/run'
    names = [e['name'] for e in out['entries']]
    assert 'scratch' in names and 'OUTCAR' in names
    scratch = next(e for e in out['entries'] if e['name'] == 'scratch')
    assert scratch['is_dir'] is True and out['entries'][0]['is_dir'] is True  # 目录排前


def test_remote_ls_connect_error_needs_trust():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store),
              connection_mod=_fake_connection(
                  connect_boom=_FakeConnectError('未知指纹', needs_trust=True)))
    out = api.remote_ls('c1', None, '.')
    assert out['ok'] is False and out['needs_trust'] is True and '指纹' in out['error']


def test_remote_ls_needs_password():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    secrets = types.SimpleNamespace(get_password=lambda n: None, set_password=lambda n, p: None)
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets,
              connection_mod=_fake_connection())
    out = api.remote_ls('c1', None, '.')
    assert out['error'] == 'NEED_PASSWORD'


def test_remote_fetch_file_downloads(tmp_path):
    calls = {}
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store),
              connection_mod=_fake_connection(calls=calls))
    out = api.remote_fetch_file('c1', None, '/home/me/run/OUTCAR', str(tmp_path))
    assert out['ok'] is True
    assert out['local_path'] == os.path.join(str(tmp_path), 'OUTCAR')
    assert os.path.isfile(out['local_path'])
    assert calls['get']['remote'] == '/home/me/run/OUTCAR'


def test_remote_fetch_file_missing_remote_error():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store), connection_mod=_fake_connection())
    out = api.remote_fetch_file('c1', None, '', '/tmp')
    assert out['ok'] is False and '远端文件' in out['error']


def test_remote_fetch_file_missing_local_error():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store), connection_mod=_fake_connection())
    out = api.remote_fetch_file('c1', None, '/r/OUTCAR', '')
    assert out['ok'] is False and '本地' in out['error']


# ── 波函数分析:probe / scenes / run / render / extrema / run_remote ───────────
def _fake_multiwfn(*, run_ret=None, probe_avail=True, extrema=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.ANALYSES = {
        'esp_extrema': {'name': 'ESP 表面极值点', 'stdin_script': lambda p: '12\n0\n',
                        'outputs': (), 'note': 'ESP 极值'},
        'homo_lumo_cube': {'name': 'HOMO/LUMO 轨道 cube',
                           'stdin_script': lambda p: '5\n4\nHOMO\n', 'outputs': ('orbital.cub',),
                           'note': '轨道 cube'},
    }
    m.probe = lambda exe=None: {'available': probe_avail, 'path': '/opt/Multiwfn' if probe_avail
                                else None, 'detail': 'ok' if probe_avail else '未找到 Multiwfn'}

    def _run(wf, key, exe=None, workdir=None, params=None):
        calls.setdefault('runs', []).append({'wf': wf, 'key': key, 'exe': exe})
        if run_ret is not None:
            return dict(run_ret)
        return {'ok': True, 'outputs': [f'{key}_orbital.cub'] if 'cube' in key else [],
                'stdout_tail': ' Minima\n 1  0.0 0.0 0.0  -12.3\n', 'elapsed_s': 1.2,
                'error': ''}
    m.run = _run
    m.extrema_parse = lambda text: (extrema if extrema is not None else
                                    {'minima': [{'value_kcal': -12.3, 'xyz': [0.0, 0.0, 0.0]}],
                                     'maxima': []})
    return m


def _fake_vmd(*, render_ret=None, probe_avail=True, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.SCENES = {
        'esp_surface': {'name': 'ESP 着色分子表面', 'files': ('density', 'esp'),
                        'note': 'ESP 表面'},
        'orbital': {'name': '分子轨道等值面', 'files': ('cube',), 'note': '轨道'},
    }
    m.probe = lambda exe=None: {'available': probe_avail, 'path': '/opt/vmd' if probe_avail
                                else None, 'detail': 'ok' if probe_avail else '未找到 VMD'}

    def _render(scene, files, out_png, exe=None, params=None, timeout=600):
        calls['render'] = {'scene': scene, 'files': files, 'out': out_png, 'exe': exe}
        if render_ret is not None:
            return dict(render_ret)
        return {'ok': True, 'png': out_png, 'tcl': 'mol new ...', 'stdout_tail': 'done',
                'error': ''}
    m.render = _render
    return m


def test_wavefn_probe_all_tools():
    api = Api(multiwfn_mod=_fake_multiwfn(probe_avail=True),
              vmd_mod=_fake_vmd(probe_avail=False),
              config_mod=_fake_config(cfg={}))
    out = api.wavefn_probe(['multiwfn', 'vmd', 'gaussview'])
    assert out['ok'] is True
    assert out['tools']['multiwfn']['available'] is True
    assert out['tools']['vmd']['available'] is False
    assert out['tools']['gaussview']['available'] is False   # PATH 无 gview,确定性未找到


def test_wavefn_probe_uses_configured_path():
    calls = {}
    mw = _fake_multiwfn(calls=calls)
    api = Api(multiwfn_mod=mw, vmd_mod=_fake_vmd(),
              config_mod=_fake_config(cfg={'tool_paths': {'multiwfn': '/custom/Multiwfn'}}))
    api.wavefn_probe(['multiwfn'])
    # probe 被调用(available 依赖 fake),配置路径读取无异常
    out = api.wavefn_probe(['multiwfn'])
    assert out['ok'] is True


def test_wavefn_scenes_catalog():
    api = Api(multiwfn_mod=_fake_multiwfn(), vmd_mod=_fake_vmd())
    out = api.wavefn_scenes()
    assert out['ok'] is True
    akeys = {a['key'] for a in out['analyses']}
    skeys = {s['key'] for s in out['scenes']}
    assert 'esp_extrema' in akeys and 'esp_surface' in skeys
    esp = next(s for s in out['scenes'] if s['key'] == 'esp_surface')
    assert esp['files'] == ['density', 'esp']


def test_wavefn_run_multiple_analyses(tmp_path):
    calls = {}
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    api = Api(multiwfn_mod=_fake_multiwfn(calls=calls), config_mod=_fake_config(cfg={}))
    out = api.wavefn_run(str(wf), ['esp_extrema', 'homo_lumo_cube'])
    assert out['ok'] is True and len(out['results']) == 2
    esp = out['results'][0]
    assert esp['analysis'] == 'esp_extrema' and 'extrema' in esp
    assert esp['extrema']['minima'][0]['value_kcal'] == -12.3
    assert [r['key'] for r in calls['runs']] == ['esp_extrema', 'homo_lumo_cube']


def test_wavefn_run_missing_file_error():
    api = Api(multiwfn_mod=_fake_multiwfn(), config_mod=_fake_config(cfg={}))
    out = api.wavefn_run('', ['esp_extrema'])
    assert out['ok'] is False and '波函数文件' in out['error']


def test_wavefn_run_no_analyses_error(tmp_path):
    wf = tmp_path / 'mol.wfn'
    wf.write_text('x', encoding='utf-8')
    api = Api(multiwfn_mod=_fake_multiwfn(), config_mod=_fake_config(cfg={}))
    out = api.wavefn_run(str(wf), [])
    assert out['ok'] is False and '分析项' in out['error']


def test_wavefn_run_missing_multiwfn_returns_script(tmp_path):
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    mw = _fake_multiwfn(run_ret={'ok': False, 'outputs': [], 'stdout_tail': '',
                                 'elapsed_s': 0.0, 'script': '12\n0\n',
                                 'error': '未找到 Multiwfn'})
    api = Api(multiwfn_mod=mw, config_mod=_fake_config(cfg={}))
    out = api.wavefn_run(str(wf), ['esp_extrema'])
    assert out['ok'] is False and out['results'][0]['script'] == '12\n0\n'
    assert 'Multiwfn' in out['results'][0]['error']


def test_wavefn_render_ok(tmp_path):
    calls = {}
    api = Api(vmd_mod=_fake_vmd(calls=calls), config_mod=_fake_config(cfg={}))
    out = api.wavefn_render('esp_surface', {'density': '/d.cub', 'esp': '/e.cub'},
                            str(tmp_path / 'esp.png'))
    assert out['ok'] is True and out['png'].endswith('esp.png')
    assert calls['render']['scene'] == 'esp_surface'


def test_wavefn_render_missing_scene_error():
    api = Api(vmd_mod=_fake_vmd(), config_mod=_fake_config(cfg={}))
    out = api.wavefn_render('', {}, '/tmp/x.png')
    assert out['ok'] is False and '场景' in out['error']


def test_wavefn_render_vmd_missing_returns_tcl(tmp_path):
    vmd = _fake_vmd(render_ret={'ok': False, 'png': None, 'tcl': 'mol new xxx',
                                'stdout_tail': '', 'error': '未找到 VMD'})
    api = Api(vmd_mod=vmd, config_mod=_fake_config(cfg={}))
    out = api.wavefn_render('orbital', {'cube': '/o.cub'}, str(tmp_path / 'o.png'))
    assert out['ok'] is False and out['tcl'] == 'mol new xxx'


def test_wavefn_extrema_parses(tmp_path):
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    api = Api(multiwfn_mod=_fake_multiwfn(), config_mod=_fake_config(cfg={}))
    out = api.wavefn_extrema(str(wf), 'esp_extrema')
    assert out['ok'] is True and out['minima'][0]['value_kcal'] == -12.3
    assert out['maxima'] == []


def test_wavefn_extrema_bad_kind_error(tmp_path):
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    api = Api(multiwfn_mod=_fake_multiwfn(), config_mod=_fake_config(cfg={}))
    out = api.wavefn_extrema(str(wf), 'nci_rdg')
    assert out['ok'] is False and '极值类型' in out['error']


def test_wavefn_run_remote_experimental_happy(tmp_path):
    calls = {}
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store),
              connection_mod=_fake_connection(calls=calls, exec_ret={'code': 0}),
              multiwfn_mod=_fake_multiwfn())
    out = api.wavefn_run_remote(str(wf), ['esp_extrema'], 'c1', None, '/scratch/wfn')
    assert out['ok'] is True and out['experimental'] is True
    assert out['results'][0]['ok'] is True
    assert calls['put'][0]['remote'].endswith('mol.fchk')       # 上传波函数
    assert any('Multiwfn' in c for c in calls['exec'])          # 远端执行


def test_wavefn_run_remote_missing_file_error():
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store), connection_mod=_fake_connection(),
              multiwfn_mod=_fake_multiwfn())
    out = api.wavefn_run_remote('/nope.fchk', ['esp_extrema'], 'c1', None, '/scratch')
    assert out['ok'] is False and out['experimental'] is True and '不存在' in out['error']


def test_wavefn_run_remote_missing_remote_dir_error(tmp_path):
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key')}
    api = Api(profiles_mod=_fake_profiles(store), connection_mod=_fake_connection(),
              multiwfn_mod=_fake_multiwfn())
    out = api.wavefn_run_remote(str(wf), ['esp_extrema'], 'c1', None, '')
    assert out['ok'] is False and '远端工作目录' in out['error']


# ── tool_paths_get / tool_paths_set ──────────────────────────────────────────
def test_tool_paths_roundtrip():
    backing = {}
    api = Api(config_mod=_fake_config_rw(backing))
    out = api.tool_paths_set({'multiwfn': '/opt/Multiwfn', 'vmd': '  /opt/vmd  ',
                              'blank': ''})
    assert out['ok'] is True and out['paths']['vmd'] == '/opt/vmd'
    got = api.tool_paths_get()
    assert got['ok'] is True and got['paths']['multiwfn'] == '/opt/Multiwfn'


def test_tool_paths_set_merges_existing():
    backing = {'tool_paths': {'multiwfn': '/old/Multiwfn'}}
    api = Api(config_mod=_fake_config_rw(backing))
    api.tool_paths_set({'vmd': '/opt/vmd'})
    got = api.tool_paths_get()
    assert got['paths']['multiwfn'] == '/old/Multiwfn' and got['paths']['vmd'] == '/opt/vmd'


def test_tool_paths_get_empty_default():
    api = Api(config_mod=_fake_config(cfg={}))
    out = api.tool_paths_get()
    assert out['ok'] is True and out['paths'] == {}


# ── derive_aimd ──────────────────────────────────────────────────────────────
def _fake_aimd(*, changes=None, warnings=None, calls=None, boom=None, ok=True, error=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(src_dir, out_dir, **kw):
        calls['build'] = {'src_dir': src_dir, 'out_dir': out_dir, **kw}
        if boom:
            raise boom
        return {'ok': ok, 'job_dir': out_dir,
                'changes': list(changes if changes is not None else
                                [{'key': 'IBRION', 'action': 'replace', 'old': '2',
                                  'new': '0', 'reason': 'MD'}]),
                'warnings': list(warnings or ['Γ 点单点']), 'error': error}
    m.build_aimd_job = _build
    return m


def test_derive_aimd_derives_and_registers(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    registered, calls = [], {}
    api = Api(aimd_mod=_fake_aimd(calls=calls), ledger_mod=_fake_ledger_register(registered))
    out = api.derive_aimd(str(tmp_path), ensemble='nvt', temp_k=300, steps=10000,
                          potim_fs=1.0, temp_end_k=350, encut=350)
    assert out['ok'] is True and out['job_dir'].endswith(
        os.path.basename(str(tmp_path)) + '_aimd')
    assert out['changes'][0]['key'] == 'IBRION'
    assert registered == [out['job_dir']]
    assert calls['build']['ensemble'] == 'nvt' and calls['build']['temp_end_k'] == 350.0
    assert calls['build']['encut'] == 350.0


def test_derive_aimd_omits_optional_when_blank(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    calls = {}
    api = Api(aimd_mod=_fake_aimd(calls=calls), ledger_mod=_fake_ledger_register([]))
    api.derive_aimd(str(tmp_path), ensemble='nve', temp_k=300)
    assert 'temp_end_k' not in calls['build'] and 'encut' not in calls['build']


def test_derive_aimd_missing_dir_error():
    api = Api(aimd_mod=_fake_aimd())
    out = api.derive_aimd('/no/such/dir')
    assert out['ok'] is False and out['job_dir'] is None and '目录' in out['error']


def test_derive_aimd_engine_failure_caught(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api(aimd_mod=_fake_aimd(ok=False, error='源目录缺 INCAR'),
              ledger_mod=_fake_ledger_register([]))
    out = api.derive_aimd(str(tmp_path))
    assert out['ok'] is False and 'INCAR' in out['error']


# ══════════════════════════════════════════════════════════════════════════════
# v3.1 GUI 总装:全 DFT 任务目录 / 一键出图管线 / AI 三能力 / starpivot 对齐
# ══════════════════════════════════════════════════════════════════════════════
def _fake_task_catalog():
    m = types.SimpleNamespace()
    m.CATEGORIES = ('基础', '电子结构', '热力学与动力学', '性质', '收敛与校验')
    m.list_catalog = lambda category=None: [
        {'key': 'relax', 'name_zh': '结构优化', 'category': '基础',
         'description': '弛豫', 'requires': 'POSCAR', 'outputs': 'CONTCAR', 'figure': None},
        {'key': 'eos', 'name_zh': '状态方程', 'category': '性质',
         'description': 'BM3', 'requires': '平衡结构', 'outputs': 'E-V', 'figure': 'eos'},
    ]
    return m


def _fake_u_library():
    m = types.SimpleNamespace()
    m.suggest_u = lambda els: [{'element': e, 'u': 4.0, 'l': 2, 'orbital': 'd',
                                'source': 'MP', 'note': '敏感性测试'} for e in els if e in ('Fe', 'Co')]
    m.ldau_keys = lambda sugg, order: {'LDAU': True, 'LDAUTYPE': 2,
                                       'LDAUL': ' '.join('2' if e in {s['element'] for s in sugg} else '-1' for e in order)}
    return m


def _fake_conv_scan(*, calls=None, series=None, analyze_ret=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _series_ret(out_root):
        s = series if series is not None else [
            {'value': 400, 'label': '400 eV', 'dir': os.path.join(out_root, 'encut_400')},
            {'value': 500, 'label': '500 eV', 'dir': os.path.join(out_root, 'encut_500')}]
        return {'out_root': out_root, 'dirs': {}, 'series': s, 'results': {}, 'warnings': []}
    m.build_encut_series = lambda src, out_root, values=None: (
        calls.__setitem__('encut', {'src': src, 'values': values}) or _series_ret(out_root))
    m.build_kmesh_series = lambda src, out_root, meshes: (
        calls.__setitem__('kmesh', {'meshes': meshes}) or _series_ret(out_root))
    m.build_vacuum_series = lambda src, out_root, vacuums: (
        calls.__setitem__('vacuum', {'vacuums': vacuums}) or _series_ret(out_root))
    m.build_slab_thickness_series = lambda src, out_root, layers, **kw: (
        calls.__setitem__('thick', {'layers': layers}) or _series_ret(out_root))
    m.analyze_series = lambda dirs, **kw: (analyze_ret if analyze_ret is not None else {
        'points': [{'x': 400, 'energy': -10.0, 'converged': False},
                   {'x': 500, 'energy': -10.001, 'converged': True}],
        'converged_at': 500, 'threshold_mev': 1.0, 'natoms': 4, 'note': '收敛点 x=500'})
    m.conv_plot = lambda pts, out, **kw: (calls.__setitem__('conv_plot', out) or [out])
    return m


def _fake_bands_builder(*, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(src, out_root, *, lattice=None, npoints=40):
        calls['bands'] = {'src': src, 'out': out_root, 'lattice': lattice, 'npoints': npoints}
        return {'out_dir': out_root, 'lattice': lattice or 'fcc',
                'changes': [{'key': 'ICHARG', 'new': '11'}], 'warnings': []}
    m.build_bands_job = _build
    return m


def _fake_cell_opt(*, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(src, out_dir, *, bump_encut=False, **kw):
        calls['cellopt'] = {'src': src, 'out': out_dir, 'bump_encut': bump_encut}
        return {'out_dir': out_dir, 'changes': [{'key': 'ISIF', 'new': '3'}], 'warnings': []}
    m.build_cellopt_job = _build
    return m


def _fake_eos_mod(*, calls=None, fit=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.DEFAULT_SCALES = (0.96, 0.98, 1.0, 1.02, 1.04)

    def _build(src, out_root, scales=None):
        s = scales if scales is not None else list(m.DEFAULT_SCALES)
        calls['eos'] = {'src': src, 'scales': s}
        return {'out_root': out_root, 'dirs': {}, 'warnings': [],
                'series': [{'scale': sc, 'volume': 100 * sc,
                            'dir': os.path.join(out_root, f'eos_{sc}')} for sc in s]}
    m.build_eos_series = _build
    m.fit_birch_murnaghan = lambda vols, ens: (fit if fit is not None else {
        'v0': 100.0, 'e0': -10.5, 'b0_gpa': 200.0, 'b0_prime': 4.0, 'r2': 0.999,
        'b0_evA3': 1.25})
    m.eos_plot = lambda pts, fit, out, **kw: (calls.__setitem__('eos_plot', out) or [out])
    return m


def _fake_workfunction(*, calls=None, wf_ret=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(src, out_root, *, add_dipole='auto'):
        calls['wf'] = {'src': src, 'out': out_root, 'add_dipole': add_dipole}
        return {'out_dir': out_root, 'changes': [{'key': 'LVTOT', 'new': 'T'}],
                'warnings': [], 'dipole': True}
    m.build_workfunction_job = _build
    m.parse_locpot_planar = lambda locpot, axis='z': {'z': [0, 1, 2], 'v_planar': [1, 5, 1], 'axis': axis}
    m.work_function = lambda v, z, ef, **kw: (wf_ret if wf_ret is not None else {
        'phi': 4.5, 'vacuum_level': 0.0, 'phi_values': [4.5], 'note': '', 'warnings': []})
    m.wf_plot = lambda v, z, ef, out, **kw: (calls.__setitem__('wf_plot', out) or [out])
    return m


def _fake_surface_energy(*, area=100.0, gamma=1.23, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.area_from_poscar = lambda text: area
    m.surface_energy = lambda e_slab, n_slab, e_bpa, a, **kw: (
        calls.__setitem__('se', {'e_slab': e_slab, 'n_slab': n_slab, 'e_bpa': e_bpa, 'a': a}) or gamma)
    return m


def _fake_dimer(*, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(src, out_root, *, amplitude=None, displaced_poscar=None, **kw):
        calls['dimer'] = {'src': src, 'amplitude': amplitude, 'displaced': displaced_poscar}
        return {'out_dir': out_root, 'changes': [{'key': 'ICHAIN', 'new': '2'}],
                'warnings': [], 'modecar_method': 'random'}
    m.build_dimer_job = _build
    return m


def _fake_bands_parse(*, gap=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.parse_bands = lambda src, efermi=None: {'bands': [], 'kpath': [],
                                              'gap': gap if gap is not None else
                                              {'value': 1.2, 'direct': True, 'metal': False, 'note': ''}}
    m.band_plot = lambda data, out, **kw: (calls.__setitem__('band_plot', out) or [out])
    return m


def _fake_auto_figures(*, ret=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _run(proj, scenario, out_dir, *, journal='nature', **kw):
        calls['run'] = {'scenario': scenario, 'journal': journal,
                        'compose_panel': kw.get('compose_panel')}
        return ret if ret is not None else {
            'ok': True, 'out_dir': os.path.join(out_dir, 'figures'),
            'files': [os.path.join(out_dir, 'figures', 'adsorption_bar.png')],
            'panel': {'files': ['panel.png'], 'n': 1}, 'manifest': [{'key': 'adsorption_bar'}],
            'error': None}
    m.run_auto_figures = _run
    return m


def _fake_campaign_tpl(*, templates=None, inst_ret=None, next_ret=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.list_templates = lambda: (templates if templates is not None else {
        'sac_lis_screening': {'name_zh': 'SAC 锂硫筛选', 'description': '全链',
                              'figures_scenario': 'lis', 'n_stages': 3, 'analyses': ['delta_e']}})

    def _inst(key, spec, out_root, *, title=None, **kw):
        calls['inst'] = {'key': key, 'spec': dict(spec), 'out_root': out_root, 'title': title}
        return inst_ret if inst_ret is not None else {
            'campaign_dir': os.path.join(out_root, '.camp', key),
            'stages': {'relax': 2}, 'n_jobs': 2, 'estimate': {'total': 96.0},
            'figures_scenario': 'lis'}
    m.instantiate = _inst
    m.next_derivations = lambda cdir: list(next_ret or [])
    m.mark_derived = lambda cdir, src, derive: calls.setdefault(
        'marked', []).append((src, derive)) or {'ok': True}
    return m


def _fake_solvation(*, calls=None, build_ret=None, boom=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.SOLVENT_PRESETS = {'lis_electrolyte': [('DOL', 2), ('DME', 1)],
                         'dol_only': [('DOL', 3)]}

    def _build(core='Li2S3', solvents=(('DOL', 2),), *, box=18.0, min_sep=2.5, seed=42):
        calls['build'] = {'core': core, 'solvents': solvents, 'box': box, 'seed': seed}
        if boom:
            raise boom
        return build_ret if build_ret is not None else {
            'poscar': 'solvated\n1.0\n...\n', 'n_atoms': 42, 'note': '核 Li2S3 + 2×DOL + 1×DME'}
    m.build_solvated_complex = _build
    return m


def _fake_paper_data(*, extract_ret=None, cmp_ret=None, md_ret='## 文献对照\n', calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _extract(text, *, transport=None, **kw):
        calls['extract'] = {'text': text, 'transport': transport}
        return extract_ret if extract_ret is not None else {
            'ok': True, 'tables': [{'label': 'T1', 'kind': 'E_ads', 'columns': [],
                                    'rows': [{'system': 'Fe@N4', 'species': 'Li2S4',
                                              'value_ev': -1.5, 'page_hint': 3}]}], 'error': None}
    m.extract_data_tables = _extract
    m.build_reference_dataset = lambda tables: (
        calls.__setitem__('refset', tables) or {'entries': [
            {'system': 'Fe@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'ref_value': -1.5}]})

    def _compare(ref, computed):
        calls['compare'] = {'ref': ref, 'computed': list(computed)}
        return cmp_ret if cmp_ret is not None else {
            'pairs': [{'system': 'Fe@N4', 'species': 'Li2S4', 'ref': -1.5, 'ours': -1.4,
                       'delta': 0.1, 'abs_delta': 0.1}],
            'mae': 0.1, 'rmse': 0.1, 'n': 1, 'worst': [], 'unmatched': [],
            'summary_zh': '共比对 1 项:MAE = 0.100 eV'}
    m.compare_with_computed = _compare
    m.mae_report_md = lambda cmp: md_ret
    return m


def _fake_variant_advisor(*, suggest_ret=None, plan_ret=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _suggest(spec):
        calls['suggest'] = spec
        return suggest_ret if suggest_ret is not None else {
            'variants': [{'kind': 'metal_swap', 'from': 'Fe', 'to': 'Co', 'metal': 'Co',
                          'template': 'N4', 'parent': 'Fe@N4', 'priority': 0,
                          'rationale_zh': '同族替换'}],
            'matrix_spec': {'metals': ['Co'], 'templates': ['N4']}}
    m.suggest_variants = _suggest
    m.variant_campaign_plan = lambda variants, *, budget_cap_hours=None: (
        plan_ret if plan_ret is not None else {
            'n_jobs': len(variants), 'estimate_hours': 48.0,
            'batches': [{'batch': 1, 'priority': 0, 'jobs': ['Co@N4'], 'n_jobs': 1,
                         'estimate_hours': 48.0, 'reason': '锚定近邻'}],
            'note': f'共 {len(variants)} 个变体'})
    return m


def _fake_manuscript(*, build_ret=None, stats_ret=None, calls=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(proj, comparison=None, figures_manifest=None, *, lang='zh', fmt='markdown'):
        calls['build'] = {'fmt': fmt, 'has_comparison': comparison is not None}
        return build_ret if build_ret is not None else {
            'ok': True, 'path': '/out/manuscript_zh.md', 'md_path': '/out/manuscript_zh.md',
            'sections': ['摘要', '引言'], 'placeholders_count': 5, 'docx_path': None,
            'docx_available': False}
    m.build_manuscript = _build
    m.draft_stats = lambda path: (stats_ret if stats_ret is not None else {
        'auto': 10, 'placeholder': 5, 'total': 15, 'auto_ratio': 0.667})
    return m


# ── task_catalog / u_suggest ──────────────────────────────────────────────────
def test_task_catalog_shape():
    api = Api(task_catalog_mod=_fake_task_catalog())
    out = api.task_catalog()
    assert out['ok'] is True and '性质' in out['categories']
    keys = {t['key'] for t in out['tasks']}
    assert 'relax' in keys and 'eos' in keys
    assert out['tasks'][0]['name_zh'] == '结构优化'


def test_task_catalog_error_caught():
    boom = types.SimpleNamespace(
        CATEGORIES=(), list_catalog=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('坏表')))
    api = Api(task_catalog_mod=boom)
    out = api.task_catalog()
    assert out['ok'] is False and '坏表' in out['error']


def test_u_suggest_returns_values_missing_and_incar_keys():
    api = Api(u_library_mod=_fake_u_library())
    out = api.u_suggest(['Fe', 'Co', 'Xx'])
    assert out['ok'] is True and len(out['suggestions']) == 2
    assert out['missing'] == ['Xx']              # 库内无经验 U,不编造
    assert out['incar_keys']['LDAU'] is True
    assert out['suggestions'][0]['source'] == 'MP'


def test_u_suggest_error_caught():
    boom = types.SimpleNamespace(
        suggest_u=lambda els: (_ for _ in ()).throw(RuntimeError('U 库坏')))
    api = Api(u_library_mod=boom)
    out = api.u_suggest(['Fe'])
    assert out['ok'] is False and 'U 库坏' in out['error']


# ── derive_task 分发 ──────────────────────────────────────────────────────────
def test_derive_task_cellopt_registers(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    registered, calls = [], {}
    api = Api(cell_opt_mod=_fake_cell_opt(calls=calls),
              ledger_mod=_fake_ledger_register(registered))
    out = api.derive_task('cellopt', str(tmp_path))
    assert out['ok'] is True and out['job_dirs'][0].endswith('_cellopt')
    assert registered == out['job_dirs']
    assert calls['cellopt']['bump_encut'] is True


def test_derive_task_electronic_elf(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    calls = {}
    api = Api(estatic_mod=_fake_estatic(calls=calls), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('elf', str(tmp_path))
    assert out['ok'] is True and out['job_dirs'][0].endswith('_elf')
    assert calls['purposes'] == ['elf']


def test_derive_task_bands_passes_params(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    calls = {}
    api = Api(bands_mod=_fake_bands_builder(calls=calls), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('bands', str(tmp_path), {'lattice': 'bcc', 'npoints': 60})
    assert out['ok'] is True and out['lattice'] == 'bcc'
    assert calls['bands']['npoints'] == 60


def test_derive_task_eos_series_registers_all(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    registered, calls = [], {}
    api = Api(eos_mod=_fake_eos_mod(calls=calls), ledger_mod=_fake_ledger_register(registered))
    out = api.derive_task('eos', str(tmp_path), {'scales': [0.98, 1.0, 1.02]})
    assert out['ok'] is True and len(out['job_dirs']) == 3
    assert len(registered) == 3 and out['series'][0]['scale'] == 0.98
    assert calls['eos']['scales'] == [0.98, 1.0, 1.02]


def test_derive_task_workfunction(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api(workfunction_mod=_fake_workfunction(), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('workfunction', str(tmp_path))
    assert out['ok'] is True and out['job_dirs'][0].endswith('_wf') and out['dipole'] is True


def test_derive_task_dimer_amplitude(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    calls = {}
    api = Api(dimer_mod=_fake_dimer(calls=calls), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('dimer', str(tmp_path), {'amplitude': 0.02})
    assert out['ok'] is True and calls['dimer']['amplitude'] == 0.02


def test_derive_task_conv_encut_series(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    registered, calls = [], {}
    api = Api(conv_scan_mod=_fake_conv_scan(calls=calls),
              ledger_mod=_fake_ledger_register(registered))
    out = api.derive_task('conv_encut', str(tmp_path), {'values': [400, 500]})
    assert out['ok'] is True and len(out['job_dirs']) == 2 and len(registered) == 2
    assert calls['encut']['values'] == [400, 500]


def test_derive_task_conv_kmesh_defaults(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    calls = {}
    api = Api(conv_scan_mod=_fake_conv_scan(calls=calls), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('conv_kmesh', str(tmp_path))
    assert out['ok'] is True and calls['kmesh']['meshes'][0] == [3, 3, 1]


def test_derive_task_freq_delegates(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api(freq_builder_mod=_fake_freq(), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('freq', str(tmp_path))
    assert out['ok'] is True and out['job_dirs'][0].endswith('_freq')


def test_derive_task_aimd_delegates(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    calls = {}
    api = Api(aimd_mod=_fake_aimd(calls=calls), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('aimd', str(tmp_path), {'temp_k': 500, 'steps': 5000})
    assert out['ok'] is True and out['job_dirs'][0].endswith('_aimd')
    assert calls['build']['temp_k'] == 500.0


def test_derive_task_unsupported_key(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api()
    out = api.derive_task('adsorption_project', str(tmp_path))
    assert out['ok'] is False and '不支持' in out['error']


def test_derive_task_missing_dir():
    api = Api()
    out = api.derive_task('cellopt', '/no/such/dir')
    assert out['ok'] is False and '不存在' in out['error']


# ── analyze_task ──────────────────────────────────────────────────────────────
def _write_poscar(d, cell=10.0, natoms=2):
    (d / 'POSCAR').write_text(
        f'demo\n1.0\n{cell} 0 0\n0 {cell} 0\n0 0 {cell}\nSi\n{natoms}\nCartesian\n'
        + '0 0 0\n' * natoms, encoding='utf-8')


def test_analyze_task_conv(tmp_path):
    for x in ('encut_400', 'encut_500'):
        (tmp_path / x).mkdir()
    calls = {}
    api = Api(conv_scan_mod=_fake_conv_scan(calls=calls))
    out = api.analyze_task(str(tmp_path), kind='conv')
    assert out['ok'] is True and out['kind'] == 'conv'
    assert out['result']['converged_at'] == 500
    assert out['figure'] and out['figure'].endswith('convergence.png')


def test_analyze_task_eos_fits(tmp_path):
    for i, sc in enumerate((0.98, 1.0, 1.02)):
        sub = tmp_path / f'eos_{sc}'
        sub.mkdir()
        _write_poscar(sub, cell=10.0 + i)
        (sub / 'OSZICAR').write_text(f'1 F= x E0= -{10 + i * 0.1:.6f}E+00 dE=0\n', encoding='utf-8')
    api = Api(eos_mod=_fake_eos_mod())
    out = api.analyze_task(str(tmp_path), kind='eos')
    assert out['ok'] is True and out['result']['b0_gpa'] == 200.0
    assert 'V0' in out['summary'] and out['figure'].endswith('eos.png')


def test_analyze_task_eos_insufficient(tmp_path):
    sub = tmp_path / 'eos_1.0'
    sub.mkdir()
    _write_poscar(sub)          # 缺 OSZICAR → 能量 None
    api = Api(eos_mod=_fake_eos_mod())
    out = api.analyze_task(str(tmp_path), kind='eos')
    assert out['ok'] is False and '≥3' in out['error']


def test_analyze_task_bands_gap(tmp_path):
    (tmp_path / 'EIGENVAL').write_text('eigen', encoding='utf-8')
    (tmp_path / 'OUTCAR').write_text(' E-fermi :   3.1234  XC\n', encoding='utf-8')
    api = Api(bands_parse_mod=_fake_bands_parse())
    out = api.analyze_task(str(tmp_path), kind='bands')
    assert out['ok'] is True and out['result']['gap']['value'] == 1.2
    assert '带隙 = 1.200 eV' in out['summary'] and '直接' in out['summary']


def test_analyze_task_workfunction(tmp_path):
    (tmp_path / 'LOCPOT').write_text('locpot', encoding='utf-8')
    (tmp_path / 'OUTCAR').write_text(' E-fermi :   1.5  XC\n', encoding='utf-8')
    api = Api(workfunction_mod=_fake_workfunction())
    out = api.analyze_task(str(tmp_path), kind='workfunction')
    assert out['ok'] is True and out['result']['phi'] == 4.5
    assert 'φ = 4.500 eV' in out['summary']


def test_analyze_task_workfunction_needs_efermi(tmp_path):
    (tmp_path / 'LOCPOT').write_text('locpot', encoding='utf-8')
    api = Api(workfunction_mod=_fake_workfunction())
    out = api.analyze_task(str(tmp_path), kind='workfunction')
    assert out['ok'] is False and 'E-fermi' in out['error']


def test_analyze_task_unknown_kind(tmp_path):
    api = Api()
    out = api.analyze_task(str(tmp_path), kind='nope')
    assert out['ok'] is False and '无法识别' in out['error']


def test_analyze_task_missing_dir():
    api = Api()
    out = api.analyze_task('/no/such/dir')
    assert out['ok'] is False and '不存在' in out['error']


# ── surface_energy_calc ───────────────────────────────────────────────────────
def test_surface_energy_calc_auto_area_and_bulk(tmp_path):
    slab = tmp_path / 'slab'
    slab.mkdir()
    _write_poscar(slab, natoms=6)
    (slab / 'OSZICAR').write_text('1 F= x E0= -60.0E+00 dE=0\n', encoding='utf-8')
    bulk = tmp_path / 'bulk'
    bulk.mkdir()
    _write_poscar(bulk, natoms=2)
    (bulk / 'OSZICAR').write_text('1 F= x E0= -20.0E+00 dE=0\n', encoding='utf-8')
    calls = {}
    api = Api(surface_energy_mod=_fake_surface_energy(calls=calls))
    out = api.surface_energy_calc(str(slab), str(bulk))
    assert out['ok'] is True and out['gamma_jm2'] == 1.23
    assert out['area_a2'] == 100.0 and out['n_slab'] == 6
    assert calls['se']['e_bpa'] == -10.0    # -20/2


def test_surface_energy_calc_missing_slab():
    api = Api(surface_energy_mod=_fake_surface_energy())
    out = api.surface_energy_calc('/no/such', '/no/bulk')
    assert out['ok'] is False and 'slab' in out['error']


# ── campaign_templates / instantiate ──────────────────────────────────────────
def test_campaign_templates_shape():
    api = Api(campaign_templates_mod=_fake_campaign_tpl())
    out = api.campaign_templates()
    assert out['ok'] is True and out['templates'][0]['key'] == 'sac_lis_screening'
    assert out['templates'][0]['n_stages'] == 3


def test_campaign_instantiate_registers(tmp_path):
    calls = {}
    backing = {'ui': {}}
    api = Api(campaign_templates_mod=_fake_campaign_tpl(calls=calls),
              config_mod=_fake_config_rw(backing))
    out = api.campaign_instantiate('sac_lis_screening',
                                   {'systems': ['Fe@N4', 'Co@N4']}, str(tmp_path))
    assert out['ok'] is True and out['n_jobs'] == 2
    assert out['campaign_dir'] in (backing['ui'].get('campaign_dirs') or [])
    assert calls['inst']['spec']['systems'] == ['Fe@N4', 'Co@N4']


def test_campaign_instantiate_needs_systems(tmp_path):
    api = Api(campaign_templates_mod=_fake_campaign_tpl())
    out = api.campaign_instantiate('sac_lis_screening', {'systems': []}, str(tmp_path))
    assert out['ok'] is False and 'system' in out['error']


def test_campaign_instantiate_missing_outroot():
    api = Api(campaign_templates_mod=_fake_campaign_tpl())
    out = api.campaign_instantiate('sac_lis_screening', {'systems': ['Fe@N4']}, '')
    assert out['ok'] is False and '输出根目录' in out['error']


# ── solvent_presets / build_solvated ──────────────────────────────────────────
def test_solvent_presets_shape():
    api = Api(solvation_mod=_fake_solvation())
    out = api.solvent_presets()
    assert out['ok'] is True
    keys = {p['key'] for p in out['presets']}
    assert 'lis_electrolyte' in keys


def test_build_solvated_preset(tmp_path):
    calls = {}
    api = Api(solvation_mod=_fake_solvation(calls=calls))
    out = api.build_solvated(core='Li2S3', solvents='lis_electrolyte')
    assert out['ok'] is True and out['n_atoms'] == 42
    assert calls['build']['solvents'] == 'lis_electrolyte'


def test_build_solvated_custom_recipe_and_save(tmp_path):
    calls = {}
    dest = tmp_path / 'solv.vasp'
    api = Api(solvation_mod=_fake_solvation(calls=calls))
    out = api.build_solvated(core='S8', solvents=[['DOL', 2], ['DME', 1]], save_to=str(dest))
    assert out['ok'] is True and out['saved_to'] == str(dest)
    assert dest.read_text(encoding='utf-8').startswith('solvated')
    assert calls['build']['solvents'] == [('DOL', 2), ('DME', 1)]


def test_build_solvated_error_caught():
    api = Api(solvation_mod=_fake_solvation(boom=ValueError('盒太小')))
    out = api.build_solvated()
    assert out['ok'] is False and '盒太小' in out['error']


# ── figure_prefs ──────────────────────────────────────────────────────────────
def test_figure_prefs_get_defaults():
    api = Api(config_mod=_fake_config(ui={}))
    out = api.figure_prefs_get()
    assert out['journal_style'] == 'nature' and out['auto_figures'] is True
    assert out['multi_panel'] is True


def test_figure_prefs_save_persists():
    backing = {'ui': {}}
    api = Api(config_mod=_fake_config_rw(backing))
    out = api.figure_prefs_save(journal_style='acs', auto_figures=False, multi_panel=False)
    assert out['ok'] is True
    assert backing['ui']['journal_style'] == 'acs'
    assert backing['ui']['auto_figures'] is False


def test_figure_prefs_save_bad_journal_falls_back():
    backing = {'ui': {}}
    api = Api(config_mod=_fake_config_rw(backing))
    api.figure_prefs_save(journal_style='comic')
    assert backing['ui']['journal_style'] == 'nature'


# ── AI 三能力:数据对照 ────────────────────────────────────────────────────────
def test_ai_extract_tables_forwards(tmp_path):
    calls = {}
    api = Api(paper_data_mod=_fake_paper_data(calls=calls))
    out = api.ai_extract_tables('论文正文...E_ads')
    assert out['ok'] is True and out['tables'][0]['rows'][0]['system'] == 'Fe@N4'
    assert calls['extract']['text'].startswith('论文')


def test_ai_extract_tables_empty():
    api = Api(paper_data_mod=_fake_paper_data())
    out = api.ai_extract_tables('   ')
    assert out['ok'] is False and '论文文本' in out['error']


def test_ai_compare_project_vs_reference(tmp_path):
    proj = {'name': 'Fe@N4', 'root': str(tmp_path)}
    rows = {'rows': [{'species': 'Li2S4', 'delta_e': -1.4, 'is_most_stable': True, 'name': 'c1'}]}
    ads = _fake_adsorption(proj_map={str(tmp_path): proj}, delta_ret=rows)
    api = Api(adsorption_mod=ads, paper_data_mod=_fake_paper_data())
    tables = [{'label': 'T1', 'kind': 'E_ads',
               'rows': [{'system': 'Fe@N4', 'species': 'Li2S4', 'value_ev': -1.5}]}]
    out = api.ai_compare(str(tmp_path), tables)
    assert out['ok'] is True and out['n'] == 1 and out['mae'] == 0.1
    assert 'MAE' in out['summary']


def test_ai_compare_missing_project():
    api = Api(adsorption_mod=_fake_adsorption(proj_map={}), paper_data_mod=_fake_paper_data())
    out = api.ai_compare('/no/proj', [])
    assert out['ok'] is False and '项目' in out['error']


def test_ai_write_validation_writes_md(tmp_path):
    proj = {'name': 'Fe@N4', 'root': str(tmp_path)}
    ads = _fake_adsorption(proj_map={str(tmp_path): proj},
                           delta_ret={'rows': [{'species': 'Li2S4', 'delta_e': -1.4,
                                                 'is_most_stable': True, 'name': 'c1'}]})
    api = Api(adsorption_mod=ads, paper_data_mod=_fake_paper_data(md_ret='## 文献对照\n表格\n'))
    out = api.ai_write_validation(str(tmp_path), [{'kind': 'E_ads', 'rows': []}])
    assert out['ok'] is True and out['path'].endswith('validation.md')
    assert os.path.isfile(out['path'])
    assert '文献对照' in open(out['path'], encoding='utf-8').read()


def test_ai_write_validation_missing_project():
    api = Api(adsorption_mod=_fake_adsorption(proj_map={}), paper_data_mod=_fake_paper_data())
    out = api.ai_write_validation('/no/proj', [])
    assert out['ok'] is False and '项目' in out['error']


# ── AI 三能力:材料变体 ────────────────────────────────────────────────────────
def test_ai_variants_returns_list_and_plan():
    calls = {}
    api = Api(variant_advisor_mod=_fake_variant_advisor(calls=calls))
    out = api.ai_variants({'systems': [{'metals': [{'value': 'Fe'}]}]}, budget_cap_hours=100)
    assert out['ok'] is True and out['variants'][0]['metal'] == 'Co'
    assert out['matrix_spec']['metals'] == ['Co']
    assert out['plan']['n_jobs'] == 1


def test_ai_variants_error_caught():
    boom = types.SimpleNamespace(
        suggest_variants=lambda spec: (_ for _ in ()).throw(RuntimeError('母版坏')))
    api = Api(variant_advisor_mod=boom)
    out = api.ai_variants({})
    assert out['ok'] is False and '母版坏' in out['error']


# ── AI 三能力:论文草稿 ────────────────────────────────────────────────────────
def test_ai_manuscript_returns_stats(tmp_path):
    proj = {'name': 'Fe@N4', 'root': str(tmp_path)}
    ads = _fake_adsorption(proj_map={str(tmp_path): proj})
    calls = {}
    api = Api(adsorption_mod=ads, manuscript_draft_mod=_fake_manuscript(calls=calls))
    out = api.ai_manuscript(str(tmp_path), fmt='markdown')
    assert out['ok'] is True and out['stats']['auto'] == 10
    assert out['placeholders_count'] == 5 and out['docx_available'] is False
    assert calls['build']['fmt'] == 'markdown'


def test_ai_manuscript_missing_project():
    api = Api(adsorption_mod=_fake_adsorption(proj_map={}),
              manuscript_draft_mod=_fake_manuscript())
    out = api.ai_manuscript('/no/proj')
    assert out['ok'] is False and '项目' in out['error']


# ── starpivot:依赖状态 / 安装 ─────────────────────────────────────────────────
def test_deps_status_external_tools_reflect_probe():
    api = Api(multiwfn_mod=_fake_multiwfn(probe_avail=True),
              vmd_mod=_fake_vmd(probe_avail=False), config_mod=_fake_config(cfg={}))
    out = api.deps_status()
    assert out['ok'] is True
    by = {d['key']: d for d in out['deps']}
    assert by['multiwfn']['available'] is True and by['vmd']['available'] is False
    assert set(by) >= {'rdkit', 'decimer', 'matplotlib', 'multiwfn', 'vmd'}


def _fake_deps_runner(*, returncode=0, calls=None, write_log=True):
    calls = calls if calls is not None else {}

    class _Proc:
        def poll(self):
            return returncode

    def _run(pip_names, log_path):
        calls['pip'] = list(pip_names)
        calls['log'] = log_path
        if write_log:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write('Collecting ' + ' '.join(pip_names) + '\nSuccessfully installed\n')
        return _Proc()
    return _run


def test_deps_install_starts_via_runner():
    calls = {}
    api = Api(deps_runner=_fake_deps_runner(returncode=None, calls=calls))
    out = api.deps_install(['rdkit', 'matplotlib'])
    assert out['ok'] is True and out['started'] is True
    assert 'rdkit' in out['pip'] and 'matplotlib' in out['pip']
    assert calls['pip']


def test_deps_install_rejects_non_whitelist():
    api = Api(deps_runner=_fake_deps_runner())
    out = api.deps_install(['evil-pkg'])
    assert out['ok'] is False and out['rejected'] == ['evil-pkg']


def test_deps_install_busy_guard():
    api = Api(deps_runner=_fake_deps_runner(returncode=None))
    api.deps_install(['rdkit'])
    out = api.deps_install(['decimer'])
    assert out['ok'] is False and '进行中' in out['error']


def test_deps_install_status_none_then_done():
    api = Api()
    assert api.deps_install_status()['active'] is False
    api2 = Api(deps_runner=_fake_deps_runner(returncode=0))
    api2.deps_install(['rdkit'])
    st = api2.deps_install_status()
    assert st['active'] is True and st['done'] is True and st['returncode'] == 0
    assert 'Successfully' in st['log_tail']


def test_deps_install_status_running():
    api = Api(deps_runner=_fake_deps_runner(returncode=None))
    api.deps_install(['decimer'])
    st = api.deps_install_status()
    assert st['running'] is True and st['done'] is False


# ── starpivot:概览核时四卡 ────────────────────────────────────────────────────
def test_overview_stats_aggregates():
    import datetime
    now = datetime.datetime.now().isoformat()
    entries = [('/a', {'state': 'RUNNING', 'created_at': now}),
               ('/b', {'state': 'QUEUED', 'created_at': now}),
               ('/c', {'state': 'DONE', 'created_at': now})]
    cdir = '/camp/x'
    camp = {cdir: {'_summary': {'total': 4, 'completed': 2},
                   '_budget': {'estimates': {'t1': 50.0, 't2': 50.0}}, 'meta': {}}}
    camp[cdir]['meta'] = {'title': 'X', 'budget_core_hours': 200.0}
    api = Api(ledger_mod=_fake_ledger(entries, []),
              campaign_mods=_fake_campaign(campaigns=camp),
              config_mod=_fake_config(ui={'campaign_dirs': [cdir]}))
    out = api.overview_stats()
    assert out['ok'] is True and out['jobs_30d'] == 3
    assert out['monitor']['running'] == 1 and out['monitor']['queued'] == 1
    assert out['monitor']['status'] == '运行中'
    assert out['budget_cap'] == 200.0 and out['remaining_core_hours'] is not None


def test_overview_stats_error_caught():
    boom = types.SimpleNamespace(load_all=lambda: (_ for _ in ()).throw(RuntimeError('台账坏')))
    api = Api(ledger_mod=boom)
    out = api.overview_stats()
    assert out['ok'] is False and '台账坏' in out['error']


# ── starpivot:波函数分组菜单 + 补充分析(v3.2.2 单一事实源=引擎注册表) ─────────
def _fake_multiwfn_full():
    """波函数分组菜单测试用:ANALYSES 覆盖引擎全量注册表(含 v3.2.2 迁入的四补充项)。"""
    m = _fake_multiwfn()
    m.ANALYSES = dict(m.ANALYSES)
    for k, name in (('density_cube', '电子密度 cube'), ('esp_cube', '静电势 cube'),
                    ('nci_rdg', 'NCI/RDG'), ('igmh', 'IGMH'), ('iri', 'IRI'),
                    ('aim_cp', 'AIM 临界点'), ('alie', 'ALIE cube'),
                    ('alie_extrema', 'ALIE 极值'),
                    ('elf_lol_section', 'ELF/LOL 截面'), ('adch_charge', 'ADCH 电荷'),
                    ('property_summary', '性质汇总'), ('fukui_cdft', 'Fukui/CDFT')):
        m.ANALYSES[k] = {'name': name, 'stdin_script': lambda p: '', 'outputs': (), 'note': ''}
    return m


def test_wavefn_analyses_grouped_all_engine():
    api = Api(multiwfn_mod=_fake_multiwfn_full())
    out = api.wavefn_analyses()
    assert out['ok'] is True
    gnames = {g['group'] for g in out['groups']}
    assert '常用' in gnames and '弱相互作用' in gnames
    allkeys = {it['key']: it for g in out['groups'] for it in g['items']}
    assert 'esp_extrema' in allkeys and allkeys['esp_extrema']['source'] == 'engine'
    # v3.2.2:四补充项由引擎注册表提供,source 统一为 engine(api 不再自带脚本)
    assert 'elf_lol_section' in allkeys and allkeys['elf_lol_section']['source'] == 'engine'
    assert 'fukui_cdft' in allkeys and 'adch_charge' in allkeys
    assert all(it['source'] == 'engine' for it in allkeys.values())


def test_wavefn_analyses_old_engine_omits_missing_items():
    # 旧引擎(注册表无四补充项)→ 菜单诚实少这四项,不虚列点不动的卡
    api = Api(multiwfn_mod=_fake_multiwfn())
    out = api.wavefn_analyses()
    assert out['ok'] is True
    allkeys = {it['key'] for g in out['groups'] for it in g['items']}
    assert 'esp_extrema' in allkeys
    assert 'elf_lol_section' not in allkeys and 'fukui_cdft' not in allkeys


def test_wavefn_analyses_error_caught():
    # multiwfn 假件无 ANALYSES 属性 → AttributeError 被 try/except 兜住
    api = Api(multiwfn_mod=types.SimpleNamespace())
    out = api.wavefn_analyses()
    assert out['ok'] is False and out['error']


def test_wavefn_run_extra_old_engine_honest_error(tmp_path):
    # 旧引擎注册表无四项 → 按项「引擎待扩展」中文说明(前端 engineMissing 归因依赖此措辞)
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    api = Api(multiwfn_mod=_fake_multiwfn(), config_mod=_fake_config(cfg={}))
    out = api.wavefn_run_extra(str(wf), ['fukui_cdft', 'adch_charge'])
    assert out['ok'] is False and len(out['results']) == 2
    assert all('引擎待扩展' in r['error'] for r in out['results'])


def test_wavefn_run_extra_runs_via_engine_registry(tmp_path):
    # v3.2.2:四项在引擎注册表 → wavefn_run_extra 直接走引擎 run()(与 wavefn_run 同路径)
    wf = tmp_path / 'mol.fchk'
    wf.write_text('x', encoding='utf-8')
    calls = {}
    mw = _fake_multiwfn(calls=calls,
                        run_ret={'ok': True, 'outputs': ['fukui_cdft_f_plus.cub'],
                                 'stdout_tail': 'done', 'elapsed_s': 2.0, 'error': ''})
    mw.ANALYSES = dict(mw.ANALYSES)
    mw.ANALYSES['fukui_cdft'] = {'name': 'Fukui/CDFT', 'stdin_script': lambda p: '22\n',
                                 'outputs': ('f_plus.cub',), 'note': ''}
    api = Api(multiwfn_mod=mw, config_mod=_fake_config(cfg={}))
    out = api.wavefn_run_extra(str(wf), ['fukui_cdft'], {'which': 'f+'})
    assert out['ok'] is True and out['results'][0]['ok'] is True
    assert 'fukui_cdft_f_plus.cub' in out['results'][0]['outputs']
    assert calls['runs'][0]['key'] == 'fukui_cdft'        # 确实经引擎 run() 执行


def test_wavefn_run_extra_missing_file():
    api = Api(multiwfn_mod=_fake_multiwfn(), config_mod=_fake_config(cfg={}))
    out = api.wavefn_run_extra('', ['fukui_cdft'])
    assert out['ok'] is False and '波函数文件' in out['error']


# ── starpivot:NCI/IRI 散点 ────────────────────────────────────────────────────
def _write_cube(path, values):
    """最小 cube:2 原子头 + 体数据。"""
    n = len(values)
    lines = ['comment', 'comment', '2 0 0 0', f'{n} 0.1 0 0', '1 0 0.1 0', '1 0 0 0.1',
             '1 0 0 0 0', '6 0 0.5 0.5 0.5']
    lines += [' '.join(f'{v:.5f}' for v in values[i:i + 6]) for i in range(0, n, 6)]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def test_wavefn_scatter_parses_and_filters(tmp_path):
    f1 = tmp_path / 'func1.cub'   # sign(λ2)ρ
    f2 = tmp_path / 'func2.cub'   # RDG
    _write_cube(f1, [0.01, -0.02, 0.5, 0.03])       # 0.5 超窗口(|x|>0.05)被滤
    _write_cube(f2, [0.5, 0.8, 0.3, 1.5])
    api = Api()
    out = api.wavefn_scatter(str(f1), str(f2), kind='nci')
    assert out['ok'] is True and out['kind'] == 'nci'
    assert out['n'] == 3          # 第三点 x=0.5 被滤
    assert {'x', 'y'} <= set(out['points'][0])


def test_wavefn_scatter_missing_file(tmp_path):
    f1 = tmp_path / 'func1.cub'
    _write_cube(f1, [0.01])
    api = Api()
    out = api.wavefn_scatter(str(f1), '/no/func2.cub')
    assert out['ok'] is False and 'func2' in out['error']


def test_wavefn_scatter_png_via_native_charts(tmp_path):
    f1 = tmp_path / 'func1.cub'
    f2 = tmp_path / 'func2.cub'
    _write_cube(f1, [0.01, -0.02])
    _write_cube(f2, [0.5, 0.8])
    calls = {}
    nc = types.SimpleNamespace(
        scaling_relation=lambda xs, ys, out, **kw: (calls.__setitem__('scatter', out) or [out]))
    dest = tmp_path / 'nci.png'
    api = Api(native_charts_mod=nc)
    out = api.wavefn_scatter(str(f1), str(f2), out_png=str(dest))
    assert out['ok'] is True and out['png'] == str(dest)
    assert calls['scatter'] == str(dest)


# ── starpivot:远程渲染(离线错误路径) ─────────────────────────────────────────
def test_wavefn_render_remote_missing_remote_dir(tmp_path):
    api = Api(vmd_mod=_fake_vmd())
    out = api.wavefn_render_remote('orbital', {'cube': str(tmp_path / 'x.cub')},
                                   'c1', None, '')
    assert out['ok'] is False and out['experimental'] is True and '远端工作目录' in out['error']


def test_wavefn_render_remote_unknown_scene(tmp_path):
    api = Api(vmd_mod=_fake_vmd())
    out = api.wavefn_render_remote('nope', {}, 'c1', None, '/remote/run')
    assert out['ok'] is False and '未知场景' in out['error']


# ── 一键出图管线接线:auto_figures + campaign 推进 ─────────────────────────────
def test_auto_figures_for_project_uses_auto_engine(tmp_path):
    calls = {}
    api = Api(config_mod=_fake_config(ui={'auto_figures': True, 'journal_style': 'acs',
                                          'multi_panel': True}),
              auto_figures_mod=_fake_auto_figures(calls=calls),
              adsorption_mod=_fake_adsorption())
    out = api._auto_figures_for_project({'name': 'p', 'root': str(tmp_path)},
                                        str(tmp_path / 'project.yaml'), str(tmp_path))
    assert out['engine'] == 'auto_figures' and len(out['files']) == 1
    assert calls['run']['journal'] == 'acs' and calls['run']['scenario'] == 'general'


def test_auto_figures_for_project_falls_back_when_disabled(tmp_path):
    calls = {}
    af = _fake_auto_figures(calls=calls)
    api = Api(config_mod=_fake_config(ui={'auto_figures': False}),
              auto_figures_mod=af, adsorption_mod=_fake_adsorption(proj_map={}),
              native_charts_mod=_fake_ncharts({}))
    out = api._auto_figures_for_project({'name': 'p', 'root': str(tmp_path)},
                                        str(tmp_path / 'project.yaml'), str(tmp_path))
    assert out['engine'] == 'proj_figures' and 'run' not in calls   # 未调 auto_figures


def test_tick_campaigns_derives_and_marks(tmp_path):
    src = tmp_path / 'Fe__clean__relax'
    src.mkdir()
    (src / 'CONTCAR').write_text('x', encoding='utf-8')
    cdir = str(tmp_path / '.camp' / 'c1')
    ct = _fake_campaign_tpl(next_ret=[{'src_id': 'Fe__clean__relax', 'src_dir': str(src),
                                       'derive': 'estatic', 'kinds': ['pdos', 'bader'],
                                       'task_id': 'Fe__clean__estatic'}])
    calls = {}
    ct.mark_derived = lambda cd, s, d: calls.setdefault('marked', []).append((s, d))
    api = Api(campaign_templates_mod=ct, estatic_mod=_fake_estatic(),
              ledger_mod=_fake_ledger_register([]),
              config_mod=_fake_config(ui={'campaign_dirs': [cdir]}))
    events, errors = [], []
    api._tick_campaigns(events, errors)
    assert any(e['kind'] == 'derive' for e in events)
    assert calls['marked'] == [('Fe__clean__relax', 'estatic')]


# ── save_text / gen_run 自定义关键词 / build_solvated 临时文件 ─────────────────
def test_save_text_writes(tmp_path):
    dest = tmp_path / 'preview.txt'
    api = Api()
    out = api.save_text(str(dest), 'INCAR\nENCUT = 500\n')
    assert out['ok'] is True and out['path'] == str(dest)
    assert dest.read_text(encoding='utf-8').startswith('INCAR')


def test_save_text_missing_path():
    api = Api()
    out = api.save_text('', 'x')
    assert out['ok'] is False and '保存路径' in out['error']


def test_gen_run_appends_extra_keywords(tmp_path):
    (tmp_path / 'INCAR').write_text('ENCUT = 500\n', encoding='utf-8')
    payload = {'ok': True, 'out_dir': str(tmp_path), 'warnings': [], 'kpoints': [3, 3, 1],
               'elements': ['Fe']}
    jb = types.SimpleNamespace(build_job_dir=lambda p, i, o, **k: dict(payload))
    api = Api(config_mod=_fake_config(), logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb, manifest_mod=_fake_manifest({}),
              ledger_mod=_fake_ledger_register([]))
    out = api.gen_run('/p/POSCAR', '/p/INCAR', str(tmp_path), '/lib', 'slab',
                      'LREAL = Auto\nNCORE = 4')
    assert out['ok'] is True
    incar = (tmp_path / 'INCAR').read_text(encoding='utf-8')
    assert 'LREAL = Auto' in incar and 'NCORE = 4' in incar
    assert any('自定义关键词' in w for w in out['warnings'])


def test_build_solvated_returns_temp_path(tmp_path):
    api = Api(solvation_mod=_fake_solvation())
    out = api.build_solvated(core='Li2S3', solvents='lis_electrolyte')
    assert out['ok'] is True and out['temp_path'] and os.path.isfile(out['temp_path'])
    assert out['poscar'] in open(out['temp_path'], encoding='utf-8').read()


# ══════════════════════════════════════════════════════════════════════════════
# QA 接线修复:NEB / 形成能·结合能 / 差分电荷 / VASPsol / conv_thickness 诚实化 / 徽标
# ══════════════════════════════════════════════════════════════════════════════
def _osz(e0):
    return f'   1 F= -.1E+02 E0= {e0:.6f}  d E =0.0\n'


def _vasp5_poscar(species, counts):
    head = f'demo\n1.0\n12 0 0\n0 12 0\n0 0 15\n{" ".join(species)}\n{" ".join(str(c) for c in counts)}\nDirect\n'
    return head + '0.0 0.0 0.0\n' * sum(counts)


def _fake_neb_builder(*, calls=None, boom=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.DEFAULT_N_IMAGES = 5

    def _build(out_dir, ini, fin, incar, *, n_images=5, potcar_fn=None, **kw):
        calls['build'] = {'out': out_dir, 'n_images': n_images, 'potcar_fn': potcar_fn,
                          'ini': ini, 'fin': fin, 'incar': incar}
        if boom:
            raise boom
        return {'job_dir': out_dir, 'n_images': n_images, 'warnings': ['端点须已弛豫']}
    m.build_neb_dir = _build
    return m


def _fake_references(*, calls=None, cohesive=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()
    m.binding_energy = lambda e_sac, e_sub, e_atom: (
        calls.__setitem__('be', (e_sac, e_sub, e_atom)) or (e_sac - e_sub - e_atom))

    def _form(e_sac, e_ref, chem_pots, counts):
        calls['form_counts'] = dict(counts)
        total = e_sac - e_ref
        for sp, n in counts.items():
            if sp not in chem_pots:
                raise ValueError(f'缺物种 {sp!r} 的化学势 μ,无法算形成能')
            total -= n * chem_pots[sp]
        return total
    m.formation_energy = _form

    def _coh(metal):
        if cohesive is not None and metal in cohesive:
            return cohesive[metal]
        raise ValueError(f'内置内聚能表无 {metal!r}')
    m.cohesive_energy = _coh
    m.stability_verdict = lambda eb, ecoh: {
        'sigma': round(-eb / ecoh, 4), 'stable': (-eb / ecoh) > 1.0, 'note': f'σ={-eb/ecoh:.2f} 判定'}
    return m


def _fake_chgdiff(*, calls=None, compute_ret=None, profile=None, compute_boom=None):
    calls = calls if calls is not None else {}
    m = types.SimpleNamespace()

    def _build(relax_dir, out_root, ads_idx):
        calls['build'] = {'relax': relax_dir, 'ads': list(ads_idx)}
        dirs = {'_AB': os.path.join(out_root, '_AB'), '_A': os.path.join(out_root, '_A'),
                '_B': os.path.join(out_root, '_B')}
        results = {tag: {'out_dir': d, 'changes': [], 'warnings': [f'{tag} 警告']}
                   for tag, d in dirs.items()}
        return {'out_root': out_root, 'dirs': dirs, 'results': results}
    m.build_chgdiff_jobs = _build

    def _compute(ab, a, b, out_path):
        calls['compute'] = {'ab': ab, 'a': a, 'b': b, 'out': str(out_path)}
        if compute_boom:
            raise compute_boom
        return compute_ret if compute_ret is not None else {
            'out': str(out_path), 'max': 8.0, 'min': -3.0, 'n_grid': 8}
    m.compute_chgdiff = _compute

    def _plane(path, axis='z'):
        if profile == 'boom':
            raise ValueError('面平均失败')
        return profile if profile is not None else {'z': [0.0, 1.0], 'rho': [0.1, -0.2], 'axis': axis}
    m.plane_averaged = _plane
    return m


def _fake_incar_builder():
    m = types.SimpleNamespace()
    m.VASPSOL_ADVISORY = 'VASPsol 需补丁编译;标准 VASP 静默给真空结果。'
    m.vaspsol_keys = lambda enabled=True, *, eb_k=78.4: (
        {'LSOL': True, 'EB_K': float(eb_k)} if enabled else {'LSOL': False})
    return m


# ── P0-1 derive_neb ───────────────────────────────────────────────────────────
def _mk_neb_dirs(tmp_path, *, with_incar=True, with_potcar=False):
    s = tmp_path / 'start'
    e = tmp_path / 'end'
    s.mkdir()
    e.mkdir()
    (s / 'CONTCAR').write_text(_vasp5_poscar(['H'], [2]), encoding='utf-8')
    (e / 'CONTCAR').write_text(_vasp5_poscar(['H'], [2]), encoding='utf-8')
    if with_incar:
        (s / 'INCAR').write_text('ENCUT = 400\n', encoding='utf-8')
    if with_potcar:
        (s / 'POTCAR').write_text('H POTCAR\n', encoding='utf-8')
    return str(s), str(e)


def test_derive_neb_happy_registers(tmp_path):
    s, e = _mk_neb_dirs(tmp_path, with_potcar=True)
    registered, calls = [], {}
    api = Api(neb_builder_mod=_fake_neb_builder(calls=calls),
              ledger_mod=_fake_ledger_register(registered))
    out = api.derive_neb(s, e, 3)
    assert out['ok'] is True and out['n_images'] == 3
    assert out['job_dir'].endswith('_neb') and registered == [out['job_dir']]
    assert calls['build']['n_images'] == 3
    assert calls['build']['potcar_fn'] == os.path.join(s, 'POTCAR')   # 有 POTCAR 则透传路径
    assert 'ENCUT = 400' in calls['build']['incar']                   # 始态 INCAR 透传


def test_derive_neb_missing_start_dir():
    api = Api(neb_builder_mod=_fake_neb_builder())
    out = api.derive_neb('/no/such', '/also/no')
    assert out['ok'] is False and '始态' in out['error']


def test_derive_neb_missing_end_dir(tmp_path):
    s = tmp_path / 'start'
    s.mkdir()
    (s / 'CONTCAR').write_text(_vasp5_poscar(['H'], [2]), encoding='utf-8')
    api = Api(neb_builder_mod=_fake_neb_builder())
    out = api.derive_neb(str(s), '/no/end')
    assert out['ok'] is False and '末态' in out['error']


def test_derive_neb_missing_struct(tmp_path):
    s = tmp_path / 'start'
    e = tmp_path / 'end'
    s.mkdir()
    e.mkdir()
    (e / 'CONTCAR').write_text(_vasp5_poscar(['H'], [2]), encoding='utf-8')
    api = Api(neb_builder_mod=_fake_neb_builder())
    out = api.derive_neb(str(s), str(e))
    assert out['ok'] is False and 'CONTCAR/POSCAR' in out['error']


def test_derive_neb_missing_incar_honest_error(tmp_path):
    s, e = _mk_neb_dirs(tmp_path, with_incar=False)
    api = Api(neb_builder_mod=_fake_neb_builder())
    out = api.derive_neb(s, e, 5)
    assert out['ok'] is False and 'INCAR' in out['error']       # 诚实报错,绝不假成功


def test_derive_neb_engine_valueerror_caught(tmp_path):
    s, e = _mk_neb_dirs(tmp_path)
    api = Api(neb_builder_mod=_fake_neb_builder(boom=ValueError('初末态原子组成不一致')),
              ledger_mod=_fake_ledger_register([]))
    out = api.derive_neb(s, e, 4)
    assert out['ok'] is False and '组成不一致' in out['error']


def test_derive_neb_real_engine_builds_tree(tmp_path):
    # 真引擎端到端:确证 build_neb_dir 真落 00/01../N+1 目录树(不是假成功)
    ini = ('H2\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n2\nDirect\n0.10 0.5 0.5\n0.30 0.5 0.5\n')
    fin = ('H2\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n2\nDirect\n0.10 0.5 0.5\n0.55 0.5 0.5\n')
    s = tmp_path / 's'
    e = tmp_path / 'e'
    s.mkdir()
    e.mkdir()
    (s / 'CONTCAR').write_text(ini, encoding='utf-8')
    (e / 'CONTCAR').write_text(fin, encoding='utf-8')
    (s / 'INCAR').write_text('ENCUT = 400\nISMEAR = 0\n', encoding='utf-8')
    api = Api(ledger_mod=_fake_ledger_register([]))          # 真 neb_builder
    out = api.derive_neb(str(s), str(e), nimages=3)          # 前端经桥传字符串路径
    assert out['ok'] is True and out['n_images'] == 3
    imgs = sorted(d for d in os.listdir(out['job_dir']) if d.isdigit())
    assert imgs == ['00', '01', '02', '03', '04']            # 00 初 + 3 中间 + 末
    assert 'IMAGES = 3' in (tmp_path / 's_neb' / 'INCAR').read_text(encoding='utf-8')


# ── P0-2 formation_binding_calc ───────────────────────────────────────────────
def _mk_fb_dirs(tmp_path, *, e_sac=-300.0, e_sub=-295.0, with_sub=True):
    sac = tmp_path / 'sac'
    sac.mkdir()
    (sac / 'CONTCAR').write_text(_vasp5_poscar(['Fe', 'N', 'C'], [1, 4, 22]), encoding='utf-8')
    (sac / 'OSZICAR').write_text(_osz(e_sac), encoding='utf-8')
    sub = None
    if with_sub:
        sub = tmp_path / 'sub'
        sub.mkdir()
        (sub / 'CONTCAR').write_text(_vasp5_poscar(['N', 'C'], [4, 22]), encoding='utf-8')
        (sub / 'OSZICAR').write_text(_osz(e_sub), encoding='utf-8')
    return str(sac), (str(sub) if sub else None)


def test_formation_binding_eb_and_sigma(tmp_path):
    sac, sub = _mk_fb_dirs(tmp_path)
    calls = {}
    api = Api(references_mod=_fake_references(calls=calls, cohesive={'Fe': 4.28}))
    out = api.formation_binding_calc(sac, sub, {'Fe': -3.0})
    assert out['ok'] is True and out['metal'] == 'Fe'
    assert out['binding_energy'] == -2.0                     # -300 -(-295) -(-3)
    assert out['sigma'] == round(2.0 / 4.28, 4) and out['stable'] is False
    assert calls['be'] == (-300.0, -295.0, -3.0)             # 真调 references.binding_energy


def test_formation_binding_ef_with_chem_pots(tmp_path):
    sac, sub = _mk_fb_dirs(tmp_path)
    api = Api(references_mod=_fake_references(cohesive={'Fe': 4.28}))
    out = api.formation_binding_calc(sac, sub, {'Fe': -3.0}, {'Fe': -4.0})
    assert out['formation_energy'] == -1.0                   # -300 -(-295) -(1*-4.0)
    assert out['counts'] == {'Fe': 1}


def test_formation_binding_missing_substrate_hints(tmp_path):
    sac, _ = _mk_fb_dirs(tmp_path, with_sub=False)
    api = Api(references_mod=_fake_references())
    out = api.formation_binding_calc(sac, None, {'Fe': -3.0})
    assert out['ok'] is False and out['binding_energy'] is None
    assert any('基底' in h for h in out['hints'])


def test_formation_binding_missing_atom_energies_hint(tmp_path):
    sac, sub = _mk_fb_dirs(tmp_path)
    api = Api(references_mod=_fake_references())
    out = api.formation_binding_calc(sac, sub, None)
    assert out['binding_energy'] is None
    assert any('金属原子能量' in h for h in out['hints'])


def test_formation_binding_metal_not_in_cohesive_table(tmp_path):
    sac, sub = _mk_fb_dirs(tmp_path)
    api = Api(references_mod=_fake_references(cohesive={}))    # 内聚能表空 → σ 缺
    out = api.formation_binding_calc(sac, sub, {'Fe': -3.0})
    assert out['binding_energy'] == -2.0 and out['sigma'] is None
    assert any('内聚能' in h for h in out['hints'])


def test_formation_binding_ef_missing_mu_caught(tmp_path):
    sac, sub = _mk_fb_dirs(tmp_path)
    api = Api(references_mod=_fake_references(cohesive={'Fe': 4.28}))
    out = api.formation_binding_calc(sac, sub, {'Fe': -3.0}, {'Ni': -4.0})  # 缺 Fe 的 μ
    assert out['formation_energy'] is None
    assert any('形成能' in h for h in out['hints'])


def test_formation_binding_missing_sac_energy(tmp_path):
    sac = tmp_path / 'sac'
    sac.mkdir()
    (sac / 'CONTCAR').write_text(_vasp5_poscar(['Fe'], [1]), encoding='utf-8')  # 无 OSZICAR
    api = Api(references_mod=_fake_references())
    out = api.formation_binding_calc(str(sac))
    assert out['ok'] is False and 'E_sac' in out['error']


def test_formation_binding_missing_dir():
    api = Api(references_mod=_fake_references())
    out = api.formation_binding_calc('/no/sac')
    assert out['ok'] is False and '不存在' in out['error']


def test_formation_binding_real_references(tmp_path):
    # 真引擎:内置 Fe 内聚能 4.28,验证 σ 真算
    sac, sub = _mk_fb_dirs(tmp_path, e_sac=-300.0, e_sub=-295.0)
    api = Api()                                              # 真 references
    out = api.formation_binding_calc(sac, sub, {'Fe': -3.0})
    assert out['binding_energy'] == -2.0
    assert out['sigma'] == round(2.0 / 4.28, 4) and out['stable'] is False


# ── P0-4 差分电荷:derive_task 分派 + compute_chgdiff ─────────────────────────────
def test_derive_task_chgdiff_dispatches_three_statics(tmp_path):
    (tmp_path / 'CONTCAR').write_text(_vasp5_poscar(['Cu', 'O'], [4, 1]), encoding='utf-8')
    registered, calls, est_calls = [], {}, {}
    api = Api(chgdiff_mod=_fake_chgdiff(calls=calls),
              estatic_mod=_fake_estatic(calls=est_calls),
              ledger_mod=_fake_ledger_register(registered))
    out = api.derive_task('chgdiff', str(tmp_path), {'adsorbate_indices': [5]})
    assert out['ok'] is True and len(out['job_dirs']) == 3   # AB/A/B 三作业
    assert len(registered) == 3
    assert calls['build']['ads'] == [5]
    assert 'purposes' not in est_calls                       # 不再误接单静态 build_static_job


def test_derive_task_chgdiff_requires_indices(tmp_path):
    (tmp_path / 'CONTCAR').write_text(_vasp5_poscar(['Cu', 'O'], [4, 1]), encoding='utf-8')
    api = Api(chgdiff_mod=_fake_chgdiff())
    out = api.derive_task('chgdiff', str(tmp_path), {})
    assert out['ok'] is False and '序号' in out['error'] and out['job_dirs'] == []


def test_chgdiff_not_in_electronic_map():
    # 回归守卫:chgdiff 不再在电子学单静态表(名副其实,不误接 build_static_job)
    assert 'chgdiff' not in Api._DERIVE_ELECTRONIC


def test_compute_chgdiff_happy_with_profile(tmp_path):
    dirs = {}
    for tag in ('AB', 'A', 'B'):
        d = tmp_path / tag
        d.mkdir()
        (d / 'CHGCAR').write_text('grid', encoding='utf-8')
        dirs[tag] = str(d)
    calls = {}
    api = Api(chgdiff_mod=_fake_chgdiff(calls=calls))
    out = api.compute_chgdiff(dirs['AB'], dirs['A'], dirs['B'], str(tmp_path / 'o'))
    assert out['ok'] is True and out['max'] == 8.0 and out['n_grid'] == 8
    assert out['out'].endswith('CHGDIFF.vasp')
    assert out['profile']['z'] == [0.0, 1.0]                 # 面平均 charge_profile
    assert os.path.basename(calls['compute']['ab']) == 'CHGCAR'


def test_compute_chgdiff_missing_chgcar(tmp_path):
    ab = tmp_path / 'AB'
    ab.mkdir()                              # 无 CHGCAR
    a = tmp_path / 'A'
    a.mkdir()
    (a / 'CHGCAR').write_text('x', encoding='utf-8')
    b = tmp_path / 'B'
    b.mkdir()
    (b / 'CHGCAR').write_text('x', encoding='utf-8')
    api = Api(chgdiff_mod=_fake_chgdiff())
    out = api.compute_chgdiff(str(ab), str(a), str(b))
    assert out['ok'] is False and 'CHGCAR' in out['error']


def test_compute_chgdiff_missing_dir(tmp_path):
    api = Api(chgdiff_mod=_fake_chgdiff())
    out = api.compute_chgdiff('/no/ab', '/no/a', '/no/b')
    assert out['ok'] is False and '不存在' in out['error']


def test_compute_chgdiff_engine_error_caught(tmp_path):
    dirs = {}
    for tag in ('AB', 'A', 'B'):
        d = tmp_path / tag
        d.mkdir()
        (d / 'CHGCAR').write_text('x', encoding='utf-8')
        dirs[tag] = str(d)
    api = Api(chgdiff_mod=_fake_chgdiff(compute_boom=ValueError('AB 与 A 网格不一致')))
    out = api.compute_chgdiff(dirs['AB'], dirs['A'], dirs['B'])
    assert out['ok'] is False and '网格不一致' in out['error']


def test_compute_chgdiff_profile_failure_does_not_block(tmp_path):
    dirs = {}
    for tag in ('AB', 'A', 'B'):
        d = tmp_path / tag
        d.mkdir()
        (d / 'CHGCAR').write_text('x', encoding='utf-8')
        dirs[tag] = str(d)
    api = Api(chgdiff_mod=_fake_chgdiff(profile='boom'))
    out = api.compute_chgdiff(dirs['AB'], dirs['A'], dirs['B'])
    assert out['ok'] is True and out['profile'] == {}        # 面平均失败不挡主产物


# ── P0-3 VASPsol ───────────────────────────────────────────────────────────────
def test_vaspsol_preview_water_default():
    api = Api(incar_builder_mod=_fake_incar_builder())
    out = api.vaspsol_preview(78.4)
    assert out['ok'] is True and out['keys'] == {'LSOL': True, 'EB_K': 78.4}
    assert out['incar_lines'] == ['LSOL = .TRUE.', 'EB_K = 78.4']
    assert 'VASPsol' in out['warning']


def test_vaspsol_preview_custom_dielectric():
    api = Api(incar_builder_mod=_fake_incar_builder())
    out = api.vaspsol_preview(37.5)
    assert out['incar_lines'] == ['LSOL = .TRUE.', 'EB_K = 37.5']


def test_vaspsol_preview_disabled():
    api = Api(incar_builder_mod=_fake_incar_builder())
    out = api.vaspsol_preview(78.4, False)
    assert out['keys'] == {'LSOL': False} and out['incar_lines'] == ['LSOL = .FALSE.']


def test_vaspsol_preview_real_engine():
    api = Api()                                             # 真 incar_builder
    out = api.vaspsol_preview(78.4)
    assert out['ok'] is True and out['keys']['LSOL'] is True
    assert 'LSOL = .TRUE.' in out['incar_lines'] and '补丁' in out['warning']


def test_gen_run_solvation_appends_keys_and_advisory(tmp_path):
    (tmp_path / 'INCAR').write_text('ENCUT = 500\n', encoding='utf-8')
    payload = {'ok': True, 'out_dir': str(tmp_path), 'warnings': [], 'kpoints': [3, 3, 1],
               'elements': ['Fe']}
    jb = types.SimpleNamespace(build_job_dir=lambda p, i, o, **k: dict(payload))
    api = Api(config_mod=_fake_config(), logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb, manifest_mod=_fake_manifest({}),
              ledger_mod=_fake_ledger_register([]),
              incar_builder_mod=_fake_incar_builder())
    out = api.gen_run('/p/POSCAR', '/p/INCAR', str(tmp_path), '/lib', 'slab', None,
                      {'enabled': True, 'eb_k': 37.5})
    assert out['ok'] is True
    incar = (tmp_path / 'INCAR').read_text(encoding='utf-8')
    assert 'LSOL = .TRUE.' in incar and 'EB_K = 37.5' in incar
    assert any('VASPsol' in w for w in out['warnings'])
    assert any('静默' in w for w in out['warnings'])         # 补丁编译 advisory 一并显示


def test_gen_run_solvation_off_is_backward_compatible(tmp_path):
    (tmp_path / 'INCAR').write_text('ENCUT = 500\n', encoding='utf-8')
    payload = {'ok': True, 'out_dir': str(tmp_path), 'warnings': [], 'kpoints': [3, 3, 1],
               'elements': ['Fe']}
    jb = types.SimpleNamespace(build_job_dir=lambda p, i, o, **k: dict(payload))
    api = Api(config_mod=_fake_config(), logic_mod=_fake_logic(errs=[]),
              job_builder_mod=jb, manifest_mod=_fake_manifest({}),
              ledger_mod=_fake_ledger_register([]),
              incar_builder_mod=_fake_incar_builder())
    out = api.gen_run('/p/POSCAR', '/p/INCAR', str(tmp_path), '/lib')  # 无 solvation 参数
    assert out['ok'] is True
    assert 'LSOL' not in (tmp_path / 'INCAR').read_text(encoding='utf-8')


# ── P1-1 conv_thickness 诚实化 ─────────────────────────────────────────────────
def _fake_conv_scan_thickness_note():
    m = _fake_conv_scan()
    m.build_slab_thickness_series = lambda src, out_root, layers, **kw: {
        'out_root': out_root, 'dirs': {}, 'series': [], 'results': {}, 'warnings': [],
        'note': '层厚收敛需从建 slab 流程发起(裸 CONTCAR 无米勒面信息)'}
    return m


def test_derive_task_conv_thickness_honest_note(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api(conv_scan_mod=_fake_conv_scan_thickness_note(),
              ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('conv_thickness', str(tmp_path), {'layers': [3, 4, 5]})
    assert out['ok'] is True and out['job_dirs'] == []       # 0 作业但不假成功
    assert out['note'] and '层厚' in out['note']
    assert any('层厚' in w for w in out['warnings'])          # note 并入 warnings,不再被吞


def test_derive_task_conv_vacuum_no_spurious_note(tmp_path):
    (tmp_path / 'CONTCAR').write_text('x', encoding='utf-8')
    api = Api(conv_scan_mod=_fake_conv_scan(), ledger_mod=_fake_ledger_register([]))
    out = api.derive_task('conv_vacuum', str(tmp_path), {'vacuums': [10, 12]})
    assert out['ok'] is True and len(out['job_dirs']) == 2 and out.get('note') is None


# ── P2 任务性质徽标 ────────────────────────────────────────────────────────────
def test_task_catalog_kind_badges():
    api = Api()                                             # 真 task_catalog
    out = api.task_catalog()
    badges = {t['key']: t['kind_badge'] for t in out['tasks']}
    assert badges['vaspsol'] == 'INCAR 顾问'
    assert badges['formation_binding'] == '结果计算器'
    assert badges['surface_energy'] == '结果计算器'
    assert badges['relax'] == '作业生成' and badges['chgdiff'] == '作业生成'
    assert badges['neb'] == '作业生成'


def test_task_badge_classifier():
    assert Api._task_badge('vcstudio.generate.incar_builder:vaspsol_keys') == 'INCAR 顾问'
    assert Api._task_badge('vcstudio.project.references:binding_energy') == '结果计算器'
    assert Api._task_badge('vcstudio.project.surface_energy:surface_energy') == '结果计算器'
    assert Api._task_badge('vcstudio.generate.job_builder:build_job_dir') == '作业生成'
    assert Api._task_badge('') == '作业生成'
