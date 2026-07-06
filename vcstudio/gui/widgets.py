"""复用 Tk 小组件:文件/目录选择行、只读日志框(按级别着色)。中文注释允许,英文标识符。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog


class FileRow(ttk.Frame):
    """一行:标签 + 输入框 + [浏览…]。mode='openfile' 选文件,'dir' 选目录。

    on_change:可选回调,输入框内容变化时以当前值调用(浏览选择或手动输入都触发)。
    """

    def __init__(self, parent, label: str, mode: str = 'openfile', width: int = 48,
                 on_change=None):
        super().__init__(parent)
        self.mode = mode
        self.var = tk.StringVar()
        ttk.Label(self, text=label, width=14, anchor='e').grid(row=0, column=0, padx=4, pady=3)
        ttk.Entry(self, textvariable=self.var, width=width).grid(row=0, column=1, padx=4)
        ttk.Button(self, text='浏览…', command=self._browse).grid(row=0, column=2, padx=4)
        if on_change is not None:
            self.var.trace_add('write', lambda *_: on_change(self.var.get()))

    def _browse(self):
        if self.mode == 'dir':
            path = filedialog.askdirectory()
        else:
            path = filedialog.askopenfilename()
        if path:
            self.var.set(path)

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str):
        self.var.set(value)


class LogBox(ttk.Frame):
    """只读多行日志框(带滚动条)。按行首符号自动着色:✅绿 / ⚠橙 / ❌红。"""

    _LEVEL_TAGS = (('✅', 'ok'), ('⚠', 'warn'), ('❌', 'err'))

    def __init__(self, parent, height: int = 12):
        super().__init__(parent)
        self.text = tk.Text(self, height=height, wrap='word', state='disabled',
                            background='#FFFFFF', foreground='#1F2937',
                            font=('Consolas', 9), relief='solid', borderwidth=1,
                            highlightthickness=0, padx=6, pady=4)
        scroll = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text.tag_configure('ok', foreground='#15803d')
        self.text.tag_configure('warn', foreground='#a16207')
        self.text.tag_configure('err', foreground='#b91c1c')

    def _tag_for(self, msg: str) -> str | None:
        s = msg.lstrip()
        for prefix, tag in self._LEVEL_TAGS:
            if s.startswith(prefix):
                return tag
        return None

    def write(self, msg: str):
        self.text.configure(state='normal')
        tag = self._tag_for(msg)
        self.text.insert('end', msg + '\n', tag or ())
        self.text.see('end')
        self.text.configure(state='disabled')

    def clear(self):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')
