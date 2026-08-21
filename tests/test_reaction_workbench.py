from __future__ import annotations

import copy

import pytest

from vcstudio.project import reaction_workbench as rw
from vcstudio.project.catalysis_contracts import (
    AdsorbateState,
    CatalystSurface,
    ConditionSet,
    DomainEnvelope,
    ElementaryStep,
    EvidenceRef,
    ExactRational,
    FluidStandardState,
    FluidState,
    MethodFingerprint,
    ReactionNetwork,
    ReactionParticipant,
)


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


def _legacy_envelope(object_type, object_id, payload):
    return {
        "schema": "vcstudio.catalysis-domain-envelope/v1",
        "object_type": object_type, "object_id": object_id,
        "object_version": "1.0.0", "payload": payload,
        "semantic_sha256": rw.semantic_sha256(payload),
        "job_source_of_truth": "job.yaml", "authorizes_execution": False,
    }


def _rehash(envelope):
    envelope["semantic_sha256"] = rw.semantic_sha256(envelope["payload"])


def _rehash_step(source):
    step = source["steps"][0]
    _rehash(step)
    saddle = source["bindings"]["step-1"]["saddle"]
    saddle["step_semantic_sha256"] = step["semantic_sha256"]
    saddle["transition_side_sha256"] = rw.semantic_sha256(
        step["payload"]["transition_state"])


def _set_domain_origin(envelope, origin):
    envelope["payload"]["provenance"] = origin
    for ref in envelope["payload"]["evidence_refs"]:
        ref["origin"] = origin
    for ref in envelope["payload"]["method_fingerprint"]["evidence_refs"]:
        ref["origin"] = origin
    _rehash(envelope)


def _domain_refs(opaque_id):
    return (EvidenceRef(
        ref_type="calculation_result", opaque_id=opaque_id,
        origin="observed", revision_id="revision-1",
    ),)


def _domain_method():
    return MethodFingerprint(
        method_id="pbe-d3", scope="periodic-dft", sha256=H["method"],
        evidence_refs=(EvidenceRef(
            ref_type="method_record", opaque_id="method-1",
            origin="observed", revision_id="revision-1",
        ),),
    )


def _domain_common(opaque_id):
    return {
        "provenance": "observed", "evidence_refs": _domain_refs(opaque_id),
        "method_fingerprint": _domain_method(), "object_revision_id": "revision-1",
    }


def _surface():
    return DomainEnvelope.wrap(CatalystSurface(
        surface_id="surface-1", composition="Pt", miller_indices=(1, 1, 1),
        termination_id=None, geometric_site_ids=("top",),
        **_domain_common("surface-evidence"),
    )).to_dict()


def _state(state_id, formula):
    return DomainEnvelope.wrap(AdsorbateState(
        state_id=state_id, surface_id="surface-1",
        adsorbate_id=f"ads-{state_id}", chemical_formula=formula,
        geometric_site_id="top", charge=0, multiplicity=1,
        **_domain_common(f"ev-{state_id}"),
    )).to_dict()


def _fluid_state(state_id, formula="H2", *, phase="gas", charge=0):
    standard_state = (
        FluidStandardState(
            phase=phase, kind="1-bar", value=100000.0, unit="Pa")
        if phase == "gas" else FluidStandardState(
            phase=phase, kind="1-molar", value=1.0, unit="mol/L"))
    return DomainEnvelope.wrap(FluidState(
        state_id=state_id, phase=phase, chemical_formula=formula,
        charge=charge, multiplicity=1, standard_state=standard_state,
        **_domain_common(f"ev-{state_id}"),
    )).to_dict()


def _transition_state():
    return _state("state-ts", "H")


def _condition():
    return DomainEnvelope.wrap(ConditionSet(
        condition_set_id="condition-1", temperature_k=300.0,
        pressure_pa=100000.0, ph=0.0, electrode_potential_v=0.0,
        **_domain_common("condition-evidence"),
    )).to_dict()


def _participant(state_id, coefficient=1, *, phase="adsorbed", charge=0, site="top"):
    return ReactionParticipant(
        state_id=state_id, coefficient=coefficient, phase=phase, charge=charge,
        site_stoichiometry=(
            {} if site is None else {site: ExactRational(1, 1)}),
    )


def _step():
    return DomainEnvelope.wrap(ElementaryStep(
        step_id="step-1",
        reactants=(_participant("state-r"),),
        transition_state=(_participant("state-ts"),),
        products=(_participant("state-p"),),
        condition_set_id="condition-1", reversible=True,
        **_domain_common("step-evidence"),
    )).to_dict()


