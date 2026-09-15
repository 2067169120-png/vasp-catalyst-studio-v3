"""提交编排测试:注入假 client/sftp,验证 preflight/上传/提交/状态刷新与 manifest 回写。"""
import hashlib
import json
import os
import posixpath
import threading

import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster import submitter
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.shared import manifest
from vcstudio.shared.scientific_inputs import record_input_closure


# ── 假件:模仿 paramiko 的最小表面 ──
class _FakeChannel:
    def __init__(self, status=0):
        self._status = status

    def recv_exit_status(self):
        return self._status


class _FakeStream:
    def __init__(self, data=b'', status=0):
        self._data = data
        self.channel = _FakeChannel(status)

    def read(self):
        return self._data


class FakeClient:
    """exec_command 按 (子串, 输出) 剧本表回放;记录全部命令供断言。

    exit_codes: {命令子串: 退出码},未命中默认 0。
    """

    def __init__(self, script=None, exit_codes=None):
        self.script = list(script or [])
        self.exit_codes = dict(exit_codes or {})
        self.commands = []

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        status = 0
        for needle, code in self.exit_codes.items():
            if needle in cmd:
                status = code
                break
        for needle, out in self.script:
            if needle in cmd:
                return _FakeStream(), _FakeStream(out.encode(), status), _FakeStream()
        return _FakeStream(), _FakeStream(b'', status), _FakeStream()


class _FakeRemoteFile:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, text):
        self.store[self.path] = text


class FakeSFTP:
    def __init__(self, missing=()):
        self.uploaded = {}     # remote → local
        self.written = {}      # remote → text
        self.missing = set(missing)

    def put(self, local, remote):
        self.uploaded[remote] = local

    def file(self, path, mode='w'):
        return _FakeRemoteFile(self.written, path)

    def get(self, remote, local):
        name = posixpath.basename(remote)
        if name in self.missing:
            raise IOError(f'no such file: {remote}')
        with open(local, 'w', encoding='utf-8') as f:
            f.write(f'fresh {name}\n')


def _profile(**kw):
    base = dict(name='1w', hostname='h', username='sk2067', auth='key', key_path='k',
                remote_root='/work/sk2067/jobs', scheduler='PBS',
                scheduler_bin='/opt/torque-6.1.2/bin',
                queue='batch', nodes=1, ppn=12, walltime='24:00:00',
                env_lines=['source /opt/intel.sh'], vasp_cmd='mpirun -np 12 vasp_std > log 2>&1',
                script_mode='auto')
    base.update(kw)
    return ClusterProfile(**base)


def _job_dir(tmp_path):
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   TITEL  = PAW_PBE C 08Apr2002\n'
        '   ENMAX  =  273.214; ENMIN = 200.000 eV\n', encoding='utf-8')
    poscar = tmp_path / 'POSCAR'
    poscar.write_text('C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
                      encoding='utf-8')
    out = tmp_path / 'zn_job'
    res = build_job_dir(str(poscar), 'ENCUT = 400\n', str(out),
                        calc_type='slab', lib_root=str(lib))
    manifest.create_from_build(str(out), res, poscar_path=str(poscar), validate=True)
    return str(out)


def _quick_job_dir(tmp_path, engine='cp2k', filename='calc.inp'):
    out = tmp_path / f'{engine}_job'
    out.mkdir(parents=True)
    text = {
        'cp2k': (
            '&GLOBAL\n  RUN_TYPE ENERGY\n&END GLOBAL\n'
            '&FORCE_EVAL\n &DFT\n  &MGRID\n   CUTOFF 400\n  &END MGRID\n &END DFT\n'
            ' &SUBSYS\n  &CELL\n   ABC 10 10 10\n  &END CELL\n'
            '  &COORD\n   H 0 0 0\n  &END COORD\n'
            '  &KIND H\n  &END KIND\n &END SUBSYS\n&END FORCE_EVAL\n'),
        'gaussian': '#P HF/STO-3G SP\n\njob\n\n0 1\nH 0 0 0\n\n',
    }.get(engine, 'input\n')
    (out / filename).write_text(text, encoding='utf-8')
    m = manifest.new_manifest(
        job_id=f'{engine}-1', system=out.name, task_type='quick', calc_type='',
        inputs={'engine': engine, 'files': [filename]})
    manifest.save_manifest(out, m)
    return str(out)


def test_preflight_catches_problems(tmp_path):
    d = _job_dir(tmp_path)
    assert submitter.preflight(_profile(), d) == []
    assert any('remote_root' in e for e in submitter.preflight(_profile(remote_root=''), d))
    assert any('绝对路径' in e for e in submitter.preflight(_profile(remote_root='rel/path'), d))
    assert any('队列' in e for e in submitter.preflight(_profile(queue=''), d))
    assert any('ppn' in e for e in submitter.preflight(_profile(ppn=0), d))
    assert any('VASP' in e for e in submitter.preflight(_profile(vasp_cmd=''), d))
    assert any('暂不支持' in e for e in submitter.preflight(_profile(scheduler='LSF'), d))
    assert any('模板' in e for e in submitter.preflight(
        _profile(script_mode='template', template_path=str(tmp_path / 'nope.sh')), d))
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert any('缺 INCAR' in e for e in submitter.preflight(_profile(), str(empty)))


