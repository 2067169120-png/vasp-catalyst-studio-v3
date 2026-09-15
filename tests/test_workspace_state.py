from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from vcstudio.gui_web.api import Api
from vcstudio.gui_web import workspace_state
from vcstudio.gui_web.workspace_state import (
    SCHEMA,
    WorkspaceStateError,
    WorkspaceStateStore,
)


def _route(area="analyze", view="analyze-energy",
           project_id="project-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", query=""):
    suffix = f"?{query}" if query else ""
    if area == "home":
        route_hash = "#/home"
    elif area == "project":
        project_view = view.removeprefix("project-")
        route_hash = f"#/projects/{project_id}/{project_view}"
    elif area == "analyze":
        analysis_path = {
            "analyze-energy": "adsorption",
            "analyze-thermo": "thermo",
            "analyze-kinetics": "kinetics",
            "analyze-electronic": "electronic",
            "analyze-charge": "charge",
            "analyze-comparison": "comparison",
            "analyze-custom": "custom",
            "analyze-properties": "properties",
            "analyze-references": "references",
        }[view]
        route_hash = f"#/projects/{project_id}/analysis/{analysis_path}"
    elif area == "run":
        route_hash = ("#/run/remote" if view == "run-remote" else "#/jobs" + suffix)
    else:
        path_view = view.removeprefix(f"{area}-")
        if area == "environment":
            path_view = {"local": "local-runner", "paths": "data-paths"}.get(
                path_view, path_view)
        route_hash = f"#/{area}/{path_view}"
    return {"hash": route_hash, "area": area, "view": view}


def test_missing_state_creates_stable_authority_with_canonical_defaults(tmp_path):
    target = tmp_path / "workspace-state.json"
    store = WorkspaceStateStore(target)

    state = store.read()

    authority_id = state.pop("authority_id")
    assert len(authority_id) == 32
    assert int(authority_id, 16) >= 0
    assert state == {
        "schema": SCHEMA,
        "revision": 0,
        "preferences": {
            "route": None,
            "current_project_id": None,
            "current_analysis_id": None,
            "selected_job_id": None,
            "panels": {},
            "filters": {},
            "sort": {},
            "scroll": {},
            "draft_refs": {},
        },
    }
    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert persisted["authority_id"] == authority_id
    assert WorkspaceStateStore(target).read()["authority_id"] == authority_id


def test_update_is_cas_and_explicit_remove_resets_or_deletes(tmp_path):
    store = WorkspaceStateStore(tmp_path / "workspace-state.json")
    first = store.update({
        "set": {
            "route": _route(),
            "current_project_id": "project-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "panels": {"left": True, "details": False},
            "filters": {"jobs": {"state": "RUNNING"}},
            "scroll": {"Analyze.adsorption": 480},
        },
        "remove": [],
    }, 0)

    assert first["ok"] is True and first["state_revision"] == 1
    assert len(first["authority_id"]) == 32
    conflict = store.update({"set": {"current_analysis_id": "taskana"}}, 0)
    assert conflict["ok"] is False and conflict["conflict"] is True
    assert conflict["state_revision"] == 1
    assert conflict["authority_id"] == first["authority_id"]
    assert conflict["preferences"] == first["preferences"]

    second = store.update({
        "set": {"current_analysis_id": "taskana"},
        "remove": ["current_project_id", "panels.details"],
    }, 1)
    assert second["ok"] is True and second["state_revision"] == 2
    assert second["preferences"]["current_project_id"] is None
    assert second["preferences"]["current_analysis_id"] == "taskana"
    assert second["preferences"]["panels"] == {"left": True}
    assert second["authority_id"] == first["authority_id"]
    assert json.loads((tmp_path / "workspace-state.json").read_text(encoding="utf-8"))[
        "revision"] == 2


