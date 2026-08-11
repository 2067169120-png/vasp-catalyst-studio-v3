from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from vcstudio.project.analysis_registry import (
    ANALYSIS_CATEGORIES,
    CATALOG_SCHEMA,
    SPEC_SCHEMA,
    AnalysisRequestError,
    analysis_catalog,
    builtin_view_templates,
    get_analysis,
    normalize_analysis_request,
)


PROJECT = "project-0123456789abcdef"


def test_catalog_unifies_analysis_and_task_capabilities_without_builder_locators():
    catalog = analysis_catalog()

    assert catalog["schema"] == CATALOG_SCHEMA
    assert catalog["categories"] == list(ANALYSIS_CATEGORIES)
    assert [item["id"] for item in catalog["analyses"]] == [
        "adsorption-energy",
        "free-energy-path",
        "task-results",
        "electronic-structure",
        "charge-wavefunction",
        "multi-project-comparison",
        "property-calculators",
    ]
    assert len(catalog["task_capabilities"]) == 23
    assert all("builder_ref" not in item for item in catalog["task_capabilities"])
    assert all(item["analysis"]["known"] is True
               for item in catalog["task_capabilities"])
    assert catalog["missing_policies"] == [
        {
            "id": "show_missing",
            "label_zh": "明确显示缺失",
            "label_en": "Show missing values",
        },
        {
            "id": "complete_cases",
            "label_zh": "仅完整案例",
            "label_en": "Complete cases only",
        },
    ]
    json.dumps(catalog, ensure_ascii=False, allow_nan=False)


def test_catalog_and_analysis_records_are_detached():
    catalog = analysis_catalog()
    catalog["analyses"][0]["outputs"].append("forged")
    record = get_analysis("adsorption-energy")
    record["sort_keys"].append("forged")

    assert "forged" not in analysis_catalog()["analyses"][0]["outputs"]
    assert "forged" not in get_analysis("adsorption-energy")["sort_keys"]


def test_catalog_only_advertises_sorting_for_the_implemented_adsorption_view():
    analyses = analysis_catalog()["analyses"]
    sortable = [item["id"] for item in analyses if "sort" in item["parameters"]]

    assert sortable == ["adsorption-energy"]
    assert get_analysis("adsorption-energy")["sort_keys"] == [
        "species", "energy", "name", "state"]
    assert all(item["sort_keys"] == [] for item in analyses
               if item["id"] != "adsorption-energy")


def test_default_adsorption_spec_is_canonical_and_semantically_hashed():
    first = normalize_analysis_request(None, project_id=PROJECT)
    second = normalize_analysis_request({}, project_id=PROJECT)

    assert first == second
    assert first.to_dict() == {
        "schema": SPEC_SCHEMA,
        "analysis_id": "adsorption-energy",
        "project_id": PROJECT,
        "comparison_project_ids": [],
        "data_mode": "stable",
        "near_degenerate_eV": 0.15,
        "precision": 4,
        "baseline_project_id": None,
        "missing_policy": "show_missing",
        "sort": {"key": "species", "direction": "asc"},
        "sensitivity_deadbands_eV": [0.1, 0.15, 0.2],
        "view_id": None,
    }
    assert len(first.semantic_sha256) == 64


@pytest.mark.parametrize("payload", [42, [], "adsorption-energy"])
def test_non_mapping_requests_raise_the_domain_error(payload):
    with pytest.raises(AnalysisRequestError, match="must be an object"):
        normalize_analysis_request(payload, project_id=PROJECT)


def test_comparison_spec_binds_baseline_and_canonical_project_order():
    spec = normalize_analysis_request({
        "analysis_id": "multi-project-comparison",
        "project_id": PROJECT,
        "comparison_project_ids": ["project-b", "project-a", "project-b"],
        "baseline_project_id": "project-a",
        "data_mode": "stable",
        "near_degenerate_eV": 0.22,
        "precision": 7,
        "missing_policy": "complete_cases",
        "sensitivity_deadbands_eV": [0.3, 0.1, 0.2, 0.1],
        "view_id": "team-screen-v2",
    }, project_id=PROJECT)

    assert spec.comparison_project_ids == (
        PROJECT, "project-b", "project-a")
    assert spec.baseline_project_id == "project-a"
    assert spec.sensitivity_deadbands_eV == (0.1, 0.2, 0.3)
    assert spec.data_mode == "stable"
    assert spec.sort_key == "species"
    assert spec.sort_direction == "asc"


