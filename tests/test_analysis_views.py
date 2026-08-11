from __future__ import annotations

import copy
import json

import pytest

from vcstudio.project.analysis_registry import normalize_analysis_request
from vcstudio.project.analysis_views import (
    VIEW_SCHEMA,
    build_adsorption_view,
    build_comparison_view,
    build_free_energy_view,
)


PROJECT = "project-a"


def _adsorption_summary():
    return {
        "has_ref": True,
        "reference_mode": "single",
        "method_consistency": {
            "status": "verified", "issues": [], "warnings": []},
        "rows": [
            {
                "name": "Li2S8-top", "configuration_id": "config-top",
                "species": "Li2S8", "state": "DONE", "delta_e": -1.23456789,
                "reference_valid": True,
                "method_check": {"status": "verified"},
            },
            {
                "name": "Li2S8-bridge", "configuration_id": "config-bridge",
                "species": "Li2S8", "state": "DONE", "delta_e": -1.15,
                "reference_valid": True,
                "method_check": {"status": "verified"},
            },
            {
                "name": "Li2S8-hollow", "configuration_id": "config-hollow",
                "species": "Li2S8", "state": "DONE", "delta_e": -0.8,
                "reference_valid": True,
                "method_check": {"status": "unverified", "warnings": ["KPOINTS unknown"]},
            },
            {
                "name": "Li2S-missing", "configuration_id": "config-missing",
                "species": "Li2S", "state": "FAILED", "delta_e": None,
                "reference_valid": True, "note": "OUTCAR missing",
                "method_check": {"status": "unverified"},
            },
        ],
    }


def test_stable_adsorption_view_preserves_near_degenerate_candidates_and_denominator():
    spec = normalize_analysis_request({
        "data_mode": "stable",
        "near_degenerate_eV": 0.10,
        "precision": 6,
    }, project_id=PROJECT)

    view = build_adsorption_view(_adsorption_summary(), spec)

    assert view["schema"] == VIEW_SCHEMA
    assert view["scientific_status"] == "verified"
    assert view["rows"][0]["configuration_id"] == "config-top"
    assert view["rows"][0]["delta_e_eV"] == -1.23456789
    assert view["rows"][0]["delta_e_display"] == "-1.234568"
    assert view["rows"][0]["near_degenerate"] is True
    assert [item["configuration_id"] for item in view["rows"][0]["co_minima"]] == [
        "config-bridge"]
    assert view["denominator"] == {
        "input_configurations": 4,
        "numeric_configurations": 3,
        "missing_configurations": 1,
        "species_with_numeric_results": 1,
        "visible_rows": 1,
        "near_degenerate_groups": 1,
    }
    assert view["missing"][0]["reason"] == "OUTCAR missing"
    assert len(view["data_fingerprint"]) == 64


def test_all_adsorption_view_shows_missing_without_imputation_and_keeps_raw_numbers():
    spec = normalize_analysis_request({
        "data_mode": "all",
        "missing_policy": "show_missing",
        "sort": {"key": "energy", "direction": "asc"},
        "precision": 2,
    }, project_id=PROJECT)

    view = build_adsorption_view(_adsorption_summary(), spec)

    assert len(view["rows"]) == 4
    assert view["rows"][0]["delta_e_eV"] == -1.23456789
    assert view["rows"][0]["delta_e_display"] == "-1.23"
    missing = next(row for row in view["rows"]
                   if row["configuration_id"] == "config-missing")
    assert missing["delta_e_eV"] is None
    assert missing["delta_e_display"] == "—"
    assert missing["note"] == "OUTCAR missing"


def test_complete_case_adsorption_view_hides_missing_but_preserves_denominator():
    spec = normalize_analysis_request({
        "data_mode": "all", "missing_policy": "complete_cases",
    }, project_id=PROJECT)

    view = build_adsorption_view(_adsorption_summary(), spec)

    assert len(view["rows"]) == 3
    assert view["denominator"]["missing_configurations"] == 1
    assert len(view["missing"]) == 1


def test_incompatible_numeric_row_never_enters_scientific_adsorption_values():
    summary = _adsorption_summary()
    summary["rows"][0]["method_check"] = {
        "status": "incompatible", "issues": ["ENCUT mismatch"]}
    spec = normalize_analysis_request({"data_mode": "all"}, project_id=PROJECT)

    view = build_adsorption_view(summary, spec)

    assert all(row["configuration_id"] != "config-top" or row["delta_e_eV"] is None
               for row in view["rows"])
    blocked = next(item for item in view["missing"]
                   if item["configuration_id"] == "config-top")
    assert blocked["reason"] == "方法证据不兼容"