def test_two_writers_with_same_revision_never_both_commit(tmp_path):
    target = tmp_path / "workspace-state.json"
    stores = [WorkspaceStateStore(target), WorkspaceStateStore(target)]
    barrier = threading.Barrier(2)
    results = []

    def writer(index):
        barrier.wait()
        results.append(stores[index].update(
            {"set": {"current_analysis_id": f"analysis-{index}"}}, 0))

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(result["ok"] for result in results) == [False, True]
    assert sorted(result["conflict"] for result in results) == [False, True]
    assert WorkspaceStateStore(target).read()["revision"] == 1
    assert (tmp_path / workspace_state.LOCK_FILENAME).is_file()


def test_atomic_replace_failure_preserves_previous_bytes_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "workspace-state.json"
    store = WorkspaceStateStore(target)
    assert store.update({"set": {"current_analysis_id": "adsorption"}}, 0)["ok"]
    before = target.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("disk full")

    monkeypatch.setattr(workspace_state.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        store.update({"set": {"current_analysis_id": "taskana"}}, 1)

    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".workspace-state.json.*.tmp"))


def test_legacy_three_field_state_is_atomically_migrated_with_stable_authority(tmp_path):
    target = tmp_path / "workspace-state.json"
    legacy = {
        "schema": SCHEMA,
        "revision": 7,
        "preferences": workspace_state.default_preferences(),
    }
    target.write_text(json.dumps(legacy), encoding="utf-8")

    first = WorkspaceStateStore(target).read()
    persisted = json.loads(target.read_text(encoding="utf-8"))
    second = WorkspaceStateStore(target).read()

    assert first["revision"] == 7
    assert first["preferences"] == legacy["preferences"]
    assert persisted == first == second
    assert len(first["authority_id"]) == 32
    assert int(first["authority_id"], 16) >= 0


