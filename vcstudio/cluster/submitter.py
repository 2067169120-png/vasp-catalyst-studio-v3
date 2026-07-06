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

    # 终态(GONE / 调度器终态原因)→ 取证 + 分类(复活 FAILED/NEEDS_HUMAN)
    remote = m.get('remote_dir') or ''
    reason = (terminal_reasons or {}).get(jid)
    outcar_size, oszicar_size = _stat_sizes(client, remote)
    converged = _grep_converged(client, remote, m.get('task_type') or 'relax')
    exit_code, log_tail = _read_log(client, remote, jid)
    energy = _read_e0(client, remote)

    d = diagnose.classify(
        scheduler_reason=reason, exit_code=exit_code,
        outcar_size=outcar_size, oszicar_size=oszicar_size,
        log_tail=log_tail, converged=converged, energy=energy)

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


def _stat_sizes(client, remote: str):
    """一次 stat 取 OUTCAR/OSZICAR 字节数 → (outcar, oszicar);缺失文件对应 None。

    与收敛 grep 分开:区分"缺输出/启动即死"(size None/0)与"跑了但没收敛",
    堵掉旧版 '|| echo 0' 把缺 OUTCAR 误当未收敛的假阴性(缺口分析 P0)。
    """
    if not remote:
        return None, None
    out, _ = run_cmd(client, f"cd {shlex.quote(remote)} && "
                             f"stat -c '%n %s' OUTCAR OSZICAR 2>/dev/null")
    sizes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip('-').isdigit():
            sizes[parts[0]] = int(parts[1])
    return sizes.get('OUTCAR'), sizes.get('OSZICAR')


# 收敛标志按任务类型分流(review-round2 收尾):'reached required accuracy' 是**离子弛豫**
# 收敛标志,static/dos/band(NSW=0)永远不出这行,用它判会把收敛的静态作业误判未收敛;
# 静态类作业查电子收敛标志 'aborting loop because EDIFF is reached'。
_IONIC_MARK = 'reached required accuracy'
_ELEC_MARK = 'aborting loop because EDIFF is reached'
_STATIC_TASKS = ('static', 'dos', 'band')


def _grep_converged(client, remote: str, task_type: str = 'relax') -> bool:
    """OUTCAR 是否含对应任务类型的收敛标志(缺文件 → False)。"""
    if not remote:
        return False
    mark = _ELEC_MARK if task_type in _STATIC_TASKS else _IONIC_MARK
    out, _ = run_cmd(client, f'grep -c "{mark}" '
                             f'{shlex.quote(remote + "/OUTCAR")} 2>/dev/null || echo 0')
    return _first_int(out) > 0


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
