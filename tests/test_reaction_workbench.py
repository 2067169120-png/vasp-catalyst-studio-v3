from __future__ import annotations

import copy

import pytest

from vcstudio.project import reaction_workbench as rw


H = {
    "method": "1" * 64,
    "structure": "2" * 64,
    "evidence": "3" * 64,
    "term": "4" * 64,
    "reference": "5" * 64,
    "standard": "6" * 64,
    "frequency": "7" * 64,
    "mode": "8" * 64,
    "response": "9" * 64,
    "object": "a" * 64,
}


def _evidence(opaque_id="ev-1"):
    return [{
        "ref_type": "calculation_result", "opaque_id": opaque_id,
        "origin": "observed", "revision_id": None,
    }]


def _method():
    return {
        "schema": "vcstudio.method-fingerprint/v1", "method_id": "pbe-d3",
        "scope": "periodic-dft", "sha256": H["method"],
        "evidence_refs": _evidence("method-1"),
    }


def _envelope(object_type, object_id, payload):
    return {
        "schema": "vcstudio.catalysis-domain-envelope/v1",
        "object_type": object_type, "object_id": object_id,
        "object_version": "1.0.0", "payload": payload,
        "semantic_sha256": rw.semantic_sha256(payload),
        "job_source_of_truth": "job.yaml", "authorizes_execution": False,
    }


def _surface():
    return _envelope("CatalystSurface", "surface-1", {
        "schema": "vcstudio.catalyst-surface/v1", "model_version": "1.0.0",
        "surface_id": "surface-1", "composition": "Pt", "miller_indices": [1, 1, 1],
        "termination_id": None, "geometric_site_ids": ["top"],
        "provenance": "observed", "evidence_refs": _evidence("surface-evidence"),
        "method_fingerprint": _method(),
    })


def _state(state_id, formula):
    return _envelope("AdsorbateState", state_id, {
        "schema": "vcstudio.adsorbate-state/v1", "model_version": "1.0.0",
        "state_id": state_id, "surface_id": "surface-1",
        "adsorbate_id": f"ads-{state_id}", "chemical_formula": formula,
        "geometric_site_id": "top", "charge": 0, "multiplicity": 1,
        "provenance": "observed", "evidence_refs": _evidence(f"ev-{state_id}"),
        "method_fingerprint": _method(),
    })


def _transition_state():
    return _envelope("TransitionState", "state-ts", {
        "schema": "vcstudio.transition-state/v1", "model_version": "1.0.0",
        "transition_state_id": "state-ts", "surface_id": "surface-1",
        "chemical_formula": "RTS", "provenance": "observed",
        "evidence_refs": _evidence("ev-state-ts"),
        "method_fingerprint": _method(),
    })


def _condition():
    return _envelope("ConditionSet", "condition-1", {
        "schema": "vcstudio.condition-set/v1", "model_version": "1.0.0",
        "condition_set_id": "condition-1", "temperature_k": 300.0,
        "pressure_pa": 100000.0, "ph": 0.0, "electrode_potential_v": 0.0,
        "coverage": 0.25,
        "provenance": "observed", "evidence_refs": _evidence("condition-evidence"),
        "method_fingerprint": _method(),
    })


def _step():
    return _envelope("ElementaryStep", "step-1", {
        "schema": "vcstudio.elementary-step/v1", "model_version": "1.0.0",
        "step_id": "step-1", "reactant_state_ids": ["state-r"],
        "product_state_ids": ["state-p"], "transition_state_id": "state-ts",
        "condition_set_id": "condition-1", "reversible": True,
        "provenance": "observed", "evidence_refs": _evidence("step-evidence"),
        "method_fingerprint": _method(),
    })


def _network():
    return _envelope("ReactionNetwork", "network-1", {
        "schema": "vcstudio.reaction-network/v1", "model_version": "1.0.0",
        "network_id": "network-1", "surface_ids": ["surface-1"],
        "state_ids": ["state-r", "state-p"],
        "step_ids": ["step-1"], "condition_set_ids": ["condition-1"],
        "provenance": "observed", "evidence_refs": _evidence("network-evidence"),
        "method_fingerprint": _method(),
    })


