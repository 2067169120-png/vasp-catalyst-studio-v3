"""Deterministic, advisory-only resource forecasts for scientific jobs.

The estimator combines the existing analytical campaign estimate with completed local jobs of the
same task kind.  It never submits a job, mutates a manifest, or treats a forecast as scientific
evidence.  Callers must show the uncertainty range and require an explicit user confirmation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections.abc import Iterable, Mapping
from typing import Any

from vcstudio.campaign.budget import estimate_job


SCHEMA = "vcstudio.resource-forecast/v1"
_SUCCESS_STATES = {"DONE", "COMPLETED", "FINISHED", "SUCCESS"}
_FAILURE_STATES = {"FAILED", "ERROR", "CANCELLED", "TIMEOUT", "LOST"}
_TASK_KINDS = {"static", "relax", "neb", "aimd", "analysis", "freq"}
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


class ResourceForecastError(ValueError):
    """The forecast request is malformed or outside the bounded model contract."""


def _bounded_int(value: Any, *, field: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ResourceForecastError(f"{field} must be an integer") from exc
    if not low <= number <= high:
        raise ResourceForecastError(f"{field} must be between {low} and {high}")
    return number


def _finite(value: Any, *, field: str, low: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ResourceForecastError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number < low:
        raise ResourceForecastError(f"{field} must be finite and at least {low:g}")
    return number


def normalize_request(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResourceForecastError("request must be an object")
    task_kind = str(value.get("task_kind") or "").strip().lower()
    if task_kind not in _TASK_KINDS:
        raise ResourceForecastError("task_kind is unsupported")
    return {
        "task_kind": task_kind,
        "natoms": _bounded_int(value.get("natoms"), field="natoms", low=1, high=100_000),
        "nkpts": _bounded_int(value.get("nkpts", 1), field="nkpts", low=1, high=10_000_000),
        "cores": _bounded_int(value.get("cores", 1), field="cores", low=1, high=65_536),
    }


def _duration_seconds(record: Mapping[str, Any]) -> float | None:
    for key, multiplier in (
        ("elapsed_seconds", 1.0),
        ("walltime_seconds", 1.0),
        ("elapsed_hours", 3600.0),
    ):
        if record.get(key) in (None, ""):
            continue
        try:
            value = float(record[key]) * multiplier
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0.0 else None
    return None


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _history_projection(
    records: Iterable[Mapping[str, Any]], *, task_kind: str,
) -> tuple[list[float], int, int]:
    ratios: list[float] = []
    observed = 0
    failed = 0
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("task_kind") or "").strip().lower()
        if kind != task_kind:
            continue
        state = str(raw.get("state") or raw.get("status") or "").strip().upper()
        if state in _SUCCESS_STATES | _FAILURE_STATES:
            observed += 1
            failed += int(state in _FAILURE_STATES)
        if state not in _SUCCESS_STATES:
            continue
        seconds = _duration_seconds(raw)
        if seconds is None:
            continue
        try:
            request = normalize_request(raw)
        except ResourceForecastError:
            continue
        analytical = estimate_job(**request)
        if analytical <= 0.0:
            continue
        actual_core_hours = seconds * request["cores"] / 3600.0
        if actual_core_hours > 0.0:
            ratios.append(actual_core_hours / analytical)
    return ratios, observed, failed


def _semantic_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def forecast(
    request: Mapping[str, Any],
    history: Iterable[Mapping[str, Any]] = (),
    *,
    remaining_budget_core_hours: float | None = None,
) -> dict[str, Any]:
    """Return an auditable point/range estimate without granting submit authority."""
    normalized = normalize_request(request)
    if remaining_budget_core_hours is not None:
        remaining_budget_core_hours = _finite(
            remaining_budget_core_hours, field="remaining_budget_core_hours",
        )
    analytical = float(estimate_job(**normalized))
    ratios, observed, failed = _history_projection(
        history, task_kind=normalized["task_kind"],
    )

    if ratios:
        calibration = statistics.median(ratios)
        point = analytical * calibration
        if len(ratios) >= 3:
            low = analytical * _percentile(ratios, 0.25)
            high = analytical * _percentile(ratios, 0.75)
        else:
            low, high = point * 0.5, point * 2.0
    else:
        calibration = None
        point = analytical
        low, high = point * 0.25, point * 4.0

    point, low, high = (round(max(item, 0.0), 3) for item in (point, low, high))
    if low > high:
        low, high = high, low
    failure_rate = (failed / observed) if observed else None
    confidence = "high" if len(ratios) >= 10 else "medium" if len(ratios) >= 3 else "low"
    risk_flags: list[str] = []
    if not ratios:
        risk_flags.append("no_matching_history")
    elif len(ratios) < 3:
        risk_flags.append("sparse_history")
    if failure_rate is not None and failure_rate >= 0.25:
        risk_flags.append("high_recent_failure_rate")
    if point > 0 and high / point >= 2.0:
        risk_flags.append("wide_uncertainty")

    if remaining_budget_core_hours is None:
        budget_status = "unlimited_or_unknown"
    elif high > remaining_budget_core_hours:
        budget_status = "at_risk"
        risk_flags.append("budget_at_risk")
    else:
        budget_status = "within_range"

    semantic = {
        "schema": SCHEMA,
        "request": normalized,
        "estimate_core_hours": point,
        "range_core_hours": {"low": low, "high": high},
        "analytical_baseline_core_hours": round(analytical, 3),
        "calibration_factor": round(calibration, 6) if calibration is not None else None,
        "matching_successful_samples": len(ratios),
        "matching_observed_samples": observed,
        "failure_rate": round(failure_rate, 4) if failure_rate is not None else None,
        "confidence": confidence,
        "remaining_budget_core_hours": (
            round(remaining_budget_core_hours, 3)
            if remaining_budget_core_hours is not None else None
        ),
        "budget_status": budget_status,
        "risk_flags": risk_flags,
        "recommendation_only": True,
        "requires_user_confirmation": True,
        "authorizes_submission": False,
    }
    return {**semantic, "semantic_sha256": _semantic_hash(semantic)}


def forecast_batch(
    requests: Iterable[Mapping[str, Any]],
    history: Iterable[Mapping[str, Any]] = (),
    *,
    remaining_budget_core_hours: float | None = None,
) -> dict[str, Any]:
    """Forecast a bounded selection while preserving per-job uncertainty.

    ``job_id`` is the only caller label retained.  It must be an opaque identifier; names, paths,
    commands, hosts, and credentials are deliberately not part of this DTO.
    """
    raw_requests = list(requests)
    if not raw_requests or len(raw_requests) > 512:
        raise ResourceForecastError("requests must contain between 1 and 512 jobs")
    reusable_history = list(history)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_requests):
        if not isinstance(raw, Mapping):
            raise ResourceForecastError(f"requests[{index}] must be an object")
        job_id = str(raw.get("job_id") or "").strip()
        if not _SAFE_JOB_ID.fullmatch(job_id) or job_id in seen:
            raise ResourceForecastError("job_id must be a unique opaque identifier")
        seen.add(job_id)
        projected = forecast(raw, reusable_history)
        jobs.append({"job_id": job_id, **projected})
    jobs.sort(key=lambda item: item["job_id"])

    if remaining_budget_core_hours is not None:
        remaining_budget_core_hours = _finite(
            remaining_budget_core_hours, field="remaining_budget_core_hours",
        )
    estimate = round(sum(item["estimate_core_hours"] for item in jobs), 3)
    low = round(sum(item["range_core_hours"]["low"] for item in jobs), 3)
    high = round(sum(item["range_core_hours"]["high"] for item in jobs), 3)
    risks = sorted({risk for item in jobs for risk in item["risk_flags"]})
    if remaining_budget_core_hours is None:
        budget_status = "unlimited_or_unknown"
    elif high > remaining_budget_core_hours:
        budget_status = "at_risk"
        if "budget_at_risk" not in risks:
            risks.append("budget_at_risk")
    else:
        budget_status = "within_range"
    confidence = min(
        (item["confidence"] for item in jobs),
        key={"low": 0, "medium": 1, "high": 2}.__getitem__,
    )
    semantic = {
        "schema": SCHEMA,
        "kind": "batch",
        "jobs": jobs,
        "job_count": len(jobs),
        "estimate_core_hours": estimate,
        "range_core_hours": {"low": low, "high": high},
        "confidence": confidence,
        "remaining_budget_core_hours": (
            round(remaining_budget_core_hours, 3)
            if remaining_budget_core_hours is not None else None
        ),
        "budget_status": budget_status,
        "risk_flags": risks,
        "recommendation_only": True,
        "requires_user_confirmation": True,
        "authorizes_submission": False,
    }
    return {**semantic, "semantic_sha256": _semantic_hash(semantic)}


__all__ = [
    "SCHEMA", "ResourceForecastError", "forecast", "forecast_batch", "normalize_request",
]
