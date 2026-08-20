"""双角色评审落地项测试:认领外部任务 / 改参续算 / task_type 推断 / 队列明细解析。"""
import os

import pytest

from vcstudio.cluster import submitter, ledger
from vcstudio.cluster.schedulers import SlurmDialect, PBSDialect
from vcstudio.generate.job_builder import build_job_dir, _infer_task_type
from vcstudio.shared import manifest

from tests.test_submitter import (          # 复用假件与工装
    FakeClient, FakeSFTP, _profile, _job_dir, _OVERLAP_CONTCAR,
)


# ── task_type 推断(静态作业误判修复) ────────────────────────────────────────
def test_infer_task_type_static_when_nsw_absent_or_zero():
    assert _infer_task_type({'ENCUT': 400}) == 'static'
    assert _infer_task_type({'NSW': 0}) == 'static'


def test_infer_task_type_relax_and_freq():
    assert _infer_task_type({'NSW': 100, 'IBRION': 2}) == 'relax'
    assert _infer_task_type({'NSW': 1, 'IBRION': 5}) == 'freq'
    assert _infer_task_type({'IBRION': 6}) == 'freq'


def test_build_job_dir_returns_task_type_and_manifest_uses_it(tmp_path):
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   TITEL  = PAW_PBE C 08Apr2002\n'
        '   ENMAX  =  273.214; ENMIN = 200.000 eV\n', encoding='utf-8')
    poscar = tmp_path / 'POSCAR'
    poscar.write_text('C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
                      encoding='utf-8')
    out = tmp_path / 'static_job'
    res = build_job_dir(str(poscar), 'ENCUT = 400\nNSW = 0\n', str(out),
                        calc_type='slab', lib_root=str(lib))
    assert res['task_type'] == 'static'
    m = manifest.create_from_build(str(out), res, poscar_path=str(poscar))
    assert m['task_type'] == 'static'

    out2 = tmp_path / 'relax_job'
    res2 = build_job_dir(str(poscar), 'ENCUT = 400\nNSW = 200\nIBRION = 2\n',
                         str(out2), calc_type='slab', lib_root=str(lib))
    assert res2['task_type'] == 'relax'


def test_single_job_generation_gets_advisor_warnings(tmp_path):
    """单作业生成路径也要抓 slab 无偶极修正 这类 warn-only 防呆。"""
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   TITEL  = PAW_PBE C 08Apr2002\n'
        '   ENMAX  =  273.214; ENMIN = 200.000 eV\n', encoding='utf-8')
    poscar = tmp_path / 'POSCAR'
    poscar.write_text('C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
                      encoding='utf-8')
    res = build_job_dir(str(poscar), 'ENCUT = 400\nNSW = 100\n',
                        str(tmp_path / 'slab_job'), calc_type='slab',
                        lib_root=str(lib))
    assert any('IDIPOL' in w or '偶极' in w for w in res['warnings'])


# ── 认领外部任务 ─────────────────────────────────────────────────────────────
def test_adopt_external_job_registers_and_tracks(tmp_path):
    led = tmp_path / 'jobs.json'
    local = tmp_path / 'adopted_job'
    prof = _profile(scheduler='Slurm')
    # ledger.register 用默认路径;打桩到 tmp
    import vcstudio.cluster.ledger as ledger_mod
    orig = ledger_mod.default_ledger_path
    ledger_mod.default_ledger_path = lambda: led
    try:
        m = submitter.adopt_external_job(str(local), prof, '88123',
                                         '/work/u/mn_li2s4', name='mn_li2s4')
    finally:
        ledger_mod.default_ledger_path = orig
    assert m['scheduler_job_id'] == '88123'
    assert m['cluster'] == prof.name
    assert m['remote_dir'] == '/work/u/mn_li2s4'
    assert m['state'] == 'SUBMITTED'
    assert (m['inputs'] or {}).get('adopted') is True
    # 已入台账 + job.yaml 落盘
    assert str(local.resolve()) in ledger.list_dirs(led)
    assert manifest.load_manifest(str(local))['scheduler_job_id'] == '88123'