def _term(value, model):
    return {"value": value, "evidence_sha256": H["term"], "model": model}


def _thermo(e0, *, ts=False):
    response = {
        "base_conditions": {
            "temperature_k": 300.0, "pressure_pa": 100000.0, "ph": 0.0,
            "electrode_potential_v": 0.0, "coverage": 0.25,
        },
        "joint_model": {
            "model": "additive", "evidence_sha256": H["response"],
        },
    }
    for parameter, slope in (
        ("temperature_k", -0.001), ("ph", 0.02),
        ("electrode_potential_v", -1.0), ("coverage", 0.4),
    ):
        response[parameter] = {
            "model": "local_linear", "slope_eV_per_unit": slope,
            "evidence_sha256": H["response"],
        }
    response["pressure_pa"] = {
        "model": "ideal_gas_log", "coefficient_eV": 0.025,
        "evidence_sha256": H["response"],
    }
    return {
        "electronic_energy_e0_eV": _term(e0, "electronic_energy"),
        "zpe_eV": _term(0.10, "harmonic"),
        "delta_h_thermal_eV": _term(0.05, "harmonic"),
        "minus_t_delta_s_eV": _term(-0.03, "harmonic"),
        "standard_state_correction_eV": _term(0.01, "ideal_gas"),
        "temperature_k": 300.0, "pressure_pa": 100000.0,
        "models": {
            "electronic": "electronic_energy",
            "translation": "ideal_gas", "rotation": "rigid_rotor",
            "vibration": "harmonic", "low_frequency": "quasi_harmonic",
            "standard_state": "ideal_gas",
        },
        "standard_state": {
            "kind": "1-bar", "value": 100000.0, "unit": "Pa",
            "evidence_sha256": H["standard"],
        },
        "reference_state_sha256": H["reference"],
        "condition_set_id": "condition-1",
        "condition_set_sha256": _condition()["semantic_sha256"],
        "ph": 0.0, "electrode_potential_v": 0.0, "coverage": 0.25,
        "solvent_model_sha256": None, "coverage_model_sha256": None,
        "low_frequency": {
            "original_frequencies_cm1": [18.0, 42.0, 120.0],
            "rule": "quasi_harmonic", "cutoff_cm1": 50.0,
            "reason": "bounded entropy sensitivity audit",
            "evidence_sha256": H["term"],
            "sensitivity": [{
                "parameter": "cutoff", "value": 50.0, "unit": "cm-1",
                "delta_g_eV": 0.012, "evidence_sha256": H["term"],
            }],
        },
        "frequency_evidence": ({
            "context": "ts", "imaginary_frequencies_cm1": [-420.0, -12.0],
            "noise_threshold_cm1": 50.0, "sha256": H["frequency"],
            "mode_evidence_sha256": H["mode"],
            "mode_alignment_status": "confirmed",
        } if ts else {
            "context": "minimum", "imaginary_frequencies_cm1": [],
            "noise_threshold_cm1": 50.0, "sha256": H["frequency"],
            "mode_evidence_sha256": H["mode"],
            "mode_alignment_status": "confirmed",
        }),
        "condition_response": response,
    }


def projection():
    bindings = {}
    for object_id, label in (
        ("surface-1", "Pt(111)"), ("state-r", "R*"),
        ("state-ts", "TS*"), ("state-p", "P*"), ("step-1", "R to P"),
        ("network-1", "Network"), ("condition-1", "300 K, 1 bar"),
    ):
        bindings[object_id] = {
            "label": label, "structure_sha256": H["structure"],
            "evidence_sha256": H["evidence"], "scientific_status": "machine_pass",
        }
    bindings["state-r"]["thermochemistry"] = _thermo(-10.0)
    bindings["state-ts"]["thermochemistry"] = _thermo(-9.0, ts=True)
    bindings["state-p"]["thermochemistry"] = _thermo(-10.5)
    return {
        "schema": rw.PROJECTION_SCHEMA, "project_id": "project-1",
        "network": _network(), "surfaces": [_surface()],
        "states": [_state("state-r", "R"), _state("state-p", "P")],
        "transition_states": [_transition_state()],
        "steps": [_step()], "conditions": [_condition()], "bindings": bindings,
        "applicability": {
            "temperature_k": {"minimum": 250.0, "maximum": 500.0,
                              "evidence_sha256": H["response"]},
            "pressure_pa": {"minimum": 1000.0, "maximum": 1e7,
                            "evidence_sha256": H["response"]},
            "ph": {"minimum": 0.0, "maximum": 14.0,
                   "evidence_sha256": H["response"]},
            "electrode_potential_v": {"minimum": -2.0, "maximum": 2.0,
                                      "evidence_sha256": H["response"]},
            "coverage": {"minimum": 0.0, "maximum": 1.0,
                         "evidence_sha256": H["response"]},
        },
    }