@pytest.mark.parametrize('filename', ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'))
def test_preflight_rejects_managed_vasp_input_changed_after_preparation(
        tmp_path, filename):
    job_dir = _job_dir(tmp_path)
    with open(os.path.join(job_dir, filename), 'a', encoding='utf-8') as handle:
        handle.write('\n# changed after preparation\n')

    errors = submitter.preflight(_profile(), job_dir)

    assert any(filename in error and 'SHA256' in error and '重新准备' in error
               for error in errors)


def test_submit_hash_mismatch_stops_before_any_remote_operation(tmp_path):
    job_dir = _job_dir(tmp_path)
    with open(os.path.join(job_dir, 'INCAR'), 'a', encoding='utf-8') as handle:
        handle.write('\nNSW = 999\n')
    client, sftp = FakeClient(), FakeSFTP()

    with pytest.raises(ValueError, match='INCAR.*SHA256.*重新准备'):
        submitter.submit_job(client, sftp, _profile(), job_dir)

    assert client.commands == []
    assert sftp.uploaded == {} and sftp.written == {}


def test_preflight_keeps_legacy_manifest_without_input_hashes_compatible(tmp_path):
    job_dir = _job_dir(tmp_path)
    item = manifest.load_manifest(job_dir)
    item['inputs'].pop('sha256', None)
    manifest.save_manifest(job_dir, item)
    with open(os.path.join(job_dir, 'INCAR'), 'a', encoding='utf-8') as handle:
        handle.write('\n# legacy manifest has no immutable hash evidence\n')

    assert submitter.preflight(_profile(), job_dir) == []


def test_preflight_potcar_titel_gate(tmp_path):
    """原版防线移植:POTCAR TITEL 段数 ≠ 物种数(截断/手改)→ 拒绝提交。"""
    d = _job_dir(tmp_path)
    assert submitter.preflight(_profile(), d) == []            # 完好 → 放行
    potcar = os.path.join(d, 'POTCAR')
    text = open(potcar, encoding='utf-8').read()
    open(potcar, 'w', encoding='utf-8').write(text + text)     # 复制一份 → TITEL 数翻倍
    errs = submitter.preflight(_profile(), d)
    assert any('TITEL' in e and '截断/手改' in e for e in errs)


def test_submit_job_happy_path_pbs(tmp_path):
    d = _job_dir(tmp_path)
    client = FakeClient(script=[('qsub', '8812345.cluster.hpc\n')])
    sftp = FakeSFTP()
    m = submitter.submit_job(client, sftp, _profile(), d)

    remote = m['remote_dir']
    assert remote.startswith('/work/sk2067/jobs/zn_job--')
    assert len(posixpath.basename(remote).rsplit('--', 1)[-1]) == 16
    # 上传:四件套 put + 脚本 write
    for f in ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR'):
        assert posixpath.join(remote, f) in sftp.uploaded
    script = sftp.written[posixpath.join(remote, 'vcs_job.sh')]
    assert '#PBS -q batch' in script and 'source /opt/intel.sh' in script
    assert '\r' not in script                                  # CRLF 消毒
    # 命令:mkdir + qsub(带 scheduler_bin 全路径)
    assert any(f'mkdir {remote}' in c for c in client.commands)
    assert any('/opt/torque-6.1.2/bin/qsub' in c for c in client.commands)
    # manifest 回写
    assert m['state'] == 'SUBMITTED'
    assert m['scheduler_job_id'] == '8812345'
    assert m['cluster'] == '1w' and m['remote_dir'] == remote
    assert [h['state'] for h in m['state_history']] == ['CREATED', 'UPLOADED', 'SUBMITTED']
    assert m['attempts'][0]['job_id'] == '8812345'
    assert m['attempts'][0]['cores'] == 12                     # v3.3.0 记核数(nodes×ppn)供实耗核时
    assert m['attempts'][0]['incar_sha256'] == manifest.sha256_file(
        os.path.join(d, 'INCAR'))
    assert m['execution_authority']['scheduler_job_id'] == '8812345'
    assert m['execution_authority']['current_incar_sha256'] == \
        m['attempts'][0]['incar_sha256']
    assert manifest.load_manifest(d)['state'] == 'SUBMITTED'   # 已落盘


def test_submit_authority_uses_preupload_incar_digest_not_later_local_bytes(
        tmp_path):
    d = _job_dir(tmp_path)
    uploaded = {}

    class MutatingSFTP(FakeSFTP):
        def put(self, local, remote):
            if posixpath.basename(remote) == 'INCAR':
                uploaded['incar_sha256'] = manifest.sha256_file(local)
                result = super().put(local, remote)
                with open(local, 'w', encoding='utf-8', newline='') as handle:
                    handle.write('ENCUT = 999\nISPIN = 2\n')
                return result
            return super().put(local, remote)

    client = FakeClient(script=[('qsub', '8812346.cluster.hpc\n')])
    result = submitter.submit_job(client, MutatingSFTP(), _profile(), d)

    assert uploaded['incar_sha256'] != manifest.sha256_file(
        os.path.join(d, 'INCAR'))
    assert result['attempts'][-1]['incar_sha256'] == uploaded['incar_sha256']
    assert result['execution_authority']['current_incar_sha256'] == \
        uploaded['incar_sha256']
    command = next(item for item in client.commands if 'qsub' in item)
    assert uploaded['incar_sha256'] in command
    assert command.index('sha256sum -c -') < command.index('qsub')


def test_vasp_submit_recovery_freezes_incar_digest_before_remote_accept(
        tmp_path, monkeypatch):
    d = _job_dir(tmp_path)
    expected = manifest.load_manifest(d)['inputs']['sha256']['INCAR']
    real_save = submitter.manifest_mod.save_manifest

    def fail_submitted(path, payload):
        if payload.get('state') == 'SUBMITTED':
            raise OSError('disk full')
        return real_save(path, payload)

    monkeypatch.setattr(submitter.manifest_mod, 'save_manifest', fail_submitted)
    with pytest.raises(submitter.UnknownRemoteSubmission):
        submitter.submit_job(
            FakeClient(script=[('qsub', '8812347.cluster.hpc\n')]),
            FakeSFTP(), _profile(), d,
            idempotency_key='initial-submit-authority-001')

    recovery = submitter._read_submission_recovery(d)
    assert recovery['status'] == 'remote_accepted'
    assert recovery['scheduler_job_id'] == '8812347'
    assert recovery['incar_sha256'] == expected
    assert manifest.load_manifest(d)['state'] == 'CREATED'


def test_batch_members_upload_their_own_distinct_incars(tmp_path):
    """同组成员的上传源必须是各自受管目录，不能回退到首个/共享 INCAR。"""
    first = _job_dir(tmp_path / 'first')
    second = _job_dir(tmp_path / 'second')
    first_text = 'ENCUT = 400\nISPIN = 2\nNSW = 40\nMAGMOM = 1*0.0\n'
    second_text = 'ENCUT = 400\nISPIN = 2\nNSW = 120\nMAGMOM = 1*2.0\n'
    for job_dir, text in ((first, first_text), (second, second_text)):
        incar = os.path.join(job_dir, 'INCAR')
        with open(incar, 'w', encoding='utf-8') as handle:
            handle.write(text)
        item = manifest.load_manifest(job_dir)
        item.setdefault('inputs', {}).setdefault('sha256', {})['INCAR'] = \
            manifest.sha256_file(incar)
        record_input_closure(job_dir, item)
        manifest.save_manifest(job_dir, item)

    sftp = FakeSFTP()
    first_manifest = submitter.submit_job(
        FakeClient(script=[('qsub', '101.cluster\n')]), sftp, _profile(), first)
    second_manifest = submitter.submit_job(
        FakeClient(script=[('qsub', '102.cluster\n')]), sftp, _profile(), second)

    first_upload = sftp.uploaded[posixpath.join(first_manifest['remote_dir'], 'INCAR')]
    second_upload = sftp.uploaded[posixpath.join(second_manifest['remote_dir'], 'INCAR')]
    assert first_upload == os.path.join(first, 'INCAR')
    assert second_upload == os.path.join(second, 'INCAR')
    assert open(first_upload, encoding='utf-8').read() == first_text
    assert open(second_upload, encoding='utf-8').read() == second_text


def test_submit_refuses_any_existing_remote_identity_or_non_created_state(tmp_path):
    """通用提交不可复用已有远端身份，尤其不能把 RUNNING 作业双提交到同一目录。"""
    d = _job_dir(tmp_path)
    submitter.submit_job(
        FakeClient(script=[('qsub', '8812345.cluster\n')]), FakeSFTP(), _profile(), d)
    client, sftp = FakeClient(), FakeSFTP()
    with pytest.raises(ValueError, match='仅全新的 CREATED|拒绝重复提交'):
        submitter.submit_job(client, sftp, _profile(), d)
    assert client.commands == [] and sftp.uploaded == {}

    fresh = _job_dir(tmp_path / 'fresh')
    data = manifest.load_manifest(fresh)
    data['cluster'] = 'old-server'                 # 即便 state 仍 CREATED，也不是全新清单
    manifest.save_manifest(fresh, data)
    assert any('远端身份' in issue for issue in submitter.preflight(_profile(), fresh))


def test_non_vasp_missing_own_command_hard_blocks_before_network(tmp_path):
    d = _quick_job_dir(tmp_path, 'cp2k')
    profile = _profile()  # 只有 vasp_cmd，不得被 CP2K 借用
    errs = submitter.preflight(profile, d)
    assert not any('四件套' in issue for issue in errs)
    assert any('engine_commands.cp2k' in issue and '不会用 VASP' in issue
               for issue in errs)

    client, sftp = FakeClient(), FakeSFTP()
    with pytest.raises(ValueError, match=r'engine_commands\.cp2k'):
        submitter.submit_job(client, sftp, profile, d)
    assert client.commands == [] and sftp.uploaded == {} and sftp.written == {}


def test_non_vasp_command_selected_rendered_and_only_declared_inputs_uploaded(tmp_path):
    d = _quick_job_dir(tmp_path, 'cp2k', 'water.inp')
    profile = _profile(
        nodes=2, ppn=8,
        engine_commands={
            'cp2k': 'srun -n {cores} cp2k.psmp -i {input} -o {stem}.out',
        })
    assert submitter.preflight(profile, d) == []

    client = FakeClient(script=[('qsub', '42.cluster\n')])
    sftp = FakeSFTP()
    m = submitter.submit_job(client, sftp, profile, d)

    remote = m['remote_dir']
    script = sftp.written[posixpath.join(remote, 'vcs_job.sh')]
    assert 'srun -n 16 cp2k.psmp -i water.inp -o water.out' in script
    assert 'vasp_std' not in script
    assert set(sftp.uploaded) == {posixpath.join(remote, 'water.inp')}
    assert m['attempts'][0]['engine'] == 'cp2k'


@pytest.mark.parametrize('task_type,extra,reason', [
    ('bands', 'CHGCAR', 'ICHARG=11'),
    ('dimer', 'MODECAR', 'Dimer'),
])
def test_vasp_task_specific_inputs_are_required_and_uploaded(
        tmp_path, task_type, extra, reason):
    """派生任务的硬输入必须随四件套上传；缺失时联网前给出可行动错误。"""
    d = _job_dir(tmp_path)
    data = manifest.load_manifest(d)
    data['task_type'] = task_type
    record_input_closure(d, data)
    manifest.save_manifest(d, data)

    errs = submitter.preflight(_profile(), d)
    assert any(extra in issue and reason in issue for issue in errs)

    with open(os.path.join(d, extra), 'w', encoding='utf-8') as handle:
        handle.write('required task input\n')
    data = manifest.load_manifest(d)
    record_input_closure(d, data)
    manifest.save_manifest(d, data)
    assert submitter.preflight(_profile(), d) == []
    client = FakeClient(script=[('qsub', '711.cluster\n')])
    sftp = FakeSFTP()
    submitted = submitter.submit_job(client, sftp, _profile(), d)
    assert posixpath.join(submitted['remote_dir'], extra) in sftp.uploaded


def test_vasp_icharg_hard_input_is_required_independent_of_task_name(tmp_path):
    d = _job_dir(tmp_path)
    with open(os.path.join(d, 'INCAR'), 'a', encoding='utf-8') as handle:
        handle.write('ICHARG = 11\n')
    # This fixture intentionally constructs a newly prepared ICHARG=11 job;
    # keep its immutable-input evidence in sync before testing CHGCAR gating.
    item = manifest.load_manifest(d)
    item['inputs']['sha256']['INCAR'] = manifest.sha256_file(os.path.join(d, 'INCAR'))
    record_input_closure(d, item)
    manifest.save_manifest(d, item)
    errs = submitter.preflight(_profile(), d)
    assert any('CHGCAR' in issue and 'ICHARG=11' in issue for issue in errs)

    with open(os.path.join(d, 'CHGCAR'), 'w', encoding='utf-8') as handle:
        handle.write('required fixed charge density\n')
    item = manifest.load_manifest(d)
    record_input_closure(d, item)
    manifest.save_manifest(d, item)
    assert submitter.preflight(_profile(), d) == []
    sftp = FakeSFTP()
    m = submitter.submit_job(
        FakeClient(script=[('qsub', '712.cluster\n')]), sftp, _profile(), d)
    assert posixpath.join(m['remote_dir'], 'CHGCAR') in sftp.uploaded


@pytest.mark.parametrize('task_type,expected', [
    ('static', ('vasprun.xml', 'CHGCAR')),
    ('dos_pdos', ('DOSCAR', 'vasprun.xml')),
    ('bands', ('EIGENVAL', 'PROCAR', 'vasprun.xml')),
    ('bader', ('CHGCAR', 'AECCAR0', 'AECCAR2', 'ACF.dat')),
    ('chgdiff', ('CHGCAR',)),
    ('elf', ('ELFCAR',)),
    ('workfunction', ('LOCPOT',)),
    ('aimd', ('XDATCAR',)),
])
def test_fetch_bundle_is_task_aware(task_type, expected):
    m = {'task_type': task_type, 'inputs': {'engine': 'vasp'}}
    files = submitter.fetch_files_for_manifest(m)
    assert files[:3] == submitter.FETCH_FILES
    assert files[3:] == expected


def test_fetch_bundle_understands_legacy_estatic_purpose_and_other_engines():
    m = {'task_type': 'static', 'inputs': {'purpose': 'bader'}}
    assert submitter.fetch_files_for_manifest(m)[3:] == (
        'CHGCAR', 'AECCAR0', 'AECCAR2', 'ACF.dat')
    cp2k = {'task_type': 'quick',
            'inputs': {'engine': 'cp2k', 'files': ['water.inp']}}
    assert submitter.fetch_files_for_manifest(cp2k) == ('water.out',)
    cp2k['inputs']['output_files'] = ['custom.out', '../escape']
    # 非法项不参与远程路径；合法显式输出仍可用。
    assert submitter.fetch_files_for_manifest(cp2k) == ('custom.out',)


def test_generated_castep_fetch_bundle_includes_task_specific_and_restart_evidence(tmp_path):
    m = {'task_type': 'freq', 'inputs': {
        'engine': 'castep', 'task': 'freq', 'generator': 'engine_generate',
        'files': ['seed.cell', 'seed.param'],
        'output_files': ['seed.castep', 'seed.geom'],
    }}
    assert submitter.fetch_files_for_manifest(m, job_dir=str(tmp_path)) == (
        'seed.castep', 'seed.geom', 'seed.check', 'seed.phonon')


def test_generated_gaussian_fetch_uses_real_percent_chk(tmp_path):
    (tmp_path / 'mol.gjf').write_text(
        '%chk=wavefunction.chk\n#P HF/STO-3G SP\n\njob\n\n0 1\nH 0 0 0\n\n',
        encoding='utf-8')
    m = {'task_type': 'static', 'inputs': {
        'engine': 'gaussian', 'task': 'static', 'generator': 'engine_generate',
        'files': ['mol.gjf'], 'output_files': ['mol.log', 'mol.chk'],
    }}
    assert submitter.fetch_files_for_manifest(m, job_dir=str(tmp_path)) == (
        'mol.log', 'wavefunction.chk')


def test_unknown_engine_and_manifest_path_traversal_are_hard_blocked(tmp_path):
    unknown = _quick_job_dir(tmp_path / 'unknown', 'orca', 'calc.inp')
    assert any('不支持的计算引擎' in issue
               for issue in submitter.preflight(_profile(), unknown))

    unsafe = _quick_job_dir(tmp_path / 'unsafe', 'cp2k', 'safe.inp')
    data = manifest.load_manifest(unsafe)
    data['inputs']['files'] = ['../secret.inp']
    manifest.save_manifest(unsafe, data)
    (tmp_path / 'unsafe' / 'secret.inp').write_text('secret', encoding='utf-8')
    assert any('文件名非法' in issue
               for issue in submitter.preflight(
                   _profile(engine_commands={'cp2k': 'cp2k -i {input}'}), unsafe))


def test_non_vasp_template_requires_command_placeholder(tmp_path):
    d = _quick_job_dir(tmp_path, 'gaussian', 'water.gjf')
    command = {'gaussian': 'g16 < {input} > {stem}.log'}
    bad = tmp_path / 'bad.sh'
    bad.write_text('#!/bin/bash\nvasp_std\n', encoding='utf-8')
    bad_profile = _profile(
        script_mode='template', template_path=str(bad), engine_commands=command)
    assert any('{command}' in issue and '防止误跑 VASP' in issue
               for issue in submitter.preflight(bad_profile, d))
    with pytest.raises(ValueError, match=r'\{command\}'):
        submitter.build_script_text(bad_profile, d)

    good = tmp_path / 'good.sh'
    good.write_text('#!/bin/bash\ncd {remote_dir}\n{command}\n', encoding='utf-8')
    good_profile = _profile(
        script_mode='template', template_path=str(good), engine_commands=command)
    assert submitter.preflight(good_profile, d) == []
    script = submitter.build_script_text(good_profile, d)
    assert 'g16 < water.gjf > water.log' in script and 'vasp_std' not in script


def test_submit_job_failure_keeps_uploaded(tmp_path):
    d = _job_dir(tmp_path)
    client = FakeClient(script=[('qsub', 'qsub: Unauthorized Request\n')])
    with pytest.raises(RuntimeError, match='提交失败'):
        submitter.submit_job(client, FakeSFTP(), _profile(), d)
    assert manifest.load_manifest(d)['state'] == 'UPLOADED'    # 留痕但不冒充已提交


def test_adopt_external_job_exact_binding_is_idempotent_and_profile_scoped(
        tmp_path, monkeypatch):
    from vcstudio.cluster import ledger

    d = tmp_path / 'adopted'
    d.mkdir()
    data = manifest.new_manifest(
        job_id='external', system='s', task_type='relax', calc_type='slab', inputs={})
    data.update({'cluster': '1w', 'remote_dir': '/work/adopted',
                 'scheduler_job_id': '777'})
    manifest.set_state(data, 'SUBMITTED')
    manifest.save_manifest(d, data)
    registered = []
    monkeypatch.setattr(ledger, 'register', lambda path: registered.append(path) or True)

    same = submitter.adopt_external_job(
        str(d), _profile(), '777', '/work/adopted')

    assert same['scheduler_job_id'] == '777'
    assert registered == [str(d)]
    assert same['cluster_binding']['fingerprint'] == \
        submitter.profile_binding(_profile())['fingerprint']
    assert same['attempts'][-1]['action'] == 'cluster_binding_claim'
    attempt_count = len(same['attempts'])
    again = submitter.adopt_external_job(
        str(d), _profile(), '777', '/work/adopted')
    assert len(again['attempts']) == attempt_count
    with pytest.raises(ValueError, match='与本次.*不同'):
        submitter.adopt_external_job(
            str(d), _profile(name='other'), '777', '/work/adopted')


def test_refresh_job_states(tmp_path):
    d = _job_dir(tmp_path)
    client = FakeClient(script=[('qsub', '8812345.cluster.hpc\n')])
    submitter.submit_job(client, FakeSFTP(), _profile(), d)

    # 排队 → QUEUED
    m = submitter.refresh_job(FakeClient(), _profile(), d, live_states={'8812345': 'QUEUED'})
    assert m['state'] == 'QUEUED'
    # 运行 → RUNNING
    m = submitter.refresh_job(FakeClient(), _profile(), d, live_states={'8812345': 'RUNNING'})
    assert m['state'] == 'RUNNING'
    # 调度器消失 + 收敛 + OSZICAR E0 → DONE + 能量
    done_client = FakeClient(script=[
        ('grep -c', '1\n1\n0\n'),
        ('tail -n 150', '   5 F= -.43561190E+03 E0= -.43561154E+03  d E =-.10E-05\n'),
    ])
    m = submitter.refresh_job(done_client, _profile(), d, live_states={})
    assert m['state'] == 'DONE'
    assert m['results']['energy_e0_eV'] == pytest.approx(-435.61154)
    assert m['results']['diagnosis']['failure_class'] == 'CONVERGED'
    # 消失 + 有输出但未收敛 → NONCONVERGED → UNCONVERGED(可续算)
    d2 = _job_dir(tmp_path / 'second')
    submitter.submit_job(FakeClient(script=[('qsub', '99.cluster\n')]), FakeSFTP(), _profile(), d2)
    m2 = submitter.refresh_job(
        FakeClient(script=[('grep -c', '0\n'), ('stat -c', 'OUTCAR 90000\nOSZICAR 3000\n')]),
        _profile(), d2, live_states={})
    assert m2['state'] == 'UNCONVERGED'
    assert m2['results']['diagnosis']['failure_class'] == 'NONCONVERGED'


def test_run_cmd_check_raises_on_nonzero_exit():
    client = FakeClient(exit_codes={'mkdir': 1})
    with pytest.raises(RuntimeError, match='mkdir'):
        submitter.run_cmd(client, 'mkdir -p /nope', check=True)
    submitter.run_cmd(client, 'mkdir -p /nope')        # 默认不查退出码,行为不变


def test_submit_job_stops_when_mkdir_fails(tmp_path):
    """远程 mkdir 失败(无权限等)必须立刻报错,不能静默继续 sftp.put。"""
    d = _job_dir(tmp_path)
    client = FakeClient(script=[('qsub', '88.c\n')], exit_codes={'mkdir': 1})
    sftp = FakeSFTP()
    with pytest.raises(RuntimeError, match='mkdir'):
        submitter.submit_job(client, sftp, _profile(), d)
    assert sftp.uploaded == {}                         # 一个文件都没上传
    assert manifest.load_manifest(d)['state'] == 'CREATED'
    assert submitter._read_submission_recovery(d)['status'] == 'preparing'
    retry = FakeClient()
    with pytest.raises(submitter.UnknownRemoteSubmission,
                       match='远端目录准备阶段'):
        submitter.submit_job(retry, FakeSFTP(), _profile(), d)
    assert retry.commands == []


def test_submit_and_refresh_quote_spaced_remote_dir(tmp_path):
    """remote_root 带空格 → mkdir/qsub/grep/tail 里的路径全部要引号(sftp 是协议路径,不引)。"""
    d = _job_dir(tmp_path)
    prof = _profile(remote_root='/work/my jobs')
    client = FakeClient(script=[('qsub', '77.c\n')])
    sftp = FakeSFTP()
    m = submitter.submit_job(client, sftp, prof, d)

    remote = m['remote_dir']
    assert remote.startswith('/work/my jobs/zn_job--')
    assert m['remote_dir'] == remote
    assert any(f"mkdir '{remote}'" in command for command in client.commands)
    assert any(f"'{remote}/vcs_job.sh'" in c for c in client.commands)
    assert posixpath.join(remote, 'INCAR') in sftp.uploaded
    # 自动脚本里的 cd 同样要引号
    assert f"cd '{remote}'" in sftp.written[posixpath.join(remote, 'vcs_job.sh')]

    done_client = FakeClient(script=[
        ('grep -c', '1\n1\n0\n'),
        ('tail -n 150', '   5 F= -.5E+01 E0= -.5E+01  d E =-.1E-05\n'),
    ])
    m = submitter.refresh_job(done_client, prof, d, live_states={})
    assert m['state'] == 'DONE'
    assert any(f"'{remote}/OUTCAR'" in c for c in done_client.commands)
    assert any(f"'{remote}/OSZICAR'" in c for c in done_client.commands)


def test_refresh_job_no_output_is_needs_human(tmp_path):
    """缺 OUTCAR/OSZICAR(启动即死/缺 POTCAR)→ NO_OUTPUT → NEEDS_HUMAN,不再误当未收敛。"""
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '55.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient(script=[('grep -c', '0\n')])      # stat/log 未脚本 → 空 = 零输出
    m = submitter.refresh_job(client, _profile(), d, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    assert m['results']['diagnosis']['failure_class'] == 'NO_OUTPUT'


def test_refresh_job_sigsegv_is_failed_with_attempt(tmp_path):
    """日志段错误签名 → SIGSEGV → FAILED,并追加一条 failed attempt。"""
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '56.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient(script=[
        ('grep -c', '0\n'),
        ('stat -c', 'OUTCAR 90000\nOSZICAR 3000\n'),
        ('___VCSLOG___', 'EXIT: 139\n___VCSLOG___\nrunning\n1\n1\n1\n'),
    ])
    m = submitter.refresh_job(client, _profile(), d, live_states={})
    assert m['state'] == 'FAILED'
    assert m['results']['diagnosis']['failure_class'] == 'SIGSEGV'
    assert any(a.get('result') == 'failed' for a in m['attempts'])


