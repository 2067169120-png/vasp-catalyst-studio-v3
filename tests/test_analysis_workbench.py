from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from vcstudio.gui_web.api import Api
from vcstudio.project.analysis_preferences import AnalysisPreferencesStore


def _analysis_api(tmp_path):
    projects = {}
    summaries = {}
    manifests = {}
    paths = []
    for project_index, (name, prefix) in enumerate(
            (("Catalyst A", "a"), ("Catalyst B", "b"), ("Catalyst C", "c"))):
        root = tmp_path / prefix
        root.mkdir()
        project_path = str(root / "project.yaml")
        Path(project_path).write_text(
            "schema: vcstudio.project/v1\n", encoding="utf-8")
        clean = str(root / "clean")
        reference = str(root / "reference")
        config_names = [
            f"{prefix}_Li2S8_low", f"{prefix}_Li2S8_near",
            f"{prefix}_Li2S6", f"{prefix}_Li2S4_missing",
        ]
        configs = [str(root / item) for item in config_names]
        for directory in (clean, reference, *configs):
            Path(directory).mkdir()
        project_uuid = f"{project_index + 1:x}" * 32
        project = {
            "name": name,
            "project_uuid": project_uuid,
            "root": str(root),
            "members": {
                "clean_slab": clean,
                "gas_ref": reference,
                "configs": configs,
            },
            "comparison_method_fingerprint": "same-method",
        }
        base = -5.0 + project_index * 0.2
        summary = {
            "slab": ("DONE", -100.0),
            "ref": ("DONE", -10.0),
            "has_ref": True,
            "reference_mode": "single",
            "comparison_method_fingerprint": "same-method",
            "comparison_method_evidence": {
                "status": "verified",
                "fingerprint": "same-method",
                "protocol": {
                    "functional": "PBE",
                    "dispersion": "D3",
                    "encut_eV": 450,
                    "kpoints_scheme": "Gamma",
                    "reference_mode": "single",
                    "energy_quantity": "E0",
                },
                "potcar_ids": {"Li": "Li_sv"},
                "u_by_element": {},
                "missing": [],
            },
            "method_consistency": {
                "status": "verified", "issues": [], "warnings": [],
            },
            "rows": [
                {
                    "name": config_names[0], "species": "Li2S8",
                    "state": "DONE", "delta_e": base,
                    "reference_valid": True,
                    "method_check": {"status": "verified"}, "note": "",
                },
                {
                    "name": config_names[1], "species": "Li2S8",
                    "state": "DONE", "delta_e": base + 0.08,
                    "reference_valid": True,
                    "method_check": {"status": "verified"}, "note": "",
                },
                {
                    "name": config_names[2], "species": "Li2S6",
                    "state": "DONE", "delta_e": -3.0 + project_index * 0.1,
                    "reference_valid": True,
                    "method_check": {"status": "verified"}, "note": "",
                },
                {
                    "name": config_names[3], "species": "Li2S4",
                    "state": "FAILED", "delta_e": None,
                    "reference_valid": False,
                    "source_job": str(root / "private" / "job.yaml"),
                    "method_check": {"status": "unverified"},
                    "note": (
                        f"failed at {root / 'private' / 'OUTCAR'}; "
                        "token=super-secret-value"),
                },
            ],
        }
        projects[project_path] = project
        summaries[project_uuid] = summary
        paths.append(project_path)
        for index, directory in enumerate((clean, reference, *configs)):
            manifests[directory] = {
                "job_uuid": f"job-{prefix}-{index}",
                "state": "FAILED" if directory == configs[-1] else "DONE",
                "results": {"energy_e0_eV": -100.0 - index},
            }

    adsorption = SimpleNamespace(
        list_projects=lambda: list(paths),
        load_project=lambda path: copy.deepcopy(projects.get(str(path))),
        delta_e_rows=lambda project: copy.deepcopy(
            summaries[str(project["project_uuid"])]),
    )
    manifest = SimpleNamespace(
        load_manifest=lambda path: copy.deepcopy(manifests.get(str(path))))
    api = Api(
        adsorption_mod=adsorption,
        manifest_mod=manifest,
        analysis_preferences_store=AnalysisPreferencesStore(
            tmp_path / "analysis-preferences.json"),
    )
    api._proj_fed = lambda *_args, **_kwargs: (None, "not available")
    return api, paths, projects