def _network():
    return DomainEnvelope.wrap(ReactionNetwork(
        network_id="network-1", surface_ids=("surface-1",),
        state_ids=("state-r", "state-ts", "state-p"), step_ids=("step-1",),
        condition_set_ids=("condition-1",),
        **_domain_common("network-evidence"),
    )).to_dict()


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
    bindings["state-p"]["thermochemistry"] = _thermo(-10.5)
    step = _step()
    network = _network()
    bindings["step-1"]["saddle"] = {
        "step_semantic_sha256": step["semantic_sha256"],
        "transition_side_sha256": rw.semantic_sha256(
            step["payload"]["transition_state"]),
        "label": "TS*", "structure_sha256": H["structure"],
        "evidence_sha256": H["evidence"],
        "scientific_status": "machine_pass", "origin": "observed",
        "thermochemistry": _thermo(-9.0, ts=True),
    }


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
        "authority": {
            "domain_authority_id": "1" * 32,
            "domain_generation": 7,
            "domain_snapshot_sha256": "d" * 64,
            "network_revision_id": network["object_revision_id"],
            "network_semantic_sha256": network["semantic_sha256"],
        },
        "network": network, "surfaces": [_surface()],
        "states": [
            _state("state-r", "H"), _state("state-p", "H"),
            _transition_state(),
        ],
        "steps": [step], "conditions": [_condition()], "bindings": bindings,
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


def _mixed_fluid_projection(*, phase="gas"):
    source = projection()
    states = [
        _fluid_state("fluid-r", phase=phase),
        _state("vacancy-r", "*"),
        _fluid_state("fluid-ts", phase=phase),
        _state("vacancy-ts", "*"),
        _state("ads-h2", "H2"),
    ]
    step = DomainEnvelope.wrap(ElementaryStep(
        step_id="step-1",
        reactants=(
            _participant("fluid-r", phase=phase, site=None),
            _participant("vacancy-r"),
        ),
        transition_state=(
            _participant("fluid-ts", phase=phase, site=None),
            _participant("vacancy-ts"),
        ),
        products=(_participant("ads-h2"),),
        condition_set_id="condition-1", reversible=True,
        **_domain_common("step-evidence"),
    )).to_dict()
    network = DomainEnvelope.wrap(ReactionNetwork(
        network_id="network-1", surface_ids=("surface-1",),
        state_ids=tuple(item["object_id"] for item in states),
        step_ids=("step-1",), condition_set_ids=("condition-1",),
        **_domain_common("network-evidence"),
    )).to_dict()
    source["states"] = states
    source["steps"] = [step]
    source["network"] = network
    source["authority"].update({
        "network_revision_id": network["object_revision_id"],
        "network_semantic_sha256": network["semantic_sha256"],
    })
    source["bindings"] = {
        object_id: {
            "label": object_id, "structure_sha256": H["structure"],
            "evidence_sha256": H["evidence"],
            "scientific_status": "machine_pass", "origin": "observed",
        }
        for object_id in (
            "network-1", "surface-1", "condition-1", "step-1",
            *(item["object_id"] for item in states),
        )
    }
    source["bindings"]["step-1"]["saddle"] = {
        "step_semantic_sha256": step["semantic_sha256"],
        "transition_side_sha256": rw.semantic_sha256(
            step["payload"]["transition_state"]),
        "label": "mixed saddle", "structure_sha256": H["structure"],
        "evidence_sha256": H["evidence"],
        "scientific_status": "machine_pass", "origin": "observed",
    }
    return source


def _all_thermochemistry_bindings(source):
    result = []
    for binding in source["bindings"].values():
        if isinstance(binding.get("thermochemistry"), dict):
            result.append(binding["thermochemistry"])
        saddle = binding.get("saddle")
        if isinstance(saddle, dict) and isinstance(saddle.get("thermochemistry"), dict):
            result.append(saddle["thermochemistry"])
    return result


def _legacy_v1_projection():
    source = projection()
    source["schema"] = rw.LEGACY_PROJECTION_SCHEMA
    for key in ("surfaces", "states", "steps", "conditions"):
        migrated = []
        for envelope in source[key]:
            payload = copy.deepcopy(envelope["payload"])
            identity_field = {
                "CatalystSurface": "surface_id",
                "AdsorbateState": "state_id",
                "ElementaryStep": "step_id",
                "ConditionSet": "condition_set_id",
            }[envelope["object_type"]]
            migrated.append(_legacy_envelope(
                envelope["object_type"], payload[identity_field], payload))
        source[key] = migrated
    source["network"] = _legacy_envelope(
        "ReactionNetwork", "network-1", copy.deepcopy(
            source["network"]["payload"]))
    source["transition_states"] = []
    return source