def test_refresh_job_converged_but_positive_energy_needs_human(tmp_path):
    """收敛串在场但 E0>0(结构重叠)→ BAD_ENERGY → NEEDS_HUMAN(防收敛却是垃圾数入表)。"""
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '57.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient(script=[
        ('grep -c', '1\n1\n0\n'),
        ('tail -n 150', '   5 F= 0.5E+01 E0= 0.5E+01  d E =0.1E-05\n'),
        ('stat -c', 'OUTCAR 90000\nOSZICAR 3000\n'),
    ])
    m = submitter.refresh_job(client, _profile(), d, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    assert m['results']['diagnosis']['failure_class'] == 'BAD_ENERGY'


_VALID_CONTCAR = ('C slab\n1.0\n10 0 0\n0 10 0\n0 0 12\nC O\n2 1\nCartesian\n'
                  '0 0 0\n1 0 0\n0 1 0\n')


def _restartable_job(tmp_path, rounds=0, restartable=True, fclass='NONCONVERGED',
                     state='UNCONVERGED'):
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '100.c\n')]), FakeSFTP(), _profile(), d)
    m = manifest.load_manifest(d)
    manifest.set_state(m, state)                # 模拟已从调度器返回的终态
    m.setdefault('results', {})['diagnosis'] = {'failure_class': fclass, 'restartable': restartable}
    if rounds:
        m['results']['continue_rounds'] = rounds
    manifest.save_manifest(d, m)
    return d


