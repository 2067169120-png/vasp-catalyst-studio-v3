"""后台线程 + 队列:让耗时活(生成/测连接)不冻界面。

用法:q = submit(fn, ...);Tk 里用 widget.after(100, ...) 轮询 poll(q)。
结果统一 ('ok', 返回值) 或 ('error', 异常)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import queue
import threading


def submit(fn, *args, **kwargs) -> queue.Queue:
    """在 daemon 线程跑 fn(*args, **kwargs),结果入队并返回该队列。"""
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            q.put(('ok', fn(*args, **kwargs)))
        except Exception as e:  # 任何异常都转消息,绝不让线程崩到界面
            q.put(('error', e))

    threading.Thread(target=worker, daemon=True).start()
    return q


def poll(q: queue.Queue):
    """非阻塞取一条结果;队列空返回 None。"""
    try:
        return q.get_nowait()
    except queue.Empty:
        return None