def _free_energy_spec(**patch):
    request = {
        "analysis_id": "free-energy-path",
        "precision": 3,
    }
    request.update(patch)
    return normalize_analysis_request(request, project_id=PROJECT)


def _free_energy_frozen():
    return {
        "result": {
            "steps": [
                {"label": "S8*", "G": 0.0, "sub_label": ""},
                {"label": "Li2S8*", "G": -0.1234567, "sub_label": ""},
                {"label": "Li2S6*", "G": -0.75, "sub_label": "1×Li2S"},
            ],
            "pds_index": 1,
            "u_l": 0.71234,
            "mu_li": -1.654321,
            "thermo_corrected": True,
            "temperature_K": 298.15,
            "thermo_correction_fingerprint": "thermo-v1",
            "reference": "Li/Li+",
            "reaction_path_id": "lis-16e-server-declared",
            "method_consistency": {
                "status": "verified", "verified": True, "managed": True,
                "errors": [], "warnings": [],
            },
            "warnings": [],
        },
        "missing": [],
        "method_consistency": {
            "status": "unverified", "warnings": ["summary fallback unused"]},
    }


def test_free_energy_view_preserves_authoritative_order_raw_values_and_precision():
    frozen = _free_energy_frozen()
    original = copy.deepcopy(frozen)

    view = build_free_energy_view(frozen, _free_energy_spec())
    direct = build_free_energy_view(frozen["result"], _free_energy_spec())

    assert frozen == original
    assert direct == view
    assert view["schema"] == VIEW_SCHEMA
    assert view["analysis_id"] == "free-energy-path"
    assert view["available"] is True
    assert view["scientific_status"] == "verified"
    assert [step["label"] for step in view["steps"]] == [
        "S8*", "Li2S8*", "Li2S6*"]
    assert [step["G"] for step in view["steps"]] == [
        0.0, -0.1234567, -0.75]
    assert [step["G_display"] for step in view["steps"]] == [
        "0.000", "-0.123", "-0.750"]
    assert view["rows"] == view["steps"]
    assert view["pds_index"] == 1
    assert view["pds"] == {
        "index": 1, "from_label": "Li2S8*", "to_label": "Li2S6*"}
    assert view["u_l"] == 0.71234 and view["u_l_display"] == "0.712"
    assert view["mu_li"] == -1.654321
    assert view["mu_li_display"] == "-1.654"
    assert view["thermo"] == {
        "corrected": True,
        "status": "corrected",
        "temperature_K": 298.15,
        "temperature_display": "298.150",
        "correction_fingerprint": "thermo-v1",
    }
    assert view["method_status"] == "verified"
    assert view["method"]["verified"] is True
    assert view["missing"] == []
    assert view["blocking"] == []
    assert view["warnings"] == []
    assert view["denominator"] == {
        "requested_paths": 1,
        "available_paths": 1,
        "input_steps": 3,
        "numeric_steps": 3,
        "missing_steps": 0,
        "input_transitions": 2,
        "valid_transitions": 2,
        "visible_rows": 3,
    }


def test_free_energy_view_missing_and_non_finite_results_fail_closed():
    reason = "缺吸附态能量:Li2S4(需 slab+X 的 DONE 能量)"
    missing = build_free_energy_view({
        "result": None,
        "missing": [reason],
        "method_consistency": {"status": "verified"},
    }, _free_energy_spec())

    assert missing["available"] is False
    assert missing["scientific_status"] == "blocked"
    assert missing["steps"] == [] and missing["rows"] == []
    assert missing["missing"] == [reason]
    assert missing["blocking"] == [reason]
    assert missing["warnings"] == []
    assert missing["denominator"] == {
        "requested_paths": 1,
        "available_paths": 0,
        "input_steps": 0,
        "numeric_steps": 0,
        "missing_steps": 0,
        "input_transitions": 0,
        "valid_transitions": 0,
        "visible_rows": 0,
    }

    non_finite_source = _free_energy_frozen()
    non_finite_source["result"]["steps"][1]["G"] = float("nan")
    non_finite = build_free_energy_view(non_finite_source, _free_energy_spec())
    assert non_finite["available"] is False
    assert non_finite["steps"] == [] and non_finite["pds_index"] is None
    assert non_finite["u_l"] is None and non_finite["mu_li"] is None
    assert non_finite["missing"] == ["steps[1].G 缺少有限数值"]
    assert non_finite["blocking"] == non_finite["missing"]
    assert non_finite["denominator"]["input_steps"] == 3
    assert non_finite["denominator"]["numeric_steps"] == 2
    assert non_finite["denominator"]["missing_steps"] == 1
    json.dumps(non_finite, ensure_ascii=False, allow_nan=False)