def test_continue_from_contcar_happy(tmp_path):
    d = _restartable_job(tmp_path)
    old = manifest.load_manifest(d)
    old['results'].update({
        'fetched': ['CONTCAR', 'OSZICAR', 'OUTCAR'],
        'fetched_missing': [],
        'missing': ['legacy-name'],
        'fetched_at': '2026-01-01T00:00:00',
        'fetched_job_id': '100',
        'fetched_remote_dir': old['remote_dir'],
    })
    manifest.save_manifest(d, old)
    for name in ('CONTCAR', 'OSZICAR', 'OUTCAR'):
        with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
            f.write('old local result\n')
    client = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '201.c\n')])
    m = submitter.continue_from_contcar(client, _profile(), d)
    assert m['state'] == 'SUBMITTED' and m['scheduler_job_id'] == '201'
    assert m['results']['continue_rounds'] == 1
    att = m['attempts'][-1]
    assert att['action'] == 'contcar_restart' and att['prev_job_id'] == '100' and att['round'] == 1
    assert os.path.isfile(os.path.join(d, 'POSCAR.bak1'))       # 旧 POSCAR 留证
    assert 'cp CONTCAR POSCAR' in ' '.join(client.commands)     # 远端提升 + 冻结 INCAR
    assert any('rm -f WAVECAR CHGCAR' in c for c in client.commands)  # 清混合历史
    assert 'diagnosis' not in m['results']                      # 上一轮诊断已消费,防再次误用
    for key in ('fetched', 'fetched_missing', 'missing', 'fetched_at',
                'fetched_job_id', 'fetched_remote_dir'):
        assert key not in m['results']                           # 新轮尚未下载，旧证据清零
    assert os.path.isfile(os.path.join(d, 'OUTCAR'))             # 旧文件保留作审计，不算新轮证据


def test_continue_rechecks_content_cas_before_any_remote_command(tmp_path):
    d = _restartable_job(tmp_path)
    value = manifest.load_manifest(d)
    incar = os.path.join(d, 'INCAR')
    contcar = os.path.join(d, 'CONTCAR')
    with open(contcar, 'w', encoding='utf-8') as handle:
        handle.write(_VALID_CONTCAR)
    files = {}
    for name in ('job.yaml', 'INCAR', 'CONTCAR'):
        files[name], _size = submitter._file_sha256_size(os.path.join(d, name))
    diagnosis = (value.get('results') or {}).get('diagnosis') or {}
    diagnosis_digest = hashlib.sha256(json.dumps(
        diagnosis, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    expected = {
        'schema': 'vcstudio.repair-cas/v1', 'ledger_job_id': value['job_id'],
        'manifest_job_id': value['job_id'], 'manifest_state': value['state'],
        'scheduler_job_id': value['scheduler_job_id'],
        'diagnosis_sha256': diagnosis_digest,
        'diagnosis_evidence_file': None, 'source_hash': 's' * 64,
        'files': files,
    }
    before = os.stat(incar)
    data = open(incar, 'rb').read()
    assert b'400' in data
    with open(incar, 'wb') as handle:
        handle.write(data.replace(b'400', b'401', 1))
    os.utime(incar, ns=(before.st_atime_ns, before.st_mtime_ns))
    client = FakeClient()

    with pytest.raises(ValueError, match='内容已变化'):
        submitter.continue_from_contcar(
            client, _profile(), d, idempotency_key='trajectory-repair:cas',
            expected_cas=expected)
    assert client.commands == []


def test_correction_linked_continue_replays_before_preview_cas_after_crash(tmp_path):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    value = manifest.load_manifest(d)
    contcar = os.path.join(d, 'CONTCAR')
    with open(contcar, 'w', encoding='utf-8') as handle:
        handle.write(_VALID_CONTCAR)
    files = {}
    for name in ('job.yaml', 'INCAR', 'CONTCAR'):
        files[name], _size = submitter._file_sha256_size(os.path.join(d, name))
    diagnosis = (value.get('results') or {}).get('diagnosis') or {}
    diagnosis_digest = hashlib.sha256(json.dumps(
        diagnosis, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    expected = {
        'schema': 'vcstudio.repair-cas/v1', 'ledger_job_id': value['job_id'],
        'manifest_job_id': value['job_id'], 'manifest_state': value['state'],
        'scheduler_job_id': value['scheduler_job_id'],
        'diagnosis_sha256': diagnosis_digest,
        'diagnosis_evidence_file': None, 'source_hash': 's' * 64,
        'files': files,
    }
    operation_key = 'trajectory-repair:journal-replay'
    correction = {
        'schema': 'vcstudio.correction-journal-link/v1',
        'correction_id': 'a' * 24, 'intent_record_hash': 'b' * 64,
        'plan_id': 'c' * 64, 'plan_token_sha256': 'd' * 64,
        'job_id': value['job_id'], 'ledger_job_id': value['job_id'],
        'manifest_job_id': value['job_id'],
        'cas_anchor_sha256': submitter._job_action_request_sha256(expected),
        'idempotency_key': operation_key,
    }
    first = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '201.c\n')])
    submitted = submitter.continue_from_contcar(
        first, _profile(), d, max_rounds=None, idempotency_key=operation_key,
        expected_cas=expected, correction_binding=correction)
    assert submitted['scheduler_job_id'] == '201'
    assert submitted['results']['continue_rounds'] == \
        submitter.CONTINUE_MAX_ROUNDS + 1
    assert submitted['attempts'][-1]['round_limit_override'] == 'manual-explicit'
    assert submitted['attempts'][-1]['correction'] == correction

    restarted = FakeClient()
    replay = submitter.continue_from_contcar(
        restarted, _profile(), d, max_rounds=None, idempotency_key=operation_key,
        expected_cas=expected, correction_binding=correction)
    assert replay['_continue_replayed'] is True
    assert replay['scheduler_job_id'] == '201'
    assert restarted.commands == []

    wrong_profile = FakeClient()
    with pytest.raises(ValueError, match='属于服务器'):
        submitter.continue_from_contcar(
            wrong_profile, _profile(name='other'), d, max_rounds=None,
            idempotency_key=operation_key,
            expected_cas=expected, correction_binding=correction)
    assert wrong_profile.commands == []

    mismatched = dict(correction, intent_record_hash='f' * 64)
    rejected = FakeClient()
    with pytest.raises(submitter.UnknownRemoteJobOperation,
                       match='correction intent'):
        submitter.continue_from_contcar(
            rejected, _profile(), d, max_rounds=None,
            idempotency_key=operation_key,
            expected_cas=expected, correction_binding=mismatched)
    assert rejected.commands == []

    changed_request = FakeClient()
    with pytest.raises(ValueError, match='不同的续算授权内容'):
        submitter.continue_from_contcar(
            changed_request, _profile(), d,
            idempotency_key=operation_key,
            expected_cas=expected, correction_binding=correction)
    assert changed_request.commands == []

    changed_cas = dict(expected, source_hash='changed-source-hash')
    changed_anchor = FakeClient()
    with pytest.raises(ValueError, match='CAS 锚点不一致'):
        submitter.continue_from_contcar(
            changed_anchor, _profile(), d, max_rounds=None,
            idempotency_key=operation_key,
            expected_cas=changed_cas, correction_binding=correction)
    assert changed_anchor.commands == []


def test_continue_from_contcar_qsub_without_job_id_fails_closed(tmp_path):
    """qsub 未返回可核验号时保留恢复证据，重启后不得自动再发一次。"""
    d = _restartable_job(tmp_path)
    local_poscar = os.path.join(d, 'POSCAR')
    before_poscar = open(local_poscar, encoding='utf-8').read()
    before_manifest = manifest.load_manifest(d)
    client = FakeClient(script=[
        ('cat', _VALID_CONTCAR),
        ('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'),
        ('qsub', 'qsub: submission rejected\n'),
    ])

    with pytest.raises(submitter.UnknownRemoteJobOperation,
                       match='submission rejected') as failure:
        submitter.continue_from_contcar(client, _profile(), d)

    assert failure.value.requires_manual_recovery is True
    assert failure.value.action == 'continue'
    assert open(local_poscar, encoding='utf-8').read() != before_poscar
    assert os.path.isfile(f'{local_poscar}.bak1')
    assert manifest.load_manifest(d) == before_manifest
    assert any('cp CONTCAR POSCAR' in command and
               '.vcstudio_previous_POSCAR' in command
               for command in client.commands)
    journal = submitter._read_job_action_journal(d)
    assert journal['operations'][-1]['status'] == 'unknown_remote_outcome'

    restarted = FakeClient()
    with pytest.raises(submitter.UnknownRemoteJobOperation):
        submitter.continue_from_contcar(restarted, _profile(), d)
    assert restarted.commands == []


def test_continue_refuses_wrong_cluster_before_remote_mutation(tmp_path):
    d = _restartable_job(tmp_path)
    client = FakeClient()
    with pytest.raises(ValueError, match='属于服务器「1w」'):
        submitter.continue_from_contcar(client, _profile(name='other'), d)
    assert client.commands == []


def test_continue_refuses_legacy_name_only_cluster_binding(tmp_path):
    d = _restartable_job(tmp_path)
    data = manifest.load_manifest(d)
    data.pop('cluster_binding', None)
    manifest.save_manifest(d, data)
    client = FakeClient()

    with pytest.raises(ValueError, match='缺服务器端点绑定'):
        submitter.continue_from_contcar(
            client, _profile(hostname='other.example'), d,
            max_rounds=None,
            idempotency_key='legacy-profile-binding-001')

    assert client.commands == []
    assert not os.path.exists(os.path.join(d, '.vcstudio-job-actions.json'))


def test_bands_continue_preserves_required_chgcar(tmp_path):
    d = _restartable_job(tmp_path)
    data = manifest.load_manifest(d)
    data['task_type'] = 'bands'
    manifest.save_manifest(d, data)
    client = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '203.c\n')])
    submitter.continue_from_contcar(client, _profile(), d)
    commands = ' '.join(client.commands)
    assert 'rm -f WAVECAR' in commands
    assert 'rm -f WAVECAR CHGCAR' not in commands