def test_adopt_rejects_relative_remote_and_double_adopt(tmp_path):
    led = tmp_path / 'jobs.json'
    local = tmp_path / 'j'
    prof = _profile()
    with pytest.raises(ValueError):
        submitter.adopt_external_job(str(local), prof, '1', 'work/rel/path')
    import vcstudio.cluster.ledger as ledger_mod
    orig = ledger_mod.default_ledger_path
    ledger_mod.default_ledger_path = lambda: led
    try:
        submitter.adopt_external_job(str(local), prof, '2', '/abs/dir')
        with pytest.raises(ValueError):
            submitter.adopt_external_job(str(local), prof, '3', '/abs/other')
    finally:
        ledger_mod.default_ledger_path = orig


def test_adopt_preserves_existing_manifest_task_type(tmp_path):
    local = tmp_path / 'existing_static'
    local.mkdir()
    original = manifest.new_manifest(
        job_id='existing', system='surface', task_type='static', calc_type='slab', inputs={})
    manifest.save_manifest(local, original)
    import vcstudio.cluster.ledger as ledger_mod
    orig = ledger_mod.default_ledger_path
    ledger_mod.default_ledger_path = lambda: tmp_path / 'jobs.json'
    try:
        adopted = submitter.adopt_external_job(
            str(local), _profile(), '55', '/work/static')
    finally:
        ledger_mod.default_ledger_path = orig
    assert adopted['task_type'] == 'static'
    assert manifest.load_manifest(local)['task_type'] == 'static'


def test_adopt_explicit_task_is_strictly_normalized_and_conflicts_rejected(tmp_path):
    import vcstudio.cluster.ledger as ledger_mod
    orig = ledger_mod.default_ledger_path
    ledger_mod.default_ledger_path = lambda: tmp_path / 'jobs.json'
    try:
        fresh = submitter.adopt_external_job(
            str(tmp_path / 'band'), _profile(), '56', '/work/band', task_type='band')
        assert fresh['task_type'] == 'bands'
        with pytest.raises(ValueError, match='未知任务类型'):
            submitter.adopt_external_job(
                str(tmp_path / 'bad'), _profile(), '57', '/work/bad', task_type='statci')
        assert not (tmp_path / 'bad').exists()

        # A new-target lock creates the target directory, so every malformed
        # request must fail before the lock is acquired or written.
        invalid_requests = (
            ('empty_job_id', _profile(), '', '/work/invalid', ''),
            ('relative_remote', _profile(), '59', 'work/invalid', ''),
            ('empty_profile', _profile(name=''), '60', '/work/invalid', ''),
            ('non_text_name', _profile(), '61', '/work/invalid', object()),
        )
        for label, profile, job_id, remote_dir, name in invalid_requests:
            target = tmp_path / label
            with pytest.raises(ValueError):
                submitter.adopt_external_job(
                    str(target), profile, job_id, remote_dir, name=name)
            assert not target.exists()
            assert not (target / '.vcstudio-job-operation.lock').exists()

        existing = tmp_path / 'existing_dos'
        existing.mkdir()
        manifest.save_manifest(existing, manifest.new_manifest(
            job_id='dos', system='s', task_type='dos_pdos', calc_type='slab', inputs={}))
        with pytest.raises(ValueError, match='冲突'):
            submitter.adopt_external_job(
                str(existing), _profile(), '58', '/work/dos', task_type='relax')
        assert manifest.load_manifest(existing)['task_type'] == 'dos_pdos'
    finally:
        ledger_mod.default_ledger_path = orig


# ── 队列全量明细解析 ─────────────────────────────────────────────────────────
def test_slurm_parse_detail_with_workdir():
    d = SlurmDialect()
    raw = ('101|R|mn_li2s4|/work/u/mn_li2s4\n'
           '102|PD|co_slab|/work/u/co_slab\n'
           '103|CD|done_job|/work/u/x\n')
    out = d.parse_detail(raw)
    assert [(j['job_id'], j['state']) for j in out] == \
        [('101', 'RUNNING'), ('102', 'QUEUED')]
    assert out[0]['workdir'] == '/work/u/mn_li2s4'
    assert out[1]['name'] == 'co_slab'