def _assert_public(payload, tmp_path):
    forbidden_keys = {
        "path", "project_path", "root", "dir", "directory", "locator",
        "source_job", "secret", "password", "token", "api_key",
    }

    def walk(value):
        if isinstance(value, dict):
            assert not (set(value) & forbidden_keys)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "super-secret-value" not in encoded


def test_bootstrap_and_single_project_all_stable_views_are_path_free(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])

    bootstrap = api.analysis_workbench_bootstrap(project_id)
    stable = api.analysis_workbench_preview(project_id, {
        "analysis_id": "adsorption-energy",
        "project_id": project_id,
        "data_mode": "stable",
        "near_degenerate_eV": 0.10,
        "precision": 4,
    })
    all_rows = api.analysis_workbench_preview(project_id, {
        "analysis_id": "adsorption-energy",
        "project_id": project_id,
        "data_mode": "all",
        "near_degenerate_eV": 0.10,
        "precision": 6,
    })

    assert bootstrap["ok"] is True
    assert bootstrap["default_spec"]["project_id"] == project_id
    assert bootstrap["catalog"]["schema"] == "vcstudio.analysis-catalog/v1"
    assert len(bootstrap["projects"]) == 3
    assert stable["ok"] is True and all_rows["ok"] is True
    assert stable["view"]["denominator"] == {
        "input_configurations": 4,
        "numeric_configurations": 3,
        "missing_configurations": 1,
        "species_with_numeric_results": 2,
        "visible_rows": 2,
        "near_degenerate_groups": 1,
    }
    assert len(all_rows["view"]["rows"]) == 4
    assert all_rows["view"]["denominator"]["visible_rows"] == 4
    numeric = next(
        row for row in all_rows["view"]["rows"]
        if row["delta_e_eV"] is not None)
    assert numeric["delta_e_display"].count(".") == 1
    assert len(numeric["delta_e_display"].split(".")[1]) == 6
    assert all(
        row["configuration_id"].startswith("job-a-")
        for row in all_rows["view"]["rows"])
    for response in (bootstrap, stable, all_rows, api.analysis_workbench_catalog()):
        _assert_public(response, tmp_path)


