from __future__ import annotations

from vcstudio.gui_web.api import Api
from vcstudio.project.lab_policies import LabPolicySelectionStore


def _api(tmp_path):
    return Api(lab_policy_store=LabPolicySelectionStore(
        tmp_path / "policy.json",
        clock=lambda: "2026-08-11T08:00:00Z",
    ))


def test_catalog_preview_and_confirm_are_advisory_and_revisioned(tmp_path):
    api = _api(tmp_path)
    catalog = api.lab_policy_catalog()
    assert catalog["ok"] is True
    assert catalog["recommendation_only"] is True
    assert catalog["authorizes_submission"] is False

    request = {
        "policy_id": "surface-production",
        "applicability": "slab",
        "overrides": {"cores": 48, "walltime": "48:00:00"},
    }
    preview = api.lab_policy_preview(request)
    assert preview["ok"] is True and preview["revision"] == 0
    assert preview["preview"]["resources"] == {
        "cores": 48, "walltime": "48:00:00",
    }
    assert api.lab_policy_read()["selection"] is None

    confirmed = api.lab_policy_confirm(
        request, 0, True)
    assert confirmed["ok"] is True and confirmed["revision"] == 1
    assert confirmed["selection"]["actor"] == "manual-local-user"
    assert confirmed["actor_attribution_only"] is True
    assert confirmed["selection"]["authorizes_submission"] is False


def test_confirm_requires_explicit_true_and_returns_authoritative_conflict(tmp_path):
    api = _api(tmp_path)
    request = {
        "policy_id": "screening-pilot", "applicability": "bulk",
        "overrides": {},
    }
    rejected = api.lab_policy_confirm(request, 0, False)
    assert rejected["ok"] is False
    assert "confirmed=true" in rejected["error"]

    saved = api.lab_policy_confirm(request, 0, True)
    conflict = api.lab_policy_confirm(
        {**request, "policy_id": "reproducible-periodic-baseline"}, 0, True)
    assert conflict["ok"] is False and conflict["conflict"] is True
    assert conflict["revision"] == 1
    assert conflict["selection"] == saved["selection"]


def test_policy_api_rejects_unknown_path_and_secret_fields_without_echo(tmp_path):
    api = _api(tmp_path)
    for request in (
        {"policy_id": "screening-pilot", "project_path": r"C:\\private"},
        {"policy_id": "screening-pilot", "overrides": {"password": "needle"}},
    ):
        result = api.lab_policy_preview(request)
        assert result["ok"] is False
        assert r"C:\\private" not in str(result)
        assert "needle" not in str(result)

    forged = api.lab_policy_confirm({
        "policy_id": "screening-pilot", "applicability": "bulk",
        "overrides": {}, "actor": "forged-browser-user",
    }, 0, True)
    assert forged["ok"] is False
    assert "unknown fields" in forged["error"]
    assert "forged-browser-user" not in str(forged)
