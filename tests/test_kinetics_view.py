"""Server-normalized Kinetic Dashboard view projections."""
from __future__ import annotations

from tests.test_kinetics import catmap_ready_network, frozen_network, valid_result
from vcstudio.project import kinetics
from vcstudio.project.analysis_registry import AnalysisSpec
from vcstudio.project.analysis_views import build_kinetic_view


def _spec(precision=4):
    return AnalysisSpec(
        analysis_id="kinetic-dashboard", project_id="project-" + "a" * 32,
        precision=precision,
    )


def _normalized(network):
    return kinetics.import_result(
        valid_result(network), network,
        expected_adapter={
            "id": "vcstudio.catmap-process-adapter", "version": "3",
            "tool_version": "0.4.0",
            "tool_sha256": "4" * 64,
        },
    )


def test_kinetic_view_formats_every_dashboard_metric_on_the_server():
    network = frozen_network()
    view = build_kinetic_view(
        network, kinetics.audit_network(network), _normalized(network),
        {"available": True, "name": "catmap.exe", "version": "0.4.0",
         "sha256": "4" * 64, "size": 123}, _spec())
    point = view["points"][0]

    assert view["analysis_id"] == "kinetic-dashboard"
    assert view["scientific_status"] == "diagnostic"
    assert view["available"] is True
    assert point["condition_display"] == "T=500.0000 K · p=1.0000 bar"
    assert point["tof"][0]["display"] == "2.5000"
    assert point["coverage"][0]["display"] == "0.3500"
    assert point["selectivity"][0]["display"] == "1.0000"
    assert point["drc"][0]["display"] == "0.9000"
    assert point["dsc"][0]["display"] == "0.1000"
    assert point["reaction_order"][0]["display"] == "0.8000"
    assert point["apparent_activation_energy"][0]["display"] == "0.7000"
    assert point["free_energy_diagram"][1]["display"] == "0.2000"
    assert point["convergence"]["residual_display"] == "1.0000e-12"
    assert view["kinetic_sensitivity"]["analyses"][0][
        "max_relative_change_display"] == "0.1200"
    assert view["limitations"]["diagnostic_only"] is True
    assert view["limitations"]["may_enter_accepted_or_final"] is False
    assert view["denominator"] == {
        "condition_points": 1, "converged_points": 1,
        "elementary_steps": 1, "species": 7, "visible_rows": 1,
    }


def test_kinetic_view_keeps_target_and_metric_axes_distinct():
    network = catmap_ready_network()
    view = build_kinetic_view(
        network, kinetics.audit_network(network), _normalized(network),
        {"available": True, "name": "catmap.exe", "version": "0.4.0",
         "sha256": "4" * 64, "size": 123}, _spec())
    point = view["points"][0]

    assert [
        (row["target_species_id"], row["step_id"], row["display"])
        for row in point["drc"]
    ] == [
        ("CO_s", "co_adsorption", "0.9000"),
        ("O2_s", "co_adsorption", "-0.2000"),
        ("CO_s", "o2_adsorption", "0.2500"),
        ("O2_s", "o2_adsorption", "0.7500"),
    ]
    assert [
        (row["target_species_id"], row["step_id"], row["display"])
        for row in point["dsc"]
    ] == [
        ("CO_s", "co_adsorption", "0.1000"),
        ("O2_s", "co_adsorption", "0.4000"),
        ("CO_s", "o2_adsorption", "-0.1000"),
        ("O2_s", "o2_adsorption", "0.6000"),
    ]
    assert [
        (row["target_species_id"], row["species_id"], row["display"])
        for row in point["reaction_order"]
    ] == [
        ("CO_s", "CO_g", "0.8000"),
        ("O2_s", "CO_g", "-0.3000"),
        ("CO_s", "O2_g", "0.2000"),
        ("O2_s", "O2_g", "0.6000"),
    ]
    assert point["completeness"]["drc"]["required_records"] == 4
    assert point["completeness"]["drc"]["duplicate_records"] == 0
    assert point["completeness"]["drc"]["complete"] is True


def test_kinetic_view_without_result_or_catmap_is_unavailable_but_keeps_audit():
    network = frozen_network()
    audit = kinetics.audit_network(network)
    view = build_kinetic_view(
        network, audit, None,
        {"available": False, "name": None, "version": None,
         "sha256": None, "size": None}, _spec())

    assert view["capability_status"] == "available"
    assert view["scientific_status"] == "unavailable"
    assert view["available"] is False
    assert view["points"] == []
    assert "CATMAP_UNAVAILABLE" in view["reason_codes"]
    assert "RESULT_NOT_IMPORTED" in view["reason_codes"]
    assert view["audit"]["input_sha256"] == network["input_sha256"]
    assert view["report_limitation"]["may_enter_accepted_or_final"] is False


def test_kinetic_view_never_projects_local_paths_or_credentials():
    network = frozen_network()
    network["extensions"] = {
        "private_path": r"C:\Users\chemist\secret\network.json",
        "token": "super-secret",
    }
    network["input_sha256"] = kinetics.compute_input_sha256(network)
    audit = kinetics.audit_network(network)
    # The extension is not copied into the view even when upstream carries it.
    view = build_kinetic_view(
        network, audit, None,
        {"available": False, "name": None, "version": None,
         "sha256": None, "size": None}, _spec())
    encoded = str(view)

    assert r"C:\Users\chemist" not in encoded
    assert "super-secret" not in encoded
    assert "extensions" not in view


def test_unconverged_server_result_keeps_rows_but_marks_dashboard_unavailable():
    network = frozen_network()
    result = valid_result(network)
    result["points"][0]["convergence"].update({
        "converged": True, "residual": 1.0e-4,
    })
    normalized = kinetics.import_result(
        result, network,
        expected_adapter={
            "id": "vcstudio.catmap-process-adapter", "version": "3",
            "tool_version": "0.4.0",
            "tool_sha256": "4" * 64,
        },
    )
    view = build_kinetic_view(
        network, kinetics.audit_network(network), normalized,
        {"available": True, "name": "catmap.exe", "version": "0.4.0",
         "sha256": "4" * 64, "size": 123}, _spec())

    assert view["available"] is False
    assert view["points"]
    assert view["points"][0]["convergence"]["status"] == "unconverged"
    assert "NUMERICAL_NOT_CONVERGED" in view["reason_codes"]


def test_confirmed_and_current_tool_identities_remain_separate_on_drift():
    network = frozen_network()
    frozen = {
        "available": True, "name": "catmap-a.exe", "version": "0.4.0",
        "sha256": "4" * 64, "size": 123,
    }
    configured = {
        "available": True, "name": "catmap-b.exe", "version": "0.5.0",
        "sha256": "5" * 64, "size": 456,
    }
    view = build_kinetic_view(
        network, kinetics.audit_network(network), None, frozen, _spec(),
        configured_tool=configured, tool_identity_matches=False)

    assert view["adapter"]["name"] == "catmap-a.exe"
    assert view["adapter"]["sha256"] == "4" * 64
    assert view["configured_adapter"]["name"] == "catmap-b.exe"
    assert view["configured_adapter"]["sha256"] == "5" * 64
    assert view["configured_adapter"]["matches_confirmed_export"] is False
    assert view["solver_status"] == "unavailable"
    assert view["available"] is False
