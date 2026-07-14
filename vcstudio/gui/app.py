"""桌面 GUI 入口:单窗口 + 顶部步骤条 + 四页(生成 / 吸附能项目 / 任务 / 集群)。

首次使用引导(PM 评审 P0-1):步骤条常驻展示标准工作流
「① 配置集群 → ② 生成输入 → ③ 提交追踪 → ④ 出报告」,点击跳对应页;
无集群配置时步骤①高亮提醒(前置条件不再藏在最后一个页签)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from vcstudio.gui import style
from vcstudio.gui.generate_tab import GenerateTab
from vcstudio.gui.project_tab import ProjectTab
from vcstudio.gui.jobs_tab import JobsTab
from vcstudio.gui.cluster_tab import ClusterTab

# 步骤 → (显示文本, 对应页签索引)。页签序 0生成/1项目/2任务/3集群。
_STEPS = (('① 配置集群', 3), ('② 生成输入', 0), ('③ 提交追踪', 2), ('④ 出报告', 1))


class StepBar(ttk.Frame):
    """可点击步骤条:当前步主色高亮;未配置集群时步骤① 橙色提醒。"""

    def __init__(self, parent, nb):
        super().__init__(parent, padding=(10, 6, 10, 2))
        self.nb = nb
        self.labels = []
        for i, (text, tab_idx) in enumerate(_STEPS):
            if i:
                ttk.Label(self, text='→', foreground=style.MUTED).pack(side='left', padx=6)
            lbl = ttk.Label(self, text=text, cursor='hand2',
                            font=style.FONT_UI_BOLD, foreground=style.MUTED)
            lbl.pack(side='left')
            lbl.bind('<Button-1>', lambda e, t=tab_idx: self.nb.select(t))
            self.labels.append((lbl, tab_idx))
        self.refresh()

    def refresh(self):
        """当前页高亮 + 无集群时步骤① 橙色警示。"""
        try:
            current = self.nb.index('current')
        except tk.TclError:
            current = 0
        try:
            from vcstudio.cluster.profiles import load_profiles
            has_cluster = bool(load_profiles())
        except Exception:                                # noqa: BLE001
            has_cluster = True                           # 读不到配置不误报
        for lbl, tab_idx in self.labels:
            if tab_idx == current:
                lbl.configure(foreground=style.ACCENT_DARK)
            elif tab_idx == 3 and not has_cluster:
                lbl.configure(foreground='#c2410c')      # 橙:前置未完成
            else:
                lbl.configure(foreground=style.MUTED)
        if not has_cluster:
            self.labels[0][0].configure(text='① 配置集群(未配置,点此)')
        else:
            self.labels[0][0].configure(text='① 配置集群')


def main(argv=None) -> int:
    root = tk.Tk()
    root.title('VASP Catalyst Studio — 生成 · 项目 · 提交 · 追踪')
    root.geometry('900x800')
    style.apply(root)
    nb = ttk.Notebook(root)
    steps = StepBar(root, nb)
    steps.pack(fill='x')
    jobs = JobsTab(nb)
    nb.add(GenerateTab(nb), text='生成')
    nb.add(ProjectTab(nb), text='吸附能项目')
    nb.add(jobs, text='任务')
    nb.add(ClusterTab(nb), text='集群')
    jobs.goto_cluster_tab = lambda: nb.select(3)         # 空态跳转:任务页一键去配置

    def _on_tab_changed(_e):
        steps.refresh()
        if nb.index('current') == 2:
            jobs.reload()        # 切到任务页自动刷新台账(生成页/项目页刚登记的作业立刻可见)
    nb.bind('<<NotebookTabChanged>>', _on_tab_changed)
    nb.pack(fill='both', expand=True)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
