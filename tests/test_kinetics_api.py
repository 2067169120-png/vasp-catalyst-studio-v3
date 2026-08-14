"""Opaque-project Kinetic Dashboard and safe adapter API contracts."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

from tests.test_kinetics import frozen_network, valid_result
from vcstudio.gui_web.api import Api


def _api(tmp_path, *, with_projection=True, with_tool=True):
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
        return frozen_network() if with_projection else None

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
    assert calls and calls[0]["project_id"] == project_id
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

    refused = api.kinetics_export_confirm(
        project_id, preview["preview_sha256"], confirmed=False)
    assert refused["ok"] is False
    assert not (root / ".vcstudio").exists()

    confirmed = api.kinetics_export_confirm(
        project_id, preview["preview_sha256"], confirmed=True)
    assert confirmed["ok"] is True
    assert confirmed["eligible_final"] is False
    assert (root / ".vcstudio" / "kinetics" / "exports" /
            preview["input_sha256"] / "manifest.json").is_file()
    _assert_public(confirmed, tmp_path)

    network = frozen_network()
    result = valid_result(network)
    result["adapter"]["tool_sha256"] = hashlib.sha256(tool.read_bytes()).hexdigest()
    imported = api.kinetics_result_import(project_id, result)
    dashboard = api.analysis_workbench_preview(project_id, {
        "analysis_id": "kinetic-dashboard", "project_id": project_id,
        "precision": 4,
    })

    assert imported["ok"] is True
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
    result = valid_result(frozen_network())
    result["adapter"]["tool_sha256"] = hashlib.sha256(tool.read_bytes()).hexdigest()

    missing_manifest = api.kinetics_result_import(project_id, result)
    assert missing_manifest["ok"] is False
    assert "manifest" in missing_manifest["error"].lower()

    preview = api.kinetics_export_preview(project_id)
    api.kinetics_export_confirm(project_id, preview["preview_sha256"], confirmed=True)
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
    api, project_id, _root, _tool, _calls = _api(tmp_path, with_tool=False)

    response = api.analysis_workbench_bootstrap(project_id, "kinetic-dashboard")
    preview = api.kinetics_export_preview(project_id)

    assert response["ok"] is True
    assert response["view"]["audit"]["machine_pass"] is True
    assert response["view"]["solver_status"] == "unavailable"
    assert "CATMAP_UNAVAILABLE" in response["view"]["reason_codes"]
    assert preview["ok"] is True
    assert preview["capability_status"] == "unavailable"
    assert preview["limitations"]["audit_report_available"] is True


def test_tool_drift_after_confirm_hides_old_result_until_new_preview(tmp_path):
    api, project_id, _root, tool, _calls = _api(tmp_path)
    preview = api.kinetics_export_preview(project_id)
    api.kinetics_export_confirm(
        project_id, preview["preview_sha256"], confirmed=True)
    result = valid_result(frozen_network())
    result["adapter"]["tool_sha256"] = hashlib.sha256(tool.read_bytes()).hexdigest()
    assert api.kinetics_result_import(project_id, result)["ok"] is True

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
