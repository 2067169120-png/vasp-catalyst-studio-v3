"""Opaque-project Kinetic Dashboard and safe adapter API contracts."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

from tests.test_kinetics import catmap_ready_network, frozen_network, valid_result
from vcstudio.gui_web.api import Api


def _api(tmp_path, *, with_projection=True, with_tool=True,
         projection_factory=catmap_ready_network):
    root = tmp_path / "project"
    root.mkdir()
    project_file = root / "project.yaml"
    project_file.write_text("schema: vcstudio.project/v1\n", encoding="utf-8")
    project = {
        "name": "Kinetic project", "project_uuid": "a" * 32,
        "root": str(root), "members": {"configs": []},
    }
    adsorption = SimpleNamespace(
        list_projects=lambda: [str(project_file)],
        load_project=lambda path: copy.deepcopy(project) if str(path) == str(project_file) else None,
        delta_e_rows=lambda _project: {"rows": []},
    )
    manifest = SimpleNamespace(load_manifest=lambda _path: None)
    tool = tmp_path / "catmap.exe"
    tool.write_bytes(b"external-user-catmap-adapter")
    config_data = {"tool_paths": {}}
    if with_tool:
        config_data["tool_paths"] = {
            "catmap": str(tool), "catmap_version": "0.4.0",
        }
    config = SimpleNamespace(load_config=lambda: copy.deepcopy(config_data))
    calls = []

    def provider(context):
        calls.append(copy.deepcopy(context))
        return projection_factory() if with_projection else None

    api = Api(
        adsorption_mod=adsorption, manifest_mod=manifest, config_mod=config,
        kinetics_projection_provider=provider,
    )
    project_id = api._workspace_project_id(str(project_file), project)
    return api, project_id, root, tool, calls


def _assert_public(value, tmp_path):
    encoded = json.dumps(value, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "project_path" not in encoded
    assert "project_root" not in encoded
    assert "export_dir" not in encoded


def _confirm(api, project_id, preview, *, confirmed=True):
    return api.kinetics_export_confirm(
        project_id, preview["preview_sha256"], preview["selection_revision"],
        preview["selected_export_sha256"], confirmed=confirmed)


def _select(api, project_id, uploaded):
    return api.kinetics_result_select(
        project_id, uploaded["result_sha256"],
        uploaded["confirmed_export_sha256"],
        uploaded["latest_result_sha256"], uploaded["selection_revision"],
        confirmed=True)


def test_bootstrap_consumes_server_projection_and_keeps_missing_result_unavailable(tmp_path):
    api, project_id, _root, _tool, calls = _api(tmp_path)

    response = api.analysis_workbench_bootstrap(project_id, "kinetic-dashboard")

    assert response["ok"] is True
    assert response["default_spec"]["analysis_id"] == "kinetic-dashboard"
    assert response["view"]["audit"]["machine_pass"] is True
    assert response["view"]["scientific_status"] == "unavailable"
    assert response["view"]["capability_status"] == "available"
    assert response["view"]["available"] is False
    assert "RESULT_NOT_IMPORTED" in response["view"]["reason_codes"]
    assert response["view"]["report_limitation"][
        "may_enter_accepted_or_final"] is False
    assert len(calls) == 1
    assert calls[0]["project_id"] == project_id
    _assert_public(response, tmp_path)


def test_preview_confirm_import_and_dashboard_round_trip(tmp_path):
    api, project_id, root, tool, _calls = _api(tmp_path)
    preview = api.kinetics_export_preview(project_id)

    assert preview["ok"] is True
    assert preview["tool"]["available"] is True
    assert preview["tool"]["sha256"] == hashlib.sha256(tool.read_bytes()).hexdigest()
    assert preview["explicit_confirmation_required"] is True
    assert preview["scientific_status"] == "diagnostic"
    assert not (root / ".vcstudio").exists()
    _assert_public(preview, tmp_path)

    refused = _confirm(api, project_id, preview, confirmed=False)
    assert refused["ok"] is False
    assert not (root / ".vcstudio").exists()

    confirmed = _confirm(api, project_id, preview)
    assert confirmed["ok"] is True
    assert confirmed["eligible_final"] is False
    assert (root / ".vcstudio" / "kinetics" / "exports" /
            preview["input_sha256"] / preview["preview_sha256"] /
            "manifest.json").is_file()
    _assert_public(confirmed, tmp_path)

    network = catmap_ready_network()
    result = valid_result(network)
    result["adapter"]["tool_sha256"] = hashlib.sha256(tool.read_bytes()).hexdigest()
    imported = api.kinetics_result_import(project_id, result)
    before_select = api.analysis_workbench_preview(project_id, {
        "analysis_id": "kinetic-dashboard", "project_id": project_id,
        "precision": 4,
    })
    selected = _select(api, project_id, imported)
    dashboard = api.analysis_workbench_preview(project_id, {
        "analysis_id": "kinetic-dashboard", "project_id": project_id,
        "precision": 4,
    })

    assert imported["ok"] is True
    assert imported["selected"] is False
    assert before_select["view"]["available"] is False
    assert selected["ok"] is True and selected["selected"] is True
    assert imported["scientific_status"] == "diagnostic"
    assert imported["eligible_final"] is False
    assert dashboard["ok"] is True
    assert dashboard["view"]["available"] is True
    assert dashboard["view"]["points"][0]["tof"][0]["display"] == "2.5000"
    assert dashboard["view"]["limitations"]["browser_solves"] is False
    assert dashboard["view"]["report_limitation"] == {
        "result_kind": "diagnostic",
        "may_enter_accepted_or_final": False,
        "human_review_not_replaced": True,
    }
    assert (root / "project.yaml").read_text(encoding="utf-8") == (
        "schema: vcstudio.project/v1\n")
    assert {item.name for item in (root / ".vcstudio").iterdir()} == {"kinetics"}
    _assert_public(imported, tmp_path)
    _assert_public(dashboard, tmp_path)


def test_result_import_requires_confirmed_hash_bound_export(tmp_path):
    api, project_id, root, tool, _calls = _api(tmp_path)
    result = valid_result(catmap_ready_network())
    result["adapter"]["tool_sha256"] = hashlib.sha256(tool.read_bytes()).hexdigest()

    missing_manifest = api.kinetics_result_import(project_id, result)
    assert missing_manifest["ok"] is False
    assert "manifest" in missing_manifest["error"].lower()

    preview = api.kinetics_export_preview(project_id)
    _confirm(api, project_id, preview)
    result["units"]["tof"] = "mol/s"
    rejected = api.kinetics_result_import(project_id, result)

    assert rejected["ok"] is False
    assert "units" in rejected["error"].lower()
    assert not (root / ".vcstudio" / "kinetics" / "results").exists()
    _assert_public(rejected, tmp_path)


def test_missing_projection_is_fail_closed_and_never_accepts_browser_network(tmp_path):
    api, project_id, _root, _tool, calls = _api(tmp_path, with_projection=False)

    bootstrap = api.analysis_workbench_bootstrap(project_id, "kinetic-dashboard")
    preview = api.kinetics_export_preview(project_id)

    assert bootstrap["ok"] is True
    assert bootstrap["view"]["scientific_status"] == "unavailable"
    assert bootstrap["view"]["capability_status"] == "missing_prerequisite"
    assert preview["ok"] is False
    assert "frozen" in preview["error"].lower()
    assert calls
    _assert_public(bootstrap, tmp_path)
    _assert_public(preview, tmp_path)


def test_tool_missing_keeps_audit_visible_but_solver_unavailable(tmp_path):
    api, project_id, root, _tool, _calls = _api(tmp_path, with_tool=False)

    response = api.analysis_workbench_bootstrap(project_id, "kinetic-dashboard")
    preview = api.kinetics_export_preview(project_id)

    assert response["ok"] is True
    assert response["view"]["audit"]["machine_pass"] is True
    assert response["view"]["solver_status"] == "unavailable"
    assert "CATMAP_UNAVAILABLE" in response["view"]["reason_codes"]
    assert preview["ok"] is True
    assert preview["capability_status"] == "unavailable"
    assert preview["export_ready"] is False
    assert preview["preview_sha256"] is None
    assert preview["limitations"]["audit_report_available"] is True
    audit_preview = api.kinetics_audit_export_preview(project_id)
    assert audit_preview["schema"] == "vcstudio.kinetics-audit-export-preview/v1"
    assert audit_preview["model_published"] is False
    audit_confirmed = api.kinetics_audit_export_confirm(
        project_id, audit_preview["preview_sha256"], confirmed=True)
    assert audit_confirmed["ok"] is True
    assert audit_confirmed["model_published"] is False
    assert not list((root / ".vcstudio" / "kinetics" / "audits").rglob("model.mkm"))


def test_unrepresentable_gas_reservoir_is_audit_only_even_with_tool(tmp_path):
    api, project_id, root, _tool, _calls = _api(
        tmp_path, projection_factory=frozen_network)

    preview = api.kinetics_export_preview(project_id)
    assert preview["ok"] is True
    assert preview["audit"]["machine_pass"] is True
    assert preview["export_ready"] is False
    assert preview["preview_sha256"] is None
    assert "positive-pressure gas reservoirs" in preview["adapter_issues"][0][
        "message"]
    refused = api.kinetics_export_confirm(
        project_id, None, preview["selection_revision"],
        preview["selected_export_sha256"], confirmed=True)
    assert refused["ok"] is False
    assert not (root / ".vcstudio").exists()

    audit_preview = api.kinetics_audit_export_preview(project_id)
    audit_confirmed = api.kinetics_audit_export_confirm(
        project_id, audit_preview["preview_sha256"], confirmed=True)
    assert audit_confirmed["ok"] is True
    assert audit_confirmed["model_published"] is False


def test_tool_drift_after_confirm_hides_old_result_until_new_preview(tmp_path):
    api, project_id, _root, tool, _calls = _api(tmp_path)
    preview = api.kinetics_export_preview(project_id)
    _confirm(api, project_id, preview)
    result = valid_result(catmap_ready_network())
    result["adapter"]["tool_sha256"] = hashlib.sha256(tool.read_bytes()).hexdigest()
    uploaded = api.kinetics_result_import(project_id, result)
    assert uploaded["ok"] is True
    assert _select(api, project_id, uploaded)["ok"] is True

    tool.write_bytes(b"different-catmap-adapter-version")
    dashboard = api.analysis_workbench_preview(project_id, {
        "analysis_id": "kinetic-dashboard", "project_id": project_id,
    })
    rejected = api.kinetics_result_import(project_id, result)

    assert dashboard["ok"] is True
    assert dashboard["view"]["available"] is False
    assert dashboard["view"]["adapter"]["sha256"] == result["adapter"][
        "tool_sha256"]
    assert dashboard["view"]["configured_adapter"]["sha256"] == hashlib.sha256(
        tool.read_bytes()).hexdigest()
    assert dashboard["view"]["configured_adapter"][
        "matches_confirmed_export"] is False
    assert "CONFIGURED_TOOL_CHANGED" in dashboard["view"]["reason_codes"]
    assert "RESULT_REVALIDATION_FAILED" in dashboard["view"]["reason_codes"]
    assert rejected["ok"] is False
    assert "does not match" in rejected["error"]

    new_preview = api.kinetics_export_preview(project_id)
    assert new_preview["preview_sha256"] != preview["preview_sha256"]
    newly_confirmed = _confirm(api, project_id, new_preview)
    assert newly_confirmed["ok"] is True
    assert newly_confirmed["selection_revision"] == 2


def test_result_selection_api_rejects_stale_revision_without_last_win(tmp_path):
    api, project_id, _root, tool, _calls = _api(tmp_path)
    preview = api.kinetics_export_preview(project_id)
    _confirm(api, project_id, preview)
    first_result = valid_result(catmap_ready_network())
    first_result["adapter"]["tool_sha256"] = hashlib.sha256(
        tool.read_bytes()).hexdigest()
    second_result = copy.deepcopy(first_result)
    second_result["points"][0]["tof"][0]["value"] = 3.5
    first = api.kinetics_result_import(project_id, first_result)
    second = api.kinetics_result_import(project_id, second_result)
    assert first["selected"] is False and second["selected"] is False
    assert _select(api, project_id, first)["ok"] is True
    stale = _select(api, project_id, second)
    assert stale["ok"] is False
    assert "conflict" in stale["error"].lower()


def test_kinetic_analysis_rejects_irrelevant_or_scientific_browser_fields(tmp_path):
    api, project_id, _root, _tool, _calls = _api(tmp_path)

    for patch in (
            {"near_degenerate_eV": 0.2},
            {"missing_policy": "show_missing"},
            {"sort": {"key": "species", "direction": "asc"}},
            {"network": frozen_network()},
            {"temperature_K": 500.0},
            {"methodology": {"method_id": "invented"}}):
        response = api.analysis_workbench_preview(project_id, {
            "analysis_id": "kinetic-dashboard", "project_id": project_id,
            "precision": 4, **patch,
        })
        assert response["ok"] is False

    accepted = api.analysis_workbench_preview(project_id, {
        "schema": "vcstudio.analysis-spec/v1",
        "analysis_id": "kinetic-dashboard", "project_id": project_id,
        "precision": 6, "view_id": None,
    })
    assert accepted["ok"] is True
    assert accepted["spec"]["precision"] == 6
