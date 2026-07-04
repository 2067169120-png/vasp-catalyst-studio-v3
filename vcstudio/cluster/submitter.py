"""提交编排:preflight → 上传四件套+脚本 → 提交 → 回写 job.yaml;查状态 → 收敛判定。

client/sftp 由调用方注入(GUI 经 connection.open_client;测试注入假件),
本模块不 import paramiko——全部逻辑可离线测试。
安全阀:preflight 不过绝不出手;提交动作逐条写入 manifest.attempts(可审计)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import time

from vcstudio.cluster import script_builder
from vcstudio.cluster.schedulers import (
    JobScriptSpec, get_dialect, QUEUED, RUNNING, GONE,
)
from vcstudio.shared import manifest as manifest_mod

SCRIPT_NAME = 'vcs_job.sh'
_INPUT_FILES = ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR')
_E0_RE = re.compile(r'E0=\s*([-+.\dEe]+)')


def run_cmd(client, cmd: str, timeout: int = 30, check: bool = False):
    """exec_command 薄封装 → (stdout_text, stderr_text)。

    check=True 时退出码非零抛 RuntimeError(先读尽输出再取退出码,防 paramiko 死锁)。
    """
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    o = out.read().decode('utf-8', errors='replace')
    e = err.read().decode('utf-8', errors='replace')
    if check:
        status = out.channel.recv_exit_status()
        if status != 0:
            raise RuntimeError(f'远程命令失败(退出码 {status}):{cmd};{e.strip()[:200]}')
    return o, e


# ── preflight(全部本地检查,不联网) ─────────────────────────────────────────
def preflight(profile, job_dir: str) -> list:
    """提交前检查,返回错误文案列表(空=可以出手)。"""
    errs = []
    for f in _INPUT_FILES:
        if not os.path.isfile(os.path.join(job_dir, f)):
            errs.append(f'作业目录缺 {f}(先在生成页产出四件套)')
    if manifest_mod.load_manifest(job_dir) is None:
        errs.append('作业目录缺 job.yaml(旧目录可重新生成一次以补台账)')
    if not profile.remote_root:
        errs.append('集群配置未填远程工作目录 remote_root')
    elif not str(profile.remote_root).startswith('/'):
        errs.append('remote_root 需为绝对路径(以 / 开头)')
    try:
        get_dialect(profile.scheduler)
    except ValueError as e:
        errs.append(str(e))
    mode = getattr(profile, 'script_mode', 'auto')
    if mode == 'template':
        tp = getattr(profile, 'template_path', '')
        if not tp or not os.path.isfile(tp):
            errs.append('模板模式但模板文件不存在;请在集群页重新选择')
    elif mode == 'auto':
        if not profile.queue:
            errs.append('自动脚本模式缺队列名')
        if int(getattr(profile, 'ppn', 0) or 0) <= 0:
            errs.append('自动脚本模式缺每节点核数 ppn')
        if not getattr(profile, 'vasp_cmd', ''):
            errs.append('自动脚本模式缺 VASP 执行命令')
    else:
        errs.append(f'未知脚本模式: {mode!r}')
    return errs


def _spec_for(profile, job_dir: str) -> JobScriptSpec:
    dir_name = os.path.basename(os.path.normpath(job_dir))
    remote_dir = posixpath.join(profile.remote_root, dir_name)
    return JobScriptSpec(
        job_name=script_builder.sanitize_job_name(dir_name),
        remote_dir=remote_dir,
        queue=getattr(profile, 'queue', ''),
        nodes=int(getattr(profile, 'nodes', 1) or 1),
        ppn=int(getattr(profile, 'ppn', 0) or 0),
        walltime=getattr(profile, 'walltime', '24:00:00') or '24:00:00',
        env_lines=list(getattr(profile, 'env_lines', []) or []),
        vasp_cmd=getattr(profile, 'vasp_cmd', ''),
    )


def build_script_text(profile, job_dir: str) -> str:
    """按 profile 双轨生成最终 job 脚本文本(预览按钮与真提交共用,所见即所交)。"""
    spec = _spec_for(profile, job_dir)
    dialect = get_dialect(profile.scheduler)
    template_text = None
    if getattr(profile, 'script_mode', 'auto') == 'template':
        with open(profile.template_path, 'r', encoding='utf-8') as f:
            template_text = f.read()
    return script_builder.build_script(
        getattr(profile, 'script_mode', 'auto'), dialect, spec, template_text)


# ── 提交 ───────────────────────────────────────────────────────────────────
def submit_job(client, sftp, profile, job_dir: str) -> dict:
    """上传 + 提交一个作业;成功回写 manifest(UPLOADED→SUBMITTED)并返回之。

    失败抛 RuntimeError/ValueError(中文),manifest 不落 SUBMITTED。
    """
    errs = preflight(profile, job_dir)
    if errs:
        raise ValueError('；'.join(errs))
    m = manifest_mod.load_manifest(job_dir)
    spec = _spec_for(profile, job_dir)
    dialect = get_dialect(profile.scheduler)
    script_text = build_script_text(profile, job_dir)

    # 远程目录 + 上传(脚本统一 LF,防 Windows CRLF 毒害 shell)
    run_cmd(client, f'mkdir -p {shlex.quote(spec.remote_dir)}', check=True)
    for fname in _INPUT_FILES:
        sftp.put(os.path.join(job_dir, fname), posixpath.join(spec.remote_dir, fname))
    with sftp.file(posixpath.join(spec.remote_dir, SCRIPT_NAME), 'w') as f:
        f.write(script_text.replace('\r\n', '\n'))
    m['cluster'] = profile.name
    m['remote_dir'] = spec.remote_dir
    manifest_mod.set_state(m, 'UPLOADED', note=f'{len(_INPUT_FILES)} 输入 + {SCRIPT_NAME}')

    out, err = run_cmd(client, dialect.submit_cmd(
        posixpath.join(spec.remote_dir, SCRIPT_NAME),
        getattr(profile, 'scheduler_bin', '')))
    job_id = dialect.parse_job_id(out)
    if not job_id:
        manifest_mod.save_manifest(job_dir, m)     # 保留 UPLOADED 痕迹
        raise RuntimeError(f'提交失败,{dialect.name} 返回:{(out or err).strip()[:300]}')

    m['scheduler_job_id'] = job_id
    m['attempts'] = list(m.get('attempts') or [])
    m['attempts'].append({
        'n': len(m['attempts']) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'job_id': job_id,
        'cluster': profile.name,
        'queue': spec.queue or None,
        'script_mode': getattr(profile, 'script_mode', 'auto'),
    })
    manifest_mod.set_state(m, 'SUBMITTED', note=f'{dialect.name} {job_id}')
    manifest_mod.save_manifest(job_dir, m)
    return m


# ── 状态刷新(含最小收敛判定,S3 雏形) ────────────────────────────────────────
def query_states(client, profile) -> dict:
    """调度器一次性查询当前用户全部作业 → {job_id: QUEUED|RUNNING}。"""
    dialect = get_dialect(profile.scheduler)
    out, _ = run_cmd(client, dialect.status_cmd(
        profile.username, getattr(profile, 'scheduler_bin', '')))
    return dialect.parse_status(out)


def refresh_job(client, profile, job_dir: str, live_states: dict | None = None) -> dict:
    """按调度器现状更新一个作业的 manifest;GONE 时做收敛判定+能量提取。

    live_states 可传入 query_states 结果避免逐作业重复查询。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None or not m.get('scheduler_job_id'):
        return m
    states = live_states if live_states is not None else query_states(client, profile)
    u = states.get(str(m['scheduler_job_id']), GONE)

    if u == QUEUED:
        if m['state'] != 'QUEUED':
            manifest_mod.set_state(m, 'QUEUED')
            manifest_mod.save_manifest(job_dir, m)
        return m
    if u == RUNNING:
        if m['state'] != 'RUNNING':
            manifest_mod.set_state(m, 'RUNNING')
            manifest_mod.save_manifest(job_dir, m)
        return m

    # GONE:调度器已无此作业 → 收敛判定(grep 'reached required accuracy',旧版实测口径)
    remote = m.get('remote_dir') or ''
    out, _ = run_cmd(client, f'grep -c "reached required accuracy" '
                             f'{shlex.quote(remote + "/OUTCAR")} 2>/dev/null || echo 0')
    converged = _first_int(out) > 0
    energy = _read_e0(client, remote)
    if energy is not None:
        m.setdefault('results', {})['energy_e0_eV'] = energy
    if converged:
        manifest_mod.set_state(m, 'DONE', note='reached required accuracy')
    else:
        manifest_mod.set_state(m, 'UNCONVERGED',
                               note='调度器已结束但未见收敛标志(可能超墙钟/中断,待修复区处理)')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _first_int(text: str) -> int:
    for tok in text.split():
        try:
            return int(tok)
        except ValueError:
            continue
    return 0


