"""资源定位:开发态用包内 assets/,PyInstaller 单文件态用 _MEIPASS/vcstudio_assets。"""
from __future__ import annotations

import sys
from pathlib import Path


def asset_dir() -> Path:
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / 'vcstudio_assets'
    return Path(__file__).parent / 'assets'


def index_html() -> str:
    return str(asset_dir() / 'index.html')