def test_generic_icharg_restart_preserves_required_chgcar(tmp_path):
    d = _restartable_job(tmp_path)
    with open(os.path.join(d, 'INCAR'), 'a', encoding='utf-8') as handle:
        handle.write('ICHARG = 1\n')
    # This fixture models a generation that was originally submitted with
    # ICHARG=1; update all three authority copies instead of mutating it in flight.
    digest = manifest.sha256_file(os.path.join(d, 'INCAR'))
    data = manifest.load_manifest(d)
    data['inputs']['sha256']['INCAR'] = digest
    data['attempts'][-1]['incar_sha256'] = digest
    data['execution_authority']['current_incar_sha256'] = digest
    manifest.save_manifest(d, data)
    client = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '205.c\n')])
    submitter.continue_from_contcar(client, _profile(), d)
    commands = ' '.join(client.commands)
    assert 'rm -f WAVECAR' in commands
    assert 'rm -f WAVECAR CHGCAR' not in commands


def test_generic_neb_continue_is_blocked_without_remote_commands(tmp_path):
    d = _restartable_job(tmp_path)
    data = manifest.load_manifest(d)
    data['task_type'] = 'neb'
    manifest.save_manifest(d, data)
    client = FakeClient()
    with pytest.raises(ValueError, match='NEB 不能使用通用 CONTCAR 续算'):
        submitter.continue_from_contcar(client, _profile(), d)
    assert client.commands == []


def test_continue_with_incar_changes_clears_fetch_evidence(tmp_path):
    d = _restartable_job(tmp_path)
    old = manifest.load_manifest(d)
    old['results'].update({
        'fetched': ['OUTCAR'], 'fetched_missing': ['CONTCAR'],
        'fetched_at': '2026-01-01T00:00:00', 'fetched_job_id': '100',
        'fetched_remote_dir': old['remote_dir'],
    })
    manifest.save_manifest(d, old)

    client = FakeClient(script=[
        ('cat', _VALID_CONTCAR),
        ('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'),
        ('qsub', '202.c\n'),
    ])
    m = submitter.continue_with_incar_changes(
        client, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'})

    assert m['scheduler_job_id'] == '202'
    assert m['results']['continue_rounds'] == 1
    for key in ('fetched', 'fetched_missing', 'missing', 'fetched_at',
                'fetched_job_id', 'fetched_remote_dir'):
        assert key not in m['results']


def test_tuned_continue_without_job_id_retains_unknown_gate(tmp_path):
    """未取得作业号时不能假定拒绝并重试；持久 journal 必须阻断第二次 qsub。"""
    d = _restartable_job(tmp_path)
    local_incar = os.path.join(d, 'INCAR')
    local_poscar = os.path.join(d, 'POSCAR')
    before_incar = open(local_incar, encoding='utf-8').read()
    before_poscar = open(local_poscar, encoding='utf-8').read()
    before_manifest = manifest.load_manifest(d)
    client = FakeClient(script=[
        ('cat', _VALID_CONTCAR),
        ('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'),
        ('qsub', 'qsub: submission rejected\n'),
    ])
    sftp = FakeSFTP()

    with pytest.raises(submitter.UnknownRemoteJobOperation,
                       match='submission rejected'):
        submitter.continue_with_incar_changes(
            client, sftp, _profile(), d, {'ALGO': 'Normal'})

    assert open(local_incar, encoding='utf-8').read() != before_incar
    assert open(local_poscar, encoding='utf-8').read() != before_poscar
    assert os.path.isfile(f'{local_incar}.bak1')
    assert os.path.isfile(f'{local_poscar}.bak1')
    assert manifest.load_manifest(d) == before_manifest
    assert any(remote.endswith('/INCAR') for remote in sftp.uploaded)
    archive = next(command for command in client.commands
                   if '.vcstudio_previous_INCAR' in command and 'for f in' in command)
    assert '.vcstudio_previous_POSCAR' in archive
    assert 'cp CONTCAR POSCAR' in archive
    journal = submitter._read_job_action_journal(d)
    assert journal['operations'][-1]['action'] == 'tune_continue'
    assert journal['operations'][-1]['status'] == 'unknown_remote_outcome'

    restarted = FakeClient()
    with pytest.raises(submitter.UnknownRemoteJobOperation):
        submitter.continue_with_incar_changes(
            restarted, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'})
    assert restarted.commands == []


def test_bands_tuned_restart_also_preserves_chgcar(tmp_path):
    d = _restartable_job(tmp_path)
    data = manifest.load_manifest(d)
    data['task_type'] = 'bands'
    manifest.save_manifest(d, data)
    client = FakeClient(script=[
        ('cat', _VALID_CONTCAR),
        ('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'),
        ('qsub', '204.c\n'),
    ])
    submitter.continue_with_incar_changes(
        client, FakeSFTP(), _profile(), d, {'ALGO': 'Normal'})
    commands = ' '.join(client.commands)
    assert 'rm -f WAVECAR' in commands
    assert 'rm -f WAVECAR CHGCAR' not in commands


def test_fetch_results_records_current_job_and_missing(tmp_path):
    d = _job_dir(tmp_path)
    submitter.submit_job(
        FakeClient(script=[('qsub', '301.cluster\n')]), FakeSFTP(), _profile(), d)
    before = manifest.load_manifest(d)
    before.setdefault('results', {})['missing'] = ['stale-old-round']
    manifest.set_state(before, 'DONE', note='test remote completion')
    manifest.save_manifest(d, before)

    fetched, missing = submitter.fetch_results(
        FakeClient(), FakeSFTP(missing=('OUTCAR',)), d)

    assert fetched == ['CONTCAR', 'OSZICAR', 'vasprun.xml', 'CHGCAR']
    assert missing == ['OUTCAR']
    updated = manifest.load_manifest(d)
    results = updated['results']
    assert results['fetched'] == fetched
    assert results['fetched_missing'] == missing
    assert results['fetched_at']
    assert results['fetched_job_id'] == '301'
    assert results['fetched_remote_dir'] == updated['remote_dir']
    assert results['fetched_state'] == 'DONE'
    assert results['fetched_attempt_token'] == submitter.current_attempt_token(updated)
    assert 'missing' not in results


def test_continue_refuses_live_job(tmp_path):
    """状态门:仍在队列/运行中的作业绝不续算(防往活作业双提交冲垮结果)。"""
    d = _restartable_job(tmp_path, state='RUNNING')
    with pytest.raises(ValueError, match='仍在队列/运行中'):
        submitter.continue_from_contcar(FakeClient(), _profile(), d)


def test_continue_consumes_diagnosis_blocks_double(tmp_path):
    """续算后再点一次:诊断已消费(restartable 不在)→ 按不可续算拒绝,不重复出手。"""
    d = _restartable_job(tmp_path)
    client = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '201.c\n')])
    submitter.continue_from_contcar(client, _profile(), d)
    # 新作业还是 SUBMITTED,状态门先拦;即便人为置终态,诊断也已被消费
    m = manifest.load_manifest(d)
    manifest.set_state(m, 'UNCONVERGED')
    manifest.save_manifest(d, m)
    with pytest.raises(ValueError, match='不可自动续算'):
        submitter.continue_from_contcar(FakeClient(), _profile(), d)


def test_continue_refuses_non_restartable(tmp_path):
    d = _restartable_job(tmp_path, restartable=False, fclass='SIGSEGV', state='FAILED')
    with pytest.raises(ValueError, match='不可自动续算'):
        submitter.continue_from_contcar(FakeClient(), _profile(), d)


def test_continue_refuses_at_round_cap(tmp_path):
    d = _restartable_job(tmp_path, rounds=3)
    with pytest.raises(RuntimeError, match='上限'):
        submitter.continue_from_contcar(FakeClient(), _profile(), d, max_rounds=3)


def test_continue_rejects_local_incar_drift_from_authorized_generation(tmp_path):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    with open(os.path.join(d, 'INCAR'), 'a', encoding='utf-8') as handle:
        handle.write('ENCUT = 999\nISPIN = 2\n')
    client = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', 'must-not-run\n')])

    with pytest.raises(ValueError, match='权威摘要'):
        submitter.continue_from_contcar(
            client, _profile(), d, max_rounds=None,
            idempotency_key='manual-authority-drift-001')

    assert client.commands == []
    assert not os.path.exists(os.path.join(d, '.vcstudio-job-actions.json'))


def test_legacy_continuation_without_reconstructable_incar_authority_fails_closed(
        tmp_path):
    d = _restartable_job(tmp_path)
    data = manifest.load_manifest(d)
    data.pop('execution_authority', None)
    data['attempts'].append({
        'action': 'contcar_restart', 'job_id': data['scheduler_job_id'],
        'prev_job_id': '99', 'round': 1,
    })
    manifest.save_manifest(d, data)
    client = FakeClient()

    with pytest.raises(ValueError, match='缺执行代次 INCAR 权威摘要'):
        submitter.continue_from_contcar(client, _profile(), d)
    assert client.commands == []


