"""vcs gui 路由测试:默认起 Web、--legacy 起 tkinter、缺 pywebview 给中文提示。

全程 mock,绝不真起窗口(不 import webview、不调 webview.start)。
"""
import importlib.util

import vcstudio.cli.main as cli


def _record_launchers(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, '_launch_web_gui',
                        lambda: (called.__setitem__('web', True), 0)[1])
    monkeypatch.setattr(cli, '_launch_legacy_gui',
                        lambda: (called.__setitem__('legacy', True), 0)[1])
    return called


def test_gui_default_routes_to_web(monkeypatch):
    called = _record_launchers(monkeypatch)
    args = cli.build_parser().parse_args(['gui'])
    assert args.func(args) == 0
    assert called == {'web': True}          # 默认 = Web


def test_gui_legacy_routes_to_tkinter(monkeypatch):
    called = _record_launchers(monkeypatch)
    args = cli.build_parser().parse_args(['gui', '--legacy'])
    assert args.func(args) == 0
    assert called == {'legacy': True}       # --legacy = 旧 tkinter


def _patch_find_spec(monkeypatch, webview_present):
    real = importlib.util.find_spec

    def fake(name, *a, **k):
        if name == 'webview':
            return object() if webview_present else None
        return real(name, *a, **k)
    monkeypatch.setattr(importlib.util, 'find_spec', fake)


def test_launch_web_missing_pywebview_prints_hint(monkeypatch, capsys):
    _patch_find_spec(monkeypatch, webview_present=False)
    rc = cli._launch_web_gui()
    assert rc == 1
    err = capsys.readouterr().err
    assert 'pywebview' in err and 'pip install pywebview' in err
    assert '--legacy' in err                # 提示同时给出降级路线


def test_launch_web_present_delegates_without_window(monkeypatch):
    _patch_find_spec(monkeypatch, webview_present=True)
    import vcstudio.gui_web.__main__ as wm
    calls = []
    monkeypatch.setattr(wm, 'main', lambda: (calls.append('web'), 0)[1])
    assert cli._launch_web_gui() == 0
    assert calls == ['web']                 # 委托 gui_web.__main__.main,不真起窗口


def test_parser_gui_has_legacy_flag():
    assert cli.build_parser().parse_args(['gui']).legacy is False
    assert cli.build_parser().parse_args(['gui', '--legacy']).legacy is True