def test_real_domain_envelope_wrap_dtos_are_the_native_end_to_end_input():
    source = projection()
    envelopes = [
        source["network"], *source["surfaces"], *source["states"],
        *source["steps"], *source["conditions"],
    ]
    parsed = [DomainEnvelope.from_dict(item) for item in envelopes]
    assert all(item.schema == rw.DOMAIN_ENVELOPE_SCHEMA for item in parsed)
    step = ElementaryStep.from_dict(source["steps"][0]["payload"])
    assert step.schema == "vcstudio.elementary-step/v3"
    assert step.transition_state[0].phase == "adsorbed"


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
    ts = next(
        row for row in view["ledger"]["rows"]
        if row["entity_type"] == "transition_state")
    assert ts["entity_id"].startswith("saddle-")
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
    assert view["frozen_network"]["version"] == "2"
    assert view["frozen_network"]["microkinetics_ready"] is True
    frozen_edge = view["frozen_network"]["edges"][0]
    assert frozen_edge["stoichiometry"] == {
        "state-r": {"numerator": -1, "denominator": 1},
        "state-p": {"numerator": 1, "denominator": 1},
    }
    assert frozen_edge["transition_state"] == _step()["payload"]["transition_state"]
    assert frozen_edge["evidence_refs"]
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
    source["authority"]["network_semantic_sha256"] = source["network"][
        "semantic_sha256"]
    with pytest.raises(rw.ReactionWorkbenchError, match="step closure"):
        rw.build_reaction_workbench_view(source, project_id="project-1")

    source = projection()
    source["bindings"]["state-p"].pop("thermochemistry")
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
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
    evidence = source["bindings"]["step-1"]["saddle"]["thermochemistry"][
        "frequency_evidence"]
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


def test_workbench_coverage_extension_must_match_across_thermochemistry():
    source = projection()
    thermo_binding = source["bindings"]["state-p"]["thermochemistry"]
    thermo_binding["coverage"] = 0.75
    thermo_binding["condition_response"]["base_conditions"]["coverage"] = 0.75
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    thermo = view["graph"]["edges"][0]["thermochemistry"]
    assert thermo["thermodynamic_status"] == "unavailable"
    assert thermo["reaction_delta_g_eV"] is None
    assert any("incompatible_method" in item for item in thermo["missing"])
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
               if item["entity_type"] == "transition_state")
    frequency = row["frequency_qualification"]
    assert frequency["original_imaginary_frequencies_cm1"] == [-420.0, -12.0]
    assert frequency["imaginary_magnitudes_cm1"] == [420.0, 12.0]
    assert frequency["sign_convention"] == "signed_negative"
    assert row["low_frequency"]["evidence_sha256"] == H["term"]


def test_declared_low_frequency_model_requires_matching_treatment_evidence():
    source = projection()
    for thermo in _all_thermochemistry_bindings(source):
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
    for thermo in _all_thermochemistry_bindings(source):
        thermo["condition_response"].pop("joint_model")
    missing_joint = rw.build_reaction_workbench_view(
        source, project_id="project-1",
        conditions={"ph": 1.0, "coverage": 0.5})
    assert missing_joint["condition_revision"]["artifact_status"] == "unavailable"
    assert any("joint_model" in " ".join(row["missing"])
               for row in missing_joint["condition_revision"]["rows"])


def test_exact_rational_stoichiometry_and_conservation_are_frozen():
    source = projection()
    for state in source["states"]:
        state["payload"]["chemical_formula"] = "H2"
        _rehash(state)
    step = source["steps"][0]["payload"]
    for side in ("reactants", "transition_state", "products"):
        step[side][0]["coefficient"] = {"numerator": 1, "denominator": 2}
    _rehash_step(source)

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge = view["graph"]["edges"][0]
    assert edge["conservation"]["status"] == "available"
    assert edge["stoichiometry"]["state-r"] == {
        "numerator": -1, "denominator": 2}
    assert view["frozen_network"]["edges"][0]["reactants"][0][
        "coefficient"] == {"numerator": 1, "denominator": 2}


