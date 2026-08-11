from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

from vcstudio.gui_web.api import Api


def _manifest(state="DONE"):
    return {
        "job_uuid": "job-a",
        "state": state,
        "task_type": "relax",
        "calc_type": "slab",
        "inputs": {"engine": "vasp"},
        "results": {},
    }


def _api(tmp_path, *, duplicate=False):
    roots = [tmp_path / "alpha"]
    if duplicate:
        roots.append(tmp_path / "beta")
    locators = []
    projects = {}
    manifests = {}
    for index, root in enumerate(roots):
        root.mkdir(parents=True)
        locator = str(root / "project.yaml")
        Path(locator).write_text("schema: vcstudio.project/v1\n", encoding="utf-8")
        member = str(root / f"member-{index}")
        Path(member).mkdir()
        project = {
            "name": f"project-{index}",
            "project_uuid": "a" * 32,
            "root": str(root),
            "members": {
                "clean_slab": member,
                "gas_ref": None,
                "configs": [],
            },
        }
        locators.append(locator)
        projects[locator] = project
        manifests[member] = _manifest()

    def load_project(path):
        wanted = str(Path(path).resolve())
        for locator, project in projects.items():
            if str(Path(locator).resolve()) == wanted:
                return project
        return None

    adsorption = SimpleNamespace(
        list_projects=lambda: list(locators),
        load_project=load_project,
        delta_e_rows=lambda _project: {
            "slab": ("DONE", -10.0),
            "ref": ("无", None),
            "has_ref": False,
            "reference_mode": "none",
            "method_consistency": {
                "status": "unverified", "issues": [], "warnings": []},
            "rows": [],
        },
    )
    manifest = SimpleNamespace(
        load_manifest=lambda path: manifests.get(str(path)))
    ledger = SimpleNamespace(
        load_all=lambda: [(path, value) for path, value in manifests.items()])
    workspace = SimpleNamespace(read=lambda: {
        "revision": 0,
        "preferences": {
            "route": None,
            "current_project_id": None,
            "current_analysis_id": None,
            "selected_job_id": None,
        },
    })
    api = Api(
        adsorption_mod=adsorption,
        manifest_mod=manifest,
        ledger_mod=ledger,
        workspace_state_store=workspace,
    )
    api.scenario_get = lambda: {
        "ok": True, "configured": False, "scenario": {
            "key": "general", "name": "General"}, "error": None}
    api.engine_get = lambda: {
        "ok": True, "configured": True, "engine": "vasp",
        "capability": {}, "error": None}
    api.calculation_get = lambda: {
        "ok": True, "configured": False, "active_calculation": "",
        "allowed": [], "engine": "vasp", "error": None}
    api.pipeline_runtime_status = lambda: {
        "ok": True,
        "state": {
            "running": False, "paused": False, "tick_running": False,
            "enabled": False, "interval_seconds": 600,
            "last_started": None, "last_finished": None,
            "next_check": None, "outcome_seq": 0,
            "last_error": None, "last_outcome": None,
            "outcome_history": [],
        },
        "error": None,
    }
    return api, locators, projects


def _assert_no_registered_locator(value, locators, *, forbid_project_keys=True):
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    for locator in locators:
        assert locator not in encoded
        assert str(Path(locator).resolve()) not in encoded

    def visit(item):
        if isinstance(item, dict):
            forbidden = {"path", "project_path", "project_uuid", "root", "locator"}
            if forbid_project_keys:
                assert forbidden.isdisjoint(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)


def test_four_public_project_dtos_are_recursively_locator_free(tmp_path):
    api, locators, _projects = _api(tmp_path)

    payloads = [
        api.proj_list()["projects"],
        api.workspace_context()["projects"],
        api.pipeline_status()["projects"],
        api.list_jobs()["jobs"],
    ]

    for payload in payloads:
        _assert_no_registered_locator(payload, locators)
    assert api.proj_list()["projects"][0]["project_id"].startswith("project-")
    assert api.workspace_context()["projects"][0]["project_id"].startswith(
        "project-")
    assert api.pipeline_status()["projects"][0]["project_id"].startswith(
        "project-")
    assert api.list_jobs()["jobs"][0]["project_id"].startswith("project-")


