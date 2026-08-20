"""「任务」页 v1:台账列表 + 上传提交 + 查询状态 + 打开目录。

状态真相在各作业目录的 job.yaml(ledger 只记路径);所有远程动作走后台线程,
未知主机指纹沿用测连接的确认流程。批量提交前有一次汇总确认(首次提交安全阀)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import copy
import os
import sys
import time

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from vcstudio.gui.widgets import LogBox
from vcstudio.gui import report_bridge, runner
from vcstudio.cluster import ledger, submitter, batch_ops
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
        # 工具栏按语义分两行(原 14 控件挤一行会溢出):
        # 行1 = 集群 + 提交/续算 + 状态类;行2 = 结果类 + 台账维护 + 自动刷新
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky='ew', pady=(3, 0))
        ttk.Label(bar, text='目标集群').grid(row=0, column=0, padx=(0, 4))
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(bar, textvariable=self.profile_var, width=16, state='readonly')
        self.profile_cb.grid(row=0, column=1, padx=4)
        self.submit_btn = ttk.Button(bar, text='📤 上传并提交(选中)', style='Accent.TButton', command=self._on_submit)
        self.submit_btn.grid(row=0, column=2, padx=4)
        self.continue_btn = ttk.Button(bar, text='♻ 续算(选中)', command=self._on_continue)
        self.continue_btn.grid(row=0, column=3, padx=4)
        self.tune_btn = ttk.Button(bar, text='🔧 改参续算(选中)', command=self._on_tune_continue)
        self.tune_btn.grid(row=0, column=4, padx=4)
        self.status_btn = ttk.Button(bar, text='🔄 查询状态', command=self._on_refresh_status)
        self.status_btn.grid(row=0, column=5, padx=4)
        self.queue_btn = ttk.Button(bar, text='🌐 集群队列', command=self._on_queue_view)
        self.queue_btn.grid(row=0, column=6, padx=4)

        bar2 = ttk.Frame(self)
        bar2.grid(row=1, column=0, sticky='ew', pady=(2, 3))
        ttk.Button(bar2, text='⟳ 刷新列表', command=self.reload).grid(row=0, column=0, padx=(0, 4))
        self.fetch_btn = ttk.Button(bar2, text='📥 拉回结果(选中)', command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=1, padx=4)
        ttk.Button(bar2, text='🔁 重新判定(选中)', command=self._on_rejudge).grid(row=0, column=2, padx=4)
        ttk.Button(bar2, text='📄 导出报告', command=self._on_report).grid(row=0, column=3, padx=4)
        ttk.Button(bar2, text='📂 打开目录', command=self._open_dir).grid(row=0, column=4, padx=4)
        ttk.Button(bar2, text='🗑 移出台账', command=self._remove).grid(row=0, column=5, padx=4)
        self.clean_btn = ttk.Button(bar2, text='🧹 清理失效条目', command=self._clean_stale)
        self.clean_btn.grid(row=0, column=6, padx=4)
        # S5 自动轮询:GUI 开着时定时查(默认关;5/15/30 分钟)
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar2, text='自动刷新', variable=self.auto_var).grid(row=0, column=7, padx=(12, 2))
        self.auto_interval_var = tk.StringVar(value='15')
        ttk.Combobox(bar2, textvariable=self.auto_interval_var, values=('5', '15', '30'),
                     width=4, state='readonly').grid(row=0, column=8)
        ttk.Label(bar2, text='分钟').grid(row=0, column=9)

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
        self.tree.grid(row=2, column=0, sticky='nsew', pady=3)
        self.tree.bind('<Double-1>', lambda e: self._open_dir())
        sb = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=2, column=1, sticky='ns')

        self.stale_lbl = ttk.Label(self, text='', foreground='#a16207')
        self.stale_lbl.grid(row=3, column=0, sticky='w')

        self.log = LogBox(self, height=6)
        self.log.grid(row=4, column=0, columnspan=2, sticky='nsew')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.rowconfigure(4, weight=0)

    # ── 列表 ──
    def reload(self):
        self.profiles = load_profiles()
        self.profile_cb['values'] = list(self.profiles)
        if self.profiles and not self.profile_var.get():
            self.profile_var.set(next(iter(self.profiles)))
        self.tree.delete(*self.tree.get_children())
        self._stale = []          # 失效条目(目录/manifest 没了)不进树,汇总一行
        for job_dir, m in ledger.load_all():
            name = os.path.basename(os.path.normpath(job_dir))
            if m is None:
                self._stale.append(job_dir)
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
            elif state == 'RUNNING':
                live = res.get('live') or {}
                if live.get('warning'):
                    diag = '⚠' + live['warning'][:24]
                elif live.get('ionic_steps') is not None:
                    diag = f"{live['ionic_steps']}步" + (
                        f" |F|max={live['fmax']}" if live.get('fmax') else '')
            self.tree.insert(
                '', 'end', iid=job_dir, text=name, tags=(state,),
                values=(state, f"{m.get('task_type','')}/{m.get('calc_type','')}",
                        m.get('cluster') or '', m.get('scheduler_job_id') or '',
                        f'{energy:.4f}' if isinstance(energy, float) else energy,
                        diag, updated))
        self.stale_lbl.configure(
            text=(f'⚠ 已隐藏 {len(self._stale)} 个失效条目(目录或 job.yaml 已不存在)'
                  f'——点「🧹 清理失效条目」一键移出台账' if self._stale else ''))

    def _clean_stale(self):
        stale = getattr(self, '_stale', [])
        if not stale:
            self.log.write('ℹ 没有失效条目,无需清理')
            return
        if messagebox.askyesno('清理失效条目',
                               f'将把 {len(stale)} 个失效条目(目录或 job.yaml 已不存在)移出台账。\n'
                               f'不删除磁盘文件。继续?'):
            for d in stale:
                ledger.unregister(d)
            self.log.write(f'🧹 已清理 {len(stale)} 个失效条目')
            self.reload()

    def _selected(self) -> list:
        return list(self.tree.selection())

    def _profile(self):
        p = self.profiles.get(self.profile_var.get())
        if p is None:
            self.log.write('❌ 请先在集群页配置并保存一个集群,再在上方选择')
            goto = getattr(self, 'goto_cluster_tab', None)
            if goto and messagebox.askyesno(
                    '未配置集群', '还没有配置集群(提交/查询的前置条件)。\n现在去「集群」页配置?'):
                goto()
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
        q = runner.submit(batch_ops.submit_batch, prof, pw, dirs, trust_new)
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

    # ── 重新判定:对选中作业强制重跑取证(NEEDS_HUMAN/FAILED 的正规出口) ──
    def _on_rejudge(self, trust_new=False):
        """人工在远端修好后点这里:重跑取证,收敛了就正规进 DONE(带能量),
        项目随之解锁自动报告。绝不手工改状态(显式状态不变式:判定只能来自证据)。"""
        dirs = [d for d in self._selected()
                if (manifest_mod.load_manifest(d) or {}).get('scheduler_job_id')]
        if not dirs:
            self.log.write('❌ 请选中至少一个提交过的作业(有作业号才能重判)')
            return
        prof = self._profile()
        if prof is None:
            return
        pw = self._password_for(prof)
        self.status_btn.configure(state='disabled')
        self.log.write(f'⏳ 重新判定 {len(dirs)} 个作业(重跑远端取证)…')
        q = runner.submit(batch_ops.refresh_batch, prof, pw, dirs, trust_new)
        self.after(200, lambda: self._poll_status(q))

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
        q = runner.submit(batch_ops.refresh_batch, prof, pw, targets, trust_new)
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
            out_dir = str(proj.get('root') or os.path.dirname(os.path.abspath(str(pth))))
            stem = f"{proj['name']}_完整报告"
            report_status = report_bridge.project_report_status(pth)
            if (report_status.get('ok')
                    and report_status.get('artifact_current')
                    and not report_status.get('scientific_stale')):
                continue                                 # canonical marker 已证明当前
            if not report_status.get('ok'):
                self.log.write(
                    f"⚠ 项目「{proj['name']}」无法读取报告 marker，将尝试重建："
                    f"{report_status.get('error') or '未知原因'}")
            self.log.write(
                f"📄 项目「{proj['name']}」全部 DONE，后台通过统一报告引擎生成 HTML…")
            q = runner.submit(
                report_bridge.generate_project_report_bundle,
                pth,
                out_dir,
                formats=('html',),
                final=True,
                stem=stem,
            )
            self.after(
                500,
                lambda qq=q, name=proj['name']: self._poll_report(qq, name),
            )

    def _poll_report(self, q, project_name=''):
        item = runner.poll(q)
        if item is None:
            self.after(500, lambda: self._poll_report(q, project_name))
            return
        kind, payload = item
        if kind == 'error':
            self.log.write(f'❌ 自动报告失败:{payload}')
            return
        if not isinstance(payload, dict):
            self.log.write('❌ 自动报告失败:报告桥返回了无效结果')
            return
        prefix = f'项目「{project_name}」' if project_name else '项目'
        self.log.write(f'ℹ {prefix}{report_bridge.status_text(payload)}')
        if payload.get('gate_reason'):
            self.log.write(f"⚠ {prefix}科学门禁:{payload['gate_reason']}")
        if not payload.get('artifact_ok'):
            self.log.write(
                f"❌ {prefix}报告产物不可用:{payload.get('error') or '生成未完成'}")
            return
        out = payload.get('primary_file')
        science = str(payload.get('scientific_status') or 'pending').lower()
        icon = '✅' if science == 'final' else '⚠'
        self.log.write(f'{icon} {prefix}报告产物已生成:{out or "路径未返回"}')

    # ── 拉回结果(S3):预设可选(轻量默认 / DOS·Bader / 自定义) ──
    _FETCH_PRESETS = (
        ('轻量(默认):CONTCAR + OSZICAR + OUTCAR',
         ('CONTCAR', 'OSZICAR', 'OUTCAR')),
        ('DOS/Bader:轻量 + vasprun.xml + DOSCAR + CHGCAR + AECCAR0/2(大文件,较慢)',
         ('CONTCAR', 'OSZICAR', 'OUTCAR', 'vasprun.xml', 'DOSCAR',
          'CHGCAR', 'AECCAR0', 'AECCAR2')),
    )

    def _ask_fetch_files(self, n_jobs):
        """预设选择弹窗。返回文件元组或 None(取消)。"""
        win = tk.Toplevel(self)
        win.title(f'拉回结果 — {n_jobs} 个作业')
        win.geometry('560x240')
        win.grab_set()
        choice = tk.IntVar(value=0)
        for i, (label, _files) in enumerate(self._FETCH_PRESETS):
            ttk.Radiobutton(win, text=label, value=i, variable=choice).pack(
                anchor='w', padx=12, pady=(10 if i == 0 else 2, 2))
        ttk.Radiobutton(win, text='自定义(逗号分隔文件名):', value=99,
                        variable=choice).pack(anchor='w', padx=12, pady=2)
        custom_var = tk.StringVar(value='CONTCAR, OUTCAR, vasprun.xml')
        ttk.Entry(win, textvariable=custom_var, width=56).pack(anchor='w', padx=32)
        result = {}

        def _ok():
            c = choice.get()
            if c == 99:
                files = tuple(f.strip() for f in custom_var.get().split(',') if f.strip())
                if not files:
                    messagebox.showinfo('拉回结果', '自定义文件列表为空', parent=win)
                    return
            else:
                files = self._FETCH_PRESETS[c][1]
            result['files'] = files
            win.destroy()

        btns = ttk.Frame(win)
        btns.pack(pady=10)
        ttk.Button(btns, text='✔ 开始拉回', style='Accent.TButton', command=_ok).pack(side='left', padx=6)
        ttk.Button(btns, text='取消', command=win.destroy).pack(side='left', padx=6)
        win.wait_window()
        return result.get('files')

    def _on_fetch(self, trust_new=False, files=None):
        dirs = [d for d in self._selected()]
        if not dirs:
            self.log.write('❌ 请先选中要拉回结果的作业(通常是 DONE/UNCONVERGED 的)')
            return
        prof = self._profile()
        if prof is None:
            return
        if files is None:
            files = self._ask_fetch_files(len(dirs))
            if files is None:
                return
        pw = self._password_for(prof)
        self.fetch_btn.configure(state='disabled')
        self.log.write(f'⏳ 拉回 {len(dirs)} 个作业的 {"、".join(files)}…')
        q = runner.submit(batch_ops.fetch_batch, prof, pw, dirs, trust_new, files)
        self.after(200, lambda: self._poll_fetch(q, files))

    def _poll_fetch(self, q, files=None):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_fetch(q, files))
            return
        kind, payload = item
        self.fetch_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 拉回异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_fetch(trust_new=True, files=files)
            return
        for dir_, ok, msg in payload['results']:
            self.log.write(('✅' if ok else '❌') + f' {os.path.basename(dir_)}:{msg}')
        self.reload()

    # ── 续算(有界恢复) ──
    def _on_continue(self, trust_new=False, frozen=None):
        if frozen is None:
            if trust_new:
                self.log.write('❌ 主机信任重试缺少已确认目标，请重新发起续算')
                return
            sel = self._selected()
            if not sel:
                self.log.write('❌ 请先选中要续算的作业(仅未收敛/墙钟/ZBRENT 等可续算)')
                return
            prof = self._profile()
            if prof is None:
                return
            dirs, skipped = batch_ops.filter_continuable(
                sel, allow_round_limit_override=True)
            if not dirs:
                self.log.write('❌ 选中作业均不可续算(需:已结束 + 诊断标可续算)')
                return
            tail = f'(跳过 {skipped} 个不可续算/仍在跑)' if skipped else ''
            if not messagebox.askyesno(
                    '确认续算',
                    f'将对 {len(dirs)} 个可续算作业从 CONTCAR 续算并重投到「{prof.name}」{tail}\n'
                    f'INCAR 冻结。自动托管最多 3 轮；这是人工确认，超过 3 轮仍可继续，'
                    f'请自行判断机时与方法合理性。\n\n继续?'):
                return
            frozen = {
                'dirs': tuple(dirs),
                'profile': copy.deepcopy(prof),
                'password': self._password_for(prof),
                'idempotency_key': f'tk-continue-{os.urandom(16).hex()}',
            }
        prof = frozen['profile']
        dirs = list(frozen['dirs'])
        pw = frozen['password']
        self.continue_btn.configure(state='disabled')
        self.log.write(f'⏳ 连接并续算 {len(dirs)} 个作业…')
        q = runner.submit(
            batch_ops.continue_batch, prof, pw, dirs, trust_new,
            idempotency_key=frozen['idempotency_key'],
            allow_round_limit_override=True)
        self.after(200, lambda: self._poll_continue(q, frozen))

    def _poll_continue(self, q, frozen):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_continue(q, frozen))
            return
        kind, payload = item
        self.continue_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 续算异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_continue(trust_new=True, frozen=frozen)
            return
        for dir_, ok, msg in payload['results']:
            self.log.write(('✅' if ok else '❌') + f' {os.path.basename(dir_)}:{msg}')
        self.reload()

    # ── 集群队列(P0:外部任务可见/可认领) ──
    def _on_queue_view(self, trust_new=False):
        prof = self._profile()
        if prof is None:
            return
        pw = self._password_for(prof)
        self.queue_btn.configure(state='disabled')
        self.log.write(f'⏳ 查询「{prof.name}」上 {prof.username} 的全部队列作业…')
        q = runner.submit(batch_ops.queue_detail, prof, pw, trust_new)
        self.after(200, lambda: self._poll_queue(q, prof))

    def _poll_queue(self, q, prof):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_queue(q, prof))
            return
        kind, payload = item
        self.queue_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 队列查询异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_queue_view(trust_new=True)
            return
        self._show_queue_window(prof, payload['jobs'])

    def _show_queue_window(self, prof, jobs):
        """集群队列窗口:标注 已纳管/未纳入,未纳入的可认领。"""
        known = {str((m or {}).get('scheduler_job_id')): d
                 for d, m in ledger.load_all() if m and m.get('scheduler_job_id')}
        win = tk.Toplevel(self)
        win.title(f'集群队列 — {prof.name}({prof.username} 的全部在队/在跑作业)')
        win.geometry('720x420')
        cols = ('state', 'name', 'workdir', 'managed')
        tree = ttk.Treeview(win, columns=cols, show='tree headings', selectmode='browse')
        tree.heading('#0', text='作业号')
        tree.column('#0', width=90, anchor='w')
        for cid, text, w in (('state', '状态', 80), ('name', '作业名', 160),
                             ('workdir', '远程目录', 240), ('managed', '纳管', 110)):
            tree.heading(cid, text=text)
            tree.column(cid, width=w, anchor='w')
        if not jobs:
            self.log.write(f'ℹ 「{prof.name}」队列为空(该用户当前没有在队/在跑作业)')
        for j in jobs:
            managed = '✔ 已纳管' if str(j['job_id']) in known else '○ 未纳入'
            tree.insert('', 'end', iid=str(j['job_id']), text=str(j['job_id']),
                        values=('运行中' if j['state'] == 'RUNNING' else '排队中',
                                j.get('name') or '', j.get('workdir') or '', managed))
        tree.pack(fill='both', expand=True, padx=8, pady=(8, 4))
        hint = ttk.Label(win, text='「○ 未纳入」= 不是本软件提交的作业(如终端手动 sbatch)。'
                                   '选中后点「认领」纳入台账,即可查状态/拉回/续算。')
        hint.pack(anchor='w', padx=8)
        btns = ttk.Frame(win)
        btns.pack(pady=6)
        ttk.Button(btns, text='📌 认领选中作业', style='Accent.TButton',
                   command=lambda: self._adopt_dialog(win, tree, prof, known)).pack(side='left', padx=6)
        ttk.Button(btns, text='关闭', command=win.destroy).pack(side='left', padx=6)

    def _adopt_dialog(self, win, tree, prof, known):
        sel = tree.selection()
        if not sel:
            messagebox.showinfo('认领', '请先选中一个「○ 未纳入」的作业', parent=win)
            return
        jid = sel[0]
        if jid in known:
            messagebox.showinfo('认领', f'作业 {jid} 已在台账中,无需认领', parent=win)
            return
        vals = tree.item(jid, 'values')
        name, workdir = vals[1], vals[2]
        remote = workdir or simpledialog.askstring(
            '远程目录', f'调度器未提供作业 {jid} 的工作目录,请输入远程绝对路径:',
            parent=win)
        if not remote:
            return
        local = filedialog.askdirectory(
            title=f'为作业 {jid}({name})选择/新建本地目录(结果将拉回到这里)',
            parent=win)
        if not local:
            return
        try:
            submitter.adopt_external_job(local, prof, jid, remote, name=name)
            self.log.write(f'📌 已认领作业 {jid} → {local}(下次「查询状态」即可追踪)')
            tree.item(jid, values=(vals[0], name, remote, '✔ 已纳管'))
            self.reload()
        except (ValueError, RuntimeError) as e:
            messagebox.showerror('认领失败', str(e), parent=win)

    # ── 改参续算(S6:诊断建议 → 白名单键受控修改重投) ──
    def _on_tune_continue(self, trust_new=False, frozen=None):
        if frozen is None:
            if trust_new:
                self.log.write('❌ 主机信任重试缺少已确认目标，请重新发起改参续算')
                return
            sel = self._selected()
            if len(sel) != 1:
                self.log.write('❌ 改参续算一次处理一个作业:请只选中一个已结束的作业')
                return
            d = sel[0]
            m = manifest_mod.load_manifest(d)
            if m is None:
                self.log.write('❌ 该条目缺 job.yaml')
                return
            if m.get('state') not in submitter.CONTINUE_TERMINAL_STATES:
                self.log.write(
                    f"❌ 该作业不在可改参重投终态(状态 {m.get('state') or '<缺失>'})")
                return
            prof = self._profile()
            if prof is None:
                return
            dgn = (m.get('results') or {}).get('diagnosis') or {}
            hint = ''
            if dgn.get('failure_class'):
                hint = f"诊断:{dgn['failure_class']} — {dgn.get('evidence', '')}"
            changes = self._ask_incar_changes(os.path.basename(d), hint)
            if not changes:
                return
            frozen = {
                'job_dir': d,
                'changes': dict(changes),
                'from_contcar': bool(getattr(self, '_tune_from_contcar', True)),
                'profile': copy.deepcopy(prof),
                'password': self._password_for(prof),
                'idempotency_key': f'tk-tune-{os.urandom(16).hex()}',
            }
        d = frozen['job_dir']
        changes = dict(frozen['changes'])
        prof = frozen['profile']
        pw = frozen['password']
        self.tune_btn.configure(state='disabled')
        self.log.write(f'⏳ 改参续算 {os.path.basename(d)}:' +
                       ', '.join(f'{k}={v}' for k, v in changes.items()))
        q = runner.submit(batch_ops.tune_batch, prof, pw, d, changes, trust_new,
                          frozen['from_contcar'],
                          idempotency_key=frozen['idempotency_key'])
        self.after(200, lambda: self._poll_tune(q, frozen))

    def _ask_incar_changes(self, job_name, hint):
        """弹窗收集白名单 INCAR 修改(每行 KEY = VALUE)。返回 dict 或 None(取消)。"""
        win = tk.Toplevel(self)
        win.title(f'改参续算 — {job_name}')
        win.geometry('560x360')
        win.grab_set()
        if hint:
            ttk.Label(win, text=hint, foreground='#a16207', wraplength=520).pack(
                anchor='w', padx=10, pady=(8, 2))
        ttk.Label(win, text='输入要修改的 INCAR 键值(每行一个,如 ALGO = Normal):').pack(
            anchor='w', padx=10, pady=(6, 2))
        txt = tk.Text(win, height=6, width=60)
        txt.pack(fill='both', expand=True, padx=10)
        ttk.Label(win, foreground='#64748B', wraplength=520, text=(
            '白名单(非方法学旋钮):' + ', '.join(sorted(submitter.INCAR_TUNE_WHITELIST)) +
            '。ENCUT/泛函/IVDW/ISPIN 不可改(保 ΔE 可比性)。'
            '修改以追加块写入 INCAR 文末(原文保留,VASP 取末次出现值),并计入续算轮次。'
            '自动续算上限为 3 轮；人工确认的改参续算可超过该上限。')).pack(
            anchor='w', padx=10, pady=4)
        from_contcar = tk.BooleanVar(value=True)
        ttk.Checkbutton(win, text='同时从 CONTCAR 续算结构(推荐;取消则保持原 POSCAR 重跑)',
                        variable=from_contcar).pack(anchor='w', padx=10)
        result = {}

        def _ok():
            raw = txt.get('1.0', 'end').strip()
            changes = {}
            for line in raw.splitlines():
                if '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k, v = k.strip().upper(), v.strip()
                if k and v:
                    changes[k] = v
            if not changes:
                messagebox.showinfo('改参续算', '未输入有效的 KEY = VALUE 行', parent=win)
                return
            bad = [k for k in changes if k not in submitter.INCAR_TUNE_WHITELIST]
            if bad:
                messagebox.showerror('改参续算', f'不在白名单:{", ".join(bad)}', parent=win)
                return
            result['changes'] = changes
            result['from_contcar'] = from_contcar.get()
            win.destroy()

        btns = ttk.Frame(win)
        btns.pack(pady=8)
        ttk.Button(btns, text='✔ 确认重投', style='Accent.TButton', command=_ok).pack(side='left', padx=6)
        ttk.Button(btns, text='取消', command=win.destroy).pack(side='left', padx=6)
        win.wait_window()
        if 'changes' not in result:
            return None
        self._tune_from_contcar = result['from_contcar']
        return result['changes']

    def _poll_tune(self, q, frozen):
        item = runner.poll(q)
        if item is None:
            self.after(200, lambda: self._poll_tune(q, frozen))
            return
        kind, payload = item
        self.tune_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 改参续算异常:{payload}')
            return
        if payload.get('needs_trust'):
            if messagebox.askyesno('未知主机', f"{payload['message']}\n\n是否信任该主机并重试?"):
                self._on_tune_continue(trust_new=True, frozen=frozen)
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
