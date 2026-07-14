"""Web GUI 入口:python -m vcstudio.gui_web。"""
from __future__ import annotations

import webview

from vcstudio.gui_web.api import Api
from vcstudio.gui_web import resources


def main() -> int:
    api = Api()
    webview.create_window(
        'VASP Catalyst Studio', resources.index_html(), js_api=api,
        width=1180, height=800, min_size=(960, 640))
    webview.start()  # 默认 EdgeChromium(WebView2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