def test_free_energy_view_keeps_method_unverified_distinct_from_missing():
    frozen = _free_energy_frozen()
    frozen["result"]["method_consistency"] = {
        "status": "unverified", "verified": False, "managed": True,
        "errors": [], "warnings": ["跨项目 ENCUT 证据不完整"],
    }
    frozen["result"]["warnings"] = ["跨项目 ENCUT 证据不完整"]

    view = build_free_energy_view(frozen, _free_energy_spec())

    assert view["available"] is True
    assert view["scientific_status"] == "unverified"
    assert view["method_status"] == "unverified"
    assert view["method"]["verified"] is False
    assert view["method"]["warnings"] == ["跨项目 ENCUT 证据不完整"]
    assert view["warnings"] == ["跨项目 ENCUT 证据不完整"]
    assert view["missing"] == [] and view["blocking"] == []


def test_free_energy_view_scrubs_paths_secrets_and_unprojected_private_fields():
    frozen = _free_energy_frozen()
    frozen["result"].update({
        "source_job": r"C:\private\molecules\job.yaml",
        "thermo_meta": {
            "source_path": "/srv/private/frequencies.json",
            "token": "super-secret-value",
        },
        "reaction_path_id": r"C:\private\reaction.json",
        "warnings": [
            r"read C:\private\OUTCAR; token=super-secret-value"],
    })
    frozen["result"]["steps"][0]["source_path"] = "/mnt/private/OSZICAR"

    view = build_free_energy_view(frozen, _free_energy_spec())
    encoded = json.dumps(view, ensure_ascii=False)

    assert "C:\\private" not in encoded
    assert "/srv/private" not in encoded
    assert "/mnt/private" not in encoded
    assert "super-secret-value" not in encoded
    assert "source_job" not in encoded
    assert "thermo_meta" not in encoded
    assert view["reaction_path_id"] == "<local-path>"
    assert view["warnings"] == ["<redacted>"]


def test_free_energy_view_fingerprint_is_deterministic_and_spec_bound():
    frozen = _free_energy_frozen()
    reordered_result = {
        key: copy.deepcopy(value)
        for key, value in reversed(list(frozen["result"].items()))
    }
    reordered_result["steps"] = [
        {key: value for key, value in reversed(list(step.items()))}
        for step in reordered_result["steps"]
    ]
    reordered = {
        "method_consistency": copy.deepcopy(frozen["method_consistency"]),
        "missing": [],
        "result": reordered_result,
    }

    first = build_free_energy_view(frozen, _free_energy_spec())
    second = build_free_energy_view(reordered, _free_energy_spec())
    other_precision = build_free_energy_view(
        frozen, _free_energy_spec(precision=6))

    assert first == second
    assert len(first["data_fingerprint"]) == 64
    assert first["data_fingerprint"] != other_precision["data_fingerprint"]


def _fed(offset=0.0):
    return {
        "steps": [
            {"label": "S8*", "G": 0.0 + offset},
            {"label": "Li2S4*", "G": -0.5 + offset},
            {"label": "Li2S*", "G": -1.1 + offset},
        ],
        "pds_index": 1,
        "u_l": 1.2,
        "thermo_corrected": True,
        "temperature_K": 298.15,
        "thermo_correction_fingerprint": "thermo-v1",
        "reference": "Li/Li+",
        "reaction_path_id": "lis-v1",
    }


def _comparison_item(project_id, name, values, offset=0.0):
    rows = []
    for species, energy in values.items():
        rows.append({
            "name": f"{name}-{species}",
            "species": species,
            "state": "DONE",
            "delta_e": energy,
            "reference_valid": True,
            "method_check": {"status": "verified"},
        })
    evidence = {
        "status": "verified",
        "fingerprint": "same-method",
        "protocol": {
            "functional": "PBE", "dispersion": "D3", "encut_eV": 520.0,
            "kpoints_scheme": "Gamma 3x3x1", "reference_mode": "single",
            "energy_quantity": "E0",
        },
        "potcar_ids": {"Li": "Li_sv", "S": "S"},
        "u_by_element": {"Li": {"enabled": False}, "S": {"enabled": False}},
        "source_job": r"C:\private\must-not-leak",
        "missing": [],
    }
    return {
        "project_id": project_id,
        "path": r"C:\private\ignored",
        "project": {
            "name": name,
            "project_uuid": project_id,
            "comparison_method_fingerprint": "same-method",
            "comparison_method_evidence": evidence,
        },
        "summary": {
            "has_ref": True,
            "reference_mode": "single",
            "method_consistency": {
                "status": "verified", "issues": [], "warnings": []},
            "comparison_method_evidence": evidence,
            "rows": rows,
        },
        "fed": _fed(offset),
    }


