"""批量远程操作线程体(UI 无关):连接→逐作业操作→结果列表。

从 gui/jobs_tab.py 搬出,Tk 与 gui_web 两个 GUI 共用。
本模块绝不 import Tk/webview 等 GUI 框架;paramiko 延迟导入。
返回统一 {'needs_trust': bool, 'message': str, 'results'|'jobs': list}。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re

from vcstudio.cluster import submitter
from vcstudio.cluster.connection import open_client, close_quiet, ConnectError
from vcstudio.shared import manifest as manifest_mod


# ── 后台线程体(不碰 Tk) ─────────────────────────────────────────────────────
def _job_errors():
    """单作业可失败的异常集:一条作业出错(含连接抖动的 SSHException)只失败那一条,
    不打断整批。paramiko 延迟导入,保持本模块可在无 paramiko 环境导入。"""
    from paramiko.ssh_exception import SSHException
    return (ValueError, RuntimeError, OSError, SSHException)


def submit_batch(prof, pw, dirs, trust_new):
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return {'needs_trust': True, 'message': str(e), 'results': []}
        raise RuntimeError(str(e))
    results = []
    try:
        sftp = client.open_sftp()
        for d in dirs:
            try:
                m = submitter.submit_job(client, sftp, prof, d)
                results.append((d, True, f"已提交,作业号 {m['scheduler_job_id']}"))
            except _job_errors() as e:
                results.append((d, False, str(e)))
        sftp.close()
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}


def fetch_batch(prof, pw, dirs, trust_new, files=submitter.FETCH_FILES):
    try:
        client, jump = open_client(prof, pw, trust_new=trust_new)
    except ConnectError as e:
        if e.needs_trust:
            return {'needs_trust': True, 'message': str(e), 'results': []}
        raise RuntimeError(str(e))
    results = []
    try:
        sftp = client.open_sftp()
        for d in dirs:
            try:
                fetched, missing = submitter.fetch_results(client, sftp, d, files=files)
                msg = '已拉回 ' + ('、'.join(fetched) if fetched else '(无)')
                if missing:
                    msg += f'(远端缺 {"、".join(missing)})'
                if 'CONTCAR' in fetched:                 # 全自动渲结构图(缓存,失败跳过)
                    from vcstudio.external import povray_render
                    rr = povray_render.render_poscar_views(
                        os.path.join(d, 'CONTCAR'), os.path.join(d, 'figs'),
                        os.path.basename(os.path.normpath(d)))
                    msg += ',结构图 ✓' if rr['ok'] else f",结构图跳过({rr['error'][:60]})"
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
            return {'needs_trust': True, 'message': str(e), 'results': []}
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
            return {'needs_trust': True, 'message': str(e), 'workdir': ''}
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
            return {'needs_trust': True, 'message': str(e), 'jobs': []}
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
            return {'needs_trust': True, 'message': str(e), 'results': []}
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
            local_dir = os.path.join(local_root, _sanitize_seg(j.get('name') or jid))
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
            return {'needs_trust': True, 'message': str(e), 'results': []}
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
            return {'needs_trust': True, 'message': str(e), 'results': []}
        raise RuntimeError(str(e))
    results = []
    try:
        live, reasons = submitter.query_scheduler(client, prof)
        for d in dirs:
            try:
                m = submitter.refresh_job(client, prof, d, live_states=live,
                                          terminal_reasons=reasons)
                note = m['state']
                res = m.get('results') or {}
                dgn = res.get('diagnosis') or {}
                if dgn.get('failure_class') and m['state'] in ('FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'):
                    note += f" [{dgn['failure_class']}{'·可续算' if dgn.get('restartable') else ''}] {dgn.get('evidence', '')}"
                live = res.get('live') or {}
                if m['state'] == 'RUNNING':
                    if live.get('warning'):
                        note += f" ⚠{live['warning']}"
                    elif live.get('ionic_steps') is not None:
                        note += f"({live['ionic_steps']} 离子步" + (
                            f",|F|max={live['fmax']}" if live.get('fmax') else '') + ')'
                e0 = res.get('energy_e0_eV')
                if e0 is not None:
                    note += f'(E0={e0:.4f} eV)'
                results.append((d, note))
            except _job_errors() as e:
                results.append((d, f'查询失败:{e}'))
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}
