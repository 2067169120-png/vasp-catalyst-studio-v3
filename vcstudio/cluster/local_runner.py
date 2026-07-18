"""本机作业运行器:在本地跑分子引擎(Gaussian/ORCA 等)与后处理(Multiwfn/VMD)。

定位:VASP 平面波作业仍走远程集群(submitter.py / schedulers.py),本模块只负责**本机**
短/中任务的启动、状态查询、取消。对齐适配器口径:纯函数(命令包装)离线可测,subprocess
经 monkeypatch 替身,缺进程/坏记录一律结构化返回不抛。

跨平台两处关键设计:
1. 退出码持久化:Popen 句柄活不过本进程(GUI 刷新状态是另开进程读文件),故用 shell 把
   作业包一层,在其结束时把退出码写进 cwd/.exit_code(见 _wrap_cmd);status() 读该文件
   判 DONE/FAILED,不依赖 Popen 句柄。
2. 存活探测:不引入 psutil。POSIX 用 os.kill(pid,0)(0 号信号只探测不投递);Windows 上
   os.kill(pid,0) 会被 CPython 映射成 GenerateConsoleCtrlEvent(CTRL_C_EVENT)——等于每次
   轮询都给作业发 Ctrl+C(会打断正在跑的任务),绝不能用它探活,改用 ctypes OpenProcess
   只读地判断进程是否仍在运行(见 _pid_alive)。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

RUNINFO_NAME = 'local_run.yaml'   # 落 cwd:pid/cmd/started_at,供 status/cancel 读
EXITCODE_NAME = '.exit_code'      # 落 cwd:作业结束后由 shell 包装写入的退出码


@dataclass
class LocalJob:
    """一个本机作业:cmd(命令 argv 列表)、cwd(工作目录)、log_file(日志落点)、env(可选环境变量)。"""
    cmd: list
    cwd: str
    log_file: str
    env: dict | None = None


def _is_windows() -> bool:
    """是否 Windows 平台。单独抽成函数是为可测:测试可 monkeypatch 本函数切平台分支,
    而不去改 os.name(改 os.name 会连累 pathlib 在非 Windows 上误判 WindowsPath 崩溃)。"""
    return os.name == 'nt'


# ── 命令包装(纯函数,跨平台) ────────────────────────────────────────────────────
def _wrap_cmd(cmd: list, platform: str) -> list:
    """把作业命令包一层 shell,使其结束后把退出码写入 cwd/.exit_code。

    platform:'nt'/'win32'/'windows'(或以 'win' 开头)视为 Windows,其余按 POSIX。
    - POSIX:sh -c '(cmd; echo $? > .exit_code)' —— $? 取前一条命令(即作业本体)的退出码。
    - Windows:cmd /v:on /c '(cmd) & echo !ERRORLEVEL!> .exit_code' —— 必须开延迟展开
      (/v:on + !ERRORLEVEL!),否则 %ERRORLEVEL% 在命令行解析期就被替换成旧值,拿不到
      作业真正的退出码。argv 用 list2cmdline 做 Windows 规范加引号。
    """
    tokens = [str(c) for c in cmd]
    plat = str(platform).lower()
    is_win = plat in ('nt', 'win32', 'windows') or plat.startswith('win')
    if is_win:
        inner = subprocess.list2cmdline(tokens)
        return ['cmd', '/v:on', '/c', f'({inner}) & echo !ERRORLEVEL!> {EXITCODE_NAME}']
    inner = ' '.join(shlex.quote(t) for t in tokens)
    return ['sh', '-c', f'({inner}; echo $? > {EXITCODE_NAME})']


# ── 运行信息读写(原子写 yaml) ──────────────────────────────────────────────────
def _write_runinfo(cwd: str, info: dict) -> Path:
    target = Path(cwd) / RUNINFO_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.yaml.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(info, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, target)
    return target


def _read_runinfo(cwd: str) -> dict | None:
    p = Path(cwd) / RUNINFO_NAME
    if not p.is_file():
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_exit_code(cwd: str):
    """读 cwd/.exit_code → int;缺文件/空/不可解析 → None。"""
    p = os.path.join(str(cwd), EXITCODE_NAME)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            txt = f.read().strip()
    except OSError:
        return None
    if not txt:
        return None
    try:
        return int(txt.split()[0])
    except (ValueError, IndexError):
        return None


def _tail_file(path: str | None, n: int = 20) -> str:
    """读文件末 n 行(状态面板/报告用);缺文件或读失败 → ''。"""
    if not path or not os.path.isfile(path):
        return ''
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except OSError:
        return ''
    return ''.join(lines[-n:]).rstrip('\n')


# ── 进程存活探测(不引入 psutil) ───────────────────────────────────────────────
def _pid_alive_windows(pid: int) -> bool:
    """Windows 只读探活:OpenProcess + WaitForSingleObject(0)。不给进程发任何信号。"""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return False   # 打不开句柄:进程不存在(或极少数权限不足,保守判为不在)
    try:
        # 仍在运行 → 立即超时 WAIT_TIMEOUT;已退出 → WAIT_OBJECT_0(0)
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid) -> bool:
    """进程存活探测。POSIX:os.kill(pid,0)(PermissionError=存在但无权限,仍算活);
    Windows:走 ctypes 只读探测(不能用 os.kill,详见模块 docstring)。"""
    if pid is None:
        return False
    pid = int(pid)
    if _is_windows():
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ── 启动 / 状态 / 取消 / 阻塞运行 ───────────────────────────────────────────────
def start(job: LocalJob) -> dict:
    """后台启动一个作业(不阻塞)。返回 {'ok','pid','error'}。

    stdout/stderr 重定向到 job.log_file(追加);Windows 加 CREATE_NO_WINDOW 不弹黑窗、
    新进程组便于整树 taskkill;POSIX 用 start_new_session 独立进程组便于 killpg。
    落 local_run.yaml(pid/cmd/started_at)到 cwd。
    """
    cwd = str(job.cwd)
    os.makedirs(cwd, exist_ok=True)
    # 清理上一轮退出码,防 status 读到旧值把新作业误判成已结束
    stale = os.path.join(cwd, EXITCODE_NAME)
    if os.path.isfile(stale):
        try:
            os.remove(stale)
        except OSError:
            pass
    wrapped = _wrap_cmd(job.cmd, os.name)
    popen_kw: dict = {}
    if _is_windows():
        popen_kw['creationflags'] = (getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                     | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    else:
        popen_kw['start_new_session'] = True   # 独立进程组,cancel 时可 killpg 整组
    env = {**os.environ, **job.env} if job.env else None
    try:
        log = open(job.log_file, 'ab')
    except OSError as e:
        return {'ok': False, 'pid': None, 'error': f'无法打开日志文件:{e}'}
    try:
        proc = subprocess.Popen(wrapped, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **popen_kw)
    except OSError as e:
        log.close()
        return {'ok': False, 'pid': None, 'error': f'启动失败:{e}'}
    log.close()   # 子进程已 dup 一份 fd,父进程可关自己的句柄
    _write_runinfo(cwd, {
        'pid': proc.pid,
        'cmd': [str(c) for c in job.cmd],
        'wrapped': wrapped,
        'log_file': str(job.log_file),
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return {'ok': True, 'pid': proc.pid, 'error': ''}


def status(cwd: str) -> dict:
    """查询本机作业状态。返回 {'state','pid','exit_code','log_tail'}。

    state:NOT_STARTED(无 local_run.yaml)/ RUNNING(进程在且无退出码)/
    DONE(退出码 0)/ FAILED(退出码非 0,或进程已不在却没写退出码——被杀/崩溃/重启)。
    """
    info = _read_runinfo(cwd)
    if not info:
        return {'state': 'NOT_STARTED', 'pid': None, 'exit_code': None, 'log_tail': ''}
    pid = info.get('pid')
    log_tail = _tail_file(info.get('log_file'), 20)
    exit_code = _read_exit_code(cwd)   # 退出码文件在,状态即已终结(权威)
    if exit_code is not None:
        state = 'DONE' if exit_code == 0 else 'FAILED'
        return {'state': state, 'pid': pid, 'exit_code': exit_code, 'log_tail': log_tail}
    if pid is not None and _pid_alive(pid):
        return {'state': 'RUNNING', 'pid': pid, 'exit_code': None, 'log_tail': log_tail}
    # 无退出码且进程已不在:异常终止(被 cancel 杀掉/崩溃/机器重启中断)
    return {'state': 'FAILED', 'pid': pid, 'exit_code': None, 'log_tail': log_tail}


def cancel(cwd: str) -> dict:
    """取消本机作业(按 local_run.yaml 记录的 pid)。返回 {'ok','error'}。

    Windows:taskkill /PID pid /T /F 杀整棵进程树;POSIX:killpg 杀整个进程组
    (start 时 start_new_session 让作业自成组,故 pgid==pid)。
    """
    info = _read_runinfo(cwd)
    if not info or info.get('pid') is None:
        return {'ok': False, 'error': '无本地运行记录(local_run.yaml 缺失或无 pid),无从取消'}
    pid = int(info['pid'])
    try:
        if _is_windows():
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                return {'ok': True, 'error': ''}   # 进程早已退出,视作已取消
    except (OSError, subprocess.SubprocessError) as e:
        return {'ok': False, 'error': f'取消失败:{e}'}
    return {'ok': True, 'error': ''}


def run_blocking(job: LocalJob, timeout: int = 600) -> dict:
    """阻塞跑一个短作业(Multiwfn/VMD 这类秒~分钟级)。返回 {'ok','exit_code','log_tail','elapsed_s'}。

    直接 subprocess.run 拿退出码(无需 shell 包装 / .exit_code 那套异步机制);超时/启动失败
    结构化返回不抛。stdout/stderr 覆盖写入 job.log_file。
    """
    cwd = str(job.cwd)
    os.makedirs(cwd, exist_ok=True)
    env = {**os.environ, **job.env} if job.env else None
    t0 = time.time()
    try:
        with open(job.log_file, 'wb') as log:
            proc = subprocess.run([str(c) for c in job.cmd], cwd=cwd,
                                  stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=env, timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        return {'ok': False, 'exit_code': None, 'elapsed_s': round(time.time() - t0, 2),
                'log_tail': _tail_file(job.log_file, 20), 'error': f'超时({timeout}s)已放弃'}
    except OSError as e:
        return {'ok': False, 'exit_code': None, 'elapsed_s': round(time.time() - t0, 2),
                'log_tail': '', 'error': f'启动失败:{e}'}
    return {'ok': code == 0, 'exit_code': code, 'elapsed_s': round(time.time() - t0, 2),
            'log_tail': _tail_file(job.log_file, 20),
            'error': '' if code == 0 else f'退出码 {code}'}
