"""「生成」页:赝势库(持久化)+ POSCAR/INCAR/类型/KPOINTS/输出 → 一键生成 + 打开目录。

生成一律复用 build_job_dir(后台线程),错误转友好文案,绝不弹 traceback。
S1 增强:①选完 POSCAR/INCAR 即时解析预览(生成前就看到会补什么)
②POSCAR/INCAR/输出目录跨会话记忆 ③生成成功后落 job.yaml(任务台账雏形)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys

import tkinter as tk
from tkinter import ttk

from vcstudio.gui.widgets import FileRow, LogBox
from vcstudio.gui import runner
from vcstudio.gui.logic import (
    parse_kpoints_field, validate_generate_inputs, poscar_preview, incar_preview,
)
from vcstudio.shared.config import load_config, set_potcar_lib_root, get_ui_state, set_ui_state
from vcstudio.shared import manifest
from vcstudio.generate.job_builder import build_job_dir


class GenerateTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self._out_dir = ''
        self._last_run = None       # 本次生成参数(成功后写 manifest 用)
        self._preview_memo = None   # 预览去重:相同输入不重复解析
        self._build()
        self._load_state()

    def _build(self):
        # 赝势库(全局,持久化)
        self.lib_row = FileRow(self, '赝势库 POTCAR', mode='dir',
                               on_change=lambda _v: self._refresh_preview())
        self.lib_row.grid(row=0, column=0, sticky='w')

        ttk.Separator(self, orient='horizontal').grid(row=1, column=0, sticky='ew', pady=6)

        self.poscar_row = FileRow(self, 'POSCAR 结构',
                                  on_change=lambda _v: self._refresh_preview())
        self.poscar_row.grid(row=2, column=0, sticky='w')
        self.incar_row = FileRow(self, 'INCAR 参数',
                                 on_change=lambda _v: self._refresh_preview())
        self.incar_row.grid(row=3, column=0, sticky='w')

        opt = ttk.Frame(self)
        opt.grid(row=4, column=0, sticky='w', pady=3)
        ttk.Label(opt, text='计算类型', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.calc_var = tk.StringVar(value='slab')
        for i, t in enumerate(('molecule', 'slab', 'bulk')):
            ttk.Radiobutton(opt, text=t, value=t, variable=self.calc_var,
                            command=self._refresh_preview).grid(row=0, column=1 + i, padx=2)
        ttk.Label(opt, text='KPOINTS').grid(row=0, column=4, padx=(16, 2))
        self.kpts_var = tk.StringVar(value='自动')
        ttk.Entry(opt, textvariable=self.kpts_var, width=10).grid(row=0, column=5)

        self.out_row = FileRow(self, '输出目录', mode='dir')
        self.out_row.grid(row=5, column=0, sticky='w')

        self.validate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text='INCAR 校验补全(缺 ENCUT/MAGMOM 自动补)',
                        variable=self.validate_var,
                        command=self._refresh_preview).grid(row=6, column=0, sticky='w',
                                                            padx=18, pady=3)

        # 即时预览:生成前就能看到解析结果与将要发生的补全
        pv = ttk.LabelFrame(self, text=' 解析预览 ', padding=(8, 4))
        pv.grid(row=7, column=0, sticky='ew', padx=4, pady=(2, 0))
        pv.columnconfigure(0, weight=1)
        self.preview_lbl = ttk.Label(pv, text='(选择 POSCAR / INCAR 后自动解析预览)',
                                     justify='left', anchor='nw')
        self.preview_lbl.grid(row=0, column=0, sticky='ew')

        btns = ttk.Frame(self)
        btns.grid(row=8, column=0, pady=6)
        self.run_btn = ttk.Button(btns, text='▶ 一键生成', style='Accent.TButton', command=self._on_run)
        self.run_btn.grid(row=0, column=0, padx=6)
        self.open_btn = ttk.Button(btns, text='📂 打开输出文件夹', command=self._open_out, state='disabled')
        self.open_btn.grid(row=0, column=1, padx=6)

        ttk.Label(self, text='运行日志').grid(row=9, column=0, sticky='w')
        self.log = LogBox(self)
        self.log.grid(row=10, column=0, sticky='nsew', pady=3)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(10, weight=1)

    def _load_state(self):
        """启动回填:赝势库 + 最近使用的 POSCAR/INCAR/输出目录(跨会话记忆)。"""
        try:
            cfg = load_config()
            self.lib_row.set(cfg.get('potcar_lib_root', ''))
            ui = get_ui_state(cfg)
            for key, row in (('last_poscar', self.poscar_row),
                             ('last_incar', self.incar_row),
                             ('last_out', self.out_row)):
                val = ui.get(key)
                if val:
                    row.set(val)
        except Exception:
            pass
        self._refresh_preview()

    def _persist_lib(self):
        lib = self.lib_row.get().strip()
        if lib:
            try:
                set_potcar_lib_root(lib)
            except Exception as e:
                self.log.write(f'⚠ 赝势库路径保存失败:{e}')

    def _refresh_preview(self):
        """即时预览(纯读、不写任何文件)。相同输入去重,避免重复扫赝势库。"""
        poscar = self.poscar_row.get().strip()
        incar = self.incar_row.get().strip()
        lib = self.lib_row.get().strip()
        key = (poscar, incar, lib, self.calc_var.get(), self.validate_var.get())
        if key == self._preview_memo:
            return
        self._preview_memo = key
        try:
            pos_txt = poscar_preview(poscar, self.calc_var.get())
            inc_txt = incar_preview(incar, poscar, lib, self.validate_var.get())
            self.preview_lbl.configure(text=pos_txt + '\n' + inc_txt)
        except Exception as e:      # 预览绝不干扰主流程
            self.preview_lbl.configure(text=f'⚠ 预览异常:{e}')

    def _on_run(self):
        self.log.clear()
        self._persist_lib()
        lib = self.lib_row.get().strip()
        poscar, incar, out = self.poscar_row.get().strip(), self.incar_row.get().strip(), self.out_row.get().strip()
        errs = validate_generate_inputs(poscar, incar, out, lib)
        if errs:
            for e in errs:
                self.log.write(f'❌ {e}')
            return
        try:
            kpts = parse_kpoints_field(self.kpts_var.get())
        except ValueError as e:
            self.log.write(f'❌ {e}')
            return

        try:                          # 路径记忆:下次启动自动回填
            set_ui_state(last_poscar=poscar, last_incar=incar, last_out=out)
        except Exception:
            pass

        self._out_dir = out
        self._last_run = {'poscar': poscar, 'validate': self.validate_var.get()}
        self.run_btn.configure(state='disabled')
        self.log.write('⏳ 生成中…')
        q = runner.submit(build_job_dir, poscar, incar, out,
                          calc_type=self.calc_var.get(), kpoints=kpts,
                          validate=self.validate_var.get(), lib_root=lib)
        self.after(100, lambda: self._poll(q))

    def _poll(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(100, lambda: self._poll(q))
            return
        kind, payload = item
        self.run_btn.configure(state='normal')
        if kind == 'ok':
            for w in payload['warnings']:
                self.log.write(f'⚠ {w}')
            self.log.write(f"✅ 已生成:{payload['out_dir']}")
            self.log.write(f"元素:{payload['elements']}   KPOINTS:{payload['kpoints']}")
            self._write_manifest(payload)
            self.open_btn.configure(state='normal')
        else:
            self.log.write(f'❌ 错误:{payload}')  # ValueError/PotcarError/OSError 的中文文案

    def _write_manifest(self, payload):
        """生成成功后落 job.yaml(任务台账雏形)。失败只告警,不影响已生成的四件套。"""
        if not self._last_run:
            return
        try:
            manifest.create_from_build(
                payload['out_dir'], payload,
                poscar_path=self._last_run['poscar'],
                validate=self._last_run['validate'])
            from vcstudio.cluster import ledger
            ledger.register(payload['out_dir'])
            self.log.write('📝 已写 job.yaml 并登记台账(到「任务」页可上传提交)')
        except Exception as e:
            self.log.write(f'⚠ job.yaml/台账写入失败(不影响四件套):{e}')

    def _open_out(self):
        if self._out_dir and os.path.isdir(self._out_dir):
            try:
                if sys.platform == 'win32':
                    os.startfile(self._out_dir)  # noqa
                else:
                    import subprocess
                    subprocess.Popen(['xdg-open', self._out_dir])
            except Exception as e:
                self.log.write(f'⚠ 无法打开目录:{e}')