def test_reaction_graph_ledger_condition_and_report_closed_loop():
    view = rw.build_reaction_workbench_view(
        projection(), project_id="project-1", precision=4,
        conditions={
            "temperature_k": 320.0, "pressure_pa": 200000.0, "ph": 1.0,
            "electrode_potential_v": 0.2, "coverage": 0.5,
        })

    assert view["schema"] == rw.VIEW_SCHEMA
    assert view["graph"]["schema"] == rw.GRAPH_SCHEMA
    assert view["ledger"]["schema"] == rw.LEDGER_SCHEMA
    assert view["condition_revision"]["schema"] == rw.DERIVED_REVISION_SCHEMA
    assert view["report_binding"]["schema"] == rw.REPORT_BINDING_SCHEMA
    assert view["graph"]["mechanism_complete"] is False
    assert view["graph"]["edges"][0]["thermochemistry"]["reaction_delta_g_eV"] == -0.5
    assert view["graph"]["edges"][0]["thermochemistry"]["activation_delta_g_eV"] == 1.0
    ts = next(row for row in view["ledger"]["rows"] if row["entity_id"] == "state-ts")
    assert ts["frequency_qualification"]["kinetic_qualification"] == "frequency_mode_supported"
    assert ts["low_frequency"]["original_frequencies_cm1"] == [18.0, 42.0, 120.0]
    assert ts["low_frequency"]["rule"] == "quasi_harmonic"
    revision = view["condition_revision"]
    assert revision["artifact_status"] == "available"
    assert revision["source_evidence_mutated"] is False
    assert revision["revision_id"].startswith("derived-")
    assert len(revision["revision_sha256"]) == 64
    assert len(view["report_binding"]["tables"]) == 2
    assert view["report_binding"]["figures"][0]["kind"] == "reaction_map"
    assert view["frozen_network"]["schema"] == rw.FROZEN_NETWORK_SCHEMA
    assert view["frozen_network"]["microkinetics_ready"] is True
    frozen_edge = view["frozen_network"]["edges"][0]
    assert frozen_edge["stoichiometry"] == {"state-r": -1.0, "state-p": 1.0}
    assert frozen_edge["reaction_delta_g_eV"] is not None
    assert frozen_edge["activation_delta_g_eV"] is not None


def test_deterministic_hashes_and_projection_is_not_mutated():
    source = projection()
    frozen = copy.deepcopy(source)
    left = rw.build_reaction_workbench_view(source, project_id="project-1")
    right = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert left == right
    assert source == frozen
    assert left["data_fingerprint"] == right["data_fingerprint"]
    assert left["graph"]["graph_sha256"] == right["graph"]["graph_sha256"]


def test_missing_edge_and_thermochemistry_remain_explicit():
    source = projection()
    source["network"]["payload"]["step_ids"].append("step-missing")
    source["network"]["semantic_sha256"] = rw.semantic_sha256(
        source["network"]["payload"])
    source["bindings"]["state-p"].pop("thermochemistry")
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert any(item["edge_id"] == "step-missing" for item in view["graph"]["missing_edges"])
    product = next(row for row in view["ledger"]["rows"] if row["entity_id"] == "state-p")
    assert product["artifact_status"] == "unavailable"
    assert product["final_delta_g_eV"] is None
    assert "electronic_energy_e0_eV" in product["missing"]


