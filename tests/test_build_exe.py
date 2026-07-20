"""Packaging policy tests; no real PyInstaller process is launched."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / 'packaging' / 'build_exe.py'
SPEC = importlib.util.spec_from_file_location('vcstudio_build_exe', SCRIPT)
assert SPEC and SPEC.loader
build_exe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_exe
SPEC.loader.exec_module(build_exe)


def _all_available(monkeypatch, *, unavailable=()):
    missing = set(unavailable)
    monkeypatch.setattr(build_exe, '_has', lambda mod: mod not in missing)


def _option_values(cmd, option):
    return [cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == option]


def test_default_is_full_and_collects_chart_and_rdkit_resources(monkeypatch):
    _all_available(monkeypatch)

    cmd, options = build_exe.build_command([])

    assert options.lite is False
    collected = set(_option_values(cmd, '--collect-all'))
    assert {'matplotlib', 'rdkit'} <= collected
    assert _option_values(cmd, '--collect-binaries') == ['numpy']
    excluded = _option_values(cmd, '--exclude-module')
    assert 'matplotlib' not in excluded
    assert 'numpy' not in excluded
    assert 'rdkit' not in excluded
    assert {'DECIMER', 'tensorflow'} <= set(excluded)
    assert cmd[-1] == build_exe.WEB_ENTRY


@pytest.mark.parametrize('flag', ['--lite', '--no-charts'])
def test_lite_profile_is_explicit_and_does_not_require_heavy_modules(monkeypatch, flag):
    _all_available(monkeypatch, unavailable={'matplotlib', 'numpy', 'rdkit', 'DECIMER'})

    cmd, options = build_exe.build_command([flag])

    assert options.lite is True
    excluded = set(_option_values(cmd, '--exclude-module'))
    assert {'matplotlib', 'numpy', 'rdkit', 'DECIMER'} <= excluded
    assert 'matplotlib' not in _option_values(cmd, '--collect-all')
    assert 'rdkit' not in _option_values(cmd, '--collect-all')


def test_full_build_fails_before_subprocess_when_feature_dependency_missing(
        monkeypatch, capsys):
    _all_available(monkeypatch, unavailable={'matplotlib', 'rdkit'})
    calls = []
    monkeypatch.setattr(build_exe.subprocess, 'call', lambda *args, **kwargs: calls.append(args))

    result = build_exe.main([])

    assert result == 2
    assert calls == []
    error = capsys.readouterr().err
    assert '完整版 EXE 缺少关键构建依赖' in error
    assert 'matplotlib' in error and 'rdkit' in error
    assert '.[packaging,gui,charts,mol]' in error
    assert '--lite' in error
    assert '运行后不能再通过 pip' in error


def test_core_dependencies_are_required_even_for_lite_build(monkeypatch):
    _all_available(monkeypatch, unavailable={'paramiko'})

    with pytest.raises(build_exe.BuildConfigurationError, match='paramiko'):
        build_exe.build_command(['--lite'])


def test_decimer_is_opt_in_and_collected_only_when_requested(monkeypatch):
    _all_available(monkeypatch)

    normal, _ = build_exe.build_command([])
    ocsr, options = build_exe.build_command(['--with-decimer'])

    assert options.with_decimer is True
    assert 'DECIMER' in _option_values(ocsr, '--collect-all')
    assert 'tensorflow' in _option_values(ocsr, '--collect-all')
    assert 'DECIMER' in _option_values(normal, '--exclude-module')
    assert 'DECIMER' not in _option_values(ocsr, '--exclude-module')


def test_decimer_opt_in_checks_its_transitive_runtime(monkeypatch):
    _all_available(monkeypatch, unavailable={'tensorflow'})

    with pytest.raises(build_exe.BuildConfigurationError, match='tensorflow'):
        build_exe.build_command(['--with-decimer'])


def test_legacy_does_not_require_or_collect_webview(monkeypatch):
    _all_available(monkeypatch, unavailable={'webview'})

    cmd, options = build_exe.build_command(['--legacy', '--lite'])

    assert options.legacy is True
    assert 'webview' not in _option_values(cmd, '--collect-all')
    assert cmd[-1] == build_exe.ENTRY


@pytest.mark.parametrize('args', [
    ['--full', '--lite'],
    ['--lite', '--with-decimer'],
    ['--web', '--legacy'],
    ['--unknown'],
])
def test_conflicting_or_unknown_options_are_rejected(monkeypatch, args):
    _all_available(monkeypatch)

    with pytest.raises(build_exe.BuildConfigurationError):
        build_exe.build_command(args)
