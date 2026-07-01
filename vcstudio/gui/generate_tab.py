"""「生成」页:赝势库(持久化)+ POSCAR/INCAR/类型/KPOINTS/输出 → 一键生成 + 打开目录。

生成一律复用 build_job_dir(后台线程),错误转友好文案,绝不弹 traceback。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys

import tkinter as tk
from tkinter import ttk

from vcstudio.gui.widgets import FileRow, LogBox
from vcstudio.gui import runner
from vcstudio.gui.logic import parse_kpoints_field, validate_generate_inputs
from vcstudio.shared.config import load_config, set_potcar_lib_root
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate.potcar import PotcarError


class GenerateTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self._out_dir = ''
        self._build()
        self._load_lib()

    def _build(self):
        # 赝势库(全局,持久化)
        self.lib_row = FileRow(self, '赝势库 POTCAR', mode='dir')
        self.lib_row.grid(row=0, column=0, sticky='w')
        self.lib_row.var.trace_add('write', lambda *a: None)  # 变更即时可读

        ttk.Separator(self, orient='horizontal').grid(row=1, column=0, sticky='ew', pady=6)

        self.poscar_row = FileRow(self, 'POSCAR 结构')
        self.poscar_row.grid(row=2, column=0, sticky='w')
        self.incar_row = FileRow(self, 'INCAR 参数')
        self.incar_row.grid(row=3, column=0, sticky='w')

        opt = ttk.Frame(self)
        opt.grid(row=4, column=0, sticky='w', pady=3)
        ttk.Label(opt, text='计算类型', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.calc_var = tk.StringVar(value='slab')
        for i, t in enumerate(('molecule', 'slab', 'bulk')):
            ttk.Radiobutton(opt, text=t, value=t, variable=self.calc_var).grid(row=0, column=1 + i, padx=2)
        ttk.Label(opt, text='KPOINTS').grid(row=0, column=4, padx=(16, 2))
        self.kpts_var = tk.StringVar(value='自动')
        ttk.Entry(opt, textvariable=self.kpts_var, width=10).grid(row=0, column=5)

        self.out_row = FileRow(self, '输出目录', mode='dir')
        self.out_row.grid(row=5, column=0, sticky='w')

        self.validate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text='INCAR 校验补全(缺 ENCUT/MAGMOM 自动补)',
                        variable=self.validate_var).grid(row=6, column=0, sticky='w', padx=18, pady=3)

        btns = ttk.Frame(self)
        btns.grid(row=7, column=0, pady=6)
        self.run_btn = ttk.Button(btns, text='▶ 一键生成', command=self._on_run)
        self.run_btn.grid(row=0, column=0, padx=6)
        self.open_btn = ttk.Button(btns, text='📂 打开输出文件夹', command=self._open_out, state='disabled')
        self.open_btn.grid(row=0, column=1, padx=6)

        ttk.Label(self, text='运行日志').grid(row=8, column=0, sticky='w')
        self.log = LogBox(self)
        self.log.grid(row=9, column=0, sticky='nsew', pady=3)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(9, weight=1)

    def _load_lib(self):
        try:
            self.lib_row.set(load_config().get('potcar_lib_root', ''))
        except Exception:
            pass

    def _persist_lib(self):
        lib = self.lib_row.get().strip()
        if lib:
            try:
                set_potcar_lib_root(lib)
            except Exception as e:
                self.log.write(f'⚠ 赝势库路径保存失败:{e}')

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

        self._out_dir = out
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
            self.open_btn.configure(state='normal')
        else:
            self.log.write(f'❌ 错误:{payload}')  # ValueError/PotcarError/OSError 的中文文案

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
