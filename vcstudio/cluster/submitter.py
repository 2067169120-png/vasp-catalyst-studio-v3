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
import shutil
import time

from vcstudio.cluster import script_builder
from vcstudio.cluster import diagnose
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
    m0 = manifest_mod.load_manifest(job_dir)
    if m0 is None:
        errs.append('作业目录缺 job.yaml(旧目录可重新生成一次以补台账)')
    else:
        # POTCAR TITEL 闸(原版提交前防线):截断/拼错的 POTCAR 会给出"收敛但静默错"
        # 的能量——TITEL 段数必须等于 POSCAR 物种数,不等拒绝提交
        errs += _potcar_gate(job_dir, m0)
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


def _potcar_gate(job_dir: str, m: dict) -> list:
    """本地 POTCAR 的 TITEL 段数 == manifest 物种数,否则拒提交(读不到不硬拦)。"""
    elements = (m.get('inputs') or {}).get('elements') or []
    if not elements:
        return []
    try:
        with open(os.path.join(job_dir, 'POTCAR'), 'r', encoding='utf-8',
                  errors='replace') as f:
            n_titel = f.read().count('TITEL')
    except OSError:
        return []                                       # 缺文件已有独立检查项
    if n_titel != len(elements):
        return [f'POTCAR 完整性检查失败:TITEL 段数 {n_titel} ≠ 物种数 {len(elements)}'
                f'({" ".join(elements)});文件疑被截断/手改,请重新生成后再提交']
    return []


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
_QOK = '___VCSQOK___'


def query_scheduler(client, profile) -> tuple:
    """调度器一次查询 → ({job_id: QUEUED|RUNNING}, {job_id: 终态原因})。

    在命令尾追加 `&& echo 哨兵`:qstat/squeue 退出码非零(调度器抖动/不可达)时哨兵
    不出现 → 抛错本轮跳过,**绝不**把空输出误判成"所有作业都结束了"再逐个标终态
    (瞬时一次抖动会永久错标 RUNNING 作业为 FAILED/UNCONVERGED,原版用跨轮确认防此)。
    终态原因(Slurm TO/CA/NF/OOM 短暂可见)喂 diagnose 消歧——超墙钟 137 若无原因
    会被误判 OOM→FAILED 困死可续算作业(审查确认的接线断点)。
    """
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.status_cmd(profile.username, getattr(profile, 'scheduler_bin', ''))
    out, _ = run_cmd(client, f'{cmd} && echo {_QOK}')
    if _QOK not in out:
        raise RuntimeError('调度器状态查询失败(qstat/squeue 无响应或报错);本轮跳过,不误判作业已结束')
    raw = out.replace(_QOK, '')
    return dialect.parse_status(raw), dialect.parse_terminal(raw)


def query_states(client, profile) -> dict:
    """兼容入口:只要统一态。新代码请用 query_scheduler(带终态原因)。"""
    return query_scheduler(client, profile)[0]


def query_queue_detail(client, profile) -> list:
    """调度器全量明细(该用户所有在队/在跑作业,含外部提交的)。

    → [{'job_id','state','name','workdir'}]。用途:「集群队列」视图 + 认领外部任务。
    同 query_scheduler 的哨兵防抖:查询失败抛错,绝不静默返回空当"队列空"。
    """
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.detail_cmd(profile.username, getattr(profile, 'scheduler_bin', ''))
    out, _ = run_cmd(client, f'{cmd} && echo {_QOK}')
    if _QOK not in out:
        raise RuntimeError('调度器队列查询失败(qstat/squeue 无响应或报错)')
    return dialect.parse_detail(out.replace(_QOK, ''))


def query_workdir(client, profile, job_id: str) -> str:
    """认领辅助:按作业号查远程工作目录(PBS 走 qstat -f;Slurm 的 detail 已带 %Z
    → 方言返回空命令,这里不发远程调用直接 '')。查不到 → ''(交前端让用户手填)。"""
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.workdir_cmd(job_id, getattr(profile, 'scheduler_bin', ''))
    if not cmd:
        return ''
    out, _ = run_cmd(client, cmd)
    return dialect.parse_workdir(out)