def test_invalid_patch_does_not_migrate_or_modify_legacy_state(tmp_path):
    target = tmp_path / "workspace-state.json"
    legacy = {
        "schema": SCHEMA,
        "revision": 7,
        "preferences": workspace_state.default_preferences(),
    }
    target.write_text(json.dumps(legacy), encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(WorkspaceStateError, match="credential"):
        WorkspaceStateStore(target).update({
            "set": {"filters": {"nested": {"label": "password=hunter2"}}},
            "remove": [],
        }, 7)

    assert target.read_bytes() == before


def test_authority_id_prevents_revision_aba_after_state_rebuild(tmp_path):
    target = tmp_path / "workspace-state.json"
    old_store = WorkspaceStateStore(target)
    old = old_store.update({"set": {"current_analysis_id": "adsorption"}}, 0)
    old_bytes = target.read_bytes()

    target.unlink()
    rebuilt_store = WorkspaceStateStore(target)
    rebuilt = rebuilt_store.update({"set": {"current_analysis_id": "taskana"}}, 0)
    assert rebuilt["state_revision"] == old["state_revision"] == 1
    assert rebuilt["authority_id"] != old["authority_id"]
    rebuilt_bytes = target.read_bytes()

    stale = rebuilt_store.update(
        {"set": {"current_analysis_id": "electronic"}},
        old["state_revision"],
        old["authority_id"],
    )

    assert stale["ok"] is False and stale["conflict"] is True
    assert stale["authority_id"] == rebuilt["authority_id"]
    assert target.read_bytes() == rebuilt_bytes
    assert target.read_bytes() != old_bytes


@pytest.mark.parametrize("patch, message", [
    ({"set": {"current_project_id": r"C:\\Users\\alice\\project.yaml"}},
     "filesystem path"),
    ({"set": {"route": {
        "hash": "#/jobs?project=C:/Users/alice/project.yaml",
        "area": "run", "view": "run-jobs"}}}, "unsupported query parameters|invalid query"),
    ({"set": {"route": {
        "hash": "#/jobs", "area": "analyze", "view": "analyze-energy"}}},
     "must match"),
    ({"set": {"unknown": True}}, "unknown keys"),
    ({"set": {"scroll": {"Analyze": -1}}}, "non-negative"),
    ({"set": {"draft_refs": {"draft-1": {"path": "/tmp/draft.txt"}}}},
     "unsupported fields"),
    ({"set": {"filters": {"source": "/home/alice/data"}}}, "filesystem path"),
    ({"set": {"filters": {"source": "failed at /mnt/research/private/job"}}},
     "filesystem path"),
    ({"set": {"filters": {"source": r"selected from C:\Users\alice\private"}}},
     "filesystem path"),
    ({"set": {"filters": {"source": r"copied from \\server\secret\job"}}},
     "filesystem path"),
    ({"set": {"filters": {"jobs": {"api_key": "masked"}}}},
     "sensitive credential key"),
    ({"set": {"panels": {"nested": {"accessToken": "masked"}}}},
     "sensitive credential key"),
    ({"set": {"panels": {"nested": {"databasePassword": "masked"}}}},
     "sensitive credential key"),
    ({"set": {"filters": {"label": "ghp_abcdefghijklmnopqrst"}}},
     "credential"),
    ({"set": {"filters": {"label": "password=hunter2"}}},
     "credential"),
    ({"set": {"filters": {
        "nested": ["safe", {"endpoint": "https://alice:hunter2@example.test/api"}],
    }}}, "credential"),
    ({"set": {"panels": {"cloud_identity": "AKIAIOSFODNN7EXAMPLE"}}},
     "credential"),
    ({"set": {"filters": {"awsAccessKeyIdHint": "masked"}}},
     "sensitive credential key"),
    ({"set": {"route": {
        "hash": "#/projects/password/overview",
        "area": "project", "view": "project-overview"}}},
     "sensitive route segment"),
    ({"set": {"route": {
        "hash": "#/projects/AKIAIOSFODNN7EXAMPLE/overview",
        "area": "project", "view": "project-overview"}}},
     "credential"),
    ({"set": {"route": {
        "hash": "#/publish/report?project=ghp_abcdefghijklmnopqrst",
        "area": "publish", "view": "publish-report"}}},
     "credential"),
    ({"set": {"route": {
        "hash": "#/jobs?cluster=password%3Dhunter2",
        "area": "run", "view": "run-jobs"}}},
     "credential"),
    ({"set": {"route": {
        "hash": "#/home?api_key=ghp_abcdefghijklmnopqrst",
        "area": "home", "view": "home"}}}, "credential|unsupported query parameters"),
    ({"set": {"route": {
        "hash": "#/jobs?project=project-abc",
        "area": "run", "view": "run-jobs"}}}, "unsupported query parameters"),
    ({"set": {"route": {
        "hash": "#/jobs?status=need&status=done",
        "area": "run", "view": "run-jobs"}}}, "duplicate query parameters"),
    ({"set": {"route": {
        "hash": "#/jobs?status=bogus",
        "area": "run", "view": "run-jobs"}}}, "unsupported job status filter"),
    ({"set": {"route": {
        "hash": "#/jobs?cluster=..%2Fsecret",
        "area": "run", "view": "run-jobs"}}}, "filesystem path|invalid cluster filter"),
])
def test_validation_rejects_unknown_unsafe_or_path_bearing_state(tmp_path, patch, message):
    store = WorkspaceStateStore(tmp_path / "workspace-state.json")
    with pytest.raises(WorkspaceStateError, match=message):
        store.update({**patch, "remove": []}, 0)
    assert not (tmp_path / "workspace-state.json").exists()


@pytest.mark.parametrize("unsafe_value", [
    "password=hunter2",
    "https://alice:hunter2@example.test/api",
    "ssh://alice:hunter2@example.test/private",
    "ftp://alice:hunter2@example.test/private",
    "postgresql://alice:hunter2@example.test/private",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key=example-secret",
    "github_pat_abcdefghijklmnopqrstuvwx",
    "glpat-abcdefghijklmnopqrstuvwx",
    "hf_abcdefghijklmnopqrstuvwx",
    "sk_live_abcdefghijklmnopqrstuvwx",
    "pk_live_abcdefghijklmnopqrstuvwx",
    "x://alice:hunter2@example.test/private",
    f"{'a' * 80}://alice:hunter2@example.test/private",
    "/srv/private/research/job-a",
])
def test_recursive_secret_gate_preserves_existing_state_bytes(tmp_path, unsafe_value):
    target = tmp_path / "workspace-state.json"
    store = WorkspaceStateStore(target)
    initial = store.update({"set": {"filters": {"status": "ready"}}}, 0)
    before = target.read_bytes()

    with pytest.raises(WorkspaceStateError):
        store.update({
            "set": {"filters": {"nested": [{"value": unsafe_value}]}},
            "remove": [],
        }, initial["state_revision"], initial["authority_id"])

    assert target.read_bytes() == before
    assert store.read()["revision"] == initial["state_revision"]


def test_draft_refs_store_only_bounded_metadata(tmp_path):
    store = WorkspaceStateStore(tmp_path / "workspace-state.json")
    digest = "a" * 64
    result = store.update({
        "set": {"draft_refs": {
            "draft-1": {
                "project_id": "project-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "route": _route(),
                "kind": "unverified_draft",
                "blob_sha256": digest.upper(),
                "dirty": True,
                "updated_at": "2026-08-10T10:30:00+08:00",
                "size": 321,
            },
        }},
        "remove": [],
    }, 0)

    assert result["ok"] is True
    assert result["preferences"]["draft_refs"]["draft-1"]["blob_sha256"] == digest
    assert "content" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("ref, message", [
    ({"kind": "verified"}, "must be unverified_draft"),
    ({"updated_at": "yesterday"}, "ISO-8601"),
    ({"updated_at": "2026-08-10T10:00:00"}, "timezone"),
    ({"size": 64 * 1024 * 1024 + 1}, "bounded"),
])
def test_draft_reference_metadata_is_fail_closed(tmp_path, ref, message):
    store = WorkspaceStateStore(tmp_path / "workspace-state.json")
    with pytest.raises(WorkspaceStateError, match=message):
        store.update({"set": {"draft_refs": {"draft-1": ref}}, "remove": []}, 0)


@pytest.mark.parametrize("route", [
    {"hash": "#/home", "area": "home", "view": "home"},
    {"hash": "#/projects/project-abc/overview", "area": "project",
     "view": "project-overview"},
    {"hash": "#/projects/project-abc/members", "area": "project",
     "view": "project-members"},
    {"hash": "#/projects/project-abc/analysis/adsorption", "area": "analyze",
     "view": "analyze-energy"},
    {"hash": "#/projects/project-abc/analysis/kinetics", "area": "analyze",
     "view": "analyze-kinetics"},
    {"hash": "#/projects/project-abc/analysis/references", "area": "analyze",
     "view": "analyze-references"},
    {"hash": "#/prepare/preflight", "area": "prepare", "view": "prepare-preflight"},
    {"hash": "#/jobs?status=need", "area": "run", "view": "run-jobs"},
    {"hash": "#/jobs?status=need&cluster=gpu-a", "area": "run", "view": "run-jobs"},
    {"hash": "#/jobs?cluster=%E4%B8%8A%E6%B5%B7+%E8%B6%85%E7%AE%97",
     "area": "run", "view": "run-jobs"},
    {"hash": "#/run/remote", "area": "run", "view": "run-remote"},
    {"hash": "#/publish/report?project=project-abc", "area": "publish",
     "view": "publish-report"},
    {"hash": "#/publish/report?project=project-abc&spec=spec-01&revision=4",
     "area": "publish", "view": "publish-report"},
    {"hash": "#/environment/local-runner", "area": "environment",
     "view": "environment-local"},
    {"hash": "#/environment/data-paths", "area": "environment",
     "view": "environment-paths"},
])
def test_canonical_phase_b_routes_round_trip(tmp_path, route):
    store = WorkspaceStateStore(tmp_path / "workspace-state.json")
    result = store.update({"set": {"route": route}, "remove": []}, 0)
    assert result["ok"] is True
    assert result["preferences"]["route"] == route


@pytest.mark.parametrize("route_hash", [
    "#/publish/report?project=project-abc&revision=0",
    "#/publish/report?project=project-abc&revision=-1",
    "#/publish/report?project=project-abc&revision=latest",
    "#/publish/report?project=project-abc&revision=01",
    "#/publish/figures?project=project-abc&spec=spec-01",
])
def test_report_route_query_is_strict_and_revision_is_positive_integer(
        tmp_path, route_hash):
    store = WorkspaceStateStore(tmp_path / "workspace-state.json")
    with pytest.raises(WorkspaceStateError):
        store.update({"set": {"route": {
            "hash": route_hash, "area": "publish", "view": (
                "publish-figures" if "/figures" in route_hash else "publish-report")
        }}, "remove": []}, 0)


def test_legacy_registry_hash_is_stable_and_does_not_reveal_path(tmp_path):
    path = str(tmp_path / "private-study" / "project.yaml")
    first = Api._workspace_project_id(path, {})
    second = Api._workspace_project_id(path, {})

    assert first == second
    assert first.startswith("registry-") and len(first) == len("registry-") + 24
    assert "private-study" not in first and str(tmp_path) not in first


def test_corrupt_or_wrong_schema_state_fails_closed(tmp_path):
    target = tmp_path / "workspace-state.json"
    target.write_text('{"schema":"old","revision":0,"preferences":{}}', encoding="utf-8")
    with pytest.raises(WorkspaceStateError, match="unsupported"):
        WorkspaceStateStore(target).read()
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkspaceStateError, match="cannot read"):
        WorkspaceStateStore(target).read()