# ── 结果回收(S3):把关键输出拉回本地作业目录 ─────────────────────────────────
FETCH_FILES = ('CONTCAR', 'OSZICAR', 'OUTCAR')


def fetch_results(client, sftp, job_dir: str, files=FETCH_FILES):
    """下载远程输出文件到本地作业目录。返回 (fetched, missing) 两个文件名列表。

    - 不覆盖输入语义:CONTCAR/OSZICAR/OUTCAR 与四件套不重名,直接落在 job_dir。
    - 单个文件缺失(如未跑出 CONTCAR)记入 missing,不中断其余下载。
    - 下载记录写回 manifest.results(fetched/fetched_at),供任务页与项目页展示。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法定位远程目录')
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业尚未提交过(manifest 无 remote_dir)')
    fetched, missing = [], []
    for fname in files:
        try:
            sftp.get(posixpath.join(remote, fname), os.path.join(job_dir, fname))
            fetched.append(fname)
        except (IOError, OSError):
            missing.append(fname)
    res = m.setdefault('results', {})
    res['fetched'] = fetched
    res['fetched_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    manifest_mod.save_manifest(job_dir, m)
    return fetched, missing


def _read_e0(client, remote_dir: str):
    """OSZICAR 末行 E0(eV);拿不到 → None(绝不编数)。"""
    if not remote_dir:
        return None
    out, _ = run_cmd(client, f'tail -2 {shlex.quote(remote_dir + "/OSZICAR")} 2>/dev/null')
    m = None
    for m in _E0_RE.finditer(out):
        pass                                   # 取最后一个匹配
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None
