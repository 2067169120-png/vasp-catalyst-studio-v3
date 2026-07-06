"""桌面 GUI 入口:单窗口 + 四页(生成 / 吸附能项目 / 任务 / 集群)。中文注释允许,英文标识符。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from vcstudio.gui import style
from vcstudio.gui.generate_tab import GenerateTab
from vcstudio.gui.project_tab import ProjectTab
from vcstudio.gui.jobs_tab import JobsTab
from vcstudio.gui.cluster_tab import ClusterTab


def main(argv=None) -> int:
    root = tk.Tk()
    root.title('VASP Catalyst Studio — 生成 · 项目 · 提交 · 追踪')
    root.geometry('900x800')
    style.apply(root)
    nb = ttk.Notebook(root)
    jobs = JobsTab(nb)
    nb.add(GenerateTab(nb), text='生成')
    nb.add(ProjectTab(nb), text='吸附能项目')
    nb.add(jobs, text='任务')
    nb.add(ClusterTab(nb), text='集群')
    # 切到任务页时自动刷新台账(生成页/项目页刚登记的作业立刻可见)
    nb.bind('<<NotebookTabChanged>>',
            lambda e: jobs.reload() if nb.index('current') == 2 else None)
    nb.pack(fill='both', expand=True)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
