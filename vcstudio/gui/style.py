"""GUI 统一视觉:ttk.Style 主题(零新依赖,纯 Tkinter 原生)。

设计语言对标 sun-valley / azure-ttk(Windows 11 Fluent 风格的开源 ttk 主题):
浅灰蓝底 + 白卡片 + 单一主色、控件留白放大、扁平边框、焦点主色描边。
主色沿用图表/报告口径 Paul Tol bright #4477AA。

字体:Segoe UI / Consolas 均无中文字形,中文会回退宋体点阵感 → 混排发虚。
改为运行时探测中文 UI 字体(微软雅黑 UI 优先),并接管 Tk 命名字体
(TkDefaultFont 等) → messagebox / 文件对话框 / 菜单同步生效。

一处定义全局生效;各 Tab 不必自带样式。apply(root) 幂等,任何失败静默降级
(样式绝不能挡住功能)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import tkinter.font as tkfont
from tkinter import ttk

ACCENT = '#4477AA'          # 主色(与 charts.py PUB_COLORS[0] 同源)
ACCENT_DARK = '#335E8C'
ACCENT_SOFT = '#E8F0F8'     # 主色淡底(选中行/悬停)
BG = '#F3F5F9'              # 窗口底
SURFACE = '#FFFFFF'         # 卡片/输入面
INK = '#1F2937'             # 正文
MUTED = '#64748B'           # 次要文字
LINE = '#D7DEE8'            # 边线

# ── 字体(运行时探测,apply() 时填充;探测前给个保险值) ────────────────────────
# 中文 UI 字体候选:含中文字形且带现代无衬线拉丁字形,数字为半角等宽感。
_UI_CANDIDATES = ('Microsoft YaHei UI', 'Microsoft YaHei', '微软雅黑',
                  'DengXian', '等线', 'Noto Sans CJK SC', 'SimHei')
# 日志区:等宽含中文的字体极少见,优先真等宽 CJK,否则退回 UI 字体(9pt)。
_MONO_CANDIDATES = ('Sarasa Mono SC', 'Noto Sans Mono CJK SC',
                    'Microsoft YaHei UI', 'Microsoft YaHei')

FONT_UI = ('Microsoft YaHei UI', 10)
FONT_UI_BOLD = ('Microsoft YaHei UI', 10, 'bold')
FONT_TAB = ('Microsoft YaHei UI', 10, 'bold')
FONT_MONO = ('Microsoft YaHei UI', 9)

# 状态色(任务页树/报告章节共用口径)
STATE_COLORS = {
    'CREATED': '#4b5563', 'UPLOADED': '#1d4ed8', 'SUBMITTED': '#7e22ce',
    'QUEUED': '#7e22ce', 'RUNNING': '#1d4ed8', 'DONE': '#15803d',
    'FAILED': '#b91c1c', 'UNCONVERGED': '#a16207', 'NEEDS_HUMAN': '#a16207',
}


def _pick_family(candidates, fallback):
    """从已装字体里挑第一个可用候选(大小写不敏感)。"""
    try:
        installed = {f.lower() for f in tkfont.families()}
    except Exception:                                    # noqa: BLE001
        return fallback
    for name in candidates:
        if name.lower() in installed:
            return name
    return fallback


def _apply_named_fonts(ui_family):
    """接管 Tk 命名字体 → messagebox/simpledialog/filedialog/菜单同步换字体。

    ttk.Style 只覆盖 ttk 控件;原生对话框吃的是这些命名字体。
    """
    spec = {
        'TkDefaultFont': (ui_family, 10, 'normal'),
        'TkTextFont': (ui_family, 10, 'normal'),
        'TkMenuFont': (ui_family, 10, 'normal'),
        'TkHeadingFont': (ui_family, 10, 'bold'),
        'TkCaptionFont': (ui_family, 11, 'bold'),
        'TkTooltipFont': (ui_family, 9, 'normal'),
        'TkIconFont': (ui_family, 10, 'normal'),
        'TkSmallCaptionFont': (ui_family, 9, 'normal'),
    }
    for name, (fam, size, weight) in spec.items():
        try:
            tkfont.nametofont(name).configure(family=fam, size=size,
                                              weight=weight)
        except Exception:                                # noqa: BLE001
            pass


def apply(root) -> None:
    """套用全局主题。幂等;失败静默(样式问题绝不影响功能)。"""
    global FONT_UI, FONT_UI_BOLD, FONT_TAB, FONT_MONO
    try:
        ui = _pick_family(_UI_CANDIDATES, 'Segoe UI')
        mono = _pick_family(_MONO_CANDIDATES, ui)
        FONT_UI = (ui, 10)
        FONT_UI_BOLD = (ui, 10, 'bold')
        FONT_TAB = (ui, 10, 'bold')
        FONT_MONO = (mono, 9)
        _apply_named_fonts(ui)

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

        # 按钮:白面扁平 + 悬停浅灰蓝 + 焦点主色描边(Fluent 手感)
        style.configure('TButton', background=SURFACE, foreground=INK,
                        bordercolor=LINE, focuscolor=ACCENT,
                        padding=(12, 6), relief='flat')
        style.map('TButton',
                  background=[('pressed', '#DDE6F0'), ('active', ACCENT_SOFT)],
                  bordercolor=[('focus', ACCENT), ('active', '#B9CBDE')])
        # 主操作按钮(各页的"生成/提交"用 style='Accent.TButton' 即可启用)
        style.configure('Accent.TButton', background=ACCENT, foreground='#FFFFFF',
                        bordercolor=ACCENT_DARK, padding=(14, 7),
                        font=FONT_UI_BOLD, relief='flat')
        style.map('Accent.TButton',
                  background=[('pressed', ACCENT_DARK), ('active', '#5588BB'),
                              ('disabled', '#A9BED6')])

        # 输入类:白面 + 焦点主色描边,内边距放大
        style.configure('TEntry', fieldbackground=SURFACE, bordercolor=LINE,
                        insertcolor=INK, padding=5)
        style.map('TEntry', bordercolor=[('focus', ACCENT)],
                  lightcolor=[('focus', ACCENT)])
        style.configure('TCombobox', fieldbackground=SURFACE, bordercolor=LINE,
                        arrowcolor=MUTED, padding=5)
        style.map('TCombobox', bordercolor=[('focus', ACCENT)],
                  lightcolor=[('focus', ACCENT)])
        style.configure('TCheckbutton', background=BG)
        style.map('TCheckbutton', background=[('active', BG)])
        style.configure('TSpinbox', fieldbackground=SURFACE, bordercolor=LINE,
                        arrowcolor=MUTED, padding=4)
        style.map('TSpinbox', bordercolor=[('focus', ACCENT)])

        # Notebook 页签:胶囊感页签,选中 = 白底 + 主色字 + 底部微抬
        style.configure('TNotebook', background=BG, borderwidth=0,
                        tabmargins=(10, 8, 10, 0))
        style.configure('TNotebook.Tab', background='#E2E8F1', foreground=MUTED,
                        font=FONT_TAB, padding=(18, 8), borderwidth=0)
        style.map('TNotebook.Tab',
                  background=[('selected', SURFACE), ('active', '#EBF1F8')],
                  foreground=[('selected', ACCENT_DARK)],
                  expand=[('selected', (0, 0, 0, 2))])

        # Treeview(任务列表):白面、行高放大、选中主色淡底(保持黑字可读)
        style.configure('Treeview', background=SURFACE, fieldbackground=SURFACE,
                        foreground=INK, rowheight=28, bordercolor=LINE)
        style.configure('Treeview.Heading', background='#EDF1F6',
                        foreground=ACCENT_DARK, font=FONT_UI_BOLD, padding=(8, 6))
        style.map('Treeview',
                  background=[('selected', ACCENT_SOFT)],
                  foreground=[('selected', INK)])
        style.map('Treeview.Heading', background=[('active', '#E2E9F1')])

        style.configure('Vertical.TScrollbar', background='#C4CFDD',
                        troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                        relief='flat')
        style.map('Vertical.TScrollbar', background=[('active', '#A9B8CB')])
        style.configure('Horizontal.TScrollbar', background='#C4CFDD',
                        troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                        relief='flat')
    except Exception:                                    # noqa: BLE001 样式失败绝不挡功能
        pass
