from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from tests.test_reproducibility_archive import _published_revision
from vcstudio.gui_web.api import Api


def _api_for_revision(tmp_path: Path, service, project_id: str) -> Api:
    api = Api.__new__(Api)
    api._dialog_fn = lambda kind: str(tmp_path / "archives") if kind == "dir" else None
    api._report_archive_destinations = None
    api._report_archive_confirmations = None
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


def test_archive_api_dry_run_confirm_export_and_verify_use_only_opaque_tokens(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=True)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)

    plan = api.report_archive_dry_run(host.project_id, revision_id)

    assert plan["ok"] is True
    assert plan["status"] == "dry_run_ready"
    assert plan["project_id"] == host.project_id
    assert plan["revision"]["revision_id"] == revision_id
    assert plan["confirmation_token"].startswith("archive-confirm.")
    assert plan["archive"]["name"].endswith("-v1.zip")
    assert plan["denominator"]["included"] > 0
    encoded_plan = json.dumps(plan, ensure_ascii=False)
    assert str(tmp_path) not in encoded_plan
    assert "project.yaml" not in encoded_plan

    wrong_revision = api.report_archive_pick_destination(
        host.project_id, "wrong-revision", plan["confirmation_token"])
    assert wrong_revision["ok"] is False
    assert wrong_revision["destination_token"] is None

    selected = api.report_archive_pick_destination(
        host.project_id, revision_id, plan["confirmation_token"])

    assert selected["ok"] is True
    assert selected["destination_token"].startswith("archive-destination.")
    assert set(selected) == {
        "schema", "ok", "cancelled", "destination_token", "error",
    }
    assert str(destination) not in json.dumps(selected, ensure_ascii=False)

    exported = api.report_archive_export(
        host.project_id, revision_id, plan["confirmation_token"],
        selected["destination_token"],
    )

    assert exported["ok"] is True
    assert exported["status"] == "verified_local_archive"
    assert exported["verification"]["status"] == "verified"
    assert exported["verification"]["checksums"] == "pass"
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

    replay = api.report_archive_export(
        host.project_id, revision_id, plan["confirmation_token"],
        selected["destination_token"],
    )
    assert replay["ok"] is False
    assert replay["status"] == "unavailable"
    assert str(tmp_path) not in json.dumps(replay, ensure_ascii=False)


def test_archive_api_rejects_browser_path_and_unknown_project_without_leaking(tmp_path):
    host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    (tmp_path / "archives").mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    browser_path = str(tmp_path / "private-project.yaml")

    rejected_path = api.report_archive_dry_run(browser_path, revision_id)
    rejected_project = api.report_archive_dry_run("project-" + "b" * 32, revision_id)

    for result in (rejected_path, rejected_project):
        assert result["ok"] is False
        assert result["confirmation_token"] is None
        assert result["decisions"] == []
        encoded = json.dumps(result, ensure_ascii=False)
        assert browser_path not in encoded
        assert str(tmp_path) not in encoded


def test_archive_api_stale_revision_after_plan_writes_no_final_file(tmp_path):
    host, service, revision_id, published = _published_revision(tmp_path, assets=False)
    destination = tmp_path / "archives"
    destination.mkdir()
    api = _api_for_revision(tmp_path, service, host.project_id)
    plan = api.report_archive_dry_run(host.project_id, revision_id)
    selected = api.report_archive_pick_destination(
        host.project_id, revision_id, plan["confirmation_token"])
    Path(published["files"]["html"]).write_text("tampered", encoding="utf-8")

    result = api.report_archive_export(
        host.project_id, revision_id, plan["confirmation_token"],
        selected["destination_token"],
    )

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
    plan = api.report_archive_dry_run(host.project_id, revision_id)
    selected = api.report_archive_pick_destination(
        host.project_id, revision_id, plan["confirmation_token"])
    destination.rename(moved)
    destination.mkdir()

    result = api.report_archive_export(
        host.project_id, revision_id, plan["confirmation_token"],
        selected["destination_token"],
    )

    assert result["ok"] is False
    assert result["file"] is None
    assert not list(destination.iterdir())
    assert not list(moved.iterdir())
    encoded = json.dumps(result, ensure_ascii=False)
    assert str(destination) not in encoded
    assert str(moved) not in encoded


def test_archive_api_failure_dto_uses_archive_credential_scanner():
    secret = "client_secret=" + ("c" * 28)

    result = Api._report_insight_failure(
        "vcstudio.vcs-archive-result/v1", ValueError(f"provider rejected {secret}"),
        revision=None, file=None,
    )

    assert result["ok"] is False
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert result["error"] == "[redacted-secret]"