def adopt_external_job(local_dir: str, profile, job_id: str, remote_dir: str,
                       name: str = '', task_type: str = 'relax') -> dict:
    """认领一个非本软件提交的集群作业:落 job.yaml + 入台账,之后查状态/拉回/续算全走原生路径。

    local_dir 是用户指定的本地目录(存在则直接用,不存在则创建;作为结果落点)。
    不上传/不动远端——认领只是登记事实:该作业号在该集群、结果在 remote_dir。
    状态置 SUBMITTED,下一次「查询状态」会按调度器现状推进(QUEUED/RUNNING/终态取证)。
    """
    if not str(remote_dir).startswith('/'):
        raise ValueError('远程目录需为绝对路径(以 / 开头)')
    os.makedirs(local_dir, exist_ok=True)
    existing = manifest_mod.load_manifest(local_dir)
    if existing and existing.get('scheduler_job_id'):
        raise ValueError(f'该本地目录已关联作业号 {existing["scheduler_job_id"]},'
                         f'请换一个目录或先移出台账')
    dir_name = os.path.basename(os.path.normpath(local_dir))
    m = existing or manifest_mod.new_manifest(
        job_id=f'external-{dir_name}-{time.strftime("%Y%m%d-%H%M%S")}',
        system=name or dir_name, task_type=task_type, calc_type='slab',
        inputs={'adopted': True, 'adopted_from': f'{profile.name}:{job_id}'})
    m['cluster'] = profile.name
    m['remote_dir'] = remote_dir
    m['scheduler_job_id'] = str(job_id)
    m['task_type'] = task_type
    m.setdefault('attempts', []).append({
        'n': len(m.get('attempts') or []) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': 'adopted',
        'job_id': str(job_id),
        'cluster': profile.name,
    })
    manifest_mod.set_state(m, 'SUBMITTED', note=f'认领外部作业 {job_id}(remote: {remote_dir})')
    manifest_mod.save_manifest(local_dir, m)
    from vcstudio.cluster import ledger
    ledger.register(local_dir)
    return m


# 续算沉降护栏:续算后连续 SETTLE_MAX_CHECKS 次仍"调度器无此作业 + OUTCAR 未刷新",
# 才放行终态取证(几乎必然是重投即被拒);正常情形新作业一两轮内就会现身/写出新 OUTCAR。
SETTLE_MAX_CHECKS = 3


def _mark_observed_alive(m: dict) -> None:
    """标记当前(重投)轮已在调度器现身(QUEUED/RUNNING)。续算沉降护栏据此放行。"""
    r = m.setdefault('results', {})
    r['observed_alive'] = True
    r.pop('settling', None)  # 现身即清沉降态


