"""复用 Tk 小组件:文件/目录选择行、只读日志框。中文注释允许,英文标识符。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog


class FileRow(ttk.Frame):
    """一行:标签 + 输入框 + [浏览…]。mode='openfile' 选文件,'dir' 选目录。"""

    def __init__(self, parent, label: str, mode: str = 'openfile', width: int = 48):
        super().__init__(parent)
        self.mode = mode
        self.var = tk.StringVar()
        ttk.Label(self, text=label, width=14, anchor='e').grid(row=0, column=0, padx=4, pady=3)
        ttk.Entry(self, textvariable=self.var, width=width).grid(row=0, column=1, padx=4)
        ttk.Button(self, text='浏览…', command=self._browse).grid(row=0, column=2, padx=4)

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
    """只读多行日志框(带滚动条)。"""

    def __init__(self, parent, height: int = 12):
        super().__init__(parent)
        self.text = tk.Text(self, height=height, wrap='word', state='disabled')
        scroll = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def write(self, msg: str):
        self.text.configure(state='normal')
        self.text.insert('end', msg + '\n')
        self.text.see('end')
        self.text.configure(state='disabled')

    def clear(self):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')
