"""GUI 统一视觉:ttk.Style 主题(零新依赖,纯 Tkinter 原生)。

设计语言与图表/报告一致:Paul Tol bright 主色 #4477AA、中性灰阶、Segoe UI。
一处定义全局生效;各 Tab 不必自带样式。apply(root) 幂等,任何失败静默降级
(样式绝不能挡住功能)。中文注释允许,英文标识符。
"""
from __future__ import annotations

from tkinter import ttk

ACCENT = '#4477AA'          # 主色(与 charts.py PUB_COLORS[0] 同源)
ACCENT_DARK = '#335E8C'
BG = '#F5F7FA'              # 窗口底
SURFACE = '#FFFFFF'         # 卡片/输入面
INK = '#1F2937'             # 正文
MUTED = '#64748B'           # 次要文字
LINE = '#D7DEE8'            # 边线

FONT_UI = ('Segoe UI', 10)
FONT_UI_BOLD = ('Segoe UI', 10, 'bold')
FONT_TAB = ('Segoe UI', 10, 'bold')
FONT_MONO = ('Consolas', 9)

# 状态色(任务页树/报告章节共用口径)
STATE_COLORS = {
    'CREATED': '#4b5563', 'UPLOADED': '#1d4ed8', 'SUBMITTED': '#7e22ce',
    'QUEUED': '#7e22ce', 'RUNNING': '#1d4ed8', 'DONE': '#15803d',
    'FAILED': '#b91c1c', 'UNCONVERGED': '#a16207', 'NEEDS_HUMAN': '#a16207',
}


def apply(root) -> None:
    """套用全局主题。幂等;失败静默(样式问题绝不影响功能)。"""
    try:
        style = ttk.Style(root)
        # clam 是跨平台里最服帖的可定制底座(vista 不吃背景色定制)
        style.theme_use('clam')
        root.configure(bg=BG)

        style.configure('.', background=BG, foreground=INK, font=FONT_UI)
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=INK)
        style.configure('TLabelframe', background=BG, bordercolor=LINE,
                        relief='solid', borderwidth=1)
        style.configure('TLabelframe.Label', background=BG,
                        foreground=ACCENT_DARK, font=FONT_UI_BOLD)

        # 按钮:默认灰阶;悬停/按下有反馈
        style.configure('TButton', background=SURFACE, foreground=INK,
                        bordercolor=LINE, focuscolor=ACCENT, padding=(10, 4))
        style.map('TButton',
                  background=[('pressed', '#E2E8F0'), ('active', '#EEF2F7')],
                  bordercolor=[('focus', ACCENT)])
        # 主操作按钮(各页的"生成/提交"用 style='Accent.TButton' 即可启用)
        style.configure('Accent.TButton', background=ACCENT, foreground='#FFFFFF',
                        bordercolor=ACCENT_DARK, padding=(12, 5), font=FONT_UI_BOLD)
        style.map('Accent.TButton',
                  background=[('pressed', ACCENT_DARK), ('active', '#5588BB'),
                              ('disabled', '#A9BEd6')])

        # 输入类
        style.configure('TEntry', fieldbackground=SURFACE, bordercolor=LINE,
                        insertcolor=INK, padding=3)
        style.map('TEntry', bordercolor=[('focus', ACCENT)])
        style.configure('TCombobox', fieldbackground=SURFACE, bordercolor=LINE,
                        arrowcolor=MUTED, padding=3)
        style.map('TCombobox', bordercolor=[('focus', ACCENT)])
        style.configure('TCheckbutton', background=BG)
        style.map('TCheckbutton', background=[('active', BG)])

        # Notebook 页签:选中 = 白底 + 主色字,未选中 = 灰底
        style.configure('TNotebook', background=BG, borderwidth=0, tabmargins=(8, 6, 8, 0))
        style.configure('TNotebook.Tab', background='#E4E9F0', foreground=MUTED,
                        font=FONT_TAB, padding=(16, 7))
        style.map('TNotebook.Tab',
                  background=[('selected', SURFACE)],
                  foreground=[('selected', ACCENT_DARK)],
                  expand=[('selected', (0, 0, 0, 2))])

        # Treeview(任务列表):白面、行高、选中主色
        style.configure('Treeview', background=SURFACE, fieldbackground=SURFACE,
                        foreground=INK, rowheight=26, bordercolor=LINE)
        style.configure('Treeview.Heading', background='#EDF1F6',
                        foreground=ACCENT_DARK, font=FONT_UI_BOLD, padding=(6, 4))
        style.map('Treeview',
                  background=[('selected', '#DCE7F3')],
                  foreground=[('selected', INK)])
        style.map('Treeview.Heading', background=[('active', '#E2E9F1')])

        style.configure('Vertical.TScrollbar', background='#CBD5E1',
                        troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
    except Exception:                                    # noqa: BLE001 样式失败绝不挡功能
        pass