def test_semantic_hash_changes_with_scientifically_visible_options():
    baseline = normalize_analysis_request({}, project_id=PROJECT)
    for patch in (
        {"data_mode": "all"},
        {"near_degenerate_eV": 0.25},
        {"precision": 8},
        {"missing_policy": "complete_cases"},
        {"sort": {"key": "energy", "direction": "desc"}},
    ):
        changed = normalize_analysis_request(patch, project_id=PROJECT)
        assert changed.semantic_sha256 != baseline.semantic_sha256

    comparison = normalize_analysis_request({
        "analysis_id": "multi-project-comparison",
        "comparison_project_ids": [PROJECT, "project-b"],
    }, project_id=PROJECT)
    sensitivity = normalize_analysis_request({
        "analysis_id": "multi-project-comparison",
        "comparison_project_ids": [PROJECT, "project-b"],
        "sensitivity_deadbands_eV": [0.05, 0.15, 0.25],
    }, project_id=PROJECT)
    assert sensitivity.semantic_sha256 != comparison.semantic_sha256


@pytest.mark.parametrize("analysis_id", [
    "free-energy-path",
    "task-results",
    "electronic-structure",
    "charge-wavefunction",
    "multi-project-comparison",
    "property-calculators",
])
def test_unimplemented_sort_is_rejected_and_cannot_change_semantic_identity(
        analysis_id):
    request = {"analysis_id": analysis_id}
    if analysis_id == "multi-project-comparison":
        request["comparison_project_ids"] = [PROJECT, "project-b"]
    canonical = normalize_analysis_request(request, project_id=PROJECT)

    with pytest.raises(AnalysisRequestError, match="does not support sorting"):
        normalize_analysis_request({
            **request,
            "sort": {"key": "species", "direction": "desc"},
        }, project_id=PROJECT)

    assert canonical.sort_key == "species"
    assert canonical.sort_direction == "asc"
    assert replace(
        canonical, sort_key="forged", sort_direction="desc",
    ).semantic_sha256 == canonical.semantic_sha256


@pytest.mark.parametrize("payload,match", [
    ({"unknown": True}, "unknown fields"),
    ({"schema": "vcstudio.analysis-spec/v9"}, "unsupported"),
    ({"project_id": "project-other"}, "binding mismatch"),
    ({"analysis_id": "unknown-analysis"}, "unknown analysis_id"),
    ({"data_mode": "minimum-ish"}, "data_mode"),
    ({"near_degenerate_eV": True}, "finite number"),
    ({"near_degenerate_eV": -0.1}, "between"),
    ({"near_degenerate_eV": 1.1}, "between"),
    ({"precision": 1}, "precision"),
    ({"precision": 9}, "precision"),
    ({"precision": 4.0}, "precision"),
    ({"missing_policy": "impute"}, "missing_policy"),
    ({"sort": {"key": "species"}}, "exactly"),
    ({"sort": {"key": "score", "direction": "asc"}}, "sort.key"),
    ({"sort": {"key": "species", "direction": "sideways"}}, "sort.direction"),
    ({"comparison_project_ids": ["project-b"]}, "only supported"),
    ({"baseline_project_id": "project-b"}, "does not support"),
    ({"sensitivity_deadbands_eV": []}, "1 to 16"),
    ({"sensitivity_deadbands_eV": [float("nan")]}, "between"),
])
def test_request_boundary_rejects_unknown_unsafe_or_unimplemented_choices(
        payload, match):
    with pytest.raises(AnalysisRequestError, match=match):
        normalize_analysis_request(payload, project_id=PROJECT)


@pytest.mark.parametrize("value", [
    r"C:\private\project",
    "/mnt/private/project",
    "../project",
    "file:///tmp/project",
    "github_pat_abcdefghijklmnopqrstuvwxyz123456",
    "Bearer private-session-token",
])
def test_request_identifiers_never_accept_paths_or_credentials(value):
    with pytest.raises(AnalysisRequestError):
        normalize_analysis_request({"view_id": value}, project_id=PROJECT)


def test_comparison_baseline_must_belong_to_frozen_project_set():
    with pytest.raises(AnalysisRequestError, match="must be in"):
        normalize_analysis_request({
            "analysis_id": "multi-project-comparison",
            "comparison_project_ids": ["project-b"],
            "baseline_project_id": "project-c",
        }, project_id=PROJECT)


def test_non_sensitivity_analysis_rejects_sensitivity_controls():
    with pytest.raises(AnalysisRequestError, match="does not support sensitivity"):
        normalize_analysis_request({
            "analysis_id": "electronic-structure",
            "sensitivity_deadbands_eV": [0.1, 0.2],
        }, project_id=PROJECT)


def test_builtin_view_templates_are_detached_and_only_use_supported_values():
    templates = builtin_view_templates()
    assert [item["id"] for item in templates] == [
        "stable-screen", "all-configurations", "method-audit",
        "robust-comparison",
    ]
    copy_for_mutation = copy.deepcopy(templates)
    copy_for_mutation[0]["values"]["precision"] = 2
    assert builtin_view_templates()[0]["values"]["precision"] == 4

    for template in templates:
        values = dict(template["values"])
        values["analysis_id"] = template["analysis_id"]
        if template["analysis_id"] == "multi-project-comparison":
            values["comparison_project_ids"] = [PROJECT, "project-b"]
        spec = normalize_analysis_request(values, project_id=PROJECT)
        assert spec.analysis_id == template["analysis_id"]
