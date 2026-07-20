"""依赖安装的冻结态回归测试。

单文件 EXE 中 sys.executable 指向应用自身，不是 Python 解释器；
测试锁住“绝不重新启动当前 EXE”与“不假报外部 pip 安装后立即可用”。
"""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from vcstudio.gui_web.api import Api


def _fake_tool(available=False):
    return types.SimpleNamespace(
        probe=lambda _path=None: {'available': available, 'detail': '测试探测'})


def test_default_runner_never_spawns_current_frozen_exe(monkeypatch, tmp_path):
    calls = []

    def forbidden_popen(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('冻结态不应启动任何 pip 子进程')

    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', r'C:\Program Files\VCS\VASP Catalyst Studio.exe')
    monkeypatch.setattr(subprocess, 'Popen', forbidden_popen)

    with pytest.raises(RuntimeError, match='单文件 EXE'):
        Api._default_deps_runner(['rdkit'], str(tmp_path / 'deps.log'))

    assert calls == []
    assert not (tmp_path / 'deps.log').exists()


def test_deps_install_frozen_returns_actionable_error_without_runner(monkeypatch):
    calls = []

    def runner(*args):
        calls.append(args)
        raise AssertionError('冻结态不应调用安装器')

    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    api = Api(deps_runner=runner)
    out = api.deps_install(['rdkit'])

    assert out['ok'] is False and out['started'] is False
    assert '单文件 EXE' in out['error']
    assert '重新打包' in out['error']
    assert calls == []
    assert api._deps_job is None


def test_deps_status_frozen_disables_runtime_install(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(Api, '_pkg_present', staticmethod(lambda _name: False))
    api = Api(multiwfn_mod=_fake_tool(), vmd_mod=_fake_tool())

    out = api.deps_status()

    assert out['ok'] is True
    assert out['runtime_install_supported'] is False
    assert '重新打包' in out['runtime_install_note']
    python_deps = [d for d in out['deps'] if d['key'] in {'rdkit', 'decimer', 'matplotlib'}]
    assert python_deps
    assert all(d['installable'] is False for d in python_deps)
    assert all('单文件 EXE' in d['note'] for d in python_deps)


def test_default_runner_source_mode_uses_python_interpreter(monkeypatch, tmp_path):
    captured = {}
    proc = object()

    def fake_popen(command, **kwargs):
        captured['command'] = command
        captured['kwargs'] = kwargs
        return proc

    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.setattr(sys, 'executable', r'C:\Python 312\python.exe')
    monkeypatch.setattr(subprocess, 'Popen', fake_popen)

    result = Api._default_deps_runner(['matplotlib', 'numpy'], str(tmp_path / 'deps.log'))
    try:
        assert result is proc
        assert captured['command'] == [
            r'C:\Python 312\python.exe', '-m', 'pip', 'install', 'matplotlib', 'numpy']
        assert captured['kwargs']['stderr'] is subprocess.STDOUT
        assert 'shell' not in captured['kwargs']
        assert captured['kwargs']['stdout'].closed
    finally:
        if not captured['kwargs']['stdout'].closed:
            captured['kwargs']['stdout'].close()
