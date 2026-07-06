"""「任务」页 v1:台账列表 + 上传提交 + 查询状态 + 打开目录。

状态真相在各作业目录的 job.yaml(ledger 只记路径);所有远程动作走后台线程,
未知主机指纹沿用测连接的确认流程。批量提交前有一次汇总确认(首次提交安全阀)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys
import time

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from vcstudio.gui.widgets import LogBox
from vcstudio.gui import runner
from vcstudio.cluster import ledger, submitter
from vcstudio.cluster.connection import open_client, close_quiet, ConnectError
from vcstudio.cluster.profiles import load_profiles
from vcstudio.project import report
from vcstudio.shared import secrets
from vcstudio.shared import manifest as manifest_mod

_STATE_TAG = {
    'CREATED': ('draft', '#4b5563'), 'UPLOADED': ('up', '#1d4ed8'),
    'SUBMITTED': ('q', '#7e22ce'), 'QUEUED': ('q', '#7e22ce'),
    'RUNNING': ('run', '#1d4ed8'), 'DONE': ('ok', '#15803d'),
    'FAILED': ('err', '#b91c1c'), 'UNCONVERGED': ('warn', '#a16207'),
    'NEEDS_HUMAN': ('warn', '#a16207'),
}


class JobsTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self.profiles = {}
        self._build()
        self.reload()
        self._schedule_auto()                            # S5 轮询链启动(开关随时生效)

    def _build(self):
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky='ew', pady=3)
        ttk.Label(bar, text='目标集群').grid(row=0, column=0, padx=(0, 4))
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(bar, textvariable=self.profile_var, width=18, state='readonly')
        self.profile_cb.grid(row=0, column=1, padx=4)
        ttk.Button(bar, text='⟳ 刷新列表', command=self.reload).grid(row=0, column=2, padx=4)
        self.submit_btn = ttk.Button(bar, text='📤 上传并提交(选中)', style='Accent.TButton', command=self._on_submit)
        self.submit_btn.grid(row=0, column=3, padx=4)
        self.status_btn = ttk.Button(bar, text='🔄 查询状态', command=self._on_refresh_status)
        self.status_btn.grid(row=0, column=4, padx=4)
        self.fetch_btn = ttk.Button(bar, text='📥 拉回结果(选中)', command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=5, padx=4)
        self.continue_btn = ttk.Button(bar, text='♻ 续算(选中)', command=self._on_continue)
        self.continue_btn.grid(row=0, column=6, padx=4)
        ttk.Button(bar, text='📄 导出报告', command=self._on_report).grid(row=0, column=7, padx=4)
        ttk.Button(bar, text='📂 打开目录', command=self._open_dir).grid(row=0, column=8, padx=4)
        ttk.Button(bar, text='🗑 移出台账', command=self._remove).grid(row=0, column=9, padx=4)
        # S5 自动轮询:GUI 开着时定时查(默认关;5/15/30 分钟)
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='自动刷新', variable=self.auto_var).grid(row=0, column=10, padx=(12, 2))
        self.auto_interval_var = tk.StringVar(value='15')
        ttk.Combobox(bar, textvariable=self.auto_interval_var, values=('5', '15', '30'),
                     width=4, state='readonly').grid(row=0, column=11)
        ttk.Label(bar, text='分钟').grid(row=0, column=12)

        cols = ('state', 'task', 'cluster', 'jobid', 'energy', 'diag', 'updated')
        self.tree = ttk.Treeview(self, columns=cols, show='tree headings', selectmode='extended')
        self.tree.heading('#0', text='作业目录')
        self.tree.column('#0', width=230, anchor='w')
        for cid, text, w in (('state', '状态', 100), ('task', '类型', 84),
                             ('cluster', '集群', 84), ('jobid', '作业号', 74),
                             ('energy', 'E0 (eV)', 96), ('diag', '诊断', 150),
                             ('updated', '更新时间', 128)):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor='w')
        for st, (tag, color) in _STATE_TAG.items():
            self.tree.tag_configure(st, foreground=color)
        self.tree.grid(row=1, column=0, sticky='nsew', pady=3)
        self.tree.bind('<Double-1>', lambda e: self._open_dir())
        sb = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=1, column=1, sticky='ns')

        self.log = LogBox(self, height=6)
        self.log.grid(row=2, column=0, columnspan=2, sticky='nsew')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(2, weight=0)

    # ── 列表 ──
    def reload(self):
        self.profiles = load_profiles()
        self.profile_cb['values'] = list(self.profiles)
        if self.profiles and not self.profile_var.get():
            self.profile_var.set(next(iter(self.profiles)))
        self.tree.delete(*self.tree.get_children())
        for job_dir, m in ledger.load_all():
            name = os.path.basename(os.path.normpath(job_dir))
            if m is None:
                self.tree.insert('', 'end', iid=job_dir, text=name,
                                 values=('缺 job.yaml/目录', '', '', '', '', '', ''))
                continue
            state = m.get('state', '?')
            hist = m.get('state_history') or []
            updated = hist[-1].get('at', '') if hist else m.get('created_at', '')
            res = m.get('results') or {}
            energy = res.get('energy_e0_eV', '')
            dgn = res.get('diagnosis') or {}
            diag = ''
            if dgn.get('failure_class') and state in ('FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'):
                diag = dgn['failure_class'] + ('♻可续算' if dgn.get('restartable') else '')
            self.tree.insert(
                '', 'end', iid=job_dir, text=name, tags=(state,),
                values=(state, f"{m.get('task_type','')}/{m.get('calc_type','')}",
                        m.get('cluster') or '', m.get('scheduler_job_id') or '',
                        f'{energy:.4f}' if isinstance(energy, float) else energy,
                        diag, updated))

    def _selected(self) -> list:
        return list(self.tree.selection())

    def _profile(self):
        p = self.profiles.get(self.profile_var.get())
        if p is None:
            self.log.write('❌ 请先在集群页配置并保存一个集群,再在上方选择')
        return p

    def _password_for(self, prof):
        if prof.auth != 'password':
            return None
        pw = secrets.get_password(prof.name)
        if pw is None:
            pw = simpledialog.askstring('密码', f'输入 {prof.username}@{prof.hostname} 的密码:', show='*')
        return pw

    # ── 提交 ──
    def _on_submit(self, trust_new=False):
        dirs = self._selected()
        if not dirs:
            self.log.write('❌ 请先在列表中选中要提交的作业(可多选)')
            return
        prof = self._profile()
        if prof is None:
            return
        # preflight 先行:有问题就地列明,不出手
        problems = []
        for d in dirs:
            errs = submitter.preflight(prof, d)
            if errs:
                problems.append(f'{os.path.basename(d)}: ' + '；'.join(errs))
        if problems:
            for p in problems:
                self.log.write(f'❌ {p}')
            return
        if not trust_new and not messagebox.askyesno(
                '确认提交',
                f'将上传并提交 {len(dirs)} 个作业到「{prof.name}」\n'
                f'远程根目录:{prof.remote_root}\n脚本模式:{prof.script_mode}\n\n继续?'):
            return
        pw = self._password_for(prof)
        self.submit_btn.configure(state='disabled')
        self.log.write(f'⏳ 连接并提交 {len(dirs)} 个作业…')
        q = runner.submit(_submit_batch, prof, pw, dirs, trust_new)
        self.after(200, lambda: self._poll_submit(q, prof, pw, dirs))

    def _poll_submit(self, q, prof, pw, dirs):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_submit(q, prof, pw, dirs))
            return
        kind, payload = item
        self.submit_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 提交异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_submit(trust_new=True)
            else:
                self.log.write('已取消(未信任主机)')
            return
        for dir_, ok, msg in payload['results']:
            self.log.write(('✅' if ok else '❌') + f' {os.path.basename(dir_)}:{msg}')
        self.reload()

    # ── 查状态(手动按钮 / S5 自动轮询共用) ──
    def _on_refresh_status(self, trust_new=False, auto=False):
        prof = self._profile()
        if prof is None:
            return
        targets = []
        for job_dir, m in ledger.load_all():
            if m and m.get('scheduler_job_id') and m.get('cluster') == prof.name \
                    and m.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING'):
                targets.append(job_dir)
        if not targets:
            if not auto:
                self.log.write(f'ℹ 「{prof.name}」上没有待查询的作业(SUBMITTED/QUEUED/RUNNING)')
            return
        if auto:
            if str(self.status_btn['state']) == 'disabled':
                return                                   # 上一轮还在跑,跳过本轮
            if prof.auth == 'password' and secrets.get_password(prof.name) is None:
                self.log.write('⚠ 自动刷新需先手动查询一次并保存密码(keyring),本轮跳过')
                return
        pw = self._password_for(prof)
        self.status_btn.configure(state='disabled')
        self.log.write(f'⏳ 查询 {len(targets)} 个作业状态…')
        q = runner.submit(_refresh_batch, prof, pw, targets, trust_new)
        self.after(200, lambda: self._poll_status(q))

    def _poll_status(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_status(q))
            return
        kind, payload = item
        self.status_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 查询异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_refresh_status(trust_new=True)
            return
        for dir_, msg in payload['results']:
            self.log.write(f'🔄 {os.path.basename(dir_)}:{msg}')
        self.reload()
        self._maybe_auto_reports()

    # ── S5 自动轮询 + 全 DONE 自动报告 ──
    def _schedule_auto(self):
        try:
            minutes = int(self.auto_interval_var.get())
        except (ValueError, tk.TclError):
            minutes = 15
        self.after(max(1, minutes) * 60000, self._auto_tick)

    def _auto_tick(self):
        if self.auto_var.get():
            self._on_refresh_status(auto=True)
        self._schedule_auto()                            # 无论开关,链条保持(开关随时生效)

    def _maybe_auto_reports(self):
        """项目成员全 DONE 且报告缺失/过期 → 后台生成完整报告(用户决策:全自动)。"""
        from vcstudio.project import adsorption, report_full
        from vcstudio.shared.config import load_config
        try:
            cfg = load_config()
        except Exception:                                # noqa: BLE001
            cfg = {}
        for pth in adsorption.list_projects():
            proj = adsorption.load_project(pth)
            if not proj:
                continue
            dirs = report_full._member_dirs(proj)
            if not dirs:
                continue
            manifests = [manifest_mod.load_manifest(d) for d in dirs]
            if not all(m and m.get('state') == 'DONE' for m in manifests):
                # 全员终态但有非 DONE → 项目被卡,明说卡在谁(审查#9:不许静默永不出报告)
                terminal = ('DONE', 'FAILED', 'NEEDS_HUMAN', 'UNCONVERGED')
                if all(m and m.get('state') in terminal for m in manifests):
                    stuck = [os.path.basename(os.path.normpath(d))
                             for d, m in zip(dirs, manifests)
                             if m and m.get('state') != 'DONE']
                    self.log.write(f"⚠ 项目「{proj['name']}」被卡:{', '.join(stuck[:5])} "
                                   f"未 DONE(修复/续算后才会自动出报告)")
                continue
            out = os.path.join(proj.get('root', ''), f"{proj['name']}_完整报告.html")
            newest = max((os.path.getmtime(manifest_mod.manifest_path(d))
                          for d in dirs if manifest_mod.manifest_path(d).is_file()),
                         default=0)
            if os.path.isfile(out) and os.path.getmtime(out) >= newest:
                continue                                 # 报告已新鲜
            self.log.write(f"📄 项目「{proj['name']}」全部 DONE,后台生成完整报告…")
            q = runner.submit(report_full.generate_project_report, proj, out, config=cfg)
            self.after(500, lambda qq=q, o=out: self._poll_report(qq, o))

    def _poll_report(self, q, out):
        item = runner.poll(q)
        if item is None:
            self.after(500, lambda: self._poll_report(q, out))
            return
        kind, payload = item
        if kind == 'error':
            self.log.write(f'❌ 自动报告失败:{payload}')
        else:
            self.log.write(f'✅ 完整报告已生成:{out}(含图表/结构图/AI 分析)')

    # ── 拉回结果(S3) ──
    def _on_fetch(self, trust_new=False):
        dirs = [d for d in self._selected()]
        if not dirs:
            self.log.write('❌ 请先选中要拉回结果的作业(通常是 DONE/UNCONVERGED 的)')
            return
        prof = self._profile()
        if prof is None:
            return
        pw = self._password_for(prof)
        self.fetch_btn.configure(state='disabled')
        self.log.write(f'⏳ 拉回 {len(dirs)} 个作业的 CONTCAR/OSZICAR/OUTCAR…')
        q = runner.submit(_fetch_batch, prof, pw, dirs, trust_new)
        self.after(200, lambda: self._poll_fetch(q))

    def _poll_fetch(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_fetch(q))
            return
        kind, payload = item
        self.fetch_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 拉回异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_fetch(trust_new=True)
            return
        for dir_, ok, msg in payload['results']:
            self.log.write(('✅' if ok else '❌') + f' {os.path.basename(dir_)}:{msg}')
        self.reload()

    # ── 续算(有界恢复) ──
    def _on_continue(self, trust_new=False):
        sel = self._selected()
        if not sel:
            self.log.write('❌ 请先选中要续算的作业(仅未收敛/墙钟/ZBRENT 等可续算)')
            return
        prof = self._profile()
        if prof is None:
            return
        dirs, skipped = _filter_continuable(sel)
        if not dirs:
            self.log.write('❌ 选中作业均不可续算(需:已结束 + 诊断标可续算 + 未达 3 轮上限)')
            return
        tail = f'(跳过 {skipped} 个不可续算/仍在跑)' if skipped else ''
        if not trust_new and not messagebox.askyesno(
                '确认续算',
                f'将对 {len(dirs)} 个可续算作业从 CONTCAR 续算并重投到「{prof.name}」{tail}\n'
                f'INCAR 冻结;每作业上限 3 轮。\n\n继续?'):
            return
        pw = self._password_for(prof)
        self.continue_btn.configure(state='disabled')
        self.log.write(f'⏳ 连接并续算 {len(dirs)} 个作业…')
        q = runner.submit(_continue_batch, prof, pw, dirs, trust_new)
        self.after(200, lambda: self._poll_continue(q))

    def _poll_continue(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_continue(q))
            return
        kind, payload = item
        self.continue_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 续算异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_continue(trust_new=True)
            return
        for dir_, ok, msg in payload['results']:
            self.log.write(('✅' if ok else '❌') + f' {os.path.basename(dir_)}:{msg}')
        self.reload()

    # ── 导出报告 ──
    def _on_report(self):
        dirs = [jd for jd, _ in ledger.load_all()]
        if not dirs:
            self.log.write('ℹ 台账为空,无可报告的作业')
            return
        path = filedialog.asksaveasfilename(       # 用户自选落点(与项目页导出 CSV 一致,不塞隐藏目录)
            title='保存运行报告', defaultextension='.html',
            initialfile=f'run-report-{time.strftime("%Y%m%d-%H%M%S")}.html',
            filetypes=[('HTML 报告', '*.html'), ('Markdown', '*.md')])
        if not path:
            return
        fmt = 'md' if str(path).lower().endswith('.md') else 'html'
        try:
            p = report.write_report(dirs, path, title='VASP 批量运行报告', fmt=fmt)
        except Exception as e:                                       # noqa: BLE001 报告失败不该崩界面
            self.log.write(f'❌ 生成报告失败:{e}')
            return
        self.log.write(f'✅ 报告已生成:{p}')
        try:
            if sys.platform == 'win32':
                os.startfile(str(p))  # noqa
            else:
                import subprocess
                subprocess.Popen(['xdg-open', str(p)])
        except Exception as e:                                       # noqa: BLE001
            self.log.write(f'⚠ 已生成但无法自动打开:{e}')

    # ── 其他 ──
    def _open_dir(self):
        for d in self._selected()[:1]:
            if os.path.isdir(d):
                try:
                    if sys.platform == 'win32':
                        os.startfile(d)  # noqa
                    else:
                        import subprocess
                        subprocess.Popen(['xdg-open', d])
                except Exception as e:
                    self.log.write(f'⚠ 无法打开目录:{e}')

    def _remove(self):
        dirs = self._selected()
        if dirs and messagebox.askyesno('移出台账', f'把 {len(dirs)} 个作业移出台账?(不删除磁盘文件)'):
            for d in dirs:
                ledger.unregister(d)
            self.reload()


# ── 后台线程体(不碰 Tk) ─────────────────────────────────────────────────────
def _job_errors():
    """单作业可失败的异常集:一条作业出错(含连接抖动的 SSHException)只失败那一条,
    不打断整批。paramiko 延迟导入,保持本模块可在无 paramiko 环境导入。"""
    from paramiko.ssh_exception import SSHException
    return (ValueError, RuntimeError, OSError, SSHException)


def _submit_batch(prof, pw, dirs, trust_new):
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


def _fetch_batch(prof, pw, dirs, trust_new):
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
                fetched, missing = submitter.fetch_results(client, sftp, d)
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


def _filter_continuable(dirs):
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


def _continue_batch(prof, pw, dirs, trust_new):
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


def _refresh_batch(prof, pw, dirs, trust_new):
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
                e0 = res.get('energy_e0_eV')
                if e0 is not None:
                    note += f'(E0={e0:.4f} eV)'
                results.append((d, note))
            except _job_errors() as e:
                results.append((d, f'查询失败:{e}'))
    finally:
        close_quiet(client, jump)
    return {'needs_trust': False, 'results': results}
