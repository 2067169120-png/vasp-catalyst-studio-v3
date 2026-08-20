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
    "neb": "b" * 64,
}


def _evidence(opaque_id="ev-1"):
    return [{
        "ref_type": "calculation_result", "opaque_id": opaque_id,
        "origin": "observed", "revision_id": None, "sha256": H["evidence"],
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


def _rehash(envelope):
    envelope["semantic_sha256"] = rw.semantic_sha256(envelope["payload"])


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
        "geometric_site_id": "top",
        "site_occupancy": [{
            "site_id": "top", "count": {"numerator": 1, "denominator": 1},
        }],
        "charge": 0, "multiplicity": 1,
        "provenance": "observed", "evidence_refs": _evidence(f"ev-{state_id}"),
        "method_fingerprint": _method(),
    })


def _transition_state():
    return _envelope("TransitionState", "state-ts", {
        "schema": "vcstudio.transition-state/v1", "model_version": "1.0.0",
        "transition_state_id": "state-ts", "surface_id": "surface-1",
        "chemical_formula": "H", "charge": 0, "multiplicity": 1,
        "site_occupancy": [{
            "site_id": "top", "count": {"numerator": 1, "denominator": 1},
        }],
        "provenance": "observed",
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
        "step_id": "step-1",
        "reactants": [{
            "state_id": "state-r",
            "coefficient": {"numerator": 1, "denominator": 1},
        }],
        "products": [{
            "state_id": "state-p",
            "coefficient": {"numerator": 1, "denominator": 1},
        }],
        "transition_state_id": "state-ts",
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
    return {
        "schema": rw.THERMOCHEMISTRY_TERM_SCHEMA,
        "value": value, "unit": "eV", "evidence_sha256": H["term"],
        "model": model, "origin": "observed",
    }


def _thermo(e0, *, ts=False):
    response = {
        "schema": rw.CONDITION_RESPONSE_SCHEMA,
        "origin": "observed", "evidence_sha256": H["response"],
        "base_conditions": {
            "temperature_k": 300.0, "pressure_pa": 100000.0, "ph": 0.0,
            "electrode_potential_v": 0.0, "coverage": 0.25,
        },
        "joint_model": {
            "schema": rw.JOINT_RESPONSE_MODEL_SCHEMA,
            "model": "additive", "evidence_sha256": H["response"],
            "origin": "observed",
        },
    }
    for parameter, slope in (
        ("temperature_k", -0.001), ("ph", 0.02),
        ("electrode_potential_v", -1.0), ("coverage", 0.4),
    ):
        response[parameter] = {
            "schema": rw.PARAMETER_RESPONSE_MODEL_SCHEMA,
            "model": "local_linear", "slope_eV_per_unit": slope,
            "evidence_sha256": H["response"],
            "origin": "observed",
        }
    response["pressure_pa"] = {
        "schema": rw.PARAMETER_RESPONSE_MODEL_SCHEMA,
        "model": "ideal_gas_log", "coefficient_eV": 0.025,
        "evidence_sha256": H["response"],
        "origin": "observed",
    }
    return {
        "schema": rw.THERMOCHEMISTRY_BINDING_SCHEMA,
        "origin": "observed",
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
            "schema": rw.STANDARD_STATE_SCHEMA,
            "kind": "1-bar", "value": 100000.0, "unit": "Pa",
            "evidence_sha256": H["standard"], "origin": "observed",
        },
        "reference_state_sha256": H["reference"],
        "condition_set_id": "condition-1",
        "condition_set_sha256": _condition()["semantic_sha256"],
        "ph": 0.0, "electrode_potential_v": 0.0, "coverage": 0.25,
        "solvent_model_sha256": None, "coverage_model_sha256": None,
        "low_frequency": {
            "schema": rw.LOW_FREQUENCY_SCHEMA,
            "original_frequencies_cm1": [18.0, 42.0, 120.0],
            "rule": "quasi_harmonic", "cutoff_cm1": 50.0,
            "reason": "bounded entropy sensitivity audit",
            "evidence_sha256": H["term"],
            "policy_evidence_sha256": H["standard"],
            "origin": "observed",
            "sensitivity": [{
                "parameter": "cutoff", "value": 50.0, "unit": "cm-1",
                "delta_g_eV": 0.012, "evidence_sha256": H["term"],
                "origin": "observed",
            }],
        },
        "frequency_evidence": ({
            "schema": rw.FREQUENCY_EVIDENCE_SCHEMA,
            "context": "ts", "imaginary_frequencies_cm1": [-420.0, -12.0],
            "sign_convention": "signed_negative",
            "noise_threshold_cm1": 50.0,
            "evidence_sha256": H["frequency"],
            "mode_evidence_sha256": H["mode"],
            "mode_alignment_status": "confirmed",
            "method_sha256": H["method"], "structure_sha256": H["structure"],
            "origin": "observed",
        } if ts else {
            "schema": rw.FREQUENCY_EVIDENCE_SCHEMA,
            "context": "minimum", "imaginary_frequencies_cm1": [],
            "sign_convention": "signed_negative",
            "noise_threshold_cm1": 50.0,
            "evidence_sha256": H["frequency"],
            "mode_evidence_sha256": H["mode"],
            "mode_alignment_status": "confirmed",
            "method_sha256": H["method"], "structure_sha256": H["structure"],
            "origin": "observed",
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
            "origin": "observed",
        }
    bindings["state-r"]["thermochemistry"] = _thermo(-10.0)
    bindings["state-ts"]["thermochemistry"] = _thermo(-9.0, ts=True)
    bindings["state-p"]["thermochemistry"] = _thermo(-10.5)
    bindings["step-1"]["edge_evidence"] = {
        "schema": rw.EDGE_EVIDENCE_SCHEMA,
        "method_sha256": H["method"],
        "reference_state_sha256": H["reference"],
        "evidence_sha256": H["evidence"], "origin": "observed",
        "reactant_structure_sha256": {"state-r": H["structure"]},
        "product_structure_sha256": {"state-p": H["structure"]},
        "transition_state_structure_sha256": H["structure"],
        "neb": {
            "schema": rw.NEB_EVIDENCE_SCHEMA,
            "method_sha256": H["method"],
            "reference_state_sha256": H["reference"],
            "evidence_sha256": H["neb"], "origin": "observed",
            "reactant_structure_sha256": {"state-r": H["structure"]},
            "product_structure_sha256": {"state-p": H["structure"]},
            "transition_state_structure_sha256": H["structure"],
            "observed_forward_delta_e_barrier": {
                "schema": rw.ENERGY_OBSERVATION_SCHEMA,
                "value": 0.8, "unit": "eV", "model": "ci_neb",
                "evidence_sha256": H["neb"], "origin": "observed",
            },
            "observed_reverse_delta_e_barrier": {
                "schema": rw.ENERGY_OBSERVATION_SCHEMA,
                "value": 1.3, "unit": "eV", "model": "ci_neb",
                "evidence_sha256": H["neb"], "origin": "observed",
            },
        },
    }
    return {
        "schema": rw.PROJECTION_SCHEMA, "project_id": "project-1",
        "network": _network(), "surfaces": [_surface()],
        "states": [_state("state-r", "H"), _state("state-p", "H")],
        "transition_states": [_transition_state()],
        "steps": [_step()], "conditions": [_condition()], "bindings": bindings,
        "applicability": {
            "temperature_k": {
                "schema": rw.APPLICABILITY_RANGE_SCHEMA,
                "minimum": 250.0, "maximum": 500.0,
                "evidence_sha256": H["response"], "origin": "observed"},
            "pressure_pa": {
                "schema": rw.APPLICABILITY_RANGE_SCHEMA,
                "minimum": 1000.0, "maximum": 1e7,
                "evidence_sha256": H["response"], "origin": "observed"},
            "ph": {
                "schema": rw.APPLICABILITY_RANGE_SCHEMA,
                "minimum": 0.0, "maximum": 14.0,
                "evidence_sha256": H["response"], "origin": "observed"},
            "electrode_potential_v": {
                "schema": rw.APPLICABILITY_RANGE_SCHEMA,
                "minimum": -2.0, "maximum": 2.0,
                "evidence_sha256": H["response"], "origin": "observed"},
            "coverage": {
                "schema": rw.APPLICABILITY_RANGE_SCHEMA,
                "minimum": 0.0, "maximum": 1.0,
                "evidence_sha256": H["response"], "origin": "observed"},
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
    assert view["graph"]["edges"][0]["thermochemistry"][
        "observed_activation_delta_e_eV"] == 0.8
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
    assert frozen_edge["stoichiometry"] == {
        "state-r": {"numerator": -1, "denominator": 1},
        "state-p": {"numerator": 1, "denominator": 1},
    }
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
    evidence["imaginary_frequencies_cm1"] = [-420.0, -350.0]
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
    with pytest.raises(rw.ReactionWorkbenchError, match="term role"):
        rw.build_reaction_workbench_view(source, project_id="project-1")


def test_original_signed_imaginary_frequencies_and_treatment_hash_are_retained():
    view = rw.build_reaction_workbench_view(projection(), project_id="project-1")
    row = next(item for item in view["ledger"]["rows"]
               if item["entity_id"] == "state-ts")
    frequency = row["frequency_qualification"]
    assert frequency["original_imaginary_frequencies_cm1"] == [-420.0, -12.0]
    assert frequency["imaginary_magnitudes_cm1"] == [420.0, 12.0]
    assert frequency["sign_convention"] == "signed_negative"
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


def test_exact_rational_stoichiometry_and_conservation_are_frozen():
    source = projection()
    reactant = source["states"][0]
    reactant["payload"]["chemical_formula"] = "H2"
    reactant["payload"]["site_occupancy"][0]["count"] = {
        "numerator": 2, "denominator": 1}
    _rehash(reactant)
    source["steps"][0]["payload"]["reactants"][0]["coefficient"] = {
        "numerator": 1, "denominator": 2}
    _rehash(source["steps"][0])

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge = view["graph"]["edges"][0]
    assert edge["conservation"]["status"] == "available"
    assert edge["stoichiometry"]["state-r"] == {
        "numerator": -1, "denominator": 2}
    assert view["frozen_network"]["edges"][0]["reactants"][0][
        "coefficient"] == {"numerator": 1, "denominator": 2}


@pytest.mark.parametrize(("mutation", "expected"), [
    ("element", "elemental_conservation"),
    ("charge", "charge_conservation"),
    ("site_occupancy", "surface_site_occupancy_conservation"),
    ("site_membership", "site_membership:bridge"),
    ("ts_site_occupancy", "transition_state_surface_site_occupancy"),
])
def test_failed_or_unknown_reaction_conservation_blocks_all_qualification(
    mutation, expected,
):
    source = projection()
    product = source["states"][1]
    if mutation == "element":
        product["payload"]["chemical_formula"] = "He"
    elif mutation == "charge":
        product["payload"]["charge"] = 1
    elif mutation == "site_occupancy":
        product["payload"]["site_occupancy"][0]["count"] = {
            "numerator": 2, "denominator": 1}
    elif mutation == "site_membership":
        product["payload"]["site_occupancy"][0]["site_id"] = "bridge"
    else:
        transition_state = source["transition_states"][0]
        transition_state["payload"]["site_occupancy"][0]["count"] = {
            "numerator": 2, "denominator": 1}
        _rehash(transition_state)
    if mutation != "ts_site_occupancy":
        _rehash(product)

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge = view["graph"]["edges"][0]
    assert any(expected in item for item in edge["conservation"]["missing"])
    assert edge["thermochemistry"]["thermodynamic_status"] == "unavailable"
    assert edge["thermochemistry"]["kinetic_status"] == "unavailable"
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["report_binding"]["artifact_status"] == "incomplete"


def test_site_occupancy_conservation_is_resolved_by_surface_and_site_id():
    source = projection()
    surface = source["surfaces"][0]
    surface["payload"]["geometric_site_ids"].append("bridge")
    _rehash(surface)
    product = source["states"][1]
    product["payload"]["site_occupancy"][0]["site_id"] = "bridge"
    _rehash(product)

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    conservation = view["graph"]["edges"][0]["conservation"]
    assert conservation["status"] == "failed"
    occupancy = conservation["surface_site_occupancy"]
    assert occupancy["key_schema"] == "(surface_id, site_id)"
    assert occupancy["reactants"] == [{
        "surface_id": "surface-1", "site_id": "top",
        "count": {"numerator": 1, "denominator": 1},
    }]
    assert occupancy["products"] == [{
        "surface_id": "surface-1", "site_id": "bridge",
        "count": {"numerator": 1, "denominator": 1},
    }]
    assert view["frozen_network"]["edges"][0]["conservation"] == conservation
    assert view["frozen_network"]["readiness"] == "blocked"


@pytest.mark.parametrize("mutation", ["duplicate", "both_sides"])
def test_ambiguous_reaction_participants_are_rejected(mutation):
    source = projection()
    step = source["steps"][0]["payload"]
    if mutation == "duplicate":
        step["reactants"].append(copy.deepcopy(step["reactants"][0]))
    else:
        step["products"][0]["state_id"] = "state-r"
    _rehash(source["steps"][0])
    with pytest.raises(rw.ReactionWorkbenchError, match="duplicate|both sides"):
        rw.build_reaction_workbench_view(source, project_id="project-1")


@pytest.mark.parametrize("mutation", [
    "unknown_term_field", "wrong_unit", "negative_zpe", "zero_temperature",
    "zero_pressure", "invalid_standard_state", "wrong_standard_dimension",
    "term_model_mismatch",
])
def test_strict_thermochemistry_schema_rejects_scientific_counterexamples(mutation):
    source = projection()
    thermo = source["bindings"]["state-r"]["thermochemistry"]
    if mutation == "unknown_term_field":
        thermo["zpe_eV"]["guessed"] = True
    elif mutation == "wrong_unit":
        thermo["zpe_eV"]["unit"] = "kJ/mol"
    elif mutation == "negative_zpe":
        thermo["zpe_eV"]["value"] = -0.1
    elif mutation == "zero_temperature":
        thermo["temperature_k"] = 0.0
    elif mutation == "zero_pressure":
        thermo["pressure_pa"] = 0.0
    elif mutation == "invalid_standard_state":
        thermo["standard_state"]["kind"] = "ambient"
    elif mutation == "wrong_standard_dimension":
        thermo["standard_state"]["unit"] = "mol/L"
    else:
        thermo["electronic_energy_e0_eV"]["model"] = "harmonic"
    with pytest.raises(rw.ReactionWorkbenchError):
        rw.build_reaction_workbench_view(source, project_id="project-1")


def test_normalized_standard_state_definition_is_in_compatibility_hash():
    source = projection()
    standard = source["bindings"]["state-p"]["thermochemistry"]["standard_state"]
    standard.update({"kind": "1-atm", "value": 101325.0, "unit": "Pa"})
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert thermo["thermodynamic_status"] == "unavailable"
    assert "incompatible_method_reference_standard_state_condition_solvent_or_coverage" \
        in thermo["missing"]


def test_frequency_sign_convention_and_ts_bindings_are_strict():
    source = projection()
    frequency = source["bindings"]["state-ts"]["thermochemistry"][
        "frequency_evidence"]
    frequency["imaginary_frequencies_cm1"] = [420.0]
    with pytest.raises(rw.ReactionWorkbenchError, match="strictly negative"):
        rw.build_reaction_workbench_view(source, project_id="project-1")

    source = projection()
    frequency = source["bindings"]["state-ts"]["thermochemistry"][
        "frequency_evidence"]
    frequency["sign_convention"] = "positive_magnitude"
    frequency.pop("imaginary_frequencies_cm1")
    frequency["imaginary_frequency_magnitudes_cm1"] = [420.0, 12.0]
    supported = rw.build_reaction_workbench_view(source, project_id="project-1")
    ts = next(row for row in supported["ledger"]["rows"]
              if row["entity_id"] == "state-ts")
    assert ts["frequency_qualification"]["kinetic_qualification"] == (
        "frequency_mode_supported")

    for field in ("method_sha256", "structure_sha256"):
        source = projection()
        source["bindings"]["state-ts"]["thermochemistry"][
            "frequency_evidence"][field] = "f" * 64
        blocked = rw.build_reaction_workbench_view(source, project_id="project-1")
        assert blocked["graph"]["edges"][0]["thermochemistry"][
            "kinetic_status"] == "unavailable"


@pytest.mark.parametrize(("entity_id", "frequencies"), [
    ("state-ts", [-420.0, -350.0]),
    ("state-r", [-350.0]),
])
def test_frequency_noise_threshold_is_server_fixed_not_data_selected(
    entity_id, frequencies,
):
    source = projection()
    evidence = source["bindings"][entity_id]["thermochemistry"][
        "frequency_evidence"]
    evidence["imaginary_frequencies_cm1"] = frequencies
    evidence["noise_threshold_cm1"] = 400.0
    with pytest.raises(rw.ReactionWorkbenchError, match="server-fixed 50"):
        rw.build_reaction_workbench_view(source, project_id="project-1")

    baseline = rw.build_reaction_workbench_view(
        projection(), project_id="project-1")
    row = next(item for item in baseline["ledger"]["rows"]
               if item["entity_id"] == entity_id)
    policy = row["frequency_qualification"]["threshold_policy"]
    assert policy["authority"] == "server_fixed_policy"
    assert policy["threshold_cm1"] == 50.0
    assert policy["scientific_maximum_cm1"] == 100.0
    assert row["compatibility"]["frequency_threshold_policy_sha256"] == policy[
        "policy_sha256"]


@pytest.mark.parametrize("mutation", [
    "edge_method", "edge_endpoint", "edge_evidence", "neb_method",
    "neb_reference", "neb_ts", "neb_observation_evidence", "neb_origin",
])
def test_edge_and_neb_compatibility_bind_every_scientific_hash(mutation):
    source = projection()
    evidence = source["bindings"]["step-1"]["edge_evidence"]
    neb = evidence["neb"]
    if mutation == "edge_method":
        evidence["method_sha256"] = "f" * 64
    elif mutation == "edge_endpoint":
        evidence["reactant_structure_sha256"]["state-r"] = "f" * 64
    elif mutation == "edge_evidence":
        evidence["evidence_sha256"] = "f" * 64
    elif mutation == "neb_method":
        neb["method_sha256"] = "f" * 64
    elif mutation == "neb_reference":
        neb["reference_state_sha256"] = "f" * 64
    elif mutation == "neb_ts":
        neb["transition_state_structure_sha256"] = "f" * 64
    elif mutation == "neb_origin":
        neb["origin"] = "imported"
    else:
        neb["observed_forward_delta_e_barrier"]["evidence_sha256"] = "f" * 64

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert thermo["kinetic_status"] == "unavailable"
    assert thermo["observed_activation_delta_e_eV"] is None
    assert thermo["observed_activation_delta_e_display"] == "unavailable"
    edge_evidence = view["graph"]["edges"][0]["edge_evidence"]
    assert edge_evidence["observed_values_available"] is False
    assert edge_evidence["neb"]["observed_forward_delta_e_barrier"][
        "value_eV"] is None
    assert view["condition_revision"]["edges"][0][
        "observed_activation_delta_e_eV"] is None
    assert view["report_binding"]["tables"][0]["rows"][0][5] == "unavailable"
    assert view["frozen_network"]["edges"][0][
        "observed_activation_delta_e_eV"] is None
    assert view["frozen_network"]["readiness"] == "blocked"


def test_observed_neb_delta_e_and_thermal_delta_g_barriers_are_distinct():
    view = rw.build_reaction_workbench_view(projection(), project_id="project-1")
    thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert thermo["observed_activation_delta_e_eV"] == 0.8
    assert thermo["thermal_activation_delta_g_eV"] == 1.0
    assert thermo["barrier_sources"] == {
        "delta_e": "observed_neb", "delta_g": "thermochemistry_ledger"}


def test_condition_response_base_must_match_ledger_and_canonical_condition_set():
    source = projection()
    response = source["bindings"]["state-r"]["thermochemistry"][
        "condition_response"]
    response["base_conditions"]["temperature_k"] = 1000.0
    view = rw.build_reaction_workbench_view(
        source, project_id="project-1", conditions={"temperature_k": 301.0})
    ledger_row = next(row for row in view["ledger"]["rows"]
                      if row["entity_id"] == "state-r")
    assert ledger_row["condition_response"]["base_binding_status"] == "unavailable"
    assert any("ledger condition" in item
               for item in ledger_row["condition_response"]["missing"])
    assert any("canonical condition set" in item
               for item in ledger_row["condition_response"]["missing"])
    derived = next(row for row in view["condition_revision"]["rows"]
                   if row["entity_id"] == "state-r")
    assert derived["status"] == "unavailable"
    assert derived["derived_delta_g_eV"] is None
    assert view["condition_revision"]["edges"][0]["reaction_delta_g_eV"] is None


def test_condition_response_base_must_be_inside_evidence_applicability():
    source = projection()
    condition = source["conditions"][0]
    condition["payload"]["temperature_k"] = 600.0
    _rehash(condition)
    for object_id in ("state-r", "state-ts", "state-p"):
        thermo = source["bindings"][object_id]["thermochemistry"]
        thermo["temperature_k"] = 600.0
        thermo["condition_set_sha256"] = condition["semantic_sha256"]
        thermo["condition_response"]["base_conditions"]["temperature_k"] = 600.0
    view = rw.build_reaction_workbench_view(
        source, project_id="project-1", conditions={"temperature_k": 500.0})
    assert view["graph"]["thermodynamic_ready"] is True
    assert view["condition_revision"]["artifact_status"] == "unavailable"
    assert all(any("base is above" in item for item in row["missing"])
               for row in view["condition_revision"]["rows"])


@pytest.mark.parametrize(("target", "origin", "ceiling"), [
    ("applicability", "inferred", "candidate"),
    ("parameter", "imported", "machine_pass"),
    ("joint", "inferred", "candidate"),
])
def test_condition_derivation_provenance_caps_status_and_readiness(
    target, origin, ceiling,
):
    source = projection()
    if target == "applicability":
        source["applicability"]["temperature_k"]["origin"] = origin
        conditions = {"temperature_k": 301.0}
    else:
        for object_id in ("state-r", "state-ts", "state-p"):
            response = source["bindings"][object_id]["thermochemistry"][
                "condition_response"]
            if target == "parameter":
                response["temperature_k"]["origin"] = origin
            else:
                response["joint_model"]["origin"] = origin
        conditions = (
            {"temperature_k": 301.0} if target == "parameter"
            else {"temperature_k": 301.0, "coverage": 0.3})
    view = rw.build_reaction_workbench_view(
        source, project_id="project-1", conditions=conditions)
    assert view["condition_revision"]["scientific_status"] == ceiling
    assert view["condition_revision"]["artifact_status"] == "unavailable"
    assert view["frozen_network"]["readiness"] == "blocked"


@pytest.mark.parametrize(("target", "ceiling"), [
    ("applicability", "candidate"),
    ("parameter", "machine_pass"),
])
def test_condition_dependency_provenance_caps_base_revision_without_a_request(
    target, ceiling,
):
    source = projection()
    for binding in source["bindings"].values():
        binding["scientific_status"] = "verified"
    if target == "applicability":
        source["applicability"]["temperature_k"]["origin"] = "inferred"
    else:
        for object_id in ("state-r", "state-ts", "state-p"):
            source["bindings"][object_id]["thermochemistry"][
                "condition_response"]["temperature_k"]["origin"] = "imported"

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert view["condition_revision"]["scientific_status"] == ceiling
    assert view["condition_revision"]["artifact_status"] == "unavailable"
    assert view["scientific_status"] == ceiling
    assert view["frozen_network"]["readiness"] == "blocked"
    if target == "parameter":
        assert view["graph"]["kinetic_ready"] is False


@pytest.mark.parametrize(("target", "field"), [
    ("applicability", "origin"), ("applicability", "evidence_sha256"),
    ("parameter", "origin"), ("parameter", "evidence_sha256"),
    ("joint", "origin"), ("joint", "evidence_sha256"),
])
def test_condition_models_require_typed_origin_and_evidence(target, field):
    source = projection()
    if target == "applicability":
        source["applicability"]["temperature_k"].pop(field)
    else:
        response = source["bindings"]["state-r"]["thermochemistry"][
            "condition_response"]
        response["temperature_k" if target == "parameter" else "joint_model"].pop(
            field)
    with pytest.raises(rw.ReactionWorkbenchError, match="required fields"):
        rw.build_reaction_workbench_view(source, project_id="project-1")


def test_low_frequency_policy_compatibility_ignores_entity_treatment_hashes():
    source = projection()
    treatment = source["bindings"]["state-p"]["thermochemistry"]["low_frequency"]
    treatment["original_frequencies_cm1"] = [21.0, 47.0, 133.0]
    treatment["reason"] = "entity-specific spectrum and sensitivity audit"
    treatment["evidence_sha256"] = "f" * 64
    treatment["sensitivity"][0]["delta_g_eV"] = 0.02
    treatment["sensitivity"][0]["evidence_sha256"] = "f" * 64

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    rows = {row["entity_id"]: row for row in view["ledger"]["rows"]}
    assert rows["state-r"]["compatibility"]["low_frequency_policy_sha256"] == (
        rows["state-p"]["compatibility"]["low_frequency_policy_sha256"])
    assert rows["state-r"]["compatibility"]["low_frequency_treatment_sha256"] != (
        rows["state-p"]["compatibility"]["low_frequency_treatment_sha256"])
    assert view["graph"]["thermodynamic_ready"] is True
    assert view["graph"]["kinetic_ready"] is True

    source["bindings"]["state-p"]["thermochemistry"]["low_frequency"][
        "cutoff_cm1"] = 60.0
    blocked = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert blocked["graph"]["thermodynamic_ready"] is False


@pytest.mark.parametrize(("target", "origin", "ceiling"), [
    ("step", "imported", "machine_pass"),
    ("surface", "imported", "machine_pass"),
    ("term", "inferred", "candidate"),
])
def test_weakest_provenance_caps_status_and_blocks_kinetic_qualification(
    target, origin, ceiling,
):
    source = projection()
    for binding in source["bindings"].values():
        binding["scientific_status"] = "verified"
    if target == "step":
        source["steps"][0]["payload"]["provenance"] = origin
        _rehash(source["steps"][0])
    elif target == "surface":
        source["surfaces"][0]["payload"]["provenance"] = origin
        _rehash(source["surfaces"][0])
    else:
        source["bindings"]["state-r"]["thermochemistry"][
            "electronic_energy_e0_eV"]["origin"] = origin

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert view["scientific_status"] == ceiling
    assert view["graph"]["kinetic_ready"] is False
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["report_binding"]["scientific_status"] == ceiling


def test_raw_payload_digest_is_recomputed_and_never_formally_frozen():
    left = projection()
    left["states"][0] = copy.deepcopy(left["states"][0]["payload"])
    left["states"][0]["semantic_sha256"] = "0" * 64
    right = copy.deepcopy(left)
    right["states"][0]["semantic_sha256"] = "f" * 64

    left_view = rw.build_reaction_workbench_view(left, project_id="project-1")
    right_view = rw.build_reaction_workbench_view(right, project_id="project-1")
    assert left_view["source_projection_sha256"] == right_view[
        "source_projection_sha256"]
    assert left_view["graph"]["canonical_envelope_authority"] is False
    assert left_view["graph"]["artifact_status"] == "incomplete"
    assert left_view["frozen_network"]["readiness"] == "blocked"
    assert left_view["report_binding"]["artifact_status"] == "incomplete"

    left["states"][0]["untrusted_extension"] = "guess"
    with pytest.raises(rw.ReactionWorkbenchError, match="unknown fields"):
        rw.build_reaction_workbench_view(left, project_id="project-1")


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
