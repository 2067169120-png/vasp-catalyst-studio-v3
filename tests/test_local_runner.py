"""本机作业运行器测试:_wrap_cmd 两平台、start→status→DONE/FAILED 全流程(真实短命令)、
run_blocking 成功/失败/超时、cancel(假 subprocess)、log_tail、状态边界。"""
import base64
import json
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
    assert w[0].lower().endswith('powershell.exe')
    assert '-NoProfile' in w and '-NonInteractive' in w and '-EncodedCommand' in w
    script = base64.b64decode(w[-1]).decode('utf-16le')
    assert 'ProcessStartInfo' in script and 'UseShellExecute = $false' in script
    assert '.exit_code' in script and 'cmd.exe' not in script


# ── LocalJob ─────────────────────────────────────────────────────────────────
def test_localjob_fields():
    job = lr.LocalJob(cmd=['a', 'b'], cwd='/x', log_file='/x/log')
    assert job.cmd == ['a', 'b'] and job.cwd == '/x' and job.env is None


# ── 状态边界 ─────────────────────────────────────────────────────────────────
def test_status_not_started(tmp_path):
    st = lr.status(str(tmp_path))
    assert st['state'] == 'NOT_STARTED' and st['pid'] is None and st['exit_code'] is None


def _poll_done(cwd, timeout=None):
    """轮询到终态(DONE/FAILED)或超时。"""
    # GitHub's covered Windows 3.10 job can need more than 20 seconds for the
    # first cold PowerShell supervisor.  While that PID is alive and no atomic
    # exit-code file exists, RUNNING is the correct production state.
    if timeout is None:
        timeout = 60.0 if lr._is_windows() else 20.0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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
    mark = 'hello 42 (x)&y|z^100%!'
    job = lr.LocalJob(cmd=[sys.executable, '-c', 'import os; print(os.environ["VCS_MARK"])'],
                      cwd=str(tmp_path), log_file=log, env={'VCS_MARK': mark})
    assert lr.start(job)['ok']
    st = _poll_done(str(tmp_path))
    assert st['state'] == 'DONE' and st['exit_code'] == 0
    assert mark in st['log_tail']


def test_start_preserves_argv_metacharacters(tmp_path):
    probe = tmp_path / 'argv_probe.py'
    captured = tmp_path / 'argv.json'
    probe.write_text(
        'import json, pathlib, sys\n'
        'pathlib.Path(sys.argv[1]).write_text('
        'json.dumps(sys.argv[2:], ensure_ascii=False), encoding="utf-8")\n',
        encoding='utf-8',
    )
    expected = [
        'two words',
        'left(right)',
        'left&right',
        'left|right',
        'left^right',
        'left%VCS_EXPAND_ME%right',
        'left!VCS_EXPAND_ME!right',
        'quote"inside',
        'ends-with-backslash\\',
        '',
        '中文参数',
    ]
    job = lr.LocalJob(
        cmd=[sys.executable, str(probe), str(captured), *expected],
        cwd=str(tmp_path),
        log_file=str(tmp_path / 'argv.log'),
        env={'VCS_EXPAND_ME': 'EXPANDED'},
    )
    assert lr.start(job)['ok']
    st = _poll_done(str(tmp_path))
    assert st['state'] == 'DONE' and st['exit_code'] == 0
    assert json.loads(captured.read_text(encoding='utf-8')) == expected


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
    monkeypatch.setattr(lr.os, 'getpgid', lambda pid: pid, raising=False)
    monkeypatch.setattr(lr.os, 'killpg', lambda pg, sig: killed.setdefault('args', (pg, sig)),
                        raising=False)
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
