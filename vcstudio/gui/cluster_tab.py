"""「集群」页(M2 前置):多 profile 连接配置 + 测连接。提交按钮置灰待 M2。

密码绝不落 yaml:保存时经 keyring 存,keyring 不可用则弹框现输(仅内存)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from vcstudio.gui.widgets import LogBox
from vcstudio.gui import runner
from vcstudio.gui.logic import profile_from_form, validate_cluster_inputs
from vcstudio.cluster.profiles import load_profiles, save_profiles, ClusterProfile
from vcstudio.cluster import ssh_test
from vcstudio.shared import secrets


class ClusterTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self.profiles = {}
        self.vars = {}
        self._build()
        self._reload_profiles()

    def _row(self, r, label, key, width=36):
        ttk.Label(self, text=label, width=14, anchor='e').grid(row=r, column=0, padx=4, pady=2)
        v = tk.StringVar()
        self.vars[key] = v
        ttk.Entry(self, textvariable=v, width=width).grid(row=r, column=1, columnspan=2, sticky='w', padx=4)

    def _build(self):
        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=3, sticky='w', pady=3)
        ttk.Label(top, text='集群配置', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(top, textvariable=self.profile_var, width=24, state='readonly')
        self.profile_cb.grid(row=0, column=1, padx=4)
        self.profile_cb.bind('<<ComboboxSelected>>', lambda e: self._fill_from_selected())
        ttk.Button(top, text='新建', command=self._new).grid(row=0, column=2, padx=2)
        ttk.Button(top, text='删除', command=self._delete).grid(row=0, column=3, padx=2)

        self._row(1, '集群名称', 'name', width=24)
        self._row(2, '主机名', 'hostname')
        self._row(3, '端口', 'port', width=8)
        self._row(4, '用户名', 'username', width=24)

        auth = ttk.Frame(self)
        auth.grid(row=5, column=0, columnspan=3, sticky='w')
        ttk.Label(auth, text='认证', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.auth_var = tk.StringVar(value='key')
        ttk.Radiobutton(auth, text='SSH 密钥', value='key', variable=self.auth_var,
                        command=self._sync_auth).grid(row=0, column=1)
        ttk.Radiobutton(auth, text='密码', value='password', variable=self.auth_var,
                        command=self._sync_auth).grid(row=0, column=2)

        keyf = ttk.Frame(self)
        keyf.grid(row=6, column=0, columnspan=3, sticky='w')
        ttk.Label(keyf, text='密钥文件', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.vars['key_path'] = tk.StringVar()
        ttk.Entry(keyf, textvariable=self.vars['key_path'], width=36).grid(row=0, column=1, padx=4)
        ttk.Button(keyf, text='浏览…', command=self._pick_key).grid(row=0, column=2)

        self._row(7, '远程工作目录', 'remote_root')

        sch = ttk.Frame(self)
        sch.grid(row=8, column=0, columnspan=3, sticky='w', pady=2)
        ttk.Label(sch, text='调度器', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.sched_var = tk.StringVar(value='Slurm')
        ttk.Combobox(sch, textvariable=self.sched_var, width=12, state='readonly',
                     values=['Slurm', 'PBS', 'LSF', 'Shell']).grid(row=0, column=1, padx=4)

        # 跳板机(可选)
        self.jump_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text='经跳板机', variable=self.jump_var).grid(row=9, column=0, sticky='e', padx=4)
        jf = ttk.Frame(self)
        jf.grid(row=9, column=1, columnspan=2, sticky='w')
        ttk.Label(jf, text='主机').grid(row=0, column=0)
        self.vars['jump_host'] = tk.StringVar()
        ttk.Entry(jf, textvariable=self.vars['jump_host'], width=16).grid(row=0, column=1, padx=2)
        ttk.Label(jf, text='用户').grid(row=0, column=2)
        self.vars['jump_user'] = tk.StringVar()
        ttk.Entry(jf, textvariable=self.vars['jump_user'], width=10).grid(row=0, column=3, padx=2)
        ttk.Label(jf, text='端口').grid(row=0, column=4)
        self.vars['jump_port'] = tk.StringVar(value='22')
        ttk.Entry(jf, textvariable=self.vars['jump_port'], width=6).grid(row=0, column=5, padx=2)

        btns = ttk.Frame(self)
        btns.grid(row=10, column=0, columnspan=3, pady=6)
        ttk.Button(btns, text='💾 保存', command=self._save).grid(row=0, column=0, padx=4)
        self.test_btn = ttk.Button(btns, text='🔌 测试连接', command=self._on_test)
        self.test_btn.grid(row=0, column=1, padx=4)
        ttk.Button(btns, text='📤 提交任务 (M2上线)', state='disabled').grid(row=0, column=2, padx=4)

        ttk.Label(self, text='连接日志').grid(row=11, column=0, sticky='w')
        self.log = LogBox(self, height=6)
        self.log.grid(row=12, column=0, columnspan=3, sticky='nsew', pady=3)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(12, weight=1)
        self.vars['port'].set('22')

    # ---- profile 下拉 ----
    def _reload_profiles(self):
        self.profiles = load_profiles()
        self.profile_cb['values'] = list(self.profiles)
        if self.profiles:
            first = next(iter(self.profiles))
            self.profile_var.set(first)
            self._fill_from_selected()

    def _fill_from_selected(self):
        p = self.profiles.get(self.profile_var.get())
        if not p:
            return
        self.vars['name'].set(p.name)
        self.vars['hostname'].set(p.hostname)
        self.vars['port'].set(str(p.port))
        self.vars['username'].set(p.username)
        self.auth_var.set(p.auth)
        self.vars['key_path'].set(p.key_path)
        self.vars['remote_root'].set(p.remote_root)
        self.sched_var.set(p.scheduler)
        self.jump_var.set(p.use_jump)
        self.vars['jump_host'].set(p.jump_host)
        self.vars['jump_user'].set(p.jump_user)
        self.vars['jump_port'].set(str(p.jump_port))

    def _new(self):
        for k in ('name', 'hostname', 'username', 'key_path', 'remote_root',
                  'jump_host', 'jump_user'):
            self.vars[k].set('')
        self.vars['port'].set('22')
        self.vars['jump_port'].set('22')
        self.auth_var.set('key')
        self.sched_var.set('Slurm')
        self.jump_var.set(False)
        self.profile_var.set('')

    def _delete(self):
        name = self.profile_var.get()
        if name and name in self.profiles and messagebox.askyesno('删除', f'删除集群「{name}」?'):
            self.profiles.pop(name)
            secrets.delete_password(name)
            save_profiles(self.profiles)
            self._new()
            self.profile_cb['values'] = list(self.profiles)

    def _pick_key(self):
        path = filedialog.askopenfilename(title='选择 SSH 私钥')
        if path:
            self.vars['key_path'].set(path)

    def _sync_auth(self):
        pass  # 占位:如需按认证方式禁用控件,可在此切换 state

    def _current_profile(self) -> ClusterProfile:
        fields = {k: v.get() for k, v in self.vars.items()}
        fields['auth'] = self.auth_var.get()
        fields['scheduler'] = self.sched_var.get()
        fields['use_jump'] = self.jump_var.get()
        return profile_from_form(self.vars['name'].get().strip(), fields)

    def _get_password(self, prof, prompt_if_missing: bool) -> str | None:
        pw = secrets.get_password(prof.name)
        if pw is None and prompt_if_missing:
            pw = simpledialog.askstring('密码', f'输入 {prof.username}@{prof.hostname} 的密码:', show='*')
        return pw

    def _save(self):
        prof = self._current_profile()
        # 保存阶段不强制已有密码(可后填):校验非密码字段,密码类错误一律滤掉
        errs = [e for e in validate_cluster_inputs(prof, has_password=True) if '密码' not in e]
        if errs:
            self.log.write('❌ ' + '；'.join(errs))
            return
        # 密码认证:弹框收一次密码存 keyring(绝不写 yaml)
        if prof.auth == 'password':
            pw = simpledialog.askstring('密码', f'输入 {prof.username}@{prof.hostname} 的密码(存系统凭据库):', show='*')
            if pw:
                if not secrets.set_password(prof.name, pw):
                    self.log.write('⚠ 系统凭据库不可用,密码未保存,测连接时会现输')
        self.profiles[prof.name] = prof
        save_profiles(self.profiles)
        self.profile_cb['values'] = list(self.profiles)
        self.profile_var.set(prof.name)
        self.log.write(f'✅ 已保存集群「{prof.name}」(密码不写入 yaml)')

    def _on_test(self, trust_new=False):
        prof = self._current_profile()
        pw = self._get_password(prof, prompt_if_missing=(prof.auth == 'password')) if prof.auth == 'password' else None
        errs = validate_cluster_inputs(prof, has_password=(pw is not None))
        if errs:
            self.log.write('❌ ' + '；'.join(errs))
            return
        self.test_btn.configure(state='disabled')
        self.log.write('⏳ 连接中…')
        q = runner.submit(ssh_test.check_connection, prof, pw, trust_new=trust_new)
        self.after(150, lambda: self._poll_test(q))

    def _poll_test(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(150, lambda: self._poll_test(q))
            return
        kind, payload = item
        self.test_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 连接异常:{payload}')
            return
        res = payload
        if res.ok:
            self.log.write(f'✅ {res.message}')
        elif res.needs_trust:
            if messagebox.askyesno('未知主机', f'{res.message}\n\n是否信任该主机并重试?'):
                self._on_test(trust_new=True)
            else:
                self.log.write('已取消(未信任主机)')
        else:
            self.log.write(f'❌ {res.message}')