@pytest.mark.parametrize("phase", ["gas", "liquid", "aqueous"])
def test_mixed_fluid_adsorbate_states_retain_type_authority_and_conservation(phase):
    source = _mixed_fluid_projection(phase=phase)
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge = view["graph"]["edges"][0]
    assert edge["conservation"]["status"] == "available"
    assert all(
        edge["conservation"][key]["status"] == "available"
        for key in ("elemental", "charge", "surface_site_occupancy"))

    fluid = next(
        node for node in view["graph"]["nodes"]
        if node["node_id"] == "fluid-r")
    assert fluid["entity_type"] == "fluid_state"
    assert fluid["object_type"] == "FluidState"
    assert fluid["phase"] == phase
    assert fluid["chemistry"]["standard_state"]["phase"] == phase

    refs = edge["participant_evidence_refs"]
    assert next(item for item in refs if item["object_id"] == "fluid-r")[
        "object_type"] == "FluidState"
    assert next(item for item in refs if item["object_id"] == "ads-h2")[
        "object_type"] == "AdsorbateState"

    frozen = view["frozen_network"]
    assert frozen["schema"] == "vcstudio.frozen-reaction-network/v2"
    assert frozen["version"] == "2"
    assert frozen["domain_authority"] == {
        "authority_id": "1" * 32, "generation": 7,
        "snapshot_sha256": "d" * 64,
    }
    assert frozen["network_identity"] == {
        "network_id": "network-1", "object_revision_id": "revision-1",
        "semantic_sha256": source["network"]["semantic_sha256"],
    }
    catalog = {item["state_id"]: item for item in frozen["state_catalog"]}
    assert catalog["fluid-r"]["object_type"] == "FluidState"
    assert catalog["fluid-r"]["phase"] == phase
    assert catalog["fluid-r"]["standard_state"]["phase"] == phase
    assert catalog["ads-h2"]["object_type"] == "AdsorbateState"
    assert catalog["ads-h2"]["site_stoichiometry"] == {
        "top": {"numerator": 1, "denominator": 1}}


@pytest.mark.parametrize(("mutation", "expected"), [
    ("phase", "participant_fluid_phase_disagrees"),
    ("site", "participant_fluid_site_stoichiometry_disagrees"),
    ("charge", "participant_state_charge_disagrees"),
])
def test_fluid_participant_declarations_cannot_self_authorize(mutation, expected):
    source = _mixed_fluid_projection()
    participant = source["steps"][0]["payload"]["reactants"][0]
    if mutation == "phase":
        participant["phase"] = "aqueous"
    elif mutation == "site":
        participant["site_stoichiometry"] = {
            "top": {"numerator": 1, "denominator": 1}}
    else:
        participant["charge"] = 1
    _rehash_step(source)

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    conservation = view["graph"]["edges"][0]["conservation"]
    assert conservation["status"] == "unavailable"
    assert any(expected in item for item in conservation["missing"])
    assert view["frozen_network"]["readiness"] == "blocked"


def test_fluid_three_side_element_conservation_failure_is_exact_and_frozen_blocked():
    source = _mixed_fluid_projection()
    fluid_ts = next(
        item for item in source["states"] if item["object_id"] == "fluid-ts")
    fluid_ts["payload"]["chemical_formula"] = "He"
    _rehash(fluid_ts)

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    conservation = view["graph"]["edges"][0]["conservation"]
    assert conservation["status"] == "failed"
    assert conservation["elemental"]["status"] == "failed"
    assert conservation["charge"]["status"] == "available"
    assert conservation["surface_site_occupancy"]["status"] == "available"
    assert view["frozen_network"]["readiness"] == "blocked"


def test_fluid_state_union_missing_wrong_type_and_cross_type_collision_fail_closed():
    missing = _mixed_fluid_projection()
    missing["states"] = [
        item for item in missing["states"] if item["object_id"] != "fluid-r"]
    with pytest.raises(rw.ReactionWorkbenchError, match="participant-state closure"):
        rw.build_reaction_workbench_view(missing, project_id="project-1")

    wrong_type = _mixed_fluid_projection()
    wrong_type["states"][0] = _condition()
    with pytest.raises(rw.ReactionWorkbenchError):
        rw.build_reaction_workbench_view(wrong_type, project_id="project-1")

    collision = _mixed_fluid_projection()
    collision["states"].append(_state("fluid-r", "H2"))
    with pytest.raises(rw.ReactionWorkbenchError, match="more than one authoritative state"):
        rw.build_reaction_workbench_view(collision, project_id="project-1")


def test_native_v2_projection_authority_is_required_and_network_bound():
    missing = projection()
    missing.pop("authority")
    with pytest.raises(rw.ReactionWorkbenchError, match="requires authority identity facts"):
        rw.build_reaction_workbench_view(missing, project_id="project-1")

    stale = projection()
    stale["authority"]["network_semantic_sha256"] = "f" * 64
    with pytest.raises(rw.ReactionWorkbenchError, match="does not match"):
        rw.build_reaction_workbench_view(stale, project_id="project-1")