def _set_continue_baseline(m: dict, old_outcar_mtime) -> None:
    """续算重投时落基线:上一轮 OUTCAR 的 mtime + 复位本轮存活标记与沉降态。

    refresh_job 的沉降护栏据此判断"新一轮是否真的重写过 OUTCAR",
    避免新作业还没启动时误读旧 OUTCAR 而判终态(见 _in_continue_settling)。
    """
    r = m.setdefault('results', {})
    r['continue_baseline'] = {'outcar_mtime': old_outcar_mtime, 'settle_checks': 0,
                              'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    r['observed_alive'] = False
    r.pop('settling', None)


def _in_continue_settling(m: dict, reason, outcar_mtime) -> bool:
    """续算后作业仍未真正启动本轮 → 返回 True(保持 SUBMITTED,不判终态)。

    仅对续算过(continue_rounds≥1)、当前 SUBMITTED、且无调度器终态原因的作业生效。
    "已启动本轮"的证据:本轮在调度器现过身(observed_alive),或 OUTCAR 相对续算基线
    被重写过(mtime 变化)。两者皆无 → 说明新作业还没被登记/还没写新 OUTCAR,手里那份是
    上一轮旧 OUTCAR,绝不能拿去判终态。连续 SETTLE_MAX_CHECKS 次仍如此才兜底放行。
    调用方已确保 u==GONE(非 QUEUED/RUNNING)。返回 True 时已就地更新 results,调用方需落盘。
    """
    r = m.get('results') or {}
    rounds = int(r.get('continue_rounds', 0) or 0)
    if rounds < 1 or m.get('state') != 'SUBMITTED' or reason:
        return False
    if r.get('observed_alive'):
        return False
    baseline = r.get('continue_baseline') or {}
    base_mtime = baseline.get('outcar_mtime')
    rewritten = outcar_mtime is not None and outcar_mtime != base_mtime
    if rewritten:
        return False  # 本轮确已重写 OUTCAR → 真终态,放行取证
    # 仍是旧 OUTCAR(或暂无 OUTCAR):记一次沉降观测
    checks = int(baseline.get('settle_checks', 0) or 0) + 1
    baseline['settle_checks'] = checks
    r['continue_baseline'] = baseline
    r['settling'] = {
        'checks': checks,
        'reason': '续算后新作业尚未被调度器登记或尚未写出新 OUTCAR,暂不判终态(疑仍在排队)',
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    m['results'] = r
    return checks < SETTLE_MAX_CHECKS


def refresh_job(client, profile, job_dir: str, live_states: dict | None = None,
                terminal_reasons: dict | None = None) -> dict:
    """按调度器现状更新一个作业的 manifest;终态时做取证 + 失败分类。

    live_states/terminal_reasons 可传入 query_scheduler 结果避免逐作业重复查询。
    终态分支不再只有 DONE/UNCONVERGED:调 diagnose.classify 综合 调度器原因 + 退出码 +
    OUTCAR/OSZICAR 完整性 + 日志签名 + 收敛串 + 能量合理性 → DONE/UNCONVERGED/FAILED/
    NEEDS_HUMAN,并把结构化诊断写回 results.diagnosis + 失败时追加 attempts(可审计)。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None or not m.get('scheduler_job_id'):
        return m
    states = live_states if live_states is not None else query_states(client, profile)
    jid = str(m['scheduler_job_id'])
    u = states.get(jid, GONE)
    remote = m.get('remote_dir') or ''

    if u == QUEUED:
        # 本轮已在调度器现身:记下"活过",续算沉降护栏据此放行(不再疑为旧 OUTCAR)
        _mark_observed_alive(m)
        if m['state'] != 'QUEUED':
            manifest_mod.set_state(m, 'QUEUED')
        manifest_mod.save_manifest(job_dir, m)
        return m
    if u == RUNNING:
        _mark_observed_alive(m)
        if m['state'] != 'RUNNING':
            manifest_mod.set_state(m, 'RUNNING')
        # 活体健康(原版 lis_sac_status 经验):运行中即查 SCF 震荡/首步假死+进度,
        # 病态作业在烧完墙钟前就被抓出来;跨轮确认计数器防瞬态误报;只告警不 qdel
        m.setdefault('results', {})['live'] = _live_check(
            client, remote, (m.get('results') or {}).get('live'))
        manifest_mod.save_manifest(job_dir, m)
        return m

    # 终态候选(GONE / 调度器终态原因):先取 OUTCAR 现状(含 mtime)
    reason = (terminal_reasons or {}).get(jid)
    outcar_size, oszicar_size, outcar_mtime = _stat_outcar_full(client, remote)

    # ── 续算沉降护栏(修复:续算后仍在排队却被误判终态)─────────────────────────
    # 症状:续算重投 → 新作业号还没进 qstat(登记有延迟)→ 这里查为 GONE → 直接进终态分支
    # → 抓到的却是**上一轮**留下的完整 OUTCAR(收敛/SCF 震荡串还在)→ 明明在排队,
    # 却被标成 DONE / 需续算 / sloshing。只对**续算过**的作业设防(唯有它们才可能有旧 OUTCAR),
    # 且要求"当前这一轮确有产出"才认终态:本轮在调度器现过身,或 OUTCAR 相对续算基线被重写过。
    if _in_continue_settling(m, reason, outcar_mtime):
        manifest_mod.save_manifest(job_dir, m)
        return m
    m.setdefault('results', {}).pop('settling', None)  # 放行终态 → 清沉降态
    # ────────────────────────────────────────────────────────────────────────

    converged, clean_exit, stopped = _grep_marks(client, remote, m.get('task_type') or 'relax')
    exit_code, log_tail = _read_log(client, remote, jid)
    energy, oszicar_tail = _read_oszicar(client, remote)

    d = diagnose.classify(
        scheduler_reason=reason, exit_code=exit_code,
        outcar_size=outcar_size, oszicar_size=oszicar_size,
        log_tail=log_tail, converged=converged, energy=energy,
        oszicar_tail=oszicar_tail, nelm=_read_nelm(job_dir),
        clean_exit=clean_exit, stopped=stopped)

    if energy is not None:
        # 物理合理性闸(审查#4):BAD_ENERGY 的垃圾数不进 energy_e0_eV(防经自由能路径
        # 漏进 ΔG/U_L 图),原始值留 raw_energy_e0_eV 供人工核查
        key = 'raw_energy_e0_eV' if d.failure_class == diagnose.BAD_ENERGY else 'energy_e0_eV'
        m.setdefault('results', {})[key] = energy
    m.setdefault('results', {})['diagnosis'] = {
        'failure_class': d.failure_class,
        'restartable': d.restartable,
        'evidence': d.evidence,
        'scheduler_reason': reason,
        'exit_code': exit_code,
        'outcar_bytes': outcar_size,
        'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if d.failure_class != diagnose.CONVERGED:
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed',
            'failure_class': d.failure_class,
            'to_state': d.state,
        })
    manifest_mod.set_state(m, d.state, note=f'{d.failure_class}: {d.evidence}')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _live_check(client, remote: str, prev: dict | None) -> dict:
    """运行中作业一次 SSH 活体取数 → live dict(健康计数器+进度)。

    取 离子步数(grep -c 'F=' 全文精确)/ 当前 |F|max(OUTCAR 'FORCES: max atom'
    末行,原版 running_progress 口径)/ OSZICAR 尾部(震荡扫描),交 diagnose.live_health
    做跨轮确认。远端取数失败 → 保留上轮计数器不误清零。
    """
    if not remote:
        return dict(prev or {})
    q = shlex.quote
    out, _ = run_cmd(
        client,
        f'grep -c "F=" {q(remote + "/OSZICAR")} 2>/dev/null || echo 0; '
        f"echo '___VCSLIVE___'; "
        f'grep "FORCES: max atom" {q(remote + "/OUTCAR")} 2>/dev/null | tail -1; '
        f"echo '___VCSLIVE___'; "
        f'tail -n 200 {q(remote + "/OSZICAR")} 2>/dev/null')
    parts = out.split('___VCSLIVE___')
    if len(parts) != 3:
        return dict(prev or {})                     # 取数异常:保留上轮计数,不误清零
    steps = _first_int(parts[0])
    fmax = ''
    toks = parts[1].split()
    if 'RMS' in toks:                                # 'FORCES: max atom, RMS  0.031  0.012'
        i = toks.index('RMS')
        if len(toks) > i + 1:
            fmax = toks[i + 1]
    live = diagnose.live_health(parts[2], steps, prev)
    live['ionic_steps'] = steps
    live['fmax'] = fmax
    return live


def _stat_sizes(client, remote: str):
    """一次 stat 取 OUTCAR/OSZICAR 字节数 → (outcar, oszicar);缺失文件对应 None。

    与收敛 grep 分开:区分"缺输出/启动即死"(size None/0)与"跑了但没收敛",
    堵掉旧版 '|| echo 0' 把缺 OUTCAR 误当未收敛的假阴性(缺口分析 P0)。
    """
    outcar, oszicar, _mtime = _stat_outcar_full(client, remote)
    return outcar, oszicar


def _stat_outcar_full(client, remote: str):
    """一次 stat 取 OUTCAR/OSZICAR 的字节数与 OUTCAR mtime(远端 epoch 秒)。

    → (outcar_size, oszicar_size, outcar_mtime);缺失/无 remote 对应 None。
    mtime 用于续算沉降护栏:判断"当前这一轮是否真的重写过 OUTCAR",
    避免续算后新作业尚未启动时误读上一轮旧 OUTCAR(见 refresh_job)。
    mtime 与 baseline 同取自集群 stat,同一时钟,无 client/server 时钟偏差问题。
    """
    if not remote:
        return None, None, None
    out, _ = run_cmd(client, f"cd {shlex.quote(remote)} && "
                             f"stat -c '%n %s %Y' OUTCAR OSZICAR 2>/dev/null")
    sizes: dict[str, int] = {}
    mtimes: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        # 兼容旧格式(仅 name size,2 列)与新格式(name size mtime,3 列)
        if len(parts) >= 2 and parts[1].lstrip('-').isdigit():
            sizes[parts[0]] = int(parts[1])
        if len(parts) >= 3 and parts[2].lstrip('-').isdigit():
            mtimes[parts[0]] = int(parts[2])
    return sizes.get('OUTCAR'), sizes.get('OSZICAR'), mtimes.get('OUTCAR')


# 收敛标志按任务类型分流(review-round2 收尾):'reached required accuracy' 是**离子弛豫**
# 收敛标志,static/dos/band(NSW=0)永远不出这行,用它判会把收敛的静态作业误判未收敛;
# 静态类作业查电子收敛标志 'aborting loop because EDIFF is reached'。
_IONIC_MARK = 'reached required accuracy'
_ELEC_MARK = 'aborting loop because EDIFF is reached'
_STATIC_TASKS = ('static', 'dos', 'band')


# 干净退出页脚 + STOPCAR 叫停(网研核对:Kitchin/sisl 口径 completed≠converged;
# pymatgen Outcar.is_stopped 的 soft stop 双空格字面串)
_CLEAN_EXIT_MARK = 'General timing and accounting informations for this job'
_STOP_MARK = 'soft stop encountered'


def _grep_marks(client, remote: str, task_type: str = 'relax'):
    """一次 SSH 取 OUTCAR 三个标志计数 → (converged, clean_exit, stopped)。

    缺文件时三者 (False, False, False);clean_exit 独立于收敛(被杀作业绝不写页脚)。
    """
    if not remote:
        return False, False, False
    mark = _ELEC_MARK if task_type in _STATIC_TASKS else _IONIC_MARK
    o = shlex.quote(remote + '/OUTCAR')
    out, _ = run_cmd(
        client,
        f'grep -c "{mark}" {o} 2>/dev/null || echo 0; '
        f'grep -c "{_CLEAN_EXIT_MARK}" {o} 2>/dev/null || echo 0; '
        f'grep -c "{_STOP_MARK}" {o} 2>/dev/null || echo 0')
    nums = [_first_int(ln) for ln in out.splitlines() if ln.strip()]
    nums += [0] * (3 - len(nums))
    return nums[0] > 0, nums[1] > 0, nums[2] > 0


def _grep_converged(client, remote: str, task_type: str = 'relax') -> bool:
    """兼容入口:只要收敛位。"""
    return _grep_marks(client, remote, task_type)[0]


def _read_nelm(job_dir: str) -> int:
    """本地作业目录 INCAR 的 NELM(缺失/读不到 → VASP 默认 60)。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        with open(os.path.join(job_dir, 'INCAR'), 'r', encoding='utf-8', errors='replace') as f:
            v = parse_incar(f.read()).get('NELM')
        return int(v) if isinstance(v, (int, float)) and int(v) > 0 else 60
    except (OSError, ValueError, TypeError):
        return 60


def _read_log(client, remote: str, job_id: str = ''):
    """取本作业的 stdout 日志:EXIT 标记(退出码)+ 尾部文本(供 diagnose.scan_log 扫签名)。

    脚本 run_block 尾部 echo "EXIT: $?";PBS -j oe 合并到 <name>.o<num>,Slurm 到
    slurm-<jid>.out。**按本作业号定位**:续算在同一目录重投,旧 attempt 的 .o<旧号>
    仍在,通配 *.o* + tail -1 会按字母序读到旧作业(o100 < o99),把新一轮误分类;
    故用 *.o<本号> / slurm-<本号>.out 精确锁定,再以每次覆盖的 vasp.out/log 兜底。
    → (exit_code|None, tail)。
    """
    if not remote:
        return None, ''
    num = str(job_id).split('.')[0].strip()
    if num:
        targets = f'*.o{num} slurm-{num}.out vasp.out log'
    else:                                        # 无作业号 → 退回宽通配(单作业目录仍准)
        targets = '*.o* slurm-*.out vasp.out log'
    out, _ = run_cmd(
        client,
        f"cd {shlex.quote(remote)} && "
        f"grep -h 'EXIT:' {targets} 2>/dev/null | tail -1; "
        f"echo '___VCSLOG___'; "
        f"tail -n 20 {targets} 2>/dev/null")
    marker, _sep, tail = out.partition('___VCSLOG___')
    mm = re.search(r'EXIT:\s*(-?\d+)', marker)
    exit_code = int(mm.group(1)) if mm else None
    return exit_code, tail


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


# ── 有界恢复:CONTCAR 续算(对齐论文 bounded recovery;人工触发,冻结 INCAR) ──────
CONTINUE_MAX_ROUNDS = 3


def _read_remote_text(client, path: str) -> str:
    out, _ = run_cmd(client, f'cat {shlex.quote(path)} 2>/dev/null')
    return out


def continue_from_contcar(client, profile, job_dir: str,
                          max_rounds: int = CONTINUE_MAX_ROUNDS) -> dict:
    """把一个可续算作业从 CONTCAR 接着跑(cp CONTCAR POSCAR + 冻结 INCAR 重投同一脚本)。

    有界恢复(论文核心 + 交接三不变式):
    - 只对 diagnose 标 restartable 的分类(未收敛/墙钟/ZBRENT)出手,否则拒绝;
    - CONTCAR 必须通过 valid_poscar 校验(防拿半个结构续出垃圾);
    - **INCAR 逐字冻结**(方法学主权,无可比性护栏前的安全默认);
    - continue_rounds 硬上限(默认 3),到顶停机交人工(防死循环);
    - 清远端 WAVECAR/CHGCAR 去混合历史(原版实测);记 prev_job_id 溯源。
    失败抛 ValueError/RuntimeError(中文)。成功返回更新后的 manifest(state=SUBMITTED)。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法续算')
    # 状态门(严重 bug 防护):仍在队列/运行中的作业绝不续算——否则会往活作业目录里
    # cp CONTCAR POSCAR + 重投第二个实例,两个 VASP 同写 OUTCAR 冲垮结果,且旧作业号被
    # 覆盖成孤儿。restartable 诊断是上一轮终态留下的,重投后必须消费掉(见函数尾)。
    if m.get('state') in ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING'):
        raise ValueError(f"该作业仍在队列/运行中(状态 {m['state']}),不能续算;请先查询状态确认已结束")
    diag = (m.get('results') or {}).get('diagnosis') or {}
    if not diag.get('restartable'):
        raise ValueError(
            f"该作业不可自动续算(分类 {diag.get('failure_class', '?')});"
            f'仅 未收敛/墙钟/ZBRENT 等可从 CONTCAR 续算,硬崩/缺输出需人工')
    rounds = int((m.get('results') or {}).get('continue_rounds', 0))
    if rounds >= max_rounds:
        raise RuntimeError(f'已续算 {rounds} 次达上限 {max_rounds},停机交人工(防死循环)')
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业无 remote_dir(未提交过),无法续算')

    contcar = _read_remote_text(client, posixpath.join(remote, 'CONTCAR'))
    if not diagnose.valid_poscar(contcar):
        raise RuntimeError('远端 CONTCAR 缺失或不完整,不能续算(防半个结构续出垃圾),请人工检查')

    # 续算沉降基线:重投前记下上一轮 OUTCAR 的 mtime(此刻新作业尚未启动,仍是旧文件)
    _o0, _z0, _base_outcar_mtime = _stat_outcar_full(client, remote)

    # 本地也留证:备份旧 POSCAR,用 CONTCAR 覆盖(保持本地目录与远端一致)
    local_poscar = os.path.join(job_dir, 'POSCAR')
    if os.path.isfile(local_poscar):
        shutil.copyfile(local_poscar, f'{local_poscar}.bak{rounds + 1}')
    with open(local_poscar, 'w', encoding='utf-8', newline='') as f:
        f.write(contcar)

    # 远端:CONTCAR→POSCAR + 清混合历史,再重投同一脚本(INCAR 不动)
    run_cmd(client, f'cd {shlex.quote(remote)} && cp CONTCAR POSCAR && rm -f WAVECAR CHGCAR',
            check=True)
    dialect = get_dialect(profile.scheduler)
    out, err = run_cmd(client, dialect.submit_cmd(
        posixpath.join(remote, SCRIPT_NAME), getattr(profile, 'scheduler_bin', '')))
    job_id = dialect.parse_job_id(out)
    if not job_id:
        raise RuntimeError(f'续算重投失败,{dialect.name} 返回:{(out or err).strip()[:300]}')

    prev = m.get('scheduler_job_id')
    m['scheduler_job_id'] = job_id
    # 消费掉上一轮的终态诊断:新作业尚未诊断,restartable=True 不能被下一次误用
    m.setdefault('results', {}).pop('diagnosis', None)
    m.setdefault('results', {})['continue_rounds'] = rounds + 1
    _set_continue_baseline(m, _base_outcar_mtime)  # 沉降护栏基线:防新作业未启动前误读旧 OUTCAR
    m.setdefault('attempts', []).append({
        'n': len(m.get('attempts') or []) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': 'continued',
        'action': 'contcar_restart',
        'prev_job_id': prev,
        'job_id': job_id,
        'round': rounds + 1,
    })
    manifest_mod.set_state(m, 'SUBMITTED',
                           note=f'CONTCAR 续算 第{rounds + 1}轮(prev {prev} → {job_id},INCAR 冻结)')
    manifest_mod.save_manifest(job_dir, m)
    return m


# ── 改参续算(S6 修复引擎):白名单键受控修改 INCAR 后重投 ─────────────────────
# 只放非方法学"数值旋钮":收敛算法/展宽/混合/步长/迭代上限/能带数。ENCUT/泛函/
# IVDW/ISPIN/赝势这类**决定可比性**的键绝不进白名单(方法学主权不破)。
INCAR_TUNE_WHITELIST = frozenset({
    'ALGO', 'ISMEAR', 'SIGMA', 'AMIX', 'BMIX', 'AMIX_MAG', 'BMIX_MAG', 'IMIX',
    'POTIM', 'IBRION', 'NELM', 'NELMIN', 'NSW', 'NBANDS', 'LREAL', 'ISYM',
    'SYMPREC', 'AMIN', 'MAXMIX', 'NCORE', 'KPAR', 'EDIFF',
})
_TUNE_BANNER = '# --- vcstudio 改参续算 第{round}轮 {at} ---'


def continue_with_incar_changes(client, sftp, profile, job_dir: str,
                                changes: dict,
                                max_rounds: int = CONTINUE_MAX_ROUNDS,
                                restart_from_contcar: bool = True) -> dict:
    """诊断建议 → 受控改参重投:白名单键追加覆盖到 INCAR 文末(原文一字不删),
    可选 CONTCAR→POSCAR,清 WAVECAR/CHGCAR,重投同一脚本。

    与冻结续算共用状态门/轮次上限;差异:
    - changes 仅允许 INCAR_TUNE_WHITELIST 键(违例 ValueError 点名,绝不静默丢弃);
    - 放宽 restartable 限制:SCF_SLOSHING/EDDDAV 等 NEEDS_HUMAN 类正是改参对象,
      故只要求终态(不在队/不在跑),不要求 diagnose.restartable;
    - INCAR 修改以"追加覆盖块"落地(VASP 取同键末次出现值;原文保留可审计),
      同步上传远端;attempts 记录完整 changes。
    """
    if not changes:
        raise ValueError('未提供任何 INCAR 修改项')
    bad = [k for k in changes if str(k).upper() not in INCAR_TUNE_WHITELIST]
    if bad:
        raise ValueError(
            f'以下键不在改参白名单,拒绝修改:{", ".join(bad)}。'
            f'白名单(非方法学旋钮):{", ".join(sorted(INCAR_TUNE_WHITELIST))}')
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法改参续算')
    if m.get('state') in ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING'):
        raise ValueError(f"该作业仍在队列/运行中(状态 {m['state']}),不能改参重投")
    rounds = int((m.get('results') or {}).get('continue_rounds', 0))
    if rounds >= max_rounds:
        raise RuntimeError(f'已续算 {rounds} 次达上限 {max_rounds},停机交人工(防死循环)')
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业无 remote_dir(未提交过),无法改参续算')

    # 1) 本地 INCAR:备份 + 追加覆盖块(原文保留)
    local_incar = os.path.join(job_dir, 'INCAR')
    if not os.path.isfile(local_incar):
        raise ValueError('本地作业目录缺 INCAR')
    with open(local_incar, 'r', encoding='utf-8', errors='replace') as f:
        incar_text = f.read()
    shutil.copyfile(local_incar, f'{local_incar}.bak{rounds + 1}')
    at = time.strftime('%Y-%m-%dT%H:%M:%S')
    block = '\n' + _TUNE_BANNER.format(round=rounds + 1, at=at) + '\n'
    block += ''.join(f'{str(k).upper()} = {v}\n' for k, v in changes.items())
    new_text = (incar_text if incar_text.endswith('\n') else incar_text + '\n') + block
    with open(local_incar, 'w', encoding='utf-8', newline='') as f:
        f.write(new_text)

    # 2) 可选 CONTCAR 续结构(结构没跑几步/硬崩时也允许保持原 POSCAR 重跑)
    if restart_from_contcar:
        contcar = _read_remote_text(client, posixpath.join(remote, 'CONTCAR'))
        if diagnose.valid_poscar(contcar):
            local_poscar = os.path.join(job_dir, 'POSCAR')
            if os.path.isfile(local_poscar):
                shutil.copyfile(local_poscar, f'{local_poscar}.bak{rounds + 1}')
            with open(local_poscar, 'w', encoding='utf-8', newline='') as f:
                f.write(contcar)
            run_cmd(client, f'cd {shlex.quote(remote)} && cp CONTCAR POSCAR', check=True)

    # 续算沉降基线:重投前记下上一轮 OUTCAR 的 mtime(此刻新作业尚未启动,仍是旧文件)
    _o0, _z0, _base_outcar_mtime = _stat_outcar_full(client, remote)

    # 3) 上传新 INCAR + 清混合历史,重投同一脚本
    sftp.put(local_incar, posixpath.join(remote, 'INCAR'))
    run_cmd(client, f'cd {shlex.quote(remote)} && rm -f WAVECAR CHGCAR', check=True)
    dialect = get_dialect(profile.scheduler)
    out, err = run_cmd(client, dialect.submit_cmd(
        posixpath.join(remote, SCRIPT_NAME), getattr(profile, 'scheduler_bin', '')))
    job_id = dialect.parse_job_id(out)
    if not job_id:
        raise RuntimeError(f'改参重投失败,{dialect.name} 返回:{(out or err).strip()[:300]}')

    prev = m.get('scheduler_job_id')
    m['scheduler_job_id'] = job_id
    m.setdefault('results', {}).pop('diagnosis', None)
    m.setdefault('results', {})['continue_rounds'] = rounds + 1
    _set_continue_baseline(m, _base_outcar_mtime)  # 沉降护栏基线:防新作业未启动前误读旧 OUTCAR
    m.setdefault('attempts', []).append({
        'n': len(m.get('attempts') or []) + 1,
        'at': at,
        'result': 'continued',
        'action': 'incar_tuned_restart',
        'incar_changes': {str(k).upper(): str(v) for k, v in changes.items()},
        'from_contcar': bool(restart_from_contcar),
        'prev_job_id': prev,
        'job_id': job_id,
        'round': rounds + 1,
    })
    manifest_mod.set_state(
        m, 'SUBMITTED',
        note=f'改参续算 第{rounds + 1}轮({", ".join(f"{str(k).upper()}={v}" for k, v in changes.items())};'
             f'prev {prev} → {job_id})')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _read_oszicar(client, remote_dir: str):
    """OSZICAR 尾部一次取数 → (E0|None, tail_text)。

    tail -150 覆盖最后一个离子步的完整 SCF 块(供 diagnose 震荡扫描)且必含末行
    E0(取最后一个匹配)。拿不到 E0 → None(绝不编数)。"""
    if not remote_dir:
        return None, ''
    out, _ = run_cmd(client, f'tail -n 150 {shlex.quote(remote_dir + "/OSZICAR")} 2>/dev/null')
    m = None
    for m in _E0_RE.finditer(out):
        pass                                   # 取最后一个匹配
    if m is None:
        return None, out
    try:
        return float(m.group(1)), out
    except ValueError:
        return None, out


def _read_e0(client, remote_dir: str):
    """兼容入口:只要 E0。"""
    return _read_oszicar(client, remote_dir)[0]
