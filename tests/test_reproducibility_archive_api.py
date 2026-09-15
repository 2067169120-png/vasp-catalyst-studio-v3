from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from tests.test_reproducibility_archive import _published_revision
from vcstudio.gui_web.api import Api


def _api_for_revision(tmp_path: Path, service, project_id: str, *, clock=None) -> Api:
    api = Api.__new__(Api)
    api._dialog_fn = lambda kind: str(tmp_path / "archives") if kind == "dir" else None
    api._report_archive_destinations = None
    api._report_archive_confirmations = None
    api._report_archive_transactions = None
    api._report_archive_transaction_lock = threading.RLock()
    api._report_archive_clock = clock or __import__("time").monotonic
    api._archive_attachment_providers = ()
    api._report_service_instance = service
    api._reports = lambda: api._report_service_instance
    api._project_identity_lock = threading.RLock()
    api._project_identity_local = threading.local()
    project_uuid = project_id.removeprefix("project-")
    loaded_project = {
        "project_uuid": project_uuid,
        "name": "Archive API project",
    }
    api._adsorption = SimpleNamespace(
        load_project=lambda path: loaded_project if str(path) == "project.yaml" else None,
    )
    record = {
        "project_id": project_id,
        "request_project_id": project_id,
        "path": "project.yaml",
        "identity_fingerprint": api._project_identity_fingerprint(
            "project.yaml", loaded_project),
    }
    api._project_registry_snapshot = lambda: {
        "by_id": {project_id: [record]},
        "public_rows": [],
        "duplicate_ids": set(),
        "failures": [],
    }
    return api


def _operation(suffix: str = "a") -> str:
    return "archive-operation." + suffix * 24


def _destination_request(plan: dict, operation_key: str) -> dict:
    return Api._archive_plan_request_binding(plan, operation_key)


def _confirmation(selected: dict, operation_key: str) -> dict:
    return {
        **selected["confirmation"],
        "confirmed": True,
        "idempotency_key": operation_key,
        "destination_token": selected["destination_token"],
    }


def test_archive_api_dry_run_confirm_export_and_verify_use_only_opaque_tokens(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=True)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)

    operation_key = _operation()
    plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)

    assert plan["ok"] is True
    assert plan["status"] == "dry_run_ready"
    assert plan["project_id"] == host.project_id
    assert plan["revision"]["revision_id"] == revision_id
    assert plan["preview_token"].startswith("archive-preview.")
    assert plan["confirmation_token"] is None
    assert len(plan["rights_sha256"]) == 64
    assert len(plan["inventory_sha256"]) == 64
    assert plan["archive"]["name"].endswith("-v1.zip")
    assert plan["denominator"]["included"] > 0
    encoded_plan = json.dumps(plan, ensure_ascii=False)
    assert str(tmp_path) not in encoded_plan
    assert "project.yaml" not in encoded_plan

    wrong_request = _destination_request(plan, operation_key)
    wrong_request["revision"]["revision_id"] = "wrong-revision"
    wrong_revision = api.report_archive_pick_destination(host.project_id, wrong_request)
    assert wrong_revision["ok"] is False
    assert wrong_revision["destination_token"] is None

    selected = api.report_archive_pick_destination(
        host.project_id, _destination_request(plan, operation_key))

    assert selected["ok"] is True
    assert selected["destination_token"].startswith("archive-destination.")
    assert selected["destination_identity_sha256"]
    assert selected["confirmation"]["confirmation_token"].startswith(
        "archive-confirm.")
    assert selected["confirmation"]["destination_identity_sha256"] == (
        selected["destination_identity_sha256"])
    assert str(destination) not in json.dumps(selected, ensure_ascii=False)

    envelope = _confirmation(selected, operation_key)
    exported = api.report_archive_export(host.project_id, envelope)

    assert exported["ok"] is True
    assert exported["status"] == "verified_local_archive"
    assert exported["verification"]["status"] == "verified"
    assert exported["verification"]["checksums"] == "pass"
    assert exported["replayed"] is False
    assert exported["receipt"]["idempotency_key"] == operation_key
    assert exported["boundaries"] == {
        "local_only": True,
        "uploaded": False,
        "doi_requested": False,
        "doi_assigned": False,
        "published": False,
    }
    assert (destination / exported["file"]["name"]).is_file()
    encoded_result = json.dumps(exported, ensure_ascii=False)
    assert str(tmp_path) not in encoded_result
    assert "project.yaml" not in encoded_result

    replay = api.report_archive_export(host.project_id, envelope)
    assert replay["ok"] is True
    assert replay["replayed"] is True
    assert replay["file"] == exported["file"]
    assert str(tmp_path) not in json.dumps(replay, ensure_ascii=False)


def test_archive_api_rejects_browser_path_and_unknown_project_without_leaking(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    (tmp_path / "archives").mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    browser_path = str(tmp_path / "private-project.yaml")

    rejected_path = api.report_archive_dry_run(browser_path, revision_id, _operation("b"))
    rejected_project = api.report_archive_dry_run(
        "project-" + "b" * 32, revision_id, _operation("c"))

    for result in (rejected_path, rejected_project):
        assert result["ok"] is False
        assert result["preview_token"] is None
        assert result["decisions"] == []
        encoded = json.dumps(result, ensure_ascii=False)
        assert browser_path not in encoded
        assert str(tmp_path) not in encoded


def test_archive_api_stale_revision_after_plan_writes_no_final_file(tmp_path):
    host, service, revision_id, published = _published_revision(tmp_path, assets=False)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    operation_key = _operation("d")
    plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)
    selected = api.report_archive_pick_destination(
        host.project_id, _destination_request(plan, operation_key))
    Path(published["files"]["html"]).write_text("tampered", encoding="utf-8")

    result = api.report_archive_export(
        host.project_id, _confirmation(selected, operation_key))

    assert result["ok"] is False
    assert result["status"] == "stale"
    assert result["file"] is None
    assert not list(destination.glob("*.zip"))