def test_incompatible_reference_or_standard_state_is_never_mixed():
    source = projection()
    thermo = source["bindings"]["state-p"]["thermochemistry"]
    thermo["reference_state_sha256"] = "b" * 64
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge_thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert edge_thermo["status"] == "incomplete"
    assert edge_thermo["reaction_delta_g_eV"] is None
    assert "incompatible_method_reference_standard_state_condition_solvent_or_coverage" in edge_thermo["missing"]

    revision_edge = view["condition_revision"]["edges"][0]
    assert revision_edge["thermodynamic_status"] == "unavailable"
    assert revision_edge["reaction_delta_g_eV"] is None
    frozen_edge = view["frozen_network"]["edges"][0]
    assert frozen_edge["reaction_delta_g_eV"] is None
    assert view["frozen_network"]["readiness"] == "blocked"


def test_invalid_ts_frequency_keeps_barrier_unavailable_without_status_promotion():
    source = projection()
    evidence = source["bindings"]["state-ts"]["thermochemistry"]["frequency_evidence"]
    evidence["imaginary_frequencies_cm1"] = [420.0, 350.0]
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge_thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert edge_thermo["activation_delta_g_eV"] is None
    assert edge_thermo["ts_qualification"] == "unavailable"
    assert edge_thermo["thermodynamic_status"] == "available"
    assert edge_thermo["kinetic_status"] == "unavailable"
    assert view["graph"]["artifact_status"] == "incomplete"
    assert view["graph"]["microkinetics_ready"] is False
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["scientific_status"] == "machine_pass"
    assert view["graph"]["mechanism_complete"] is False


def test_outside_applicability_or_missing_response_fails_closed():
    source = projection()
    view = rw.build_reaction_workbench_view(
        source, project_id="project-1", conditions={"temperature_k": 800.0})
    assert view["condition_revision"]["artifact_status"] == "unavailable"
    assert view["condition_revision"]["outside_applicability"]
    assert all(row["derived_delta_g_eV"] is None
               for row in view["condition_revision"]["rows"])

    source["bindings"]["state-r"]["thermochemistry"]["condition_response"].pop("ph")
    missing = rw.build_reaction_workbench_view(
        source, project_id="project-1", conditions={"ph": 1.0})
    row = next(item for item in missing["condition_revision"]["rows"]
               if item["entity_id"] == "state-r")
    assert row["status"] == "unavailable"
    assert "ph response evidence is unavailable" in row["missing"]


def test_temperature_model_and_condition_mixing_is_blocked():
    source = projection()
    source["bindings"]["state-p"]["thermochemistry"]["temperature_k"] = 450.0
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert thermo["thermodynamic_status"] == "unavailable"
    assert thermo["reaction_delta_g_eV"] is None
    assert any("temperature_k" in item for item in thermo["missing"])


def test_condition_set_coverage_must_match_thermochemistry():
    source = projection()
    condition = source["conditions"][0]
    condition["payload"]["coverage"] = 0.75
    condition["semantic_sha256"] = rw.semantic_sha256(condition["payload"])
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert thermo["thermodynamic_status"] == "unavailable"
    assert thermo["reaction_delta_g_eV"] is None
    assert any("coverage" in item for item in thermo["missing"])
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["frozen_network"]["edges"][0]["reaction_delta_g_eV"] is None


def test_term_models_and_typed_model_roles_cannot_be_empty_shells():
    source = projection()
    thermo = source["bindings"]["state-r"]["thermochemistry"]
    for key, _label in rw._TERM_DEFINITIONS:
        thermo[key]["model"] = ""
    thermo["models"] = {"unrelated_slot": "harmonic"}
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    row = next(item for item in view["ledger"]["rows"]
               if item["entity_id"] == "state-r")
    assert row["artifact_status"] == "unavailable"
    assert row["final_delta_g_eV"] is None
    assert "models" in row["missing"]