def test_explicit_manual_continue_can_exceed_automatic_round_cap(tmp_path):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    client = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '204.c\n')])

    updated = submitter.continue_from_contcar(
        client, _profile(), d, max_rounds=None)

    assert updated['results']['continue_rounds'] == submitter.CONTINUE_MAX_ROUNDS + 1
    assert updated['attempts'][-1]['round_limit_override'] == 'manual-explicit'
    assert '人工确认超出自动上限' in updated['state_history'][-1]['note']
    journal = submitter._read_job_action_journal(d)
    request = journal['operations'][-1]['request']
    assert request['round_limit_override'] == 'manual-explicit'
    assert request['source_scheduler_job_id'] == '100'
    assert request['source_round'] == submitter.CONTINUE_MAX_ROUNDS
    assert request['intent']['round_policy'] == 'manual-unbounded'
    assert request['contcar_source_sha256']
    assert request['incar_source_sha256']


def test_old_continue_key_cannot_replay_after_scheduler_id_reuse(tmp_path):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    key = 'manual-scheduler-generation-reuse-001'
    first = submitter.continue_from_contcar(
        FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '204.c\n')]),
        _profile(), d, max_rounds=None, idempotency_key=key)
    old_transaction = first['attempts'][-1]['operation_transaction_id']

    # A later scheduler generation reuses the same visible job id.  The old
    # journal key must not be replayable merely because the numeric id matches.
    current = manifest.load_manifest(d)
    current['attempts'].append({
        'n': len(current['attempts']) + 1,
        'at': '2026-08-21T12:00:00',
        'action': 'external_generation',
        'job_id': '204',
        'attempt_token': 'new-generation-token',
        'incar_sha256': current['execution_authority'][
            'current_incar_sha256'],
    })
    current['execution_authority']['transaction_id'] = 'new-generation-tx'
    manifest.set_state(current, 'UNCONVERGED')
    current.setdefault('results', {})['diagnosis'] = {
        'failure_class': 'NONCONVERGED', 'restartable': True}
    manifest.save_manifest(d, current)

    retry = FakeClient()
    with pytest.raises(submitter.UnknownRemoteJobOperation,
                       match='当前作业代次不一致'):
        submitter.continue_from_contcar(
            retry, _profile(), d, max_rounds=None,
            idempotency_key=key)

    assert retry.commands == []
    assert old_transaction != current['execution_authority']['transaction_id']


def test_manual_continue_manifest_failure_retains_durable_authorization(
        tmp_path, monkeypatch):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    key = 'manual-continue-crash-001'
    real_save = submitter.manifest_mod.save_manifest

    def fail_final(path, payload):
        if (payload.get('state') == 'SUBMITTED'
                and str(payload.get('scheduler_job_id') or '') == '501'):
            raise OSError('disk full')
        return real_save(path, payload)

    monkeypatch.setattr(submitter.manifest_mod, 'save_manifest', fail_final)
    with pytest.raises(submitter.UnknownRemoteJobOperation, match='job.yaml'):
        submitter.continue_from_contcar(
            FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '501.c\n')]),
            _profile(), d, max_rounds=None, idempotency_key=key)

    record = submitter._read_job_action_journal(d)['operations'][-1]
    assert record['status'] == 'remote_accepted'
    assert record['request']['round_limit_override'] == 'manual-explicit'
    assert record['request']['source_scheduler_job_id'] == '100'
    assert record['request']['intent']['round_policy'] == 'manual-unbounded'
    assert record['request_sha256']
    assert record['intent_sha256']

    monkeypatch.setattr(submitter.manifest_mod, 'save_manifest', real_save)
    retry = FakeClient()
    with pytest.raises(submitter.UnknownRemoteJobOperation):
        submitter.continue_from_contcar(
            retry, _profile(), d, max_rounds=None, idempotency_key=key)
    assert retry.commands == []


def test_manual_continue_hash_guards_are_in_the_submit_transaction(tmp_path):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    client = FakeClient(script=[('cat', _VALID_CONTCAR), ('qsub', '502.c\n')])

    submitter.continue_from_contcar(
        client, _profile(), d, max_rounds=None,
        idempotency_key='manual-continue-hash-001')

    record = submitter._read_job_action_journal(d)['operations'][-1]
    request = record['request']
    command = next(item for item in client.commands if 'qsub' in item)
    assert command.count('sha256sum -c -') == 4
    assert request['incar_source_sha256'] in command
    assert command.count(request['contcar_source_sha256']) == 2
    assert command.index('sha256sum -c -') < command.index('cp CONTCAR POSCAR')
    assert command.rindex('sha256sum -c -') < command.index('qsub')


def test_manual_continue_hash_precondition_failure_blocks_replay(tmp_path):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    before = manifest.load_manifest(d)
    client = FakeClient(
        script=[('cat', _VALID_CONTCAR), ('qsub', 'must-not-accept.c\n')],
        exit_codes={'sha256sum -c -': 1})

    with pytest.raises(submitter.UnknownRemoteJobOperation, match='事务中断'):
        submitter.continue_from_contcar(
            client, _profile(), d, max_rounds=None,
            idempotency_key='manual-continue-hash-fail-001')

    assert manifest.load_manifest(d) == before
    record = submitter._read_job_action_journal(d)['operations'][-1]
    assert record['status'] == 'unknown_remote_outcome'
    assert 'sha256sum -c -' in next(
        item for item in client.commands if 'qsub' in item)

    retry = FakeClient()
    with pytest.raises(submitter.UnknownRemoteJobOperation):
        submitter.continue_from_contcar(
            retry, _profile(), d, max_rounds=None,
            idempotency_key='manual-continue-hash-fail-001')
    assert retry.commands == []


def test_continue_refuses_invalid_contcar(tmp_path):
    d = _restartable_job(tmp_path)
    client = FakeClient(script=[('cat', 'garbage\nshort\n')])   # CONTCAR 不完整
    with pytest.raises(RuntimeError, match='CONTCAR'):
        submitter.continue_from_contcar(client, _profile(), d)


@pytest.mark.parametrize('contcar', [
    ('singular\n1.0\n1 0 0\n2 0 0\n0 0 10\nC\n2\nDirect\n'
     '0 0 0\n0.5 0.5 0.5\n'),
    ('nonfinite\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n2\nDirect\n'
     '0 0 0\nnan 0.5 0.5\n'),
])
def test_continue_refuses_unverifiable_contcar_geometry(tmp_path, contcar):
    d = _restartable_job(tmp_path, rounds=submitter.CONTINUE_MAX_ROUNDS)
    client = FakeClient(script=[('cat', contcar), ('qsub', 'must-not-run\n')])

    with pytest.raises(RuntimeError, match='有限|非奇异'):
        submitter.continue_from_contcar(
            client, _profile(), d, max_rounds=None,
            idempotency_key='manual-geometry-reject-001')

    assert not any('qsub' in command for command in client.commands)
    assert not os.path.exists(os.path.join(d, '.vcstudio-job-actions.json'))


def test_continue_and_tune_require_source_scheduler_generation(tmp_path):
    d = _restartable_job(tmp_path)
    data = manifest.load_manifest(d)
    data['scheduler_job_id'] = None
    manifest.save_manifest(d, data)

    standard = FakeClient()
    with pytest.raises(ValueError, match='scheduler_job_id'):
        submitter.continue_from_contcar(standard, _profile(), d)
    tuned = FakeClient()
    with pytest.raises(ValueError, match='scheduler_job_id'):
        submitter.continue_with_incar_changes(
            tuned, FakeSFTP(), _profile(), d, {'NELM': '120'},
            max_rounds=None, restart_from_contcar=False)
    assert standard.commands == []
    assert tuned.commands == []


# CONTCAR 通过 valid_poscar 但存在原子重叠(C-C 0.3 Å < 0.7 Å)
_OVERLAP_CONTCAR = ('C slab\n1.0\n10 0 0\n0 10 0\n0 0 12\nC O\n2 1\nCartesian\n'
                    '0 0 0\n0.3 0 0\n5 5 5\n')


def test_continue_refuses_overlapping_contcar(tmp_path):
    """几何健全:CONTCAR 原子重叠(最小间距 < 0.7 Å)→ 拒续算,manifest 转 NEEDS_HUMAN。"""
    d = _restartable_job(tmp_path)
    client = FakeClient(script=[('cat', _OVERLAP_CONTCAR), ('qsub', '201.c\n')])
    with pytest.raises(RuntimeError, match='原子重叠'):
        submitter.continue_from_contcar(client, _profile(), d)
    m = manifest.load_manifest(d)
    assert m['state'] == 'NEEDS_HUMAN'
    assert '几何病态' in m['state_history'][-1].get('note', '')
    # 未重投:没执行 cp/清历史/qsub(在几何检查处就拦下)
    joined = ' '.join(client.commands)
    assert 'cp CONTCAR POSCAR' not in joined and 'rm -f WAVECAR' not in joined
    assert m['scheduler_job_id'] == '100'              # 仍是旧作业号,未推进


def _continue_client(qsub_id='201.c\n', base_mtime='1000'):
    """续算用假 client:回放 CONTCAR + 基线 stat(旧 OUTCAR mtime)+ qsub 新号。"""
    return FakeClient(script=[
        ('cat', _VALID_CONTCAR),
        ('stat -c', f'OUTCAR 90000 {base_mtime}\nOSZICAR 3000 {base_mtime}\n'),
        ('qsub', qsub_id),
    ])


def test_continue_then_still_queued_stays_submitted(tmp_path):
    """修复回归:续算重投后新作业还没进 qstat(GONE)、OUTCAR 未刷新 →
    保持 SUBMITTED(疑仍在排队),绝不拿上一轮旧 OUTCAR 误判终态。

    这正是用户报告的 bug:续算后作业还在排队,却显示已完成/需续算/SCF sloshing,
    因为旧 OUTCAR 的收敛/震荡串被当成了本轮结果。"""
    d = _restartable_job(tmp_path)
    submitter.continue_from_contcar(_continue_client(), _profile(), d)
    # 新号 201 不在 qstat(GONE)，旧轮输出已归档且本轮尚无 OUTCAR。
    refresh_client = FakeClient(script=[('stat -c', '')])
    m = submitter.refresh_job(refresh_client, _profile(), d, live_states={})
    assert m['state'] == 'SUBMITTED'              # 仍视为在途,未误判终态
    assert m['results'].get('settling')           # 记录了沉降原因(可供前端提示)
    assert 'diagnosis' not in m['results']         # 没有落任何终态诊断