def _api_for_context(tmp_path, *, selected=True, work_mode="lis", pipeline_ok=True):
    project_path = str(tmp_path / "private" / "project.yaml")
    member_dir = str(tmp_path / "private" / "job-a")
    project_uuid = "a" * 32
    project_id = f"project-{project_uuid}"
    project = {
        "name": "demo",
        "project_uuid": project_uuid,
        "members": {"clean_slab": member_dir, "gas_ref": None, "configs": []},
    }
    if work_mode is not None:
        project["work_mode"] = work_mode
    manifest = {
        "job_id": "job-a",
        "state": "RUNNING",
        "task_type": "relax",
        "calc_type": "slab",
        "inputs": {"engine": "cp2k"},
        "state_history": [{"state": "RUNNING", "at": "2026-08-10T10:00:00"}],
    }
    adsorption = SimpleNamespace(
        list_projects=lambda: [project_path],
        load_project=lambda path: project if path == project_path else None)
    manifests = SimpleNamespace(load_manifest=lambda path: manifest if path == member_dir else None)
    store = WorkspaceStateStore(tmp_path / "state" / "workspace-state.json")
    wanted = project_id if selected else "registry-deadbeefdeadbeefdeadbeef"
    assert store.update({
        "set": {
            "route": _route(project_id=wanted),
            "current_project_id": wanted,
            "current_analysis_id": "adsorption",
            "selected_job_id": "job-a",
            "panels": {"context_assistant": True},
            "filters": {"jobs": {"state": "RUNNING"}},
            "sort": {"jobs": "updated-desc"},
            "scroll": {"Analyze.adsorption": 120},
            "draft_refs": {"draft-1": {"project_id": wanted, "dirty": True}},
        },
        "remove": [],
    }, 0)["ok"]

    api = Api(adsorption_mod=adsorption, manifest_mod=manifests,
              workspace_state_store=store)
    api.pipeline_status = lambda: ({
        "ok": True,
        "projects": [{
            "project_id": project_id, "name": "demo", "stage": "monitor",
            "stage_index": 2, "stages": ["generate", "submit", "monitor"],
            "needs_human": False, "recover_round": 0, "done": 0, "total": 1,
            "artifact_status": "missing", "scientific_status": None,
            "scientific_qualification": None,
            "publication_gate_status": "pending",
            "desired_report_kind": "diagnostic", "report_kind": None,
            "report_status": None, "report_reason": "",
        }],
        "error": None,
    } if pipeline_ok else {"ok": False, "projects": [], "error": "status failed"})
    api.scenario_get = lambda: {
        "ok": True, "configured": True,
        "scenario": {"key": "lis", "name": "Li-S"}, "error": None}
    api.engine_get = lambda: {
        "ok": True, "engine": "vasp", "configured": True,
        "capability": {"task_keys": ["relax"]}, "error": None}
    api.calculation_get = lambda: {
        "ok": True, "active_calculation": "adsorption_project",
        "configured": True, "allowed": ["adsorption_project"],
        "engine": "vasp", "error": None}
    api.pipeline_runtime_status = lambda: {
        "ok": True,
        "state": {
            "running": True, "paused": False, "tick_running": False,
            "enabled": True, "interval_seconds": 600, "last_started": "start",
            "last_finished": "finish", "next_check": "next", "outcome_seq": 4,
            "last_error": None,
            "last_outcome": {"synced": 1, "events": [{"kind": "refresh"}],
                             "errors": []},
            "outcome_history": [{
                "seq": 4, "finished": "finish",
                "outcome": {"synced": 1, "events": [{"kind": "refresh"}],
                            "errors": []}, "error": None,
            }],
        },
        "error": None,
    }
    return api, project_path, project_id


