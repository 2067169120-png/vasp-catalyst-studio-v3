"""Packaging policy tests; no real PyInstaller process is launched."""
from __future__ import annotations

import importlib.util
import os
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
    assert {'matplotlib', 'rdkit', 'docx', 'pypdf', 'reportlab'} <= collected
    assert _option_values(cmd, '--collect-binaries') == ['numpy']
    excluded = _option_values(cmd, '--exclude-module')
    assert 'matplotlib' not in excluded
    assert 'numpy' not in excluded
    assert 'rdkit' not in excluded
    assert {'DECIMER', 'tensorflow'} <= set(excluded)
    data_targets = _option_values(cmd, '--add-data')
    assert any(value.endswith(os.pathsep + 'vcstudio_assets') for value in data_targets)
    assert any(
        value.endswith(os.pathsep + os.path.join('vcstudio', 'shared', 'locales'))
        for value in data_targets
    )
    assert 'vcstudio.cli.main' in _option_values(cmd, '--hidden-import')
    hidden_imports = set(_option_values(cmd, '--hidden-import'))
    assert set(build_exe.JOURNEY_HIDDEN_IMPORTS) <= hidden_imports
    assert Path(build_exe.WEB_ENTRY).name == 'frozen_entry.py'
    assert cmd[-1] == build_exe.WEB_ENTRY


def test_lite_profile_is_explicit_and_does_not_require_heavy_modules(monkeypatch):
    _all_available(
        monkeypatch,
        unavailable={
            'matplotlib', 'numpy', 'rdkit', 'docx', 'pypdf', 'reportlab', 'DECIMER'},
    )

    cmd, options = build_exe.build_command(['--lite'])

    assert options.lite is True
    excluded = set(_option_values(cmd, '--exclude-module'))
    assert {
        'matplotlib', 'numpy', 'rdkit', 'docx', 'pypdf', 'reportlab', 'DECIMER'
    } <= excluded
    assert 'matplotlib' not in _option_values(cmd, '--collect-all')
    assert 'rdkit' not in _option_values(cmd, '--collect-all')


def test_no_charts_preserves_old_meaning_and_keeps_other_full_features(monkeypatch):
    _all_available(monkeypatch, unavailable={'matplotlib'})

    cmd, options = build_exe.build_command(['--no-charts'])

    assert options.lite is False
    assert options.no_charts is True
    excluded = set(_option_values(cmd, '--exclude-module'))
    collected = set(_option_values(cmd, '--collect-all'))
    assert 'matplotlib' in excluded
    assert 'numpy' not in excluded
    assert {'rdkit', 'docx', 'pypdf', 'reportlab'} <= collected
    assert _option_values(cmd, '--collect-binaries') == ['numpy']


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
    assert '.[packaging,gui,charts,mol,docs]' in error
    assert '--lite' in error
    assert '运行后不能再通过 pip' in error


def test_core_dependencies_are_required_even_for_lite_build(monkeypatch):
    _all_available(monkeypatch, unavailable={'paramiko'})

    with pytest.raises(build_exe.BuildConfigurationError, match='paramiko'):
        build_exe.build_command(['--lite'])


@pytest.mark.parametrize(('module', 'package'), [
    ('docx', 'python-docx'),
    ('pypdf', 'pypdf'),
    ('reportlab', 'reportlab'),
])
def test_full_build_requires_document_features(monkeypatch, module, package):
    _all_available(monkeypatch, unavailable={module})

    with pytest.raises(build_exe.BuildConfigurationError, match=package):
        build_exe.build_command([])


def test_install_hint_is_powershell_ready_for_windows_python_with_spaces(monkeypatch):
    monkeypatch.setattr(build_exe.os, 'name', 'nt')
    monkeypatch.setattr(build_exe.sys, 'executable', r'C:\Python Envs\vcstudio\python.exe')

    hint = build_exe._install_hint(build_exe.BuildOptions())

    assert hint == (
        r'& "C:\Python Envs\vcstudio\python.exe" -m pip install -e '
        r'".[packaging,gui,charts,mol,docs]"')


def test_no_charts_install_hint_keeps_numeric_and_document_features(monkeypatch):
    monkeypatch.setattr(build_exe.os, 'name', 'posix')
    monkeypatch.setattr(build_exe.sys, 'executable', '/opt/vcstudio/python')

    hint = build_exe._install_hint(build_exe.BuildOptions(no_charts=True))

    assert hint.endswith('".[packaging,gui,numeric,mol,docs]"')


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
    ['--full', '--no-charts'],
    ['--lite', '--with-decimer'],
    ['--web', '--legacy'],
    ['--unknown'],
])
def test_conflicting_or_unknown_options_are_rejected(monkeypatch, args):
    _all_available(monkeypatch)

    with pytest.raises(build_exe.BuildConfigurationError):
        build_exe.build_command(args)
