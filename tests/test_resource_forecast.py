from __future__ import annotations

import copy

import pytest

from vcstudio.project import resource_forecast


def _record(*, elapsed_hours: float, state: str = "DONE", task_kind: str = "relax"):
    return {
        "task_kind": task_kind,
        "natoms": 40,
        "nkpts": 4,
        "cores": 32,
        "elapsed_hours": elapsed_hours,
        "state": state,
        "path": "C:/must/not/escape",
        "password": "must-not-escape",
    }


def test_forecast_is_deterministic_path_free_and_never_authorizes_submission():
    history = [_record(elapsed_hours=2.0), _record(elapsed_hours=3.0)]
    request = {"task_kind": "relax", "natoms": 50, "nkpts": 6, "cores": 64}
    left = resource_forecast.forecast(request, history)
    right = resource_forecast.forecast(copy.deepcopy(request), reversed(history))
    assert left == right
    assert left["recommendation_only"] is True
    assert left["requires_user_confirmation"] is True
    assert left["authorizes_submission"] is False
    assert "C:/" not in str(left) and "must-not-escape" not in str(left)


def test_forecast_uses_same_kind_successes_and_reports_failures_separately():
    history = [
        _record(elapsed_hours=1.0),
        _record(elapsed_hours=2.0),
        _record(elapsed_hours=3.0),
        _record(elapsed_hours=0.0, state="FAILED"),
        _record(elapsed_hours=100.0, task_kind="static"),
    ]
    out = resource_forecast.forecast(
        {"task_kind": "relax", "natoms": 50, "nkpts": 4, "cores": 32}, history,
    )
    assert out["matching_successful_samples"] == 3
    assert out["matching_observed_samples"] == 4
    assert out["failure_rate"] == 0.25
    assert out["confidence"] == "medium"
    assert "high_recent_failure_rate" in out["risk_flags"]
    assert out["range_core_hours"]["low"] <= out["estimate_core_hours"]
    assert out["range_core_hours"]["high"] >= out["estimate_core_hours"]


def test_no_history_is_honest_low_confidence_and_budget_uses_high_bound():
    out = resource_forecast.forecast(
        {"task_kind": "neb", "natoms": 80, "nkpts": 2, "cores": 64},
        remaining_budget_core_hours=0,
    )
    assert out["confidence"] == "low"
    assert "no_matching_history" in out["risk_flags"]
    assert out["budget_status"] == "at_risk"
    assert "budget_at_risk" in out["risk_flags"]


@pytest.mark.parametrize(
    "forecast_request",
    [
        {},
        {"task_kind": "unknown", "natoms": 1, "nkpts": 1, "cores": 1},
        {"task_kind": "relax", "natoms": 0, "nkpts": 1, "cores": 1},
        {"task_kind": "relax", "natoms": 1, "nkpts": 0, "cores": 1},
        {"task_kind": "relax", "natoms": 1, "nkpts": 1, "cores": 0},
    ],
)
def test_invalid_requests_fail_closed(forecast_request):
    with pytest.raises(resource_forecast.ResourceForecastError):
        resource_forecast.forecast(forecast_request)


def test_nonfinite_or_negative_budget_fails_closed():
    request = {"task_kind": "static", "natoms": 10, "nkpts": 1, "cores": 4}
    with pytest.raises(resource_forecast.ResourceForecastError):
        resource_forecast.forecast(request, remaining_budget_core_hours=float("nan"))
    with pytest.raises(resource_forecast.ResourceForecastError):
        resource_forecast.forecast(request, remaining_budget_core_hours=-1)


def test_batch_forecast_is_sorted_aggregated_and_budgeted_on_high_bound():
    requests = [
        {"job_id": "job-b", "task_kind": "static", "natoms": 20,
         "nkpts": 2, "cores": 8},
        {"job_id": "job-a", "task_kind": "relax", "natoms": 30,
         "nkpts": 3, "cores": 16},
    ]
    out = resource_forecast.forecast_batch(
        requests, [_record(elapsed_hours=2.0)], remaining_budget_core_hours=0,
    )
    assert [item["job_id"] for item in out["jobs"]] == ["job-a", "job-b"]
    assert out["job_count"] == 2
    assert out["estimate_core_hours"] == round(sum(
        item["estimate_core_hours"] for item in out["jobs"]), 3)
    assert out["range_core_hours"]["high"] == round(sum(
        item["range_core_hours"]["high"] for item in out["jobs"]), 3)
    assert out["budget_status"] == "at_risk"
    assert out["authorizes_submission"] is False


@pytest.mark.parametrize(
    "requests",
    [
        [],
        [{"job_id": "bad/path", "task_kind": "static", "natoms": 1,
          "nkpts": 1, "cores": 1}],
        [
            {"job_id": "same", "task_kind": "static", "natoms": 1,
             "nkpts": 1, "cores": 1},
            {"job_id": "same", "task_kind": "static", "natoms": 1,
             "nkpts": 1, "cores": 1},
        ],
    ],
)
def test_batch_forecast_rejects_empty_unsafe_or_duplicate_job_ids(requests):
    with pytest.raises(resource_forecast.ResourceForecastError):
        resource_forecast.forecast_batch(requests)