def test_all_public_project_bridges_reject_raw_absolute_locator(tmp_path):
    api, locators, _projects = _api(tmp_path)
    raw = locators[0]
    calls = [
        lambda: api.proj_prepare_lis("x", "s", [], "i", "o", raw),
        lambda: api.submit_project_with_resources(raw, "hpc", 1, "01:00:00"),
        lambda: api.proj_delta(raw),
        lambda: api.proj_export_csv(raw, str(tmp_path / "x.csv")),
        lambda: api.proj_report(raw, str(tmp_path / "x.html")),
        lambda: api.proj_report_status(raw),
        lambda: api.proj_report_bundle(raw, str(tmp_path / "reports")),
        lambda: api.proj_evaluate_candidate(raw),
        lambda: api.proj_figures(raw),
        lambda: api.draft_ready(raw, str(tmp_path / "draft")),
        lambda: api.render_figure_preset("adsorption_bar", raw),
        lambda: api.ai_compare(raw, []),
        lambda: api.ai_write_validation(raw, []),
        lambda: api.ai_manuscript(raw),
        lambda: api.proj_compare_preview([raw, raw + ".other"]),
        lambda: api.proj_batch_report(
            [raw, raw + ".other"], str(tmp_path / "batch")),
        lambda: api.proj_compare_figures([raw, raw + ".other"]),
    ]

    for invoke in calls:
        result = invoke()
        assert result["ok"] is False
        _assert_no_registered_locator(
            result, locators, forbid_project_keys=False)


def test_unknown_and_duplicate_project_ids_fail_closed(tmp_path):
    api, _locators, _projects = _api(tmp_path)
    unknown = api.proj_delta("project-" + "f" * 32)
    assert unknown["ok"] is False

    duplicate_api, locators, _projects = _api(tmp_path / "duplicate", duplicate=True)
    duplicate_id = "project-" + "a" * 32
    duplicate = duplicate_api.proj_delta(duplicate_id)
    assert duplicate["ok"] is False
    assert duplicate_api.proj_list()["projects"] == []
    _assert_no_registered_locator(duplicate, locators)


def test_create_import_and_prepare_responses_do_not_round_trip_locator(tmp_path):
    api, locators, projects = _api(tmp_path)
    slab = tmp_path / "POSCAR"
    incar = tmp_path / "INCAR"
    slab.write_text("slab", encoding="utf-8")
    incar.write_text("ENCUT=400", encoding="utf-8")
    created_root = tmp_path / "created"
    created_locator = str(created_root / "project.yaml")
    created = {
        "name": "created",
        "project_uuid": "b" * 32,
        "root": str(created_root),
        "members": {"clean_slab": str(created_root / "clean"),
                    "gas_ref": None, "configs": []},
    }
    api._adsorption.create_project = lambda *_args, **_kwargs: {
        "ok": True, "project_path": created_locator, "project": created,
        "generated": [], "errors": [], "advisories": [],
    }
    api._adsorption.save_project = lambda *_args: None

    create_result = api.proj_create(
        "created", str(slab), [str(slab)], str(incar), "", str(tmp_path))
    assert create_result["ok"] is True
    assert set(create_result) >= {"project_id", "name"}
    assert "project_path" not in create_result

    api._result_import = SimpleNamespace(commit_import=lambda *_args, **_kwargs: {
        "ok": True, "project_path": created_locator, "project": created,
        "imported": [{"path": str(created_root / "clean")}],
        "summary": {"project_path": created_locator},
    })
    api._member_states = lambda _project: []
    import_result = api.proj_import_commit(
        str(tmp_path / "source"), str(tmp_path), "created",
        [{"selected": True, "role": "clean_slab", "path": str(slab)}],
    )
    assert import_result["ok"] is True
    assert "project_path" not in import_result
    _assert_no_registered_locator(import_result, [created_locator])

    projects[created_locator] = created
    api._proj_prepare_lis_for_reference_path = lambda *_args, **_kwargs: {
        "ok": True,
        "project_path": created_locator,
        "job_dirs": [str(created_root / "clean")],
        "preparation": {"reference_project": locators[0]},
        "reference_species": [], "advisories": [], "warnings": [],
        "method_check": {}, "needs_method_confirmation": False,
        "repair_plan": None, "needs_repair_decision": False, "error": None,
    }
    reference_id = api.proj_list()["projects"][0]["project_id"]
    prepare_result = api.proj_prepare_lis(
        "created", str(slab), [], str(incar), str(tmp_path), reference_id)
    assert prepare_result["ok"] is True
    assert "project_path" not in prepare_result and "job_dirs" not in prepare_result
    assert "preparation" not in prepare_result
    _assert_no_registered_locator(prepare_result, [*locators, created_locator])


