"""Executable contracts for the isolated frozen bridge persistence journey."""
from __future__ import annotations

import os
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from vcstudio.gui_web import frozen_healthcheck


ROOT = Path(__file__).resolve().parents[1]
def _content(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _phase(report: dict, name: str) -> dict:
    return next(item for item in report['phases'] if item['name'] == name)


def test_real_bridge_journey_is_offline_isolated_and_persistent(tmp_path):
    journey_root = tmp_path / 'journey'
    real_profile = Path(
        os.environ.get('APPDATA') or Path.home() / '.config'
    ) / 'vcstudio'
    external_files = [
        real_profile / 'config.yaml',
        real_profile / 'projects.json',
        real_profile / 'jobs.json',
        ROOT / 'config.yaml',
        ROOT / 'projects.json',
        ROOT / 'jobs.json',
    ]
    if os.environ.get('VCSTUDIO_CONFIG'):
        external_files.append(Path(os.environ['VCSTUDIO_CONFIG']))
    external_before = {path: _content(path) for path in external_files}

    # A fresh Python process bypasses pytest's path-isolation monkeypatches and
    # intentionally executes the production Api and ReportService.  The hard timeout
    # also ensures a deadlocked bridge journey cannot hang the packaging gate.
    script = (
        'import json,sys; from pathlib import Path; '
        'from vcstudio.gui_web.frozen_healthcheck import run_bridge_journey; '
        'result=run_bridge_journey(journey_root=Path(sys.argv[1])); '
        'print(json.dumps(result, ensure_ascii=False)); '
        'raise SystemExit(0 if result["ok"] else 1)'
    )
    environment = dict(os.environ)
    environment['PYTHONUTF8'] = '1'
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    completed = subprocess.run(
        [sys.executable, '-X', 'utf8', '-c', script, str(journey_root)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)

    assert report['schema'] == frozen_healthcheck.JOURNEY_SCHEMA
    assert report['ok'] is True, report
    assert report['phase_count'] == 10
    assert [item['name'] for item in report['phases']] == [
        'isolation',
        'bridge-start',
        'offline-fixture-register',
        'jobs-visible',
        'analysis-bootstrap-preview',
        'diagnostic-report-publish',
        'history-status-current',
        'bridge-restart',
        'restart-persistence',
        'offline-boundary',
    ]
    assert {item['status'] for item in report['phases']} == {'passed'}

    started = _phase(report, 'bridge-start')['detail']
    restarted = _phase(report, 'bridge-restart')['detail']
    assert started['api_class'] == 'vcstudio.gui_web.api.Api'
    assert started['report_service_class'] == 'vcstudio.project.report_service.ReportService'
    assert restarted['api_class'] == started['api_class']
    assert restarted['report_service_class'] == started['report_service_class']
    assert restarted['api_recreated'] is True
    assert restarted['report_service_recreated'] is True

    fixture = _phase(report, 'offline-fixture-register')['detail']
    analysis = _phase(report, 'analysis-bootstrap-preview')['detail']
    published = _phase(report, 'diagnostic-report-publish')['detail']
    persisted = _phase(report, 'restart-persistence')['detail']
    assert fixture['registered_jobs'] == 2
    assert fixture['job_states'] == ['CREATED']
    assert fixture['scientific_evidence'] == 'incomplete'
    assert analysis['acceptance_state'] == 'blocked'
    assert analysis['scientific_status'] == 'unverified'
    assert analysis['denominator']['numeric_configurations'] == 0
    assert analysis['denominator']['missing_configurations'] == 1
    assert published['scientific_status'] == 'diagnostic'
    assert published['publication_gate_status'] == 'blocked'
    assert published['html_available'] is True
    assert persisted == {
        'project_persisted': True,
        'jobs_persisted': 2,
        'history_persisted': True,
        'status_current': True,
        'scientific_status': 'diagnostic',
        'publication_gate_status': 'blocked',
    }
    assert report['network_attempts'] == []
    assert _phase(report, 'offline-boundary')['detail'] == {
        'connection_attempts': 0,
        'cluster_operations': 0,
    }

    # Persistence is real and inspectable, but every mutable file belongs to the
    # supplied temporary root rather than the checkout or the user's actual profile.
    assert (journey_root / 'isolation' / 'appdata' / 'vcstudio' / 'projects.json').is_file()
    assert (journey_root / 'isolation' / 'appdata' / 'vcstudio' / 'jobs.json').is_file()
    assert list((journey_root / 'isolation' / 'reports').rglob('*.html'))
    assert {path: _content(path) for path in external_files} == external_before


def test_network_guard_rejects_and_records_socket_connections():
    with frozen_healthcheck._offline_network_guard() as attempts:
        with socket.socket() as client, pytest.raises(
                RuntimeError, match='network access is forbidden'):
            client.connect(('127.0.0.1', 9))

    assert attempts == ["('127.0.0.1', 9)"]


def test_journey_profile_is_a_mandatory_healthcheck_result(monkeypatch):
    expected = {
        'schema': frozen_healthcheck.JOURNEY_SCHEMA,
        'ok': False,
        'phases': [{'name': 'bridge-start', 'status': 'failed'}],
        'phase_count': 1,
    }
    monkeypatch.setattr(frozen_healthcheck, 'run_bridge_journey', lambda: expected)

    report = frozen_healthcheck.run_healthcheck(profile='journey', require_frozen=False)

    assert report['profile'] == 'journey'
    assert report['ok'] is False
    assert report['checks']['bridge-journey'] == {'ok': False, 'detail': expected}
