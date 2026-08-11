from __future__ import annotations

import json

import pytest

from vcstudio.project.lab_policies import (
    LabPolicyError,
    LabPolicySelectionStore,
    STORE_SCHEMA,
)


def _store(tmp_path):
    return LabPolicySelectionStore(
        tmp_path / "selection.json",
        clock=lambda: "2026-08-11T02:03:04Z",
    )


def test_confirm_is_atomic_revisioned_and_recommendation_only(tmp_path):
    store = _store(tmp_path)
    initial = store.read()
    confirmed = store.confirm(
        "surface-production", {"cores": 48, "walltime": "48:00:00"},
        applicability="slab", actor="operator-42", confirmed=True,
        expected_revision=0,
    )

    assert initial == {
        "ok": True, "conflict": False, "revision": 0,
        "selection": None, "error": None,
    }
    assert confirmed["ok"] is True and confirmed["revision"] == 1
    selected = confirmed["selection"]
    assert selected["policy_id"] == "surface-production"
    assert selected["policy_version"] == 1
    assert selected["overrides"] == {"cores": 48, "walltime": "48:00:00"}
    assert selected["applicability"] == "slab"
    assert selected["actor"] == "operator-42"
    assert selected["confirmed_at"] == "2026-08-11T02:03:04Z"
    assert selected["recommendation_only"] is True
    assert selected["requires_user_confirmation"] is True
    assert selected["authorizes_submission"] is False
    assert len(selected["semantic_sha256"]) == 64
    raw = json.loads((tmp_path / "selection.json").read_text(encoding="utf-8"))
    assert raw["schema"] == STORE_SCHEMA and raw["revision"] == 1


def test_stale_confirm_returns_authoritative_snapshot_without_overwrite(tmp_path):
    first = _store(tmp_path)
    second = _store(tmp_path)
    saved = first.confirm(
        "screening-pilot", applicability="molecule", actor="operator-a",
        confirmed=True, expected_revision=0)
    conflict = second.confirm(
        "reproducible-periodic-baseline", applicability="bulk",
        actor="operator-b", confirmed=True, expected_revision=0)

    assert saved["revision"] == 1
    assert conflict["ok"] is False and conflict["conflict"] is True
    assert conflict["revision"] == 1
    assert conflict["selection"] == saved["selection"]
    assert first.read()["selection"] == saved["selection"]


@pytest.mark.parametrize("kwargs,match", [
    ({"confirmed": False}, "confirmed=true"),
    ({"actor": r"C:\\private\\operator"}, "actor"),
    ({"actor": "token=private"}, "actor"),
    ({"actor": "ghp_abcdefghijk"}, "actor"),
    ({"expected_revision": True}, "expected_revision"),
])
def test_confirm_rejects_missing_confirmation_paths_secrets_and_bad_revision(
        tmp_path, kwargs, match):
    request = {
        "policy_id": "screening-pilot", "applicability": "bulk",
        "actor": "operator", "confirmed": True, "expected_revision": 0,
    }
    request.update(kwargs)
    policy_id = request.pop("policy_id")
    with pytest.raises(LabPolicyError, match=match):
        _store(tmp_path).confirm(policy_id, **request)


def test_store_rejects_unknown_or_tampered_persisted_fields(tmp_path):
    path = tmp_path / "selection.json"
    path.write_text(json.dumps({
        "schema": STORE_SCHEMA, "revision": 1, "selection": None,
        "project_path": r"C:\\private",
    }), encoding="utf-8")
    with pytest.raises(LabPolicyError, match="unknown fields"):
        LabPolicySelectionStore(path).read()