def test_workspace_context_separates_intent_from_project_facts_and_redacts_paths(tmp_path):
    api, project_path, project_id = _api_for_context(tmp_path)

    result = api.workspace_context()

    assert result["ok"] is True
    assert result["schema"] == "vcstudio.workspace-context/v1"
    assert len(result["authority_id"]) == 32
    assert result["state_revision"] == 1
    assert result["selection"] == {
        "route": _route(project_id=project_id),
        "route_hash": _route(project_id=project_id)["hash"],
        "project_id": project_id,
        "analysis_id": "adsorption",
        "job_id": "job-a",
        "selection_status": "valid",
        "source": "persisted_user_preference",
    }
    assert result["workspace_intent"]["engine"]["key"] == "vasp"
    assert result["workspace_intent"]["calculation"]["key"] == "adsorption_project"
    assert result["project"]["project_mode"] == "lis"
    assert result["project"]["actual_engine"] == "cp2k"
    assert result["project"]["actual_task_type"] == "relax"
    assert result["project"]["selected_job"]["id"] == "job-a"
    assert result["pipeline"]["stage"] == "monitor"
    assert result["sync"] == {
        "last_check_at": "finish", "last_success_at": "finish",
        "status": "succeeded", "synced_targets": 1,
    }
    assert result["unsaved"] == {
        "known": True, "dirty": True, "draft_ids": ["draft-1"]}
    assert result["projects"][0]["project_id"] == project_id
    assert project_path not in json.dumps(result, ensure_ascii=False)
    assert api.workspace_context_get() == result