def test_pbs_parse_detail_names():
    d = PBSDialect()
    raw = ('Job ID    Username Queue  Jobname  SessID NDS TSK Memory Time S Time\n'
           '--------- -------- ------ -------- ------ --- --- ------ ---- - ----\n'
           '8812345.c sk2067   batch  mn_job   123    1   12  --     24:0 R 01:0\n')
    out = d.parse_detail(raw)
    assert out and out[0]['job_id'] == '8812345'
    assert out[0]['state'] == 'RUNNING'
    assert out[0]['name'] == 'mn_job'
    assert out[0]['workdir'] == ''       # PBS 拿不到工作目录


def test_query_queue_detail_sentinel_guard():
    prof = _profile(scheduler='Slurm')
    client = FakeClient(script=[('squeue', '101|R|j|/w\n')])   # 无哨兵 → 抛错
    with pytest.raises(RuntimeError):
        submitter.query_queue_detail(client, prof)


# ── 改参续算 ─────────────────────────────────────────────────────────────────
def _terminal_job(tmp_path, state='NEEDS_HUMAN', fclass='SCF_SLOSHING'):
    d = _job_dir(tmp_path)
    m = manifest.load_manifest(d)
    m['cluster'] = '1w'
    m['remote_dir'] = '/work/sk2067/jobs/zn_job'
    m['scheduler_job_id'] = '900'
    m.setdefault('results', {})['diagnosis'] = {
        'failure_class': fclass, 'restartable': False, 'evidence': 'dE 不降'}
    manifest.set_state(m, state)
    manifest.save_manifest(d, m)
    return d


_CONTCAR = ('C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nDirect\n0 0 0\n')


def test_tune_continue_appends_whitelist_and_resubmits(tmp_path):
    d = _terminal_job(tmp_path)
    client = FakeClient(script=[('cat', _CONTCAR), ('qsub', '901.cluster\n')])
    sftp = FakeSFTP()
    m = submitter.continue_with_incar_changes(
        client, sftp, _profile(), d, {'ALGO': 'Normal', 'ISMEAR': '0'})
    assert m['scheduler_job_id'] == '901'
    assert m['state'] == 'SUBMITTED'
    # INCAR 追加块(原文保留 + 白名单键)
    text = open(os.path.join(d, 'INCAR'), encoding='utf-8').read()
    assert 'ENCUT = 400' in text and 'ALGO = Normal' in text and 'ISMEAR = 0' in text
    assert os.path.isfile(os.path.join(d, 'INCAR.bak1'))
    # 新 INCAR 已上传
    assert any(r.endswith('/INCAR') for r in sftp.uploaded)
    # attempts 记录 changes
    last = m['attempts'][-1]
    assert last['action'] == 'incar_tuned_restart'
    assert last['incar_changes'] == {'ALGO': 'Normal', 'ISMEAR': '0'}
    # 诊断被消费 + 轮次 +1
    assert 'diagnosis' not in (m['results'] or {})
    assert m['results']['continue_rounds'] == 1


def test_tune_continue_rejects_non_whitelist_key(tmp_path):
    d = _terminal_job(tmp_path)
    with pytest.raises(ValueError) as ei:
        submitter.continue_with_incar_changes(
            FakeClient(), FakeSFTP(), _profile(), d, {'ENCUT': '500'})
    assert 'ENCUT' in str(ei.value)


def test_tune_continue_refuses_live_job(tmp_path):
    d = _terminal_job(tmp_path)
    m = manifest.load_manifest(d)
    manifest.set_state(m, 'RUNNING')
    manifest.save_manifest(d, m)
    with pytest.raises(ValueError):
        submitter.continue_with_incar_changes(
            FakeClient(), FakeSFTP(), _profile(), d, {'ALGO': 'Normal'})


def test_tune_continue_respects_round_cap(tmp_path):
    d = _terminal_job(tmp_path)
    m = manifest.load_manifest(d)
    m['results']['continue_rounds'] = submitter.CONTINUE_MAX_ROUNDS
    manifest.save_manifest(d, m)
    with pytest.raises(RuntimeError):
        submitter.continue_with_incar_changes(
            FakeClient(), FakeSFTP(), _profile(), d, {'ALGO': 'Normal'})


