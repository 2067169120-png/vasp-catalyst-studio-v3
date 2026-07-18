"""本机作业运行器测试:_wrap_cmd 两平台、start→status→DONE/FAILED 全流程(真实短命令)、
run_blocking 成功/失败/超时、cancel(假 subprocess)、log_tail、状态边界。"""
import os
import sys
import time

from vcstudio.cluster import local_runner as lr


# ── 命令包装(纯函数,跨平台) ────────────────────────────────────────────────────
def test_wrap_cmd_posix():
    w = lr._wrap_cmd(['Multiwfn', 'mol.wfn'], 'posix')
    assert w[0] == 'sh' and w[1] == '-c'
    assert 'echo $?' in w[2] and '.exit_code' in w[2] and 'Multiwfn' in w[2]


def test_wrap_cmd_windows():
    w = lr._wrap_cmd(['Multiwfn', 'mol path.wfn'], 'nt')
    assert w[0] == 'cmd' and '/v:on' in w and w[2] == '/c'
    joined = w[-1]
    assert 'ERRORLEVEL' in joined and '.exit_code' in joined
    assert '"mol path.wfn"' in joined                       # list2cmdline 给含空格路径加引号


# ── LocalJob ─────────────────────────────────────────────────────────────────
def test_localjob_fields():
    job = lr.LocalJob(cmd=['a', 'b'], cwd='/x', log_file='/x/log')
    assert job.cmd == ['a', 'b'] and job.cwd == '/x' and job.env is None


# ── 状态边界 ─────────────────────────────────────────────────────────────────
def test_status_not_started(tmp_path):
    st = lr.status(str(tmp_path))
    assert st['state'] == 'NOT_STARTED' and st['pid'] is None and st['exit_code'] is None


def _poll_done(cwd, timeout=20.0):
    """轮询到终态(DONE/FAILED)或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = lr.status(cwd)
        if st['state'] in ('DONE', 'FAILED'):
            return st
        time.sleep(0.1)
    return lr.status(cwd)


# ── start → status 全流程(真实短命令) ──────────────────────────────────────────
def test_start_status_done(tmp_path):
    log = str(tmp_path / 'run.log')
    job = lr.LocalJob(cmd=[sys.executable, '-c', 'print(12345)'],
                      cwd=str(tmp_path), log_file=log)
    r = lr.start(job)
    assert r['ok'] and isinstance(r['pid'], int)
    assert os.path.isfile(tmp_path / 'local_run.yaml')
    st = _poll_done(str(tmp_path))
    assert st['state'] == 'DONE' and st['exit_code'] == 0
    assert '12345' in st['log_tail']


def test_start_status_failed(tmp_path):
    job = lr.LocalJob(cmd=[sys.executable, '-c', 'import sys; sys.exit(3)'],
                      cwd=str(tmp_path), log_file=str(tmp_path / 'l.log'))
    assert lr.start(job)['ok']
    st = _poll_done(str(tmp_path))
    assert st['state'] == 'FAILED' and st['exit_code'] == 3


def test_start_with_env(tmp_path):
    log = str(tmp_path / 'env.log')
    job = lr.LocalJob(cmd=[sys.executable, '-c', 'import os; print(os.environ["VCS_MARK"])'],
                      cwd=str(tmp_path), log_file=log, env={'VCS_MARK': 'hello42'})
    assert lr.start(job)['ok']
    st = _poll_done(str(tmp_path))
    assert st['state'] == 'DONE' and 'hello42' in st['log_tail']


# ── run_blocking ─────────────────────────────────────────────────────────────
def test_run_blocking_ok(tmp_path):
    job = lr.LocalJob(cmd=[sys.executable, '-c', 'print(777)'],
                      cwd=str(tmp_path), log_file=str(tmp_path / 'b.log'))
    r = lr.run_blocking(job, timeout=30)
    assert r['ok'] and r['exit_code'] == 0 and r['elapsed_s'] >= 0.0 and '777' in r['log_tail']


def test_run_blocking_failure(tmp_path):
    job = lr.LocalJob(cmd=[sys.executable, '-c', 'import sys; sys.exit(5)'],
                      cwd=str(tmp_path), log_file=str(tmp_path / 'b.log'))
    r = lr.run_blocking(job, timeout=30)
    assert not r['ok'] and r['exit_code'] == 5


def test_run_blocking_timeout(tmp_path):
    job = lr.LocalJob(cmd=[sys.executable, '-c', 'import time; time.sleep(5)'],
                      cwd=str(tmp_path), log_file=str(tmp_path / 'b.log'))
    r = lr.run_blocking(job, timeout=1)
    assert not r['ok'] and '超时' in r['error']


# ── cancel(假 subprocess / 假 killpg) ──────────────────────────────────────────
def test_cancel_no_runinfo(tmp_path):
    r = lr.cancel(str(tmp_path))
    assert not r['ok'] and 'local_run.yaml' in r['error']


def test_cancel_posix_killpg(tmp_path, monkeypatch):
    lr._write_runinfo(str(tmp_path),
                      {'pid': 4242, 'cmd': ['x'], 'log_file': 'l', 'started_at': 'now'})
    killed = {}
    monkeypatch.setattr(lr, '_is_windows', lambda: False)
    monkeypatch.setattr(lr.os, 'getpgid', lambda pid: pid)
    monkeypatch.setattr(lr.os, 'killpg', lambda pg, sig: killed.setdefault('args', (pg, sig)))
    r = lr.cancel(str(tmp_path))
    assert r['ok'] and killed['args'][0] == 4242


def test_cancel_windows_taskkill(tmp_path, monkeypatch):
    lr._write_runinfo(str(tmp_path),
                      {'pid': 9001, 'cmd': ['x'], 'log_file': 'l', 'started_at': 'now'})
    calls = {}
    monkeypatch.setattr(lr, '_is_windows', lambda: True)

    def fake_run(cmd, **kw):
        calls['cmd'] = cmd

        class P:
            returncode = 0
        return P()

    monkeypatch.setattr(lr.subprocess, 'run', fake_run)
    r = lr.cancel(str(tmp_path))
    assert r['ok'] and calls['cmd'][:2] == ['taskkill', '/PID']
    assert '9001' in calls['cmd'] and '/F' in calls['cmd']


# ── log_tail 末 20 行 ────────────────────────────────────────────────────────
def test_log_tail_last_20(tmp_path):
    log = tmp_path / 'big.log'
    log.write_text('\n'.join(f'line{i}' for i in range(50)) + '\n', encoding='utf-8')
    lr._write_runinfo(str(tmp_path),
                      {'pid': 1, 'cmd': ['x'], 'log_file': str(log), 'started_at': 'now'})
    (tmp_path / '.exit_code').write_text('0\n', encoding='utf-8')   # 令 status 走 DONE 读 log_tail
    st = lr.status(str(tmp_path))
    tail_lines = st['log_tail'].splitlines()
    assert len(tail_lines) == 20 and tail_lines[-1] == 'line49' and tail_lines[0] == 'line30'


def test_exit_code_parse_and_alive_helpers(tmp_path):
    # _read_exit_code:缺文件 → None;有值 → int
    assert lr._read_exit_code(str(tmp_path)) is None
    (tmp_path / '.exit_code').write_text('7\n', encoding='utf-8')
    assert lr._read_exit_code(str(tmp_path)) == 7
    # 当前进程一定存活
    assert lr._pid_alive(os.getpid()) is True
    assert lr._pid_alive(None) is False