def test_workspace_public_text_redacts_general_absolute_paths_but_preserves_urls():
    message = (
        "failed at /mnt/research/a and /opt/tool/b; fallback /srv/cache/c; "
        "custom=/science/private/d; Windows C:\\Users\\alice\\job; "
        "UNC \\\\server\\share\\job; file:///home/alice/e; "
        "docs https://example.test/help/path and route #/jobs"
    )

    public = Api._workspace_public_text(message, limit=2000)

    for private in ("/mnt/", "/opt/", "/srv/", "/science/", "C:\\Users",
                    "server\\share", "/home/alice"):
        assert private not in public
    assert public.count("<local-path>") == 7
    assert "https://example.test/help/path" in public
    assert "#/jobs" in public


def test_workspace_runtime_reports_partial_sync_and_matching_activity(tmp_path):
    api, _project_path, _project_id = _api_for_context(tmp_path)
    api.pipeline_runtime_status = lambda: {
        "ok": True,
        "state": {
            "running": True, "paused": False, "tick_running": False,
            "enabled": True, "interval_seconds": 600, "last_started": "start",
            "last_finished": "finish", "next_check": "next", "outcome_seq": 5,
            "last_error": None,
            "last_outcome": {
                "synced": 1, "events": [{"kind": "refresh"}],
                "errors": ["failed at /mnt/private/job"],
            },
            "outcome_history": [{
                "seq": 5, "finished": "finish", "error": None,
                "outcome": {
                    "synced": 1, "events": [{"kind": "refresh"}],
                    "errors": ["failed at /mnt/private/job"],
                },
            }],
        },
        "error": None,
    }

    result = api.workspace_context()

    assert result["sync"] == {
        "last_check_at": "finish", "last_success_at": "finish",
        "status": "partial", "synced_targets": 1,
    }
    assert result["activity"]["recent"][0]["status"] == "partial"
    degraded = [item for item in result["degraded"]
                if item["kind"] == "sync_partial_failure"]
    assert degraded == [{
        "kind": "sync_partial_failure",
        "message": "1 sync error(s) occurred after at least one target succeeded",
    }]
    assert "/mnt/private/job" not in json.dumps(result, ensure_ascii=False)


