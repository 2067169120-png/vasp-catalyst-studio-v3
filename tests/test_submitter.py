"""提交编排测试:注入假 client/sftp,验证 preflight/上传/提交/状态刷新与 manifest 回写。"""
import os
import posixpath

import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster import submitter
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.shared import manifest


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
    def __init__(self):
        self.uploaded = {}     # remote → local
        self.written = {}      # remote → text

    def put(self, local, remote):
        self.uploaded[remote] = local

    def file(self, path, mode='w'):
        return _FakeRemoteFile(self.written, path)


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

    remote = '/work/sk2067/jobs/zn_job'
    # 上传:四件套 put + 脚本 write
    for f in ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR'):
        assert posixpath.join(remote, f) in sftp.uploaded
    script = sftp.written[posixpath.join(remote, 'vcs_job.sh')]
    assert '#PBS -q batch' in script and 'source /opt/intel.sh' in script
    assert '\r' not in script                                  # CRLF 消毒
    # 命令:mkdir + qsub(带 scheduler_bin 全路径)
    assert any(c.startswith(f'mkdir -p {remote}') for c in client.commands)
    assert any('/opt/torque-6.1.2/bin/qsub' in c for c in client.commands)
    # manifest 回写
    assert m['state'] == 'SUBMITTED'
    assert m['scheduler_job_id'] == '8812345'
    assert m['cluster'] == '1w' and m['remote_dir'] == remote
    assert [h['state'] for h in m['state_history']] == ['CREATED', 'UPLOADED', 'SUBMITTED']
    assert m['attempts'][0]['job_id'] == '8812345'
    assert manifest.load_manifest(d)['state'] == 'SUBMITTED'   # 已落盘


def test_submit_job_failure_keeps_uploaded(tmp_path):
    d = _job_dir(tmp_path)
    client = FakeClient(script=[('qsub', 'qsub: Unauthorized Request\n')])
    with pytest.raises(RuntimeError, match='提交失败'):
        submitter.submit_job(client, FakeSFTP(), _profile(), d)
    assert manifest.load_manifest(d)['state'] == 'UPLOADED'    # 留痕但不冒充已提交


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
        ('grep -c', '1\n'),
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


def test_submit_and_refresh_quote_spaced_remote_dir(tmp_path):
    """remote_root 带空格 → mkdir/qsub/grep/tail 里的路径全部要引号(sftp 是协议路径,不引)。"""
    d = _job_dir(tmp_path)
    prof = _profile(remote_root='/work/my jobs')
    client = FakeClient(script=[('qsub', '77.c\n')])
    sftp = FakeSFTP()
    m = submitter.submit_job(client, sftp, prof, d)

    remote = '/work/my jobs/zn_job'
    assert m['remote_dir'] == remote
    assert f"mkdir -p '{remote}'" in client.commands
    assert any(f"'{remote}/vcs_job.sh'" in c for c in client.commands)
    assert posixpath.join(remote, 'INCAR') in sftp.uploaded
    # 自动脚本里的 cd 同样要引号
    assert f"cd '{remote}'" in sftp.written[posixpath.join(remote, 'vcs_job.sh')]

    done_client = FakeClient(script=[
        ('grep -c', '1\n'),
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
        ('grep -c', '1\n'),
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


def test_continue_refuses_invalid_contcar(tmp_path):
    d = _restartable_job(tmp_path)
    client = FakeClient(script=[('cat', 'garbage\nshort\n')])   # CONTCAR 不完整
    with pytest.raises(RuntimeError, match='CONTCAR'):
        submitter.continue_from_contcar(client, _profile(), d)


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
    # 新号 201 不在 qstat(GONE);OUTCAR mtime 仍是基线 1000(本轮尚未重写);
    # 且旧 OUTCAR 还带收敛串 + E0 —— 正是会被误判 DONE 的陷阱
    refresh_client = FakeClient(script=[
        ('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'),
        ('grep -c', '1\n'),
        ('tail -n 150', '   5 F= -.5E+01 E0= -.5E+01  d E =-.1E-05\n'),
    ])
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
        ('grep -c', '1\n'),
        ('tail -n 150', '   5 F= -.5E+01 E0= -.5E+01  d E =-.1E-05\n'),
    ])
    m = submitter.refresh_job(refresh_client, _profile(), d, live_states={})
    assert m['state'] == 'DONE'
    assert 'settling' not in m['results']


def test_continue_seen_in_scheduler_then_gone_classifies(tmp_path):
    """续算后先在 qstat 现身(QUEUED)→ 状态离开 SUBMITTED;再消失即正常判终态,不沉降。"""
    d = _restartable_job(tmp_path)
    submitter.continue_from_contcar(_continue_client(), _profile(), d)
    submitter.refresh_job(FakeClient(), _profile(), d, live_states={'201': 'QUEUED'})
    # 再消失,OUTCAR 仍旧(mtime 未变)但已确认活过 → 落终态
    refresh_client = FakeClient(script=[('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'),
                                        ('grep -c', '0\n')])
    m = submitter.refresh_job(refresh_client, _profile(), d, live_states={})
    assert m['state'] != 'SUBMITTED'
    assert 'settling' not in m['results']


def test_continue_settling_cap_eventually_classifies(tmp_path):
    """兜底:连续 SETTLE_MAX_CHECKS 次仍 GONE+旧 OUTCAR(疑重投即被拒)→ 放行落终态,
    不永久卡 SUBMITTED。"""
    d = _restartable_job(tmp_path)
    submitter.continue_from_contcar(_continue_client(), _profile(), d)
    stale = [('stat -c', 'OUTCAR 90000 1000\nOSZICAR 3000 1000\n'), ('grep -c', '0\n')]
    last = None
    for _ in range(submitter.SETTLE_MAX_CHECKS):
        last = submitter.refresh_job(FakeClient(script=stale), _profile(), d, live_states={})
    assert last['state'] != 'SUBMITTED'            # 到上限后兜底落终态


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
    client = FakeClient(script=[('grep -c', '1\n')])
    submitter._grep_converged(client, '/w/j', 'static')
    assert 'aborting loop because EDIFF is reached' in client.commands[-1]
    client2 = FakeClient(script=[('grep -c', '1\n')])
    submitter._grep_converged(client2, '/w/j', 'relax')
    assert 'reached required accuracy' in client2.commands[-1]


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
    assert '#PBS -N zn_job' in text and 'cd X/zn_job' in text and '-np 12' in text


def test_refresh_job_running_live_health(tmp_path):
    """RUNNING 分支活体取数:离子步/|F|max 进度入 manifest;跨轮计数器持久化。"""
    d = _job_dir(tmp_path)
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