def _items():
    return [
        _comparison_item("project-a", "Fe", {
            "Li2S8": -1.0, "Li2S": -2.8}),
        _comparison_item("project-b", "Co", {
            "Li2S8": -1.08, "Li2S": -2.7}, 0.1),
        _comparison_item("project-c", "Ni", {
            "Li2S8": -0.7}, 0.2),
    ]


def _comparison_spec(**patch):
    data = {
        "analysis_id": "multi-project-comparison",
        "comparison_project_ids": ["project-a", "project-b", "project-c"],
        "baseline_project_id": "project-a",
        "precision": 4,
        "near_degenerate_eV": 0.15,
        "sensitivity_deadbands_eV": [0.05, 0.10, 0.15],
    }
    data.update(patch)
    return normalize_analysis_request(data, project_id="project-a")


def test_comparison_view_exposes_baseline_method_matrix_sensitivity_and_denominators():
    items = _items()
    original = copy.deepcopy(items)

    view = build_comparison_view(items, _comparison_spec())

    assert items == original
    assert view["schema"] == VIEW_SCHEMA
    assert view["scientific_status"] == "verified"
    assert view["matrix"]["project_ids"] == [
        "project-a", "project-b", "project-c"]
    assert view["matrix"]["cols"] == ["Li2S8", "Li2S"]
    assert view["matrix"]["display_values"][0] == ["-1.0000", "-2.8000"]
    baseline_b = next(item for item in view["baseline"]["deltas"]
                      if item["project_id"] == "project-b")
    assert baseline_b["values_eV"] == pytest.approx([-0.08, 0.1])
    assert baseline_b["display_values"] == ["-0.0800", "0.1000"]
    assert all(record["functional"] == "PBE" for record in view["method_matrix"])
    assert view["denominator"] == {
        "selected_projects": 3,
        "ready_projects": 3,
        "blocked_projects": 0,
        "species_columns": 2,
        "possible_numeric_cells": 6,
        "observed_numeric_cells": 5,
        "missing_numeric_cells": 1,
    }
    first_point = view["sensitivity"]["points"][0]
    li2s8 = next(item for item in first_point["lowest_energy_sets"]
                 if item["species"] == "Li2S8")
    assert li2s8["within_deadband_project_ids"] == ["project-b"]
    last_point = view["sensitivity"]["points"][-1]
    li2s8 = next(item for item in last_point["lowest_energy_sets"]
                 if item["species"] == "Li2S8")
    assert li2s8["within_deadband_project_ids"] == ["project-a", "project-b"]
    encoded = json.dumps(view, ensure_ascii=False)
    assert "C:\\private" not in encoded
    assert "source_job" not in encoded
    assert len(view["data_fingerprint"]) == 64


def test_complete_case_comparison_keeps_missing_denominator_but_filters_columns():
    view = build_comparison_view(
        _items(), _comparison_spec(missing_policy="complete_cases"))

    assert view["matrix"]["cols"] == ["Li2S8"]
    assert view["denominator"]["species_columns"] == 1
    assert view["denominator"]["missing_numeric_cells"] == 0
    assert view["projects"][2]["species"][0]["species"] == "Li2S8"


def test_comparison_view_rejects_missing_server_identity_and_unready_baseline():
    with pytest.raises(ValueError, match="identity is unavailable"):
        build_comparison_view(_items()[:2], _comparison_spec())

    blocked = _items()
    blocked[0]["summary"]["has_ref"] = False
    blocked[0]["summary"]["reference_mode"] = "none"
    blocked[0]["summary"]["rows"][0]["reference_valid"] = False
    with pytest.raises(ValueError, match="baseline project is not ready"):
        build_comparison_view(blocked, _comparison_spec())


def test_view_functions_require_the_matching_analysis_kind():
    adsorption = normalize_analysis_request(
        {"analysis_id": "adsorption-energy"}, project_id=PROJECT)
    comparison = _comparison_spec()

    with pytest.raises(ValueError, match="comparison view"):
        build_comparison_view(_items(), adsorption)
    with pytest.raises(ValueError, match="adsorption view"):
        build_adsorption_view(_adsorption_summary(), comparison)
