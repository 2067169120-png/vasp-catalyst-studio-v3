"""Regression tests for CI dependency, package, and release provenance gates."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / '.github' / 'workflows' / 'ci.yml'
RELEASE = ROOT / '.github' / 'workflows' / 'release.yml'


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def test_workflow_files_are_valid_yaml_mappings():
    for path in (CI, RELEASE):
        parsed = yaml.safe_load(_read(path))
        assert isinstance(parsed, dict)
        assert parsed.get('jobs')


def test_job_environment_uses_contexts_allowed_by_github_actions():
    """Job-level ``env`` cannot access the runtime-only ``runner`` context."""
    workspace_cache_values = []
    for path in (CI, RELEASE):
        jobs = yaml.safe_load(_read(path))['jobs']
        for job in jobs.values():
            environment = job.get('env') or {}
            assert all('runner.' not in str(value) for value in environment.values())
            cache = environment.get('MPLCONFIGDIR')
            if cache:
                workspace_cache_values.append(cache)

    assert len(workspace_cache_values) == 4
    assert all('${{ github.workspace }}' in value
               for value in workspace_cache_values)


def test_full_matrix_and_first_party_javascript_gate_are_explicit():
    text = _read(CI)

    assert 'python-version: ["3.10", "3.11", "3.12"]' in text
    assert 'os: [ubuntu-latest, windows-latest]' in text
    assert '.[dev,gui,charts,mol,docs]' in text
    assert 'PYTHONUTF8: "1"' in text
    assert 'MPLCONFIGDIR:' in text
    assert "git ls-files -z" in text and 'node --check "$file"' in text
    assert ':(exclude,glob)vcstudio/gui_web/assets/vendor/**' in text


def test_package_smoke_builds_and_executes_the_windowed_artifact():
    text = _read(CI)
    package = text[text.index('  package-smoke:'):]

    assert 'packaging/build_exe.py --full' in package
    assert "Resolve-Path -LiteralPath 'dist/VASP Catalyst Studio.exe'" in package
    assert 'Start-Process -FilePath $exe -WindowStyle Hidden -PassThru' in package
    assert 'Wait-Process -Timeout 300' in package
    assert 'Stop-Process -Id $process.Id -Force' in package
    assert "'--healthcheck'" in package
    assert "Invoke-FrozenHealthcheck 'full'" in package
    assert "Invoke-FrozenHealthcheck 'journey'" in package
    assert 'frozen-healthcheck-full.json' in package
    assert 'frozen-healthcheck-journey.json' in package
    assert "'bridge-journey'" in package
    assert "'web-assets'" in package and "'locales'" in package
    assert 'actions/upload-artifact@v4' in package
    assert 'dist/VASP Catalyst Studio.exe' in package
    assert 'Import entry points' not in package


def test_release_validates_identity_and_provenance_before_upload():
    text = _read(RELEASE)
    gate = text.index('Verify tag identity and integration-branch provenance')
    upload = text.index('Upload to Release')

    assert gate < upload
    assert 'fetch-depth: 0' in text
    assert 'import vcstudio; print(vcstudio.__version__)' in text
    assert '$env:RELEASE_TAG -cne $expectedTag' in text
    assert 'git merge-base --is-ancestor' in text
    assert "--jq '.protected'" in text
    assert 'DEFAULT_BRANCH:' in text
    assert 'Run the frozen executable healthcheck' in text[gate:upload]
    assert "'numeric-and-charts', 'molecule-and-documents'" in text[gate:upload]
    assert "Invoke-FrozenHealthcheck 'full'" in text[gate:upload]
    assert "Invoke-FrozenHealthcheck 'journey'" in text[gate:upload]
    assert 'Wait-Process -Timeout 300' in text[gate:upload]
    assert "'bridge-journey'" in text[gate:upload]