def test_multi_project_ids_are_server_resolved_with_baseline_and_sensitivity(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    ids = [api._workspace_project_id(path, projects[path]) for path in paths]
    called = []
    original = api._comparison_items

    def capture(resolved_paths, preset_key=None):
        called.append(list(resolved_paths))
        return original(resolved_paths, preset_key)

    api._comparison_items = capture
    response = api.analysis_workbench_preview(ids[0], {
        "analysis_id": "multi-project-comparison",
        "project_id": ids[0],
        "comparison_project_ids": ids,
        "data_mode": "stable",
        "baseline_project_id": ids[0],
        "missing_policy": "complete_cases",
        "sensitivity_deadbands_eV": [0.05, 0.20],
        "precision": 5,
    })

    assert response["ok"] is True
    assert called == [paths]
    assert response["view"]["matrix"]["project_ids"] == ids
    assert response["view"]["baseline"]["project_id"] == ids[0]
    assert len(response["view"]["baseline"]["deltas"]) == 3
    assert [item["deadband_eV"] for item in
            response["view"]["sensitivity"]["points"]] == [0.05, 0.20]
    assert response["view"]["denominator"]["selected_projects"] == 3
    _assert_public(response, tmp_path)


def test_comparison_unknown_duplicate_too_small_and_cross_binding_fail_closed(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    ids = [api._workspace_project_id(path, projects[path]) for path in paths]
    base = {
        "analysis_id": "multi-project-comparison",
        "project_id": ids[0],
    }

    unknown = api.analysis_workbench_preview(ids[0], {
        **base, "comparison_project_ids": [ids[0], "project-unknown"],
    })
    duplicate = api.analysis_workbench_preview(ids[0], {
        **base, "comparison_project_ids": [ids[0], ids[1], ids[1]],
    })
    too_small = api.analysis_workbench_preview(ids[0], {
        **base, "comparison_project_ids": [ids[0]],
    })
    cross_bound = api.analysis_workbench_preview(ids[0], {
        **base, "project_id": ids[1],
        "comparison_project_ids": [ids[0], ids[1]],
    })
    unknown_analysis = api.analysis_workbench_preview(ids[0], {
        "analysis_id": "not-a-real-analysis", "project_id": ids[0],
    })
    unknown_project = api.analysis_workbench_preview("project-unknown", {
        "analysis_id": "adsorption-energy", "project_id": ids[0],
    })

    for response in (
            unknown, duplicate, too_small, cross_bound, unknown_analysis,
            unknown_project):
        assert response["ok"] is False
        assert response["spec"] is None and response["view"] is None
        assert response["error"]
        _assert_public(response, tmp_path)
    assert "comparison_project_ids" in unknown["error"]
    assert "重复" in duplicate["error"]
    assert "至少" in too_small["error"]
    assert "binding mismatch" in cross_bound["error"]
    assert "unknown analysis_id" in unknown_analysis["error"]
    assert "workspace opaque" in unknown_project["error"]


def test_every_registered_analysis_bootstraps_with_real_or_blocked_view(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])
    catalog = api.analysis_workbench_catalog()
    analysis_ids = [item["id"] for item in catalog["analyses"]]

    results = {
        analysis_id: api.analysis_workbench_bootstrap(project_id, analysis_id)
        for analysis_id in analysis_ids
    }

    assert all(result["ok"] is True for result in results.values())
    assert results["adsorption-energy"]["view"]["scientific_status"] == "verified"
    assert results["multi-project-comparison"]["view"]["analysis_id"] == (
        "multi-project-comparison")
    for analysis_id in (
            "free-energy-path", "electronic-structure", "charge-wavefunction",
            "task-results", "property-calculators"):
        view = results[analysis_id]["view"]
        assert view["schema"] == "vcstudio.analysis-view/v1"
        assert view["analysis_id"] == analysis_id
        assert view["scientific_status"] == "unavailable"
        assert view["available"] is False
        assert view["rows"] == []
        assert view["blocking"] and view["reason"]
        assert view["denominator"] == {
            "available_results": 0, "visible_rows": 0,
        }
    for result in results.values():
        assert result["preferences"]["ok"] is True
        assert result["preference_revision"] == 0
        _assert_public(result, tmp_path)


def test_free_energy_bootstrap_and_preview_use_only_explicit_lis_server_chain(
        tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_path = paths[0]
    project_id = api._workspace_project_id(
        project_path, projects[project_path])
    projects[project_path]["work_mode"] = "lis"
    calls = []
    fed = {
        "steps": [
            {"label": "S8*", "G": 0.0, "source_path": str(tmp_path / "private")},
            {"label": "Li2S8*", "G": -0.1234567},
            {"label": "Li2S6*", "G": -0.75},
        ],
        "pds_index": 1,
        "u_l": 0.71234,
        "mu_li": -1.654321,
        "thermo_corrected": True,
        "temperature_K": 298.15,
        "thermo_correction_fingerprint": "thermo-v1",
        "method_consistency": {
            "status": "verified", "verified": True, "managed": True,
            "errors": [], "warnings": [],
        },
        "warnings": [],
        "source_job": str(tmp_path / "private" / "job.yaml"),
    }

    def authoritative(project, summary):
        calls.append((copy.deepcopy(project), copy.deepcopy(summary)))
        return copy.deepcopy(fed), None

    def preset_must_not_run(*_args, **_kwargs):
        raise AssertionError("free-energy workbench must not infer a reaction preset")

    api._proj_fed = authoritative
    api._proj_fed_preset = preset_must_not_run

    bootstrap = api.analysis_workbench_bootstrap(
        project_id, "free-energy-path")
    preview = api.analysis_workbench_preview(project_id, {
        "analysis_id": "free-energy-path",
        "project_id": project_id,
        "precision": 6,
    })

    assert bootstrap["ok"] is True and preview["ok"] is True
    assert len(calls) == 2
    assert all(project["work_mode"] == "lis" for project, _summary in calls)
    assert all(summary["slab"] == ("DONE", -100.0)
               for _project, summary in calls)
    assert bootstrap["view"]["scientific_status"] == "verified"
    assert bootstrap["view"]["steps"][1]["G_display"] == "-0.1235"
    assert preview["view"]["steps"][1]["G"] == -0.1234567
    assert preview["view"]["steps"][1]["G_display"] == "-0.123457"
    assert [item["label"] for item in preview["view"]["steps"]] == [
        "S8*", "Li2S8*", "Li2S6*"]
    assert preview["view"]["pds_index"] == 1
    assert preview["view"]["u_l"] == 0.71234
    assert preview["view"]["mu_li"] == -1.654321
    for response in (bootstrap, preview):
        _assert_public(response, tmp_path)


def test_free_energy_reaction_preset_without_lis_work_mode_stays_unavailable(
        tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_path = paths[0]
    project_id = api._workspace_project_id(
        project_path, projects[project_path])
    projects[project_path]["work_mode"] = "electrocat"
    projects[project_path]["reaction_preset"] = "LIS_16E"

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("reaction preset must not imply Li-S project mode")

    api._proj_fed = must_not_run
    bootstrap = api.analysis_workbench_bootstrap(
        project_id, "free-energy-path")
    preview = api.analysis_workbench_preview(project_id, {
        "analysis_id": "free-energy-path", "project_id": project_id,
    })

    for response in (bootstrap, preview):
        assert response["ok"] is True
        assert response["view"]["scientific_status"] == "unavailable"
        assert response["view"]["available"] is False
        assert response["view"]["rows"] == []
        _assert_public(response, tmp_path)


def test_analysis_preferences_bridge_validates_opaque_project_and_bootstrap_revision(
        tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])
    template = {
        "id": "my-energy-view",
        "name": "My energy view",
        "analysis_id": "adsorption-energy",
        "spec": {
            "analysis_id": "adsorption-energy",
            "project_id": project_id,
            "data_mode": "all",
            "precision": 7,
            "sort": {"key": "energy", "direction": "asc"},
        },
    }

    initial = api.analysis_preferences_read()
    saved = api.analysis_preferences_update(template, project_id, 0, True)
    rejected = api.analysis_preferences_update(
        {**template, "id": "must-not-save"}, "project-unknown", 1, False)
    favored = api.analysis_preferences_favorite(
        "adsorption-energy", True, saved["revision"])
    defaulted = api.analysis_preferences_set_default(
        "adsorption-energy", "stable-screen", favored["revision"])
    deleted = api.analysis_preferences_delete(
        "my-energy-view", defaulted["revision"])
    bootstrap = api.analysis_workbench_bootstrap(project_id)

    assert initial["ok"] is True and initial["revision"] == 0
    assert saved["ok"] is True and saved["revision"] == 1
    assert saved["preferences"]["templates"][0]["spec"]["precision"] == 7
    assert rejected["ok"] is False and rejected["revision"] is None
    assert favored["ok"] is True
    assert defaulted["preferences"]["default_template_by_analysis"] == {
        "adsorption-energy": "stable-screen",
    }
    assert deleted["ok"] is True
    assert deleted["preferences"]["templates"] == []
    assert bootstrap["preference_revision"] == deleted["revision"]
    assert bootstrap["preferences"]["preferences"] == deleted["preferences"]
    for result in (
            initial, saved, rejected, favored, defaulted, deleted, bootstrap):
        _assert_public(result, tmp_path)