def test_continue_then_new_run_rewrote_outcar_classifies(tmp_path):
    """续算后新一轮确实跑过(OUTCAR mtime 相对基线变化)再消失 → 正常判终态。"""
    d = _restartable_job(tmp_path)
    submitter.continue_from_contcar(_continue_client(base_mtime='1000'), _profile(), d)
    # 新一轮把 OUTCAR 重写到 mtime=2000,收敛,GONE → DONE
    refresh_client = FakeClient(script=[
        ('stat -c', 'OUTCAR 95000 2000\nOSZICAR 3200 2000\n'),
        ('grep -c', '1\n1\n0\n'),
        ('tail -n 150', '   5 F= -.5E+01 E0= -.5E+01  d E =-.1E-05\n'),
    ])
    m = submitter.refresh_job(refresh_client, _profile(), d, live_states={})
    assert m['state'] == 'DONE'
    assert 'settling' not in m['results']


def test_continue_seen_running_then_gone_classifies(tmp_path):
    """续算后真正进入 RUNNING，再消失时才可放行终态取证。"""
    d = _restartable_job(tmp_path)
    submitter.continue_from_contcar(_continue_client(), _profile(), d)
    submitter.refresh_job(FakeClient(), _profile(), d, live_states={'201': 'RUNNING'})
    # 再消失,OUTCAR mtime 未变，但已有本轮 RUNNING 证据 → 允许取证。
    refresh_client = FakeClient(script=[('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'),
                                        ('grep -c', '0\n')])
    m = submitter.refresh_job(refresh_client, _profile(), d, live_states={})
    assert m['state'] != 'SUBMITTED'
    assert 'settling' not in m['results']


def test_continue_seen_only_queued_never_reuses_old_outcar(tmp_path):
    """QUEUED 只证明进过队列；未见 RUNNING/新输出时不得复用旧 OUTCAR。"""
    d = _restartable_job(tmp_path)
    submitter.continue_from_contcar(_continue_client(), _profile(), d)
    submitter.refresh_job(FakeClient(), _profile(), d, live_states={'201': 'QUEUED'})
    # 旧轮输出已归档；工作目录尚无本轮 OUTCAR。
    stale = [('stat -c', '')]
    m = submitter.refresh_job(FakeClient(script=stale), _profile(), d, live_states={})
    assert m['state'] == 'QUEUED'
    assert m['results']['settling']['checks'] == 1
    assert 'diagnosis' not in m['results']


def test_continue_settling_cap_stops_for_human_without_old_output(tmp_path):
    """连续达上限仍 GONE+无本轮 OUTCAR 时转人工，不得放行旧轮终态取证。"""
    d = _restartable_job(tmp_path)
    submitter.continue_from_contcar(_continue_client(), _profile(), d)
    stale = [('stat -c', '')]
    last = None
    for _ in range(submitter.SETTLE_MAX_CHECKS):
        last = submitter.refresh_job(FakeClient(script=stale), _profile(), d, live_states={})
    assert last['state'] == 'NEEDS_HUMAN'
    assert last['results']['diagnosis']['failure_class'] == 'CONTINUE_RUN_NOT_OBSERVED'
    assert last['results'].get('energy_e0_eV') is None