def test_registry_errors_are_projected_without_private_path(tmp_path):
    secret = str(tmp_path / "secret" / "project.yaml")
    adsorption = SimpleNamespace(
        list_projects=lambda: (_ for _ in ()).throw(
            RuntimeError(f"cannot read {secret}")))
    result = Api(adsorption_mod=adsorption).proj_list()

    assert result == {
        "projects": [], "error": "The registered project list is unavailable."}
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_public_project_binding_rejects_identity_changed_on_second_load(tmp_path):
    api, locators, projects = _api(tmp_path)
    locator = locators[0]
    first = projects[locator]
    replacement = {**first, "name": "replacement", "project_uuid": "b" * 32}
    loads = 0
    private_calls = []

    def changing_load(_path):
        nonlocal loads
        loads += 1
        return first if loads == 1 else replacement

    api._adsorption.load_project = changing_load
    api._proj_delta_for_path = lambda path: private_calls.append(path) or {
        "ok": True, "rows": [{"name": "replacement"}], "note": "B"}

    result = api.proj_delta("project-" + "a" * 32)

    assert result["ok"] is False
    assert result["error_code"] == "identity_mismatch"
    assert private_calls == []
    assert "replacement" not in json.dumps(result, ensure_ascii=False)


def test_pipeline_public_egress_recursively_removes_secret_locators(tmp_path):
    secret = r"C:\Users\alice\Private Project\project.yaml"
    api, _locators, _projects = _api(tmp_path)
    malicious_outcome = {
        "ok": True,
        "events": [{
            "kind": "report_done", "report": secret,
            "files": {"pdf": secret}, "figures_dir": secret,
            "text": f"generated {secret}",
        }],
        "errors": [f"cannot read {secret}"],
        "synced": 0,
    }
    api._pipeline_tick_once = lambda: malicious_outcome

    direct = api.pipeline_tick()
    assert secret not in json.dumps(direct, ensure_ascii=False)
    assert {"report", "files", "figures_dir"}.isdisjoint(direct["events"][0])

    state = {
        **api._pipeline_runtime,
        "last_error": f"failed at {secret}",
        "last_outcome": malicious_outcome,
        "outcome_history": [{
            "seq": 1, "outcome": malicious_outcome,
            "error": f"failed at {secret}",
        }],
    }
    api._pipeline_supervisor = SimpleNamespace(snapshot=lambda: state)
    runtime = api.pipeline_runtime_status()
    assert secret not in json.dumps(runtime, ensure_ascii=False)


def test_pipeline_unknown_scalars_are_rechecked_after_stringification():
    class TextValue:
        def __init__(self, text):
            self.text = text

        def __str__(self):
            return self.text

    secrets = [
        PureWindowsPath(r"C:\Users\alice\Private Project\artifact.pdf"),
        TextValue(r"\\server\share\Private Project\artifact.pdf"),
        TextValue("/home/alice/Private Project/artifact.pdf"),
        TextValue("file:///C:/Users/alice/Private%20Project/artifact.pdf"),
        TextValue("Bearer vcs_pipeline_secret_token_123456"),
    ]
    payload = {
        "nested": [{"value": value, "project_path": value}
                   for value in secrets],
        "integer": 7,
        "ratio": 1.25,
        "enabled": True,
        "missing": None,
    }

    public = Api._pipeline_public_value(payload)
    encoded = json.dumps(public, ensure_ascii=False)

    for secret in secrets:
        assert str(secret) not in encoded
    assert all("project_path" not in item for item in public["nested"])
    assert all(item["value"] == "[redacted-sensitive-value]"
               for item in public["nested"])
    assert public["integer"] == 7 and type(public["integer"]) is int
    assert public["ratio"] == 1.25 and type(public["ratio"]) is float
    assert public["enabled"] is True
    assert public["missing"] is None