@pytest.mark.parametrize(("mutation", "expected"), [
    ("element", "elemental_conservation"),
    ("charge", "charge_conservation"),
    ("site_occupancy", "participant_state_site_stoichiometry_disagrees"),
    ("site_membership", "surface_site_occupancy_conservation"),
    ("ts_site_occupancy", "participant_state_site_stoichiometry_disagrees"),
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
        source["steps"][0]["payload"]["products"][0]["charge"] = 1
    elif mutation == "site_occupancy":
        source["steps"][0]["payload"]["products"][0]["site_stoichiometry"][
            "top"] = {"numerator": 2, "denominator": 1}
    elif mutation == "site_membership":
        source["surfaces"][0]["payload"]["geometric_site_ids"].append("bridge")
        _rehash(source["surfaces"][0])
        product["payload"]["geometric_site_id"] = "bridge"
        participant = source["steps"][0]["payload"]["products"][0]
        participant["site_stoichiometry"]["bridge"] = participant[
            "site_stoichiometry"].pop("top")
    else:
        source["steps"][0]["payload"]["transition_state"][0][
            "site_stoichiometry"]["top"] = {"numerator": 2, "denominator": 1}
    _rehash(product)
    if mutation in {"charge", "site_occupancy", "site_membership", "ts_site_occupancy"}:
        _rehash_step(source)

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
    product["payload"]["geometric_site_id"] = "bridge"
    _rehash(product)
    participant = source["steps"][0]["payload"]["products"][0]
    participant["site_stoichiometry"]["bridge"] = participant[
        "site_stoichiometry"].pop("top")
    _rehash_step(source)

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
    _rehash_step(source)
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
    frequency = source["bindings"]["step-1"]["saddle"]["thermochemistry"][
        "frequency_evidence"]
    frequency["imaginary_frequencies_cm1"] = [420.0]
    with pytest.raises(rw.ReactionWorkbenchError, match="strictly negative"):
        rw.build_reaction_workbench_view(source, project_id="project-1")

    source = projection()
    frequency = source["bindings"]["step-1"]["saddle"]["thermochemistry"][
        "frequency_evidence"]
    frequency["sign_convention"] = "positive_magnitude"
    frequency.pop("imaginary_frequencies_cm1")
    frequency["imaginary_frequency_magnitudes_cm1"] = [420.0, 12.0]
    supported = rw.build_reaction_workbench_view(source, project_id="project-1")
    ts = next(row for row in supported["ledger"]["rows"]
              if row["entity_type"] == "transition_state")
    assert ts["frequency_qualification"]["kinetic_qualification"] == (
        "frequency_mode_supported")

    for field in ("method_sha256", "structure_sha256"):
        source = projection()
        source["bindings"]["step-1"]["saddle"]["thermochemistry"][
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
    binding = (
        source["bindings"]["step-1"]["saddle"]
        if entity_id == "state-ts" else source["bindings"][entity_id])
    evidence = binding["thermochemistry"][
        "frequency_evidence"]
    evidence["imaginary_frequencies_cm1"] = frequencies
    evidence["noise_threshold_cm1"] = 400.0
    with pytest.raises(rw.ReactionWorkbenchError, match="server-fixed 50"):
        rw.build_reaction_workbench_view(source, project_id="project-1")

    baseline = rw.build_reaction_workbench_view(
        projection(), project_id="project-1")
    row = next(item for item in baseline["ledger"]["rows"]
               if (item["entity_type"] == "transition_state"
                   if entity_id == "state-ts" else item["entity_id"] == entity_id))
    policy = row["frequency_qualification"]["threshold_policy"]
    assert policy["authority"] == "server_fixed_policy"
    assert policy["threshold_cm1"] == 50.0
    assert policy["scientific_maximum_cm1"] == 100.0
    assert row["compatibility"]["frequency_threshold_policy_sha256"] == policy[
        "policy_sha256"]


def test_frequency_boundary_always_uses_exact_server_threshold_constant():
    source = projection()
    evidence = source["bindings"]["state-r"]["thermochemistry"][
        "frequency_evidence"]
    evidence["imaginary_frequencies_cm1"] = [-50.0]
    evidence["noise_threshold_cm1"] = 50.0 + 5e-13
    with pytest.raises(rw.ReactionWorkbenchError, match="server-fixed 50"):
        rw.build_reaction_workbench_view(source, project_id="project-1")

    source = projection()
    evidence = source["bindings"]["state-r"]["thermochemistry"][
        "frequency_evidence"]
    evidence["imaginary_frequencies_cm1"] = [-50.0]
    evidence["noise_threshold_cm1"] = 50.0
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    row = next(item for item in view["ledger"]["rows"]
               if item["entity_id"] == "state-r")
    assert row["frequency_qualification"]["noise_threshold_cm1"] == 50.0
    assert row["frequency_qualification"]["minimum_qualification"] == "unavailable"
    assert "minimum_has_significant_imaginary_mode" in row["missing"]

    source = projection()
    source["bindings"]["state-r"]["thermochemistry"]["frequency_evidence"][
        "noise_threshold_cm1"] = "50.0"
    with pytest.raises(rw.ReactionWorkbenchError, match="canonical JSON number"):
        rw.build_reaction_workbench_view(source, project_id="project-1")


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


def test_low_pressure_condition_base_comparison_is_exact_after_normalization():
    source = projection()
    condition = source["conditions"][0]
    condition["payload"]["pressure_pa"] = 1e-12
    _rehash(condition)
    for thermo in _all_thermochemistry_bindings(source):
        thermo["pressure_pa"] = 1e-12
        thermo["condition_set_sha256"] = condition["semantic_sha256"]
        thermo["condition_response"]["base_conditions"]["pressure_pa"] = 1e-12
    source["bindings"]["state-r"]["thermochemistry"]["condition_response"][
        "base_conditions"]["pressure_pa"] = 2e-12

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    row = next(item for item in view["ledger"]["rows"]
               if item["entity_id"] == "state-r")
    response = row["condition_response"]
    assert response["base_binding_status"] == "unavailable"
    assert any("pressure_pa response base does not match ledger" in item
               for item in response["missing"])
    assert any("pressure_pa response base does not match canonical" in item
               for item in response["missing"])
    assert view["graph"]["microkinetics_ready"] is False
    assert view["frozen_network"]["readiness"] == "blocked"


def test_condition_response_base_must_be_inside_evidence_applicability():
    source = projection()
    condition = source["conditions"][0]
    condition["payload"]["temperature_k"] = 600.0
    _rehash(condition)
    for thermo in _all_thermochemistry_bindings(source):
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
        for thermo in _all_thermochemistry_bindings(source):
            response = thermo["condition_response"]
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
        if isinstance(binding.get("saddle"), dict):
            binding["saddle"]["scientific_status"] = "verified"
    if target == "applicability":
        source["applicability"]["temperature_k"]["origin"] = "inferred"
    else:
        for thermo in _all_thermochemistry_bindings(source):
            thermo["condition_response"]["temperature_k"]["origin"] = "imported"

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


@pytest.mark.parametrize("object_id", [
    "state-r", "state-ts", "step-1", "condition-1", "network-1",
])
@pytest.mark.parametrize("scientific_status", [
    "blocked", "unavailable", "unknown",
])
def test_disqualifying_binding_status_blocks_microkinetics_and_frozen_readiness(
    object_id, scientific_status,
):
    source = projection()
    source["bindings"][object_id]["scientific_status"] = scientific_status
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    gate = view["graph"]["binding_status_gate"]
    assert gate["status"] == "blocked"
    assert gate["eligible"] is False
    assert any(object_id in item and scientific_status in item
               for item in gate["blocking"])
    assert view["graph"]["kinetic_ready"] is False
    assert view["graph"]["microkinetics_ready"] is False
    assert view["frozen_network"]["binding_status_gate"] == gate
    assert view["frozen_network"]["readiness"] == "blocked"


@pytest.mark.parametrize("entity_id", ["state-p", "saddle"])
def test_ledger_reference_mismatch_withholds_observed_barrier_from_all_outputs(
    entity_id,
):
    source = projection()
    binding = (
        source["bindings"]["step-1"]["saddle"]
        if entity_id == "saddle" else source["bindings"][entity_id])
    binding["thermochemistry"][
        "reference_state_sha256"] = "f" * 64
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge = view["graph"]["edges"][0]
    thermo = edge["thermochemistry"]
    assert thermo["observed_barrier_status"] == "unavailable"
    assert thermo["observed_activation_delta_e_eV"] is None
    assert thermo["observed_activation_delta_e_display"] == "unavailable"
    assert "observed_barrier_reference_state_binding" in thermo[
        "observed_barrier_missing"]
    assert edge["edge_evidence"]["observed_values_available"] is False
    assert edge["edge_evidence"]["neb"]["observed_forward_delta_e_barrier"][
        "value_eV"] is None
    assert edge["edge_evidence"]["neb"]["observed_reverse_delta_e_barrier"][
        "value_eV"] is None
    assert view["condition_revision"]["edges"][0][
        "observed_activation_delta_e_eV"] is None
    assert view["report_binding"]["tables"][0]["rows"][0][5] == "unavailable"
    assert view["frozen_network"]["edges"][0][
        "observed_activation_delta_e_eV"] is None
    assert view["frozen_network"]["readiness"] == "blocked"


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
        if isinstance(binding.get("saddle"), dict):
            binding["saddle"]["scientific_status"] = "verified"
    if target == "step":
        _set_domain_origin(source["steps"][0], origin)
        _rehash_step(source)
    elif target == "surface":
        _set_domain_origin(source["surfaces"][0], origin)
    else:
        source["bindings"]["state-r"]["thermochemistry"][
            "electronic_energy_e0_eV"]["origin"] = origin

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert view["scientific_status"] == ceiling
    assert view["graph"]["kinetic_ready"] is False
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["report_binding"]["scientific_status"] == ceiling


def test_frozen_status_uses_weakest_graph_ledger_and_revision_qualification():
    source = projection()
    for binding in source["bindings"].values():
        binding["scientific_status"] = "verified"
        if isinstance(binding.get("saddle"), dict):
            binding["saddle"]["scientific_status"] = "verified"
    source["bindings"]["state-r"]["thermochemistry"][
        "electronic_energy_e0_eV"]["origin"] = "inferred"

    view = rw.build_reaction_workbench_view(source, project_id="project-1")

    assert view["graph"]["scientific_status"] == "verified"
    assert view["ledger"]["scientific_status"] == "candidate"
    assert view["condition_revision"]["scientific_status"] == "candidate"
    assert view["scientific_status"] == "candidate"
    assert view["frozen_network"]["scientific_status"] == "candidate"
    assert view["frozen_network"]["readiness"] == "blocked"


def test_native_v3_multi_participant_transition_side_round_trips_to_frozen_network():
    source = projection()
    source["states"].append(_state("state-ts-2", "H"))
    source["bindings"]["state-ts-2"] = {
        "label": "TS component 2", "structure_sha256": H["structure"],
        "evidence_sha256": H["evidence"], "scientific_status": "machine_pass",
        "origin": "observed",
    }
    transition_side = source["steps"][0]["payload"]["transition_state"]
    transition_side[0]["coefficient"] = {"numerator": 1, "denominator": 2}
    transition_side.append(_participant(
        "state-ts-2", ExactRational(1, 2)).to_dict())
    source["network"]["payload"]["state_ids"].append("state-ts-2")
    _rehash(source["network"])
    source["authority"]["network_semantic_sha256"] = source["network"][
        "semantic_sha256"]
    _rehash_step(source)

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    edge = view["graph"]["edges"][0]
    assert edge["conservation"]["status"] == "available"
    assert len(edge["transition_state"]) == 2
    assert edge["transition_state"][1] == {
        "state_id": "state-ts-2",
        "coefficient": {"numerator": 1, "denominator": 2},
        "phase": "adsorbed", "charge": 0,
        "site_stoichiometry": {
            "top": {"numerator": 1, "denominator": 1}},
    }
    frozen = view["frozen_network"]
    assert frozen["microkinetics_ready"] is True
    assert frozen["edges"][0]["transition_state"] == edge["transition_state"]


def test_missing_or_mismatched_explicit_saddle_binding_blocks_or_rejects():
    source = projection()
    source["bindings"]["step-1"].pop("saddle")
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    assert view["graph"]["edges"][0]["thermochemistry"][
        "kinetic_status"] == "unavailable"
    assert "explicit_step_scoped_saddle_binding" in next(
        node for node in view["graph"]["nodes"]
        if node["entity_type"] == "transition_state")["missing"]
    assert view["frozen_network"]["readiness"] == "blocked"

    source = projection()
    source["bindings"]["step-1"]["saddle"]["transition_side_sha256"] = "f" * 64
    with pytest.raises(rw.ReactionWorkbenchError, match="transition side hash mismatch"):
        rw.build_reaction_workbench_view(source, project_id="project-1")


def test_legacy_v1_projection_and_envelopes_are_render_only_never_formal():
    view = rw.build_reaction_workbench_view(
        _legacy_v1_projection(), project_id="project-1")
    assert view["source_projection_schema"] == rw.LEGACY_PROJECTION_SCHEMA
    assert view["migration_only"] is True
    assert view["available"] is True
    assert view["graph"]["canonical_envelope_authority"] is False
    assert view["graph"]["microkinetics_ready"] is False
    assert view["frozen_network"]["schema"] == rw.FROZEN_NETWORK_SCHEMA
    assert view["frozen_network"]["version"] == "2"
    assert rw.FROZEN_NETWORK_SCHEMA != "vcstudio.frozen-reaction-network/v1"
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["report_binding"]["artifact_status"] == "incomplete"


@pytest.mark.parametrize("field", ["semantic_sha256", "object_revision_id"])
def test_canonical_envelope_hash_and_revision_tampering_are_rejected(field):
    source = projection()
    source["steps"][0][field] = "f" * 64 if field == "semantic_sha256" else "revision-x"
    with pytest.raises(rw.ReactionWorkbenchError, match="canonical domain envelope"):
        rw.build_reaction_workbench_view(source, project_id="project-1")


@pytest.mark.parametrize(("mutation", "expected"), [
    ("phase", "participant_state_phase_disagrees"),
    ("site", "participant_state_site_stoichiometry_disagrees"),
])
def test_v3_participant_phase_and_site_tampering_is_unavailable(
        mutation, expected):
    source = projection()
    participant = source["steps"][0]["payload"]["transition_state"][0]
    if mutation == "phase":
        participant["phase"] = "gas"
    else:
        participant["site_stoichiometry"] = {
            "bridge": {"numerator": 1, "denominator": 1}}
    _rehash_step(source)
    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    conservation = view["graph"]["edges"][0]["conservation"]
    assert conservation["status"] == "unavailable"
    assert any(expected in item for item in conservation["missing"])
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["frozen_network"]["microkinetics_ready"] is False


@pytest.mark.parametrize(("case", "expected"), [
    ("top_two", "participant_state_site_stoichiometry_disagrees"),
    ("top_half", "participant_state_site_stoichiometry_disagrees"),
    ("multiple_sites", "participant_state_site_stoichiometry_disagrees"),
    ("empty_sites", "participant_state_site_stoichiometry_disagrees"),
    ("state_site_missing", "authoritative_state_site_unavailable"),
])
def test_v1_state_authorizes_exactly_one_geometric_site_and_never_self_certifies(
        case, expected):
    source = projection()
    step = source["steps"][0]["payload"]
    participants = [
        step[side][0] for side in ("reactants", "transition_state", "products")]
    if case == "top_two":
        for participant in participants:
            participant["site_stoichiometry"] = {
                "top": {"numerator": 2, "denominator": 1}}
    elif case == "top_half":
        for participant in participants:
            participant["site_stoichiometry"] = {
                "top": {"numerator": 1, "denominator": 2}}
    elif case == "multiple_sites":
        source["surfaces"][0]["payload"]["geometric_site_ids"].append("bridge")
        _rehash(source["surfaces"][0])
        for participant in participants:
            participant["site_stoichiometry"]["bridge"] = {
                "numerator": 1, "denominator": 1}
    elif case == "empty_sites":
        for participant in participants:
            participant["site_stoichiometry"] = {}
    else:
        for state in source["states"]:
            state["payload"]["geometric_site_id"] = None
            _rehash(state)
    _rehash_step(source)

    view = rw.build_reaction_workbench_view(source, project_id="project-1")
    conservation = view["graph"]["edges"][0]["conservation"]
    assert conservation["status"] == "unavailable"
    assert any(expected in item for item in conservation["missing"])
    assert view["frozen_network"]["edges"][0]["conservation"] == conservation
    assert view["frozen_network"]["readiness"] == "blocked"
    assert view["frozen_network"]["microkinetics_ready"] is False
    if case == "top_two":
        assert conservation["surface_site_occupancy"]["reactants"] == [{
            "surface_id": "surface-1", "site_id": "top",
            "count": {"numerator": 1, "denominator": 1},
        }]


def test_valid_partial_condition_and_unresolved_state_charge_block_without_parse_failure():
    source = projection()
    source["conditions"][0]["payload"]["pressure_pa"] = None
    _rehash(source["conditions"][0])
    condition_view = rw.build_reaction_workbench_view(
        source, project_id="project-1")
    assert condition_view["graph"]["thermodynamic_ready"] is False
    assert condition_view["frozen_network"]["readiness"] == "blocked"

    source = projection()
    source["states"][0]["payload"]["charge"] = None
    _rehash(source["states"][0])
    state_view = rw.build_reaction_workbench_view(source, project_id="project-1")
    conservation = state_view["graph"]["edges"][0]["conservation"]
    assert conservation["status"] == "unavailable"
    assert "authoritative_state_charge:state-r" in conservation["missing"]
    assert state_view["frozen_network"]["readiness"] == "blocked"


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
    assert left_view["migration_only"] is True
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
