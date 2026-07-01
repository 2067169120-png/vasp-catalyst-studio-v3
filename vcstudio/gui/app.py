"""桌面 GUI 入口:单窗口 + 两页(生成 / 集群)。中文注释允许,英文标识符。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from vcstudio.gui.generate_tab import GenerateTab
from vcstudio.gui.cluster_tab import ClusterTab


def main(argv=None) -> int:
    root = tk.Tk()
    root.title('VASP Catalyst Studio — 输入生成器')
    root.geometry('720x640')
    nb = ttk.Notebook(root)
    nb.add(GenerateTab(nb), text='生成')
    nb.add(ClusterTab(nb), text='集群')
    nb.pack(fill='both', expand=True)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