def test_explicit_manual_tune_can_exceed_automatic_round_cap(tmp_path):
    d = _terminal_job(tmp_path)
    m = manifest.load_manifest(d)
    m['results']['continue_rounds'] = submitter.CONTINUE_MAX_ROUNDS
    manifest.save_manifest(d, m)
    client = FakeClient(script=[('cat', _CONTCAR), ('qsub', '904.cluster\n')])

    updated = submitter.continue_with_incar_changes(
        client, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'}, max_rounds=None)

    assert updated['results']['continue_rounds'] == submitter.CONTINUE_MAX_ROUNDS + 1
    assert updated['attempts'][-1]['round_limit_override'] == 'manual-explicit'
    assert '人工确认超出自动上限' in updated['state_history'][-1]['note']


@pytest.mark.parametrize('contcar', ['garbage\n', _OVERLAP_CONTCAR])
def test_tune_requested_contcar_is_validated_before_any_mutation(tmp_path, contcar):
    d = _terminal_job(tmp_path)
    incar = os.path.join(d, 'INCAR')
    poscar = os.path.join(d, 'POSCAR')
    before = (open(incar, encoding='utf-8').read(),
              open(poscar, encoding='utf-8').read(),
              manifest.load_manifest(d))
    client = FakeClient(script=[('cat', contcar), ('qsub', 'must-not-run\n')])

    with pytest.raises(RuntimeError, match='CONTCAR|原子重叠'):
        submitter.continue_with_incar_changes(
            client, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'},
            max_rounds=None, restart_from_contcar=True,
            idempotency_key='manual-tune-geometry-001')

    assert open(incar, encoding='utf-8').read() == before[0]
    assert open(poscar, encoding='utf-8').read() == before[1]
    assert manifest.load_manifest(d) == before[2]
    assert not os.path.exists(os.path.join(d, '.vcstudio-job-actions.json'))
    assert not any('qsub' in command for command in client.commands)


def test_tune_operation_key_replays_exact_request_and_rejects_changed_request(tmp_path):
    d = _terminal_job(tmp_path)
    key = 'manual-tune-replay-001'
    first = submitter.continue_with_incar_changes(
        FakeClient(script=[('cat', _CONTCAR), ('qsub', '905.cluster\n')]),
        FakeSFTP(), _profile(), d, {'ALGO': 'Normal'}, max_rounds=None,
        idempotency_key=key)
    assert first['scheduler_job_id'] == '905'

    replay_client = FakeClient()
    replay = submitter.continue_with_incar_changes(
        replay_client, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'},
        max_rounds=None, idempotency_key=key)
    assert replay['_tune_continue_replayed'] is True
    assert replay_client.commands == []

    changed_client = FakeClient()
    with pytest.raises(ValueError, match='不同的改参续算内容'):
        submitter.continue_with_incar_changes(
            changed_client, FakeSFTP(), _profile(), d, {'ALGO': 'Fast'},
            max_rounds=None, idempotency_key=key)
    assert changed_client.commands == []


def test_tune_manifest_failure_retains_durable_manual_gate(tmp_path, monkeypatch):
    d = _terminal_job(tmp_path)
    data = manifest.load_manifest(d)
    data['results']['continue_rounds'] = submitter.CONTINUE_MAX_ROUNDS
    manifest.save_manifest(d, data)
    key = 'manual-tune-crash-001'
    real_save = submitter.manifest_mod.save_manifest

    def fail_final(path, payload):
        if (payload.get('state') == 'SUBMITTED'
                and str(payload.get('scheduler_job_id') or '') == '906'):
            raise OSError('disk full')
        return real_save(path, payload)

    monkeypatch.setattr(submitter.manifest_mod, 'save_manifest', fail_final)
    with pytest.raises(submitter.UnknownRemoteJobOperation,
                       match='job.yaml') as failure:
        submitter.continue_with_incar_changes(
            FakeClient(script=[('cat', _CONTCAR), ('qsub', '906.cluster\n')]),
            FakeSFTP(), _profile(), d, {'ALGO': 'Normal'}, max_rounds=None,
            idempotency_key=key)
    assert failure.value.scheduler_job_id == '906'

    journal = submitter._read_job_action_journal(d)
    record = journal['operations'][-1]
    assert record['status'] == 'remote_accepted'
    assert record['request']['round_limit_override'] == 'manual-explicit'
    assert record['request']['intent']['changes'] == {'ALGO': 'Normal'}
    assert record['request']['source_scheduler_job_id'] == '900'

    monkeypatch.setattr(submitter.manifest_mod, 'save_manifest', real_save)
    restarted = FakeClient()
    with pytest.raises(submitter.UnknownRemoteJobOperation):
        submitter.continue_with_incar_changes(
            restarted, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'},
            max_rounds=None, idempotency_key=key)
    assert restarted.commands == []


def test_continue_and_tune_require_explicit_terminal_state(tmp_path):
    d = _terminal_job(tmp_path)
    data = manifest.load_manifest(d)
    data['state'] = 'CREATED'
    data['results']['diagnosis']['restartable'] = True
    manifest.save_manifest(d, data)

    continue_client = FakeClient()
    with pytest.raises(ValueError, match='不是可续算的终态'):
        submitter.continue_from_contcar(continue_client, _profile(), d)
    tune_client = FakeClient()
    with pytest.raises(ValueError, match='不是可改参重投的终态'):
        submitter.continue_with_incar_changes(
            tune_client, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'},
            max_rounds=None)
    assert continue_client.commands == []
    assert tune_client.commands == []


def test_tune_rejects_multiline_value_before_any_mutation(tmp_path):
    d = _terminal_job(tmp_path)
    client = FakeClient()

    with pytest.raises(ValueError, match='非空单值文本'):
        submitter.continue_with_incar_changes(
            client, FakeSFTP(), _profile(), d,
            {'NELM': '120\nISPIN = 2'}, max_rounds=None,
            restart_from_contcar=False,
            idempotency_key='tune-value-injection-0001')

    assert client.commands == []
    assert not os.path.exists(os.path.join(d, '.vcstudio-job-actions.json'))
    with open(os.path.join(d, 'INCAR'), encoding='utf-8') as handle:
        assert 'ISPIN = 2' not in handle.read()


@pytest.mark.parametrize('value', [
    '120 ; ISPIN = 2',
    '120 # ISPIN = 2',
    '120 ! ISPIN = 2',
])
def test_tune_rejects_single_line_tag_or_comment_injection(tmp_path, value):
    d = _terminal_job(tmp_path)
    client = FakeClient()

    with pytest.raises(ValueError, match='标签分隔符|注释符'):
        submitter.continue_with_incar_changes(
            client, FakeSFTP(), _profile(), d, {'NELM': value},
            max_rounds=None, restart_from_contcar=False,
            idempotency_key='tune-separator-injection-001')

    assert client.commands == []
    assert not os.path.exists(os.path.join(d, '.vcstudio-job-actions.json'))


def test_tune_continue_empty_changes_rejected(tmp_path):
    d = _terminal_job(tmp_path)
    with pytest.raises(ValueError):
        submitter.continue_with_incar_changes(
            FakeClient(), FakeSFTP(), _profile(), d, {})


# ── P1b:query_workdir(认领免手填远程目录) ──────────────────────────────────
def test_query_workdir_pbs_roundtrip():
    from tests.test_submitter import FakeClient
    from vcstudio.cluster import submitter

    class _Prof:
        scheduler = 'PBS'
        scheduler_bin = '/opt/torque-6.1.2/bin'

    raw = ('Job Id: 205690.cluster.hpc\n'
           '    init_work_dir = /home/Maple123/new structure/Nb_S\n'
           '\t8\n')
    c = FakeClient(script=[('qstat', raw)])
    assert submitter.query_workdir(c, _Prof(), '205690') == '/home/Maple123/new structure/Nb_S8'
    assert any('-f' in cmd and '205690' in cmd for cmd in c.commands)


def test_query_workdir_slurm_skips_remote_call():
    from tests.test_submitter import FakeClient
    from vcstudio.cluster import submitter

    class _Prof:
        scheduler = 'Slurm'
        scheduler_bin = ''

    c = FakeClient(script=[])
    assert submitter.query_workdir(c, _Prof(), '42') == ''
    assert c.commands == []                    # 方言不支持 → 不发一条远程命令