def test_original_signed_imaginary_frequencies_and_treatment_hash_are_retained():
    view = rw.build_reaction_workbench_view(projection(), project_id="project-1")
    row = next(item for item in view["ledger"]["rows"]
               if item["entity_id"] == "state-ts")
    frequency = row["frequency_qualification"]
    assert frequency["original_imaginary_frequencies_cm1"] == [-420.0, -12.0]
    assert frequency["imaginary_magnitudes_cm1"] == [420.0, 12.0]
    assert frequency["sign_convention"] == "source_preserved"
    assert row["low_frequency"]["evidence_sha256"] == H["term"]


def test_declared_low_frequency_model_requires_matching_treatment_evidence():
    source = projection()
    for binding in source["bindings"].values():
        thermo = binding.get("thermochemistry")
        if thermo:
            thermo["low_frequency"] = {}
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert all(row["artifact_status"] == "unavailable"
               for row in view["ledger"]["rows"])
    assert all(row["low_frequency"]["original_frequencies_display"] == "unavailable"
               and row["low_frequency"]["evidence_sha256"] is None
               for row in view["ledger"]["rows"])
    assert all("low_frequency_model_evidence" in row["missing"]
               for row in view["ledger"]["rows"])
    assert view["graph"]["thermodynamic_ready"] is False
    assert view["frozen_network"]["readiness"] == "blocked"


def test_significant_imaginary_mode_on_minimum_blocks_network_readiness():
    source = projection()
    evidence = source["bindings"]["state-r"]["thermochemistry"][
        "frequency_evidence"]
    evidence["imaginary_frequencies_cm1"] = [-350.0]
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    reactant = next(row for row in view["ledger"]["rows"]
                    if row["entity_id"] == "state-r")
    assert reactant["frequency_qualification"]["minimum_qualification"] == (
        "unavailable")
    assert "minimum_has_significant_imaginary_mode" in reactant["missing"]
    assert reactant["artifact_status"] == "unavailable"
    assert view["graph"]["thermodynamic_ready"] is False
    assert view["frozen_network"]["microkinetics_ready"] is False


def test_condition_response_projection_drops_untyped_private_fields():
    source = projection()
    response = source["bindings"]["state-r"]["thermochemistry"][
        "condition_response"]
    response["operator_note"] = r"C:\private\reaction-notes"
    response["auth_blob"] = "token=not-for-derived-artifacts"
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    row = next(item for item in view["ledger"]["rows"]
               if item["entity_id"] == "state-r")
    assert "operator_note" not in row["condition_response"]
    assert "auth_blob" not in row["condition_response"]


def test_missing_applicability_and_missing_joint_response_are_unavailable():
    source = projection()
    source["applicability"].pop("ph")
    missing_range = rw.build_reaction_workbench_view(
        source, project_id="project-1", conditions={"ph": 1.0})
    assert missing_range["condition_revision"]["artifact_status"] == "unavailable"
    assert "no evidence-bound applicability" in " ".join(
        missing_range["condition_revision"]["outside_applicability"])

    source = projection()
    for binding in source["bindings"].values():
        thermo = binding.get("thermochemistry")
        if thermo:
            thermo["condition_response"].pop("joint_model")
    missing_joint = rw.build_reaction_workbench_view(
        source, project_id="project-1",
        conditions={"ph": 1.0, "coverage": 0.5})
    assert missing_joint["condition_revision"]["artifact_status"] == "unavailable"
    assert any("joint_model" in " ".join(row["missing"])
               for row in missing_joint["condition_revision"]["rows"])


@pytest.mark.parametrize("field,value", [
    ("temperature_k", 0.0), ("pressure_pa", 0.0), ("ph", 30.0),
    ("electrode_potential_v", 30.0), ("coverage", 1.1),
])
def test_condition_request_bounds(field, value):
    with pytest.raises(rw.ReactionWorkbenchError, match=field):
        rw.normalize_conditions({field: value})


def test_projection_project_binding_and_path_text_are_rejected():
    with pytest.raises(rw.ReactionWorkbenchError, match="project binding"):
        rw.build_reaction_workbench_view(projection(), project_id="project-2")
    source = projection()
    source["bindings"]["state-r"]["label"] = r"C:\private\state"
    with pytest.raises(rw.ReactionWorkbenchError, match="path"):
        rw.build_reaction_workbench_view(source, project_id="project-1")