def test_archive_api_rejects_destination_rename_and_replacement(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    destination = tmp_path / "archives"
    moved = tmp_path / "archives-moved"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    operation_key = _operation("e")
    plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)
    selected = api.report_archive_pick_destination(
        host.project_id, _destination_request(plan, operation_key))
    destination.rename(moved)
    destination.mkdir()

    result = api.report_archive_export(
        host.project_id, _confirmation(selected, operation_key))

    assert result["ok"] is False
    assert result["file"] is None
    assert not list(destination.iterdir())
    assert not list(moved.iterdir())
    encoded = json.dumps(result, ensure_ascii=False)
    assert str(destination) not in encoded
    assert str(moved) not in encoded


def test_archive_api_failure_dto_uses_archive_credential_scanner():
    secret = "client_secret=" + ("c" * 28)

    result = Api._archive_failure(
        "vcstudio.vcs-archive-result/v1", ValueError(f"provider rejected {secret}"),
        revision=None, file=None,
    )

    assert result["ok"] is False
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert "redacted" in result["error"]


def test_archive_api_direct_call_bypass_and_old_positional_api_fail_closed(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    operation_key = _operation("f")
    plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)
    selected = api.report_archive_pick_destination(
        host.project_id, _destination_request(plan, operation_key))

    missing_human_confirmation = {
        **selected["confirmation"],
        "idempotency_key": operation_key,
        "destination_token": selected["destination_token"],
    }
    bypass = api.report_archive_export(host.project_id, missing_human_confirmation)
    old_destination = api.report_archive_pick_destination(
        host.project_id, revision_id, plan["preview_token"])
    old_export = api.report_archive_export(
        host.project_id, revision_id, plan["preview_token"],
        selected["destination_token"])

    for result in (bypass, old_destination, old_export):
        assert result["ok"] is False
        assert result["status"] == "blocked"
    assert not list(destination.glob("*.zip"))


def test_archive_api_expiry_and_wrong_project_envelope_write_nothing(tmp_path):
    now = [1000.0]
    host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(
        tmp_path, service, host.project_id, clock=lambda: now[0])
    operation_key = _operation("g")
    plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)
    selected = api.report_archive_pick_destination(
        host.project_id, _destination_request(plan, operation_key))
    envelope = _confirmation(selected, operation_key)

    wrong_project = api.report_archive_export("project-" + "9" * 32, envelope)
    assert wrong_project["ok"] is False
    assert not list(destination.glob("*.zip"))

    now[0] += float(selected["confirmation"]["ttl_seconds"]) + 1
    expired = api.report_archive_export(host.project_id, envelope)
    assert expired["ok"] is False
    assert expired["status"] == "blocked"
    assert not list(destination.glob("*.zip"))


def test_archive_api_same_key_different_payload_rejected_and_exact_replay_cached(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    operation_key = _operation("h")
    first_plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)
    replay_plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)
    assert replay_plan["ok"] is True
    assert replay_plan["replayed"] is True
    assert replay_plan["preview_token"] == first_plan["preview_token"]

    different_plan = api.report_archive_dry_run(
        host.project_id, "different-revision", operation_key)
    assert different_plan["ok"] is False

    selected = api.report_archive_pick_destination(
        host.project_id, _destination_request(first_plan, operation_key))
    envelope = _confirmation(selected, operation_key)
    changed = {**envelope, "destination_token": "archive-destination.changed"}
    rejected = api.report_archive_export(host.project_id, changed)
    assert rejected["ok"] is False
    assert not list(destination.glob("*.zip"))

    exported = api.report_archive_export(host.project_id, envelope)
    replay = api.report_archive_export(host.project_id, envelope)
    assert exported["ok"] is True
    assert replay["ok"] is True
    assert replay["replayed"] is True
    assert len(list(destination.glob("*.zip"))) == 1


def test_archive_api_concurrent_exact_confirmation_exports_once_and_replays(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    operation_key = _operation("i")
    plan = api.report_archive_dry_run(host.project_id, revision_id, operation_key)
    selected = api.report_archive_pick_destination(
        host.project_id, _destination_request(plan, operation_key))
    envelope = _confirmation(selected, operation_key)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(api.report_archive_export, host.project_id, envelope)
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert all(result["ok"] is True for result in results)
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert results[0]["file"] == results[1]["file"]
    assert len(list(destination.glob("*.zip"))) == 1


def test_archive_api_all_dtos_recursively_redact_paths_and_credentials():
    payload = {
        "ok": True,
        "preview_token": "archive-preview.opaque-value",
        "nested": {
            "project_path": r"C:\private\project.yaml",
            "message": r"C:\private\report password=bridge-secret",
            "credential": "bridge-secret",
        },
        "items": [{"archive_path": "reports/report.html", "secret": "hidden"}],
    }

    public = Api._archive_public_dto(payload)
    encoded = json.dumps(public, ensure_ascii=False)

    assert public["preview_token"] == "archive-preview.opaque-value"
    assert "project_path" not in public["nested"]
    assert "credential" not in public["nested"]
    assert public["items"][0]["archive_path"] == "reports/report.html"
    assert "secret" not in public["items"][0]
    assert "private" not in encoded
    assert "bridge-secret" not in encoded
