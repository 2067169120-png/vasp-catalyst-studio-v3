from __future__ import annotations

import json
from pathlib import Path

from vcstudio.gui_web.api import Api


class _Ledger:
    def __init__(self, entries):
        self.entries = entries

    def load_all(self):
        return list(self.entries)


def _write_inputs(path: Path, *, natoms=2, mesh=(2, 2, 1)):
    path.mkdir()
    coordinates = "\n".join(f"0 0 {index / max(natoms, 1):.6f}" for index in range(natoms))
    (path / "POSCAR").write_text(
        "system\n1\n1 0 0\n0 1 0\n0 0 1\nSi\n"
        f"{natoms}\nDirect\n{coordinates}\n",
        encoding="utf-8",
    )
    (path / "KPOINTS").write_text(
        f"mesh\n0\nGamma\n{mesh[0]} {mesh[1]} {mesh[2]}\n0 0 0\n",
        encoding="utf-8",
    )


def _manifest(job_id, *, cores=8, task="relax", state="DONE"):
    return {
        "job_id": job_id,
        "task_type": task,
        "state": state,
        "state_history": [
            {"state": "SUBMITTED", "at": "2026-08-11T00:00:00+08:00"},
            {"state": "RUNNING", "at": "2026-08-11T00:10:00+08:00"},
            {"state": state, "at": "2026-08-11T01:10:00+08:00"},
        ],
        "attempts": ([] if cores is None else [{"cores": cores}]),
        "inputs": {},
    }


def _assert_no_path_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert key.lower() not in {"path", "dir", "directory", "root"}
            _assert_no_path_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_path_keys(item)


def test_forecast_resolves_ids_and_measurements_from_server_ledger(tmp_path):
    ready = tmp_path / "ready"
    unknown = tmp_path / "unknown"
    _write_inputs(ready, natoms=4, mesh=(3, 3, 1))
    _write_inputs(unknown, natoms=2, mesh=(1, 1, 1))
    api = Api(ledger_mod=_Ledger([
        (str(ready), _manifest("opaque-ready", cores=16)),
        (str(unknown), _manifest("opaque-unknown", cores=None)),
    ]))

    result = api.jobs_resource_forecast(["opaque-ready", "opaque-unknown"])

    assert result["ok"] is True and result["status"] == "ready"
    assert result["denominators"] == {
        "selected_jobs": 2, "forecastable_jobs": 1, "unknown_jobs": 1,
        "ledger_entries": 2, "usable_history_entries": 1,
        "unknown_history_entries": 1,
    }
    assert result["unknown_jobs"] == [
        {"job_id": "opaque-unknown", "missing": ["cores"]},
    ]
    forecast = result["forecast"]
    assert forecast["job_count"] == 1
    assert forecast["jobs"][0]["request"] == {
        "task_kind": "relax", "natoms": 4, "nkpts": 9, "cores": 16,
    }
    assert forecast["recommendation_only"] is True
    assert forecast["authorizes_submission"] is False
    assert result["budget_evidence_status"] == "unknown"
    assert len(result["history_basis_sha256"]) == 64
    assert len(result["semantic_sha256"]) == 64
    assert str(tmp_path) not in json.dumps(result)
    _assert_no_path_keys(result)


def test_history_basis_hash_distinguishes_different_actual_records(tmp_path):
    job = tmp_path / "job"
    _write_inputs(job)
    manifest = _manifest("opaque-id", cores=8)
    first = Api(ledger_mod=_Ledger([(str(job), manifest)])).jobs_resource_forecast(
        ["opaque-id"])
    changed = dict(manifest)
    changed["state_history"] = [dict(item) for item in manifest["state_history"]]
    changed["state_history"][-1]["at"] = "2026-08-11T02:10:00+08:00"
    second = Api(ledger_mod=_Ledger([(str(job), changed)])).jobs_resource_forecast(
        ["opaque-id"])

    assert first["history_basis_sha256"] != second["history_basis_sha256"]
    assert first["semantic_sha256"] != second["semantic_sha256"]


def test_forecast_reports_budget_risk_only_from_consistent_manifest_evidence(tmp_path):
    jobs = []
    for index in range(2):
        path = tmp_path / f"job-{index}"
        _write_inputs(path, natoms=100, mesh=(8, 8, 8))
        manifest = _manifest(f"job-{index}", cores=128, task="static")
        manifest["remaining_budget_core_hours"] = 0.1
        jobs.append((str(path), manifest))
    result = Api(ledger_mod=_Ledger(jobs)).jobs_resource_forecast(["job-0", "job-1"])
    assert result["ok"] is True and result["status"] == "at_risk"
    assert result["budget_evidence_status"] == "available"
    assert result["forecast"]["budget_status"] == "at_risk"
    assert "budget_at_risk" in result["forecast"]["risk_flags"]


def test_forecast_rejects_browser_paths_and_never_accepts_browser_numbers(tmp_path):
    job = tmp_path / "job"
    _write_inputs(job)
    api = Api(ledger_mod=_Ledger([(str(job), _manifest("opaque-id"))]))
    for request in (
        [str(job)],
        [{"job_id": "opaque-id", "natoms": 1, "cores": 1}],
    ):
        result = api.jobs_resource_forecast(request)
        assert result["ok"] is False
        assert result["forecast"] is None
        assert str(job) not in str(result)


def test_existing_unsupported_kpoints_and_malformed_poscar_fail_closed(tmp_path):
    line_mode = tmp_path / "line-mode"
    _write_inputs(line_mode)
    (line_mode / "KPOINTS").write_text(
        "bands\n20\nLine-mode\nReciprocal\n0 0 0 1\n",
        encoding="utf-8",
    )
    line_manifest = _manifest("line-mode", cores=8, task="bands")
    line_manifest["inputs"] = {"kpoints": [9, 9, 9]}

    malformed = tmp_path / "malformed"
    _write_inputs(malformed)
    (malformed / "POSCAR").write_text(
        "bad\n1\n1 0 0\n0 1 0\n0 0 1\nSi\nnot-counts\nDirect\n1 1 1\n",
        encoding="utf-8",
    )
    malformed_manifest = _manifest("malformed", cores=8)
    api = Api(ledger_mod=_Ledger([
        (str(line_mode), line_manifest),
        (str(malformed), malformed_manifest),
    ]))

    result = api.jobs_resource_forecast(["line-mode", "malformed"])
    assert result["status"] == "unavailable"
    assert result["forecast"] is None
    assert result["unknown_jobs"] == [
        {"job_id": "line-mode", "missing": ["nkpts"]},
        {"job_id": "malformed", "missing": ["natoms"]},
    ]


def test_submitted_to_terminal_fallback_is_not_treated_as_actual_runtime(tmp_path):
    job = tmp_path / "job"
    _write_inputs(job)
    manifest = _manifest("opaque-id", cores=8)
    manifest["state_history"] = [
        {"state": "SUBMITTED", "at": "2026-08-11T00:00:00+08:00"},
        {"state": "DONE", "at": "2026-08-11T01:00:00+08:00"},
    ]
    result = Api(ledger_mod=_Ledger([(str(job), manifest)])).jobs_resource_forecast(
        ["opaque-id"])

    assert result["denominators"]["usable_history_entries"] == 0
    assert result["history_missing_reasons"]["actual_walltime"] == 1
