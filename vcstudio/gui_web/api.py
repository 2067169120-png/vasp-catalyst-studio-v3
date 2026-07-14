"""pywebview js_api:薄处理器。每个 JS 调用由 pywebview 派独立线程执行,可同步阻塞。"""
from __future__ import annotations


class Api:
    def ping(self) -> str:
        return 'pong'
