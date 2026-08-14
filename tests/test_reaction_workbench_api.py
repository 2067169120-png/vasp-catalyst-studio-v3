from __future__ import annotations

import copy

from tests.test_analysis_workbench import _analysis_api, _assert_public
from tests.test_reaction_workbench import projection


class _DomainSource:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def load_reaction_projection(self, *, project_id, project):
        self.calls.append((project_id, project["project_uuid"]))
        value = copy.deepcopy(self.value)
        value["project_id"] = project_id
        return value


def test_analysis_api_consumes_protocol_projection_and_returns_stable_schemas(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    source = _DomainSource(projection())
    api._reaction_domain_source = source
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])

    boot = api.analysis_workbench_bootstrap(project_id, "free-energy-path")

    assert boot["ok"] is True
    assert boot["view"]["schema"] == "vcstudio.reaction-workbench-view/v1"
    assert boot["view"]["graph"]["schema"] == "vcstudio.reaction-graph/v1"
    assert boot["view"]["ledger"]["schema"] == "vcstudio.thermochemistry-ledger/v1"
    assert boot["view"]["report_binding"]["schema"] == "vcstudio.reaction-report-binding/v1"
    assert boot["default_spec"]["conditions"] == {}
    assert source.calls and source.calls[0][0] == project_id
    _assert_public(boot, tmp_path)

    preview = api.analysis_workbench_preview(project_id, {
        "schema": "vcstudio.analysis-spec/v1", "analysis_id": "free-energy-path",
        "project_id": project_id, "data_mode": "stable", "precision": 5,
        "near_degenerate_eV": 0.15, "missing_policy": "show_missing",
        "conditions": {
            "temperature_k": 320.0, "pressure_pa": 200000.0,
            "ph": 1.0, "electrode_potential_v": 0.2, "coverage": 0.5,
        },
    })
    assert preview["ok"] is True
    revision = preview["view"]["condition_revision"]
    assert revision["artifact_status"] == "available"
    assert revision["conditions"]["temperature_k"] == 320.0
    assert preview["spec"]["conditions"]["coverage"] == 0.5
    _assert_public(preview, tmp_path)


def test_analysis_api_without_canonical_projection_fails_closed(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])

    boot = api.analysis_workbench_bootstrap(project_id, "free-energy-path")

    assert boot["ok"] is True
    assert boot["view"]["available"] is False
    assert boot["view"]["capability_status"] == "missing_prerequisite"
    assert "canonical reaction-domain projection" in boot["view"]["reason"]
    assert boot["catalog"]["analyses"][1]["activatable"] is False


def test_browser_conditions_are_bounded_and_cannot_submit_scientific_values(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    api._reaction_domain_source = _DomainSource(projection())
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])

    bad = api.analysis_workbench_preview(project_id, {
        "analysis_id": "free-energy-path", "project_id": project_id,
        "conditions": {"coverage": 1.5},
    })
    assert bad["ok"] is False
    assert "conditions.coverage" in bad["error"]

    injected = api.analysis_workbench_preview(project_id, {
        "analysis_id": "free-energy-path", "project_id": project_id,
        "conditions": {}, "final_delta_g_eV": -999.0,
    })
    assert injected["ok"] is False
    assert "unknown fields" in injected["error"]