def test_fresh_submit_gone_still_classifies_immediately(tmp_path):
    """守恒:非续算的新提交作业(无旧 OUTCAR 隐患)行为不变——GONE 即刻取证分类。"""
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '77.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient(script=[('grep -c', '0\n')])   # 零输出
    m = submitter.refresh_job(client, _profile(), d, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'                  # 与修复前一致,未被沉降护栏拦住


def test_grep_converged_static_uses_electronic_mark(tmp_path):
    """static/dos/band(NSW=0)永远不出'reached required accuracy'(离子标志),
    改查电子收敛标志,否则收敛的静态作业被误判未收敛(review-round2 收尾)。"""
    client = FakeClient(script=[('grep -c', '1\n1\n0\n')])
    submitter._grep_converged(client, '/w/j', 'static')
    assert 'aborting loop because EDIFF is reached' in client.commands[-1]
    client2 = FakeClient(script=[('grep -c', '1\n1\n0\n')])
    submitter._grep_converged(client2, '/w/j', 'relax')
    assert 'reached required accuracy' in client2.commands[-1]


@pytest.mark.parametrize('task_type', [
    'static', 'dos_pdos', 'bands', 'bader', 'chgdiff', 'elf', 'eos',
    'workfunction', 'vaspsol', 'conv_scan',
])
def test_all_electronic_tasks_use_ediff_completion(task_type):
    client = FakeClient(script=[('grep -c', '1\n1\n0\n')])
    converged, _clean, _stopped = submitter._grep_marks(client, '/w/j', task_type)
    assert converged is True
    assert 'aborting loop because EDIFF is reached' in client.commands[-1]


def test_static_marker_without_clean_footer_is_not_done(tmp_path):
    """旧收敛串 + 截断 OUTCAR 必须交人工，不得 DONE/自动续算。"""
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '63.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient(script=[
        ('grep -c', '1\n0\n0\n'),
        ('stat -c', 'OUTCAR 90000\nOSZICAR 3000\n'),
        ('tail -n 150', '1 F= -10 E0= -10 d E=0\n'),
    ])
    m = submitter.refresh_job(client, _profile(), d, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    assert m['results']['diagnosis']['failure_class'] == 'UNKNOWN'
    assert m['results']['diagnosis']['clean_exit'] is False
    assert m['results']['diagnosis']['restartable'] is False


def test_clean_footer_with_nonzero_wrapper_exit_is_not_done(tmp_path):
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '64.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient(script=[
        ('grep -c', '1\n1\n0\n'),
        ('stat -c', 'OUTCAR 90000\nOSZICAR 3000\n'),
        ('___VCSLOG___', 'EXIT: 1\n___VCSLOG___\n'),
        ('tail -n 150', '1 F= -10 E0= -10 d E=0\n'),
    ])
    m = submitter.refresh_job(client, _profile(), d, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    assert m['results']['diagnosis']['failure_class'] == 'UNKNOWN'
    assert m['results']['diagnosis']['exit_code'] == 1


def test_frequency_requires_modes_and_clean_footer():
    complete = FakeClient(script=[('grep -c', '6\n1\n0\n')])
    assert submitter._grep_marks(complete, '/w/freq', 'freq') == (True, True, False)
    assert 'THz' in complete.commands[-1]

    partial = FakeClient(script=[('grep -c', '6\n0\n0\n')])
    assert submitter._grep_marks(partial, '/w/freq', 'freq')[0] is False


def test_aimd_requires_requested_step_count_and_clean_footer():
    short = FakeClient(script=[('grep -c', '99\n1\n0\n')])
    assert submitter._grep_marks(
        short, '/w/md', 'aimd', expected_steps=100)[0] is False
    assert '/OSZICAR' in short.commands[-1] and 'F=' in short.commands[-1]

    complete = FakeClient(script=[('grep -c', '100\n1\n0\n')])
    assert submitter._grep_marks(
        complete, '/w/md', 'aimd', expected_steps=100) == (True, True, False)


def test_static_running_does_not_emit_false_zero_ionic_step_warning(tmp_path):
    d = _job_dir(tmp_path)  # helper 的 INCAR 无 NSW，规范推断为 static
    submitter.submit_job(FakeClient(script=[('qsub', '62.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient()
    m = submitter.refresh_job(client, _profile(), d, live_states={'62': 'RUNNING'})
    assert m['state'] == 'RUNNING'
    assert m['results']['live']['progress_kind'] == 'static'
    assert m['results']['live']['warning'] == ''
    assert not any('___VCSLIVE___' in cmd for cmd in client.commands)


def test_non_vasp_terminal_uses_own_normal_footer_and_energy(tmp_path):
    d = _quick_job_dir(tmp_path, 'cp2k', 'water.inp')
    profile = _profile(engine_commands={
        'cp2k': 'cp2k.psmp -i {input} -o {stem}.out',
    })
    submitter.submit_job(
        FakeClient(script=[('qsub', '901.cluster\n')]), FakeSFTP(), profile, d)
    output = ('2400\n___VCSENGINE___\n'
              ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -10.000000\n'
              ' PROGRAM ENDED AT 2026-07-20 12:00:00\n')
    client = FakeClient(script=[
        ('___VCSENGINE___', output),
        ('___VCSLOG___', 'EXIT: 0\n___VCSLOG___\n'),
    ])
    m = submitter.refresh_job(client, profile, d, live_states={})
    assert m['state'] == 'DONE'
    assert m['results']['diagnosis']['engine'] == 'cp2k'
    assert m['results']['diagnosis']['failure_class'] == 'CONVERGED'
    assert m['results']['energy_e0_eV'] == pytest.approx(-272.11386245988)
    assert not any('/OUTCAR' in cmd or '/OSZICAR' in cmd for cmd in client.commands)


def test_non_vasp_output_without_normal_footer_needs_human(tmp_path):
    d = _quick_job_dir(tmp_path, 'gaussian', 'mol.gjf')
    profile = _profile(engine_commands={'gaussian': 'g16 < {input} > {stem}.log'})
    submitter.submit_job(
        FakeClient(script=[('qsub', '902.cluster\n')]), FakeSFTP(), profile, d)
    client = FakeClient(script=[
        ('___VCSENGINE___', '500\n___VCSENGINE___\nSCF Done: E(RHF) = -2.0\n'),
        ('___VCSLOG___', 'EXIT: 0\n___VCSLOG___\n'),
    ])
    m = submitter.refresh_job(client, profile, d, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    assert m['results']['diagnosis']['failure_class'] == 'NORMAL_TERMINATION_NOT_FOUND'


def test_non_vasp_normal_footer_does_not_fake_relax_convergence(tmp_path):
    d = _quick_job_dir(tmp_path, 'cp2k', 'water.inp')
    data = manifest.load_manifest(d)
    data['inputs']['task'] = 'relax'
    manifest.save_manifest(d, data)
    profile = _profile(engine_commands={
        'cp2k': 'cp2k.psmp -i {input} -o {stem}.out',
    })
    submitter.submit_job(
        FakeClient(script=[('qsub', '903.cluster\n')]), FakeSFTP(), profile, d)
    output = ('2400\n___VCSENGINE___\n'
              ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -10.0\n'
              ' PROGRAM ENDED AT 2026-07-20\n')
    client = FakeClient(script=[
        ('___VCSENGINE___', output),
        ('___VCSLOG___', 'EXIT: 0\n___VCSLOG___\n'),
    ])
    result = submitter.refresh_job(client, profile, d, live_states={})
    assert result['state'] == 'UNCONVERGED'
    assert result['results']['diagnosis']['failure_class'] == 'TASK_NOT_CONVERGED'
    assert 'energy_e0_eV' not in result['results']
    assert result['results']['raw_energy_e0_eV'] == pytest.approx(-272.11386)


def test_non_vasp_output_failure_marker_beats_footer_and_energy(tmp_path):
    d = _quick_job_dir(tmp_path, 'cp2k', 'water.inp')
    profile = _profile(engine_commands={
        'cp2k': 'cp2k.psmp -i {input} -o {stem}.out',
    })
    submitter.submit_job(
        FakeClient(script=[('qsub', '904.cluster\n')]), FakeSFTP(), profile, d)
    output = ('2400\n___VCSENGINE___\n'
              ' ENERGY| Total FORCE_EVAL ( QS ) energy [a.u.]: -10.0\n'
              ' *** SCF run NOT converged ***\n PROGRAM ENDED AT 2026-07-20\n')
    client = FakeClient(script=[
        ('___VCSENGINE___', output),
        ('___VCSLOG___', 'EXIT: 0\n___VCSLOG___\n'),
    ])
    result = submitter.refresh_job(client, profile, d, live_states={})
    assert result['state'] == 'FAILED'
    assert result['results']['diagnosis']['failure_class'] == 'ENGINE_OUTPUT_ERROR'
    assert 'energy_e0_eV' not in result['results']


def test_non_vasp_cannot_enter_vasp_contcar_restart_even_if_manifest_is_tampered(tmp_path):
    d = _quick_job_dir(tmp_path, 'cp2k', 'water.inp')
    data = manifest.load_manifest(d)
    data.update({'cluster': '1w', 'remote_dir': '/work/cp2k',
                 'scheduler_job_id': '1', 'state': 'UNCONVERGED'})
    data.setdefault('results', {})['diagnosis'] = {
        'failure_class': 'WALLTIME', 'restartable': True}
    manifest.save_manifest(d, data)
    client = FakeClient()
    with pytest.raises(ValueError, match='不能走 VASP CONTCAR'):
        submitter.continue_from_contcar(client, _profile(), d)
    assert client.commands == []


def test_read_log_targets_job_number(tmp_path):
    """续算后旧 .o<旧号> 仍在:_read_log 按本作业号精确定位,不用宽通配 *.o*(否则误读旧轮)。"""
    client = FakeClient()
    submitter._read_log(client, '/work/j', '8812345')
    cmd = ' '.join(client.commands)
    assert '*.o8812345' in cmd and 'slurm-8812345.out' in cmd
    assert '*.o*' not in cmd


def test_query_states_raises_on_failed_query(tmp_path):
    """qstat/squeue 抖动(无哨兵)→ 抛错本轮跳过,绝不把空输出误判成作业全终态。"""
    client = FakeClient(script=[('qstat', 'qstat: cannot connect to server\n')])
    with pytest.raises(RuntimeError, match='查询失败'):
        submitter.query_states(client, _profile())


def test_query_states_empty_with_sentinel_is_no_jobs(tmp_path):
    """成功但用户无在跑作业(有哨兵)→ 空 dict,不误报失败。"""
    client = FakeClient(script=[('qstat', '___VCSQOK___\n')])
    assert submitter.query_states(client, _profile()) == {}


def test_query_scheduler_returns_terminal_reasons(tmp_path):
    """Slurm 终态原因(TO/CA/NF/OOM)被捕获并随状态一起返回(审查#1 接线)。"""
    prof = _profile()
    prof.scheduler = 'Slurm'
    client = FakeClient(script=[('squeue', '101|R\n102|TO\n103|CA\n___VCSQOK___\n')])
    states, reasons = submitter.query_scheduler(client, prof)
    assert states == {'101': 'RUNNING'}
    assert reasons == {'102': 'TIMEOUT', '103': 'CANCELLED'}


def test_refresh_job_timeout_reason_beats_exit137(tmp_path):
    """超墙钟:调度器 TIMEOUT + 退出码 137 → WALLTIME 可续算,不误判 OOM(审查#8)。"""
    d = _job_dir(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '77.c\n')]), FakeSFTP(), _profile(), d)
    client = FakeClient(script=[
        ('grep -c', '0\n'),
        ('stat -c', 'OUTCAR 90000\nOSZICAR 3000\n'),
        ('___VCSLOG___', 'EXIT: 137\n___VCSLOG___\nrunning\n'),
    ])
    m = submitter.refresh_job(client, _profile(), d, live_states={},
                              terminal_reasons={'77': 'TIMEOUT'})
    assert m['results']['diagnosis']['failure_class'] == 'WALLTIME'
    assert m['results']['diagnosis']['restartable'] is True
    assert m['state'] == 'UNCONVERGED'


def test_build_script_text_template_mode(tmp_path):
    d = _job_dir(tmp_path)
    tpl = tmp_path / 'my_job.sh'
    tpl.write_text('#!/bin/bash\n#PBS -N {short}\ncd X/{dir}\nmpirun -np {cores} vasp\n',
                   encoding='utf-8')
    prof = _profile(script_mode='template', template_path=str(tpl))
    text = submitter.build_script_text(prof, d)
    leaf = posixpath.basename(submitter._spec_for(prof, d).remote_dir)
    assert '#PBS -N zn_job' in text and f'cd X/{leaf}' in text and '-np 12' in text


def test_legacy_vasp_template_remains_valid_without_command_placeholder(tmp_path):
    d = _job_dir(tmp_path)
    tpl = tmp_path / 'legacy_vasp.sh'
    tpl.write_text('#!/bin/bash\n#PBS -q batch\nmpirun -np 24 vasp_std\n',
                   encoding='utf-8')
    prof = _profile(
        ppn=0, vasp_cmd='mpirun -np {cores} vasp_std',
        script_mode='template', template_path=str(tpl))

    assert submitter.preflight(prof, d) == []
    assert 'mpirun -np 24 vasp_std' in submitter.build_script_text(prof, d)


def test_refresh_job_running_live_health(tmp_path):
    """RUNNING 分支活体取数:离子步/|F|max 进度入 manifest;跨轮计数器持久化。"""
    d = _job_dir(tmp_path)
    current = manifest.load_manifest(d)
    current['task_type'] = 'relax'
    record_input_closure(d, current)
    manifest.save_manifest(d, current)
    submitter.submit_job(FakeClient(script=[('qsub', '61.c\n')]), FakeSFTP(), _profile(), d)
    live_out = ('4\n___VCSLIVE___\n  FORCES: max atom, RMS   0.031456   0.0122\n'
                '___VCSLIVE___\nDAV:  12  -0.38E+03  -0.1E-04  x  x  x\n')
    client = FakeClient(script=[('___VCSLIVE___', live_out)])
    m = submitter.refresh_job(client, _profile(), d, live_states={'61': 'RUNNING'})
    live = m['results']['live']
    assert m['state'] == 'RUNNING' and live['ionic_steps'] == 4
    assert live['fmax'] == '0.031456'
    assert live['sloshing_polls'] == 0 and live['warning'] == ''
    # 后续轮:0 离子步 → 计数器从 manifest 恢复并累加,第 3 轮告警首步假死
    stall = '0\n___VCSLIVE___\n\n___VCSLIVE___\n\n'
    for expect in (1, 2, 3):
        m = submitter.refresh_job(FakeClient(script=[('___VCSLIVE___', stall)]),
                                  _profile(), d, live_states={'61': 'RUNNING'})
        assert m['results']['live']['zero_step_polls'] == expect
    assert '首步假死' in m['results']['live']['warning']


def test_remote_namespace_isolated_but_legacy_path_stays_compatible(tmp_path):
    job = _job_dir(tmp_path)
    profile = _profile()
    default_remote = submitter._spec_for(profile, job).remote_dir
    assert default_remote.startswith('/work/sk2067/jobs/zn_job--')
    data = manifest.load_manifest(job)
    data['inputs']['remote_namespace'] = 'lis-a1b2c3d4e5f6'
    manifest.save_manifest(job, data)
    namespaced = submitter._spec_for(profile, job).remote_dir
    assert namespaced.startswith('/work/sk2067/jobs/lis-a1b2c3d4e5f6/zn_job--')
    data['inputs']['remote_namespace'] = '../escape'
    manifest.save_manifest(job, data)
    assert any('命名空间非法' in issue for issue in submitter.preflight(profile, job))
    with pytest.raises(ValueError, match='命名空间非法'):
        submitter._spec_for(profile, job)


def test_remote_dir_avoids_same_basename_collisions_and_is_stable(tmp_path):
    first = _job_dir(tmp_path / 'project-a')
    second = _job_dir(tmp_path / 'project-b')
    profile = _profile()

    first_remote = submitter._spec_for(profile, first).remote_dir
    assert first_remote == submitter._spec_for(profile, first).remote_dir
    assert first_remote != submitter._spec_for(profile, second).remote_dir
    assert posixpath.basename(first_remote).startswith('zn_job--')
    assert str(tmp_path) not in first_remote


def test_remote_dir_preserves_legacy_path_for_same_profile(tmp_path):
    job = _job_dir(tmp_path)
    data = manifest.load_manifest(job)
    data['cluster'] = '1w'
    data['remote_dir'] = '/work/sk2067/jobs/legacy-job'
    manifest.save_manifest(job, data)

    assert submitter._spec_for(_profile(), job).remote_dir == data['remote_dir']
    other = _profile(name='other', remote_root='/scratch/other')
    assert submitter._spec_for(other, job).remote_dir.startswith(
        '/scratch/other/zn_job--')


def test_job_operation_rejects_overlapping_cross_thread_mutation(tmp_path):
    job = _job_dir(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock():
        with submitter.job_operation(job, '测试占用'):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(RuntimeError, match='正在执行另一项'):
            submitter.submit_job(FakeClient(), FakeSFTP(), _profile(), job)
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
