"""「吸附能项目」页 v1:清洁表面 + 组态族 + 气相参考 → 批量生成;ΔE 汇总与导出。

单表单形态(向导式分步留待 v2):所有生成走后台线程;ΔE 只在成员全部 DONE 时给出。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from vcstudio.gui.widgets import FileRow, LogBox
from vcstudio.gui import runner
from vcstudio.project import adsorption, report_full
from vcstudio.shared.config import load_config


class ProjectTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self._configs = []          # 组态 POSCAR 路径列表
        self._build()
        self._reload_projects()

    def _build(self):
        # ── 新建项目 ──
        new = ttk.LabelFrame(self, text=' 新建吸附能项目(一次生成整组作业) ', padding=(8, 4))
        new.grid(row=0, column=0, sticky='ew')
        new.columnconfigure(0, weight=1)

        nrow = ttk.Frame(new)
        nrow.grid(row=0, column=0, sticky='w')
        ttk.Label(nrow, text='项目名', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.name_var = tk.StringVar()
        ttk.Entry(nrow, textvariable=self.name_var, width=24).grid(row=0, column=1, padx=4)

        self.root_row = FileRow(new, '输出根目录', mode='dir')
        self.root_row.grid(row=1, column=0, sticky='w')
        self.slab_row = FileRow(new, '清洁表面 POSCAR')
        self.slab_row.grid(row=2, column=0, sticky='w')
        self.ref_row = FileRow(new, '气相参考(可选)')
        self.ref_row.grid(row=3, column=0, sticky='w')
        self.incar_row = FileRow(new, '共享 INCAR')
        self.incar_row.grid(row=4, column=0, sticky='w')

        crow = ttk.Frame(new)
        crow.grid(row=5, column=0, sticky='ew', pady=3)
        ttk.Label(crow, text='吸附组态', width=14, anchor='ne').grid(row=0, column=0, padx=4, sticky='n')
        self.cfg_list = tk.Listbox(crow, height=5, width=64)
        self.cfg_list.grid(row=0, column=1, padx=4)
        cbtn = ttk.Frame(crow)
        cbtn.grid(row=0, column=2, sticky='n')
        ttk.Button(cbtn, text='添加(可多选)…', command=self._add_configs).grid(row=0, column=0, pady=2)
        ttk.Button(cbtn, text='移除选中', command=self._remove_config).grid(row=1, column=0, pady=2)

        gbar = ttk.Frame(new)
        gbar.grid(row=6, column=0, pady=4)
        self.gen_btn = ttk.Button(gbar, text='🚀 批量生成并登记台账', command=self._on_generate)
        self.gen_btn.grid(row=0, column=0, padx=6)
        ttk.Label(gbar, text='类型自动:表面/组态=slab,气相参考=molecule(Γ点)',
                  foreground='#666666').grid(row=0, column=1, padx=6)

        # ── 已有项目:ΔE 汇总 ──
        ex = ttk.LabelFrame(self, text=' 项目 ΔE 汇总(成员全部 DONE 才给数) ', padding=(8, 4))
        ex.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        ttk.Label(ex, text='选择项目', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.proj_var = tk.StringVar()
        self.proj_cb = ttk.Combobox(ex, textvariable=self.proj_var, width=52, state='readonly')
        self.proj_cb.grid(row=0, column=1, padx=4)
        pbtn = ttk.Frame(ex)
        pbtn.grid(row=1, column=0, columnspan=2, pady=4)
        ttk.Button(pbtn, text='⟳ 刷新', command=self._reload_projects).grid(row=0, column=0, padx=4)
        ttk.Button(pbtn, text='Σ 计算 ΔE', command=self._on_delta).grid(row=0, column=1, padx=4)
        ttk.Button(pbtn, text='📊 导出 CSV', command=self._on_export).grid(row=0, column=2, padx=4)
        ttk.Button(pbtn, text='📄 导出报告(含ΔE)', command=self._on_report).grid(row=0, column=3, padx=4)
        ttk.Button(pbtn, text='📂 打开项目目录', command=self._open_project).grid(row=0, column=4, padx=4)

        ttk.Label(self, text='项目日志 / ΔE 结果').grid(row=2, column=0, sticky='w', pady=(6, 0))
        self.log = LogBox(self, height=12)
        self.log.grid(row=3, column=0, sticky='nsew', pady=3)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

    # ── 组态列表 ──
    def _add_configs(self):
        paths = filedialog.askopenfilenames(title='选择吸附组态 POSCAR(可多选)')
        for p in paths:
            if p and p not in self._configs:
                self._configs.append(p)
                self.cfg_list.insert('end', p)

    def _remove_config(self):
        for idx in reversed(self.cfg_list.curselection()):
            self._configs.pop(idx)
            self.cfg_list.delete(idx)

    # ── 批量生成 ──
    def _on_generate(self):
        name = self.name_var.get().strip()
        root, slab = self.root_row.get().strip(), self.slab_row.get().strip()
        incar, ref = self.incar_row.get().strip(), self.ref_row.get().strip()
        errs = []
        if not name:
            errs.append('未填项目名')
        if not root:
            errs.append('未选输出根目录')
        if not slab or not os.path.isfile(slab):
            errs.append('清洁表面 POSCAR 不存在')
        if not incar or not os.path.isfile(incar):
            errs.append('共享 INCAR 不存在')
        if not self._configs:
            errs.append('至少添加一个吸附组态')
        if ref and not os.path.isfile(ref):
            errs.append('气相参考文件不存在')
        if errs:
            for e in errs:
                self.log.write(f'❌ {e}')
            return
        lib = ''
        try:
            lib = load_config().get('potcar_lib_root', '')
        except Exception:
            pass
        self.gen_btn.configure(state='disabled')
        self.log.clear()
        self.log.write(f'⏳ 批量生成:清洁表面 + {len(self._configs)} 组态'
                       + (' + 气相参考' if ref else '') + ' …')
        q = runner.submit(adsorption.create_project, os.path.join(root, name), name,
                          clean_poscar=slab, config_poscars=list(self._configs),
                          incar_path=incar, ref_poscar=(ref or None), lib_root=lib or None)
        self.after(150, lambda: self._poll_gen(q))

    def _poll_gen(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(150, lambda: self._poll_gen(q))
            return
        kind, payload = item
        self.gen_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 批量生成失败:{payload}')
            return
        for member, d, warnings in payload['generated']:
            for w in warnings:
                self.log.write(f'⚠ {member}:{w}')
            self.log.write(f'✅ {member} → {d}')
        for member, msg in payload['errors']:
            self.log.write(f'❌ {member}:{msg}')
        self.log.write(f"📁 project.yaml:{payload['project_path']}")
        self.log.write('➡ 到「任务」页选中这组作业上传提交;全部 DONE 后回本页算 ΔE')
        self._reload_projects()

    # ── 项目管理 ──
    def _reload_projects(self):
        items = adsorption.list_projects()
        self.proj_cb['values'] = items
        if items and not self.proj_var.get():
            self.proj_var.set(items[-1])

    def _current_project(self):
        p = self.proj_var.get()
        proj = adsorption.load_project(p) if p else None
        if proj is None:
            self.log.write('❌ 请先选择一个项目(或其 project.yaml 已被移动)')
        return proj

    def _on_delta(self):
        proj = self._current_project()
        if not proj:
            return
        s = adsorption.delta_e_rows(proj)
        slab_state, e_slab = s['slab']
        self.log.write(f"━━ 项目「{proj['name']}」 ━━")
        self.log.write(f"清洁表面:{slab_state}"
                       + (f'  E={e_slab:.6f} eV' if e_slab is not None else ''))
        if s['has_ref']:
            ref_state, e_ref = s['ref']
            self.log.write(f"气相参考:{ref_state}"
                           + (f'  E={e_ref:.6f} eV' if e_ref is not None else ''))
        for r in s['rows']:
            if r['delta_e'] is not None:
                self.log.write(f"✅ {r['name']}:ΔE = {r['delta_e']:.4f} eV"
                               + (f"({r['note']})" if r['note'] else ''))
            else:
                self.log.write(f"⚠ {r['name']}:{r['state']} — {r['note']}")

    def _on_export(self):
        proj = self._current_project()
        if not proj:
            return
        s = adsorption.delta_e_rows(proj)
        out = filedialog.asksaveasfilename(
            title='导出 ΔE 表', defaultextension='.csv',
            initialfile=f"{proj['name']}_deltaE.csv",
            filetypes=[('CSV(Excel 可开)', '*.csv')])
        if not out:
            return
        try:
            adsorption.export_csv(proj, s, out)
            self.log.write(f'✅ 已导出:{out}')
        except OSError as e:
            self.log.write(f'❌ 导出失败:{e}')

    def _member_dirs(self, proj):
        """项目全部成员作业目录(清洁表面 + 气相参考 + 组态族)。"""
        mem = proj.get('members') or {}
        return [d for d in ([mem.get('clean_slab'), mem.get('gas_ref')]
                            + list(mem.get('configs') or [])) if d]

    def _on_report(self):
        proj = self._current_project()
        if not proj:
            return
        if not self._member_dirs(proj):
            self.log.write('❌ 项目无成员作业')
            return
        out = filedialog.asksaveasfilename(
            title='导出完整报告', defaultextension='.html',
            initialfile=f"{proj['name']}_完整报告.html",
            filetypes=[('HTML 完整报告', '*.html')])
        if not out:
            return
        try:
            cfg = load_config()
        except Exception:                               # noqa: BLE001
            cfg = {}
        self.log.write('⏳ 生成完整报告(Origin 图表 + 结构图 + AI 分析,可能需一两分钟)…')
        q = runner.submit(report_full.generate_project_report, proj, out, config=cfg)
        self.after(300, lambda: self._poll_report(q, out))

    def _poll_report(self, q, out):
        item = runner.poll(q)
        if item is None:
            self.after(300, lambda: self._poll_report(q, out))
            return
        kind, payload = item
        if kind == 'error':
            self.log.write(f'❌ 生成报告失败:{payload}')
            return
        self.log.write(f'✅ 完整报告已生成:{out}')
        try:
            if sys.platform == 'win32':
                os.startfile(str(out))  # noqa
            else:
                import subprocess
                subprocess.Popen(['xdg-open', str(out)])
        except Exception as e:                          # noqa: BLE001
            self.log.write(f'⚠ 已生成但无法自动打开:{e}')

    def _open_project(self):
        proj = self._current_project()
        if not proj:
            return
        root = proj.get('root', '')
        if root and os.path.isdir(root):
            try:
                if sys.platform == 'win32':
                    os.startfile(root)  # noqa
                else:
                    import subprocess
                    subprocess.Popen(['xdg-open', root])
            except Exception as e:
                self.log.write(f'⚠ 无法打开目录:{e}')