def test_workspace_context_missing_selection_is_fail_closed(tmp_path):
    api, _project_path, _project_id = _api_for_context(tmp_path, selected=False)

    result = api.workspace_context()

    assert result["ok"] is True
    assert result["selection"]["project_id"] == "registry-deadbeefdeadbeefdeadbeef"
    assert result["selection"]["selection_status"] == "missing"
    assert result["project"] is None
    assert result["pipeline"] is None
    assert any(item["kind"] == "selected_project_missing"
               for item in result["degraded"])


def test_workspace_context_does_not_infer_project_mode_from_scenario(tmp_path):
    api, _project_path, _project_id = _api_for_context(tmp_path, work_mode=None)

    result = api.workspace_context()

    assert result["workspace_intent"]["scenario"]["key"] == "lis"
    assert result["project"]["project_mode"] is None


def test_workspace_context_pipeline_failure_does_not_fall_back_to_generate(tmp_path):
    api, _project_path, _project_id = _api_for_context(tmp_path, pipeline_ok=False)

    result = api.workspace_context()

    assert result["project"] is not None
    assert result["pipeline"] is None
    assert any(item["kind"] == "pipeline_status_unavailable"
               for item in result["degraded"])
    assert any(item["kind"] == "selected_pipeline_unavailable"
               for item in result["degraded"])


def test_workspace_preferences_api_returns_stable_conflict_and_validation_envelopes(tmp_path):
    target = tmp_path / "workspace-state.json"
    store = WorkspaceStateStore(target)
    api = Api(workspace_state_store=store)
    authority_id = store.read()["authority_id"]

    first = api.workspace_preferences_update(
        {"set": {"route": _route("home", "home")}, "remove": []},
        0,
        authority_id,
    )
    assert first == {
        "ok": True, "conflict": False, "authority_id": first["authority_id"],
        "state_revision": 1,
        "preferences": first["preferences"], "error": None,
    }
    conflict = api.workspace_preferences_update(
        {"set": {"current_analysis_id": "adsorption"}, "remove": []},
        0,
        authority_id,
    )
    assert conflict["ok"] is False and conflict["conflict"] is True
    assert conflict["state_revision"] == 1 and conflict["preferences"]
    invalid = api.workspace_preferences_update(
        {"set": {"current_project_id": "/home/alice/project.yaml"}, "remove": []},
        1,
        authority_id,
    )
    assert invalid["ok"] is False and invalid["conflict"] is False
    assert invalid["state_revision"] is None and "filesystem path" in invalid["error"]

    before = target.read_bytes()
    missing_authority = api.workspace_preferences_update(
        {"set": {"current_analysis_id": "electronic"}, "remove": []}, 1)
    assert missing_authority == {
        "ok": False,
        "conflict": False,
        "authority_id": None,
        "state_revision": None,
        "preferences": None,
        "error": "expected_authority_id is required for workspace-state CAS",
    }
    assert target.read_bytes() == before


def test_workspace_context_corrupt_state_has_full_failure_contract(tmp_path):
    target = tmp_path / "workspace-state.json"
    target.write_text("not-json", encoding="utf-8")
    api = Api(workspace_state_store=WorkspaceStateStore(target))

    result = api.workspace_context()

    assert set(result) == {
        "ok", "schema", "authority_id", "state_revision", "selection", "workspace_intent",
        "project", "pipeline", "runtime", "sync", "restore", "unsaved",
        "activity", "projects", "degraded", "error",
    }
    assert result["ok"] is False and result["selection"] is None
    assert result["authority_id"] is None
    assert result["project"] is None and result["pipeline"] is None
    assert result["degraded"][0]["kind"] == "workspace_state_unavailable"
