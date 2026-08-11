"""批量远程操作线程体(UI 无关):连接→逐作业操作→结果列表。

从 gui/jobs_tab.py 搬出,Tk 与 gui_web 两个 GUI 共用。
本模块绝不 import Tk/webview 等 GUI 框架;paramiko 延迟导入。
返回统一 {'needs_trust': bool, 'message': str, 'results'|'jobs': list}。
needs_trust=True 时透传 fingerprint/algorithm/host,供界面展示并精确 pin。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import posixpath
import re

from vcstudio.cluster import submitter
from vcstudio.cluster.connection import open_client, close_quiet, ConnectError
from vcstudio.cluster.schedulers import get_dialect
from vcstudio.shared import manifest as manifest_mod


def _trust_payload(error: ConnectError, **empty) -> dict:
    """把连接层 missing_host_key 捕获的真实 key 证据原样透传。"""
    empty.update({
        'needs_trust': True,
        'message': str(error),
        'fingerprint': getattr(error, 'fingerprint', ''),
        'algorithm': getattr(error, 'algorithm', ''),
        'host': getattr(error, 'host', ''),
    })
    return empty


# ── 后台线程体(不碰 Tk) ─────────────────────────────────────────────────────
def _job_errors():
    """单作业可失败的异常集:一条作业出错(含连接抖动的 SSHException)只失败那一条,
    不打断整批。paramiko 延迟导入,保持本模块可在无 paramiko 环境导入。"""
    from paramiko.ssh_exception import SSHException
    return (ValueError, RuntimeError, OSError, SSHException)


def submit_batch(prof, pw, dirs, trust_new, *, idempotency_key=None):
    dirs = list(dirs)
    # 整批联网前解析远程目标。不同本地目录（或同一路径被重复选中）若映射到同一个
    # remote_dir，后上传者会覆盖先上传者并产生两个同目录作业，必须整批 fail closed。
    planned = {}
    for d in dirs:
        try:
            remote = posixpath.normpath(submitter.planned_remote_dir(prof, d))
        except (ValueError, TypeError, OSError, AttributeError):
            continue                     # 单作业的详细输入错误仍由 submit_job/preflight 返回
        planned.setdefault(remote, []).append(str(d))
    conflicts = {remote: paths for remote, paths in planned.items() if len(paths) > 1}
    if conflicts:
        detail = '；'.join(
            f'{remote} ← {", ".join(paths)}' for remote, paths in conflicts.items())
        message = ('整批未提交：多个本地作业映射到同一远程目录，可能互相覆盖。'
                   f'请重命名作业目录或设置不同 remote_namespace：{detail}')
        return {'needs_trust': False,
                'results': [(d, False, message) for d in dirs]}
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, results=[])
        raise RuntimeError(str(e))
    results = []
    busy_count = 0
    recovery_count = 0
    recovery_job_ids = []
    try:
        sftp = client.open_sftp()
        for d in dirs:
            try:
                if idempotency_key:
                    m = submitter.submit_job(
                        client, sftp, prof, d, idempotency_key=idempotency_key)
                else:
                    # Preserve the historical injectable four-argument seam;
                    # production web calls always provide their operation id.
                    m = submitter.submit_job(client, sftp, prof, d)
                verb = '已确认提交' if m.get('_submission_replayed') else '已提交'
                results.append((d, True, f"{verb},作业号 {m['scheduler_job_id']}"))
            except submitter.JobOperationBusy as e:
                busy_count += 1
                results.append((d, False, str(e)))
            except submitter.UnknownRemoteSubmission as e:
                recovery_count += 1
                if e.scheduler_job_id:
                    recovery_job_ids.append(e.scheduler_job_id)
                results.append((d, False, str(e)))
            except _job_errors() as e:
                results.append((d, False, str(e)))
        sftp.close()
    finally:
        close_quiet(client, jump)
    payload = {'needs_trust': False, 'results': results}
    if busy_count:
        payload.update({
            'ok': False,
            'busy': True,
            'code': 'job_busy',
            'busy_count': busy_count,
        })
    if recovery_count:
        payload.update({
            'ok': False,
            'code': 'unknown_remote_submission',
            'requires_manual_recovery': True,
            'recovery_count': recovery_count,
        })
        if recovery_job_ids:
            payload['scheduler_job_ids'] = recovery_job_ids
    return payload


def fetch_batch(prof, pw, dirs, trust_new, files=None):
    """Fetch selected jobs in one SSH session.

    ``files=None`` deliberately stays ``None`` for every job so submitter can
    inspect that directory's manifest and choose DOS/能带/Bader/ELF/功函数/AIMD
    outputs independently.  An explicit list remains the user's manual
    override and is applied to every selected job.
    """
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, results=[])
        raise RuntimeError(str(e))
    results = []
    try:
        sftp = client.open_sftp()
        for d in dirs:
            try:
                submitter.assert_profile_binding(prof, d, '拉回结果')
                # fetch_results 内再用同一 profile 对读到的 manifest 做硬校验，
                # 避免外层检查与真正 SFTP 下载之间 job.yaml 被换代的 TOCTOU。
                fetched, missing = submitter.fetch_results(
                    client, sftp, d, files=files, profile=prof)
                msg = '已拉回 ' + ('、'.join(fetched) if fetched else '(无)')
                if missing:
                    msg += f'(远端缺 {"、".join(missing)})'
                if 'CONTCAR' in fetched:                 # 全自动渲结构图(缓存,失败跳过)
                    from vcstudio.external import povray_render
                    figs_dir = os.path.join(d, 'figs')
                    rr = povray_render.render_poscar_views(
                        os.path.join(d, 'CONTCAR'), figs_dir,
                        os.path.basename(os.path.normpath(d)))
                    msg += (f',结构图 ✓ → {figs_dir}' if rr['ok']
                            else f",结构图跳过({rr['error'][:60]})")
                results.append((d, bool(fetched), msg))
            except _job_errors() as e:
                results.append((d, False, str(e)))
        sftp.close()
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}


def filter_continuable(dirs):
    """本地预筛(证据都在 job.yaml):终态 + diagnosis.restartable + 未达轮次上限。

    返回 (可续算 dirs, 跳过数)。避免把整批原样送去连接后逐个失败刷屏,且不对仍在跑的
    作业出手(与 submitter.continue_from_contcar 的状态门一致)。纯函数,可离线测。
    """
    eligible, skipped = [], 0
    for d in dirs:
        m = manifest_mod.load_manifest(d)
        res = (m or {}).get('results') or {}
        dgn = res.get('diagnosis') or {}
        rounds = int(res.get('continue_rounds', 0))
        if (m is not None
                and m.get('state') not in ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING')
                and str(m.get('task_type') or '') != 'neb'
                and dgn.get('restartable')
                and rounds < submitter.CONTINUE_MAX_ROUNDS):
            eligible.append(d)
        else:
            skipped += 1
    return eligible, skipped


def continue_batch(prof, pw, dirs, trust_new):
    """CONTCAR 续算批量线程体:每作业调 submitter.continue_from_contcar(不可续算的自失败)。"""
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, results=[])
        raise RuntimeError(str(e))
    results = []
    try:
        for d in dirs:
            try:
                m = submitter.continue_from_contcar(client, prof, d)
                results.append((d, True, f"已续算重投,新作业号 {m['scheduler_job_id']}"))
            except _job_errors() as e:
                results.append((d, False, str(e)))
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}


def workdir_lookup(prof, pw, job_id, trust_new):
    """单作业远程工作目录查询线程体(P1b 认领免手填;PBS qstat -f,Slurm 直接空)。"""
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, workdir='')
        raise RuntimeError(str(e))
    try:
        wd = submitter.query_workdir(client, prof, str(job_id))
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'workdir': wd}


def queue_detail(prof, pw, trust_new):
    """集群队列全量明细线程体。"""
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, jobs=[])
        raise RuntimeError(str(e))
    try:
        jobs = submitter.query_queue_detail(client, prof)
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'jobs': jobs}


def _sanitize_seg(name: str) -> str:
    """作业名 → 安全本地目录段:保留 [A-Za-z0-9_.-],其余替换 '_';空则 '_'。"""
    s = re.sub(r'[^A-Za-z0-9_.-]', '_', str(name or ''))
    return s or '_'


def adopt_scan(prof, pw, trust_new, known_ids, local_root):
    """一键认领:一次连接完成 队列明细 → 补工作目录 → 逐个落 adopt_external_job。

    known_ids: 已纳管的调度器作业号集合(str);local_root: 本地目录根。
    对每个 str(job_id) 不在 known_ids 的队列作业:
      - workdir = job['workdir'] 或(空时)同连接内 submitter.query_workdir(不重连);
      - 有 workdir → local_dir = local_root/<sanitize(作业名或job_id)>(makedirs)→ 认领;
      - 查不到 workdir → 标记需手填(交前端走单个认领弹窗)。
    已纳管作业跳过(不入 results)。返回 {'needs_trust', 'results': [[job_id, ok, msg]]}。
    """
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, results=[])
        raise RuntimeError(str(e))
    known = {str(k) for k in (known_ids or [])}
    results = []
    try:
        jobs = submitter.query_queue_detail(client, prof)
        for j in jobs:
            jid = str(j.get('job_id'))
            if jid in known:
                continue                                 # 已纳管:静默跳过
            workdir = j.get('workdir') or submitter.query_workdir(client, prof, jid)
            if not workdir:
                results.append([jid, False, '查不到工作目录,请单个认领手填'])
                continue
            seg = _sanitize_seg(j.get('name') or jid)
            local_dir = os.path.join(local_root, seg)
            remote_key = posixpath.normpath(str(workdir))
            # 同名撞车(qstat 常截断作业名):目标目录已属于另一作业则追加 _<jid> 避让,
            # 否则第二个认领会落到第一个的目录并报第一个的作业号。已属本 jid 则复用(幂等)。
            if os.path.isdir(local_dir):
                existing = manifest_mod.load_manifest(local_dir)
                same_binding = bool(
                    existing
                    and str(existing.get('scheduler_job_id') or '') == jid
                    and str(existing.get('cluster') or '') == str(prof.name)
                    and posixpath.normpath(str(existing.get('remote_dir') or ''))
                    == remote_key)
                if same_binding:
                    # 台账丢失但本地绑定仍完整时，认领应幂等恢复登记，不能因为
                    # adopt_external_job 拒绝覆盖已有作业号而制造一个假失败。
                    try:
                        from vcstudio.cluster import ledger
                        ledger.register(local_dir)
                        results.append([jid, True, f'已纳管（原绑定）→ {local_dir}'])
                    except _job_errors() as e:
                        results.append([jid, False, str(e)])
                    continue
                # 不同服务器可出现同名、同作业号；后缀必须含 profile，且目标若仍
                # 被占用就继续编号，绝不能复用另一台服务器的本地目录。
                profile_seg = _sanitize_seg(getattr(prof, 'name', '') or 'cluster')
                base = os.path.join(local_root, f'{seg}_{profile_seg}_{jid}')
                local_dir = base
                serial = 2
                while os.path.isdir(local_dir):
                    bound = manifest_mod.load_manifest(local_dir)
                    if (bound
                            and str(bound.get('scheduler_job_id') or '') == jid
                            and str(bound.get('cluster') or '') == str(prof.name)
                            and posixpath.normpath(str(bound.get('remote_dir') or ''))
                            == remote_key):
                        try:
                            from vcstudio.cluster import ledger
                            ledger.register(local_dir)
                            results.append([jid, True, f'已纳管（原绑定）→ {local_dir}'])
                        except _job_errors() as e:
                            results.append([jid, False, str(e)])
                        local_dir = ''
                        break
                    local_dir = f'{base}_{serial}'
                    serial += 1
                if not local_dir:
                    continue
            try:
                os.makedirs(local_dir, exist_ok=True)
                submitter.adopt_external_job(local_dir, prof, jid, workdir,
                                             name=j.get('name', ''))
                results.append([jid, True, f'已认领 → {local_dir}'])
            except _job_errors() as e:
                results.append([jid, False, str(e)])
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}


def tune_batch(prof, pw, job_dir, changes, trust_new, from_contcar=True):
    """改参续算线程体(单作业)。"""
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, results=[])
        raise RuntimeError(str(e))
    results = []
    try:
        sftp = client.open_sftp()
        try:
            m = submitter.continue_with_incar_changes(
                client, sftp, prof, job_dir, changes,
                restart_from_contcar=from_contcar)
            results.append((job_dir, True, f"已改参重投,新作业号 {m['scheduler_job_id']}"))
        except _job_errors() as e:
            results.append((job_dir, False, str(e)))
        sftp.close()
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}


def refresh_batch(prof, pw, dirs, trust_new):
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return _trust_payload(e, results=[])
        raise RuntimeError(str(e))
    results = []
    try:
        scheduler_states, reasons = submitter.query_scheduler(client, prof)
        for d in dirs:
            try:
                submitter.assert_profile_binding(prof, d, '刷新状态')
                m = submitter.refresh_job(client, prof, d, live_states=scheduler_states,
                                          terminal_reasons=reasons)
                note = m['state']
                res = m.get('results') or {}
                dgn = res.get('diagnosis') or {}
                if dgn.get('failure_class') and m['state'] in ('FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'):
                    note += f" [{dgn['failure_class']}{'·可续算' if dgn.get('restartable') else ''}] {dgn.get('evidence', '')}"
                live_result = res.get('live') or {}
                if m['state'] == 'RUNNING':
                    if live_result.get('warning'):
                        note += f" ⚠{live_result['warning']}"
                    elif live_result.get('ionic_steps') is not None:
                        note += f"({live_result['ionic_steps']} 离子步" + (
                            f",|F|max={live_result['fmax']}"
                            if live_result.get('fmax') else '') + ')'
                e0 = res.get('energy_e0_eV')
                if e0 is not None:
                    note += f'(E0={e0:.4f} eV)'
                results.append((d, note))
            except _job_errors() as e:
                results.append((d, f'查询失败:{e}'))
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}


def cancel_batch(profile, jobs, *, password=None, trust_new=False):
    """批量取消作业线程体:逐作业 qdel/scancel + 回写 manifest 状态。

    jobs:作业目录列表(每目录 job.yaml 的 scheduler_job_id 提供调度器作业号)。复用
    submit/refresh 同一 open_client 连接模式;单作业失败(缺作业号/取消命令报错)只失败
    该条,不打断整批。取消命令走 run_cmd(check=True),调度器退出码非零(如作业已不存在/
    无权限)记为该条 failed 并透传调度器文案。

    状态回写:manifest.VALID_STATES 无 'CANCELLED',按裁决改写 FAILED + note '用户取消'
    (绝不发明 manifest 不认的状态)。

    返回 {'ok', 'cancelled':[job_id], 'failed':[{'job_id','reason'}], 'error', 'needs_trust'}。
    调度器不支持/连接失败 → ok=False + error;needs_trust 供前端可信任重试。
    """
    out = {'ok': False, 'cancelled': [], 'failed': [], 'error': None,
           'needs_trust': False}
    try:
        dialect = get_dialect(profile.scheduler)      # 不支持的调度器早失败(绝不静默)
    except ValueError as e:
        out['error'] = str(e)
        return out
    try:
        client, jump = open_client(profile, password, trust_new=trust_new)
    except ConnectError as e:
        out['error'] = str(e)
        out['needs_trust'] = e.needs_trust
        if e.needs_trust:
            out.update({
                'fingerprint': getattr(e, 'fingerprint', ''),
                'algorithm': getattr(e, 'algorithm', ''),
                'host': getattr(e, 'host', ''),
            })
        return out

    bin_path = getattr(profile, 'scheduler_bin', '')
    try:
        for d in jobs:
            m = manifest_mod.load_manifest(d)
            jid = str((m or {}).get('scheduler_job_id') or '')
            if not m or not jid:
                out['failed'].append(
                    {'job_id': jid or str(d),
                     'reason': '无 job.yaml 或缺 scheduler_job_id,无法取消'})
                continue
            try:
                with submitter.job_operation(d, '取消作业'):
                    # 入锁后重读，防止等待期间作业已被续算为另一 job id。
                    m = manifest_mod.load_manifest(d)
                    current_jid = str((m or {}).get('scheduler_job_id') or '')
                    if not m or current_jid != jid:
                        raise RuntimeError('取消前作业代次已变化，请刷新后重试')
                    submitter.assert_profile_binding(
                        profile, d, '取消作业', manifest=m)
                    submitter.run_cmd(
                        client, dialect.cancel_cmd(jid, bin_path), check=True)
                    # CANCELLED 非 manifest 合法态 → FAILED + note '用户取消'(裁决口径)
                    manifest_mod.set_state(m, 'FAILED', note='用户取消')
                    manifest_mod.save_manifest(d, m)
                    out['cancelled'].append(jid)
            except _job_errors() as e:
                out['failed'].append({'job_id': jid, 'reason': str(e)})
    finally:
        close_quiet(client, jump)
    out['ok'] = True
    return out
