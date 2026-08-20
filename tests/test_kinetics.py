"""Strict microkinetic input-audit and result-import contracts."""
from __future__ import annotations

import copy
import hashlib

import pytest

from vcstudio.project import kinetics


_EVIDENCE = {
    "reaction-map:rev-7": b"canonical frozen reaction network revision 7",
    "neb:co-o-ts": b"NEB transition-state artifact",
    "manifest:job-1": b"project job manifest evidence",
}


class FrozenNetwork(dict):
    """Test-only stand-in for the server-injected canonical provider DTO."""

    def kinetics_input(self):
        return copy.deepcopy(dict(self))

    def kinetics_evidence(self, reference):
        return _EVIDENCE[reference]

    def canonical_source_projection(self):
        value = copy.deepcopy(dict(self))
        value.pop("input_sha256", None)
        metadata = value.pop("source_projection")
        value["schema"] = metadata["schema"]
        value["version"] = metadata["version"]
        value["evidence_refs"] = copy.deepcopy(metadata["evidence_refs"])
        hash_field = (
            "frozen_network_sha256"
            if metadata["schema"] == "vcstudio.frozen-reaction-network/v1"
            else "projection_sha256")
        value[hash_field] = ""
        if (metadata["schema"], metadata["version"]) in (
                kinetics.SOURCE_PROJECTION_PROTOCOLS):
            value[hash_field] = kinetics.compute_source_projection_sha256(value)
        else:
            value[hash_field] = "f" * 64
        return value


def _freeze(network):
    network["source_projection"]["projection_sha256"] = (
        kinetics.compute_projection_sha256(network))
    network["input_sha256"] = kinetics.compute_input_sha256(network)
    return network


def _source(kind="dft", reference="manifest:job-1"):
    return {
        "kind": kind,
        "reference": reference,
        "evidence_sha256": hashlib.sha256(_EVIDENCE[reference]).hexdigest(),
    }


def _energy(value, *, method_id="rpbe-d3", uncertainty_eV=0.05):
    return {
        "value": value,
        "unit": "eV",
        "method_id": method_id,
        "source": _source(),
        "uncertainty_eV": uncertainty_eV,
    }


def frozen_network():
    network = FrozenNetwork({
        "schema": kinetics.NETWORK_SCHEMA,
        "input_sha256": "",
        "network_id": "co-oxidation-111",
        "revision": "rev-7",
        "source_projection": {
            "schema": "vcstudio.frozen-reaction-network/v1",
            "version": "1",
            "projection_sha256": "",
            "evidence_refs": [
                {
                    "reference": reference,
                    "artifact_sha256": hashlib.sha256(data).hexdigest(),
                }
                for reference, data in _EVIDENCE.items()
            ],
        },
        "assumptions": {
            "mean_field": True,
            "steady_state": True,
            "site_uniformity": "uniform",
            "lateral_interactions": "neglected",
            "mechanism_completeness": "claimed_complete",
        },
        "standard_state": {
            "temperature": {"value": 500.0, "unit": "K"},
            "pressure": {"value": 1.0, "unit": "bar"},
            "concentration": {"value": 1.0, "unit": "mol/L"},
            "potential": None,
        },
        "operating_range": {
            "temperature_K": [450.0, 650.0],
            "pressure_bar": [0.1, 10.0],
            "potential_V": None,
        },
        "methodology": {
            "method_id": "rpbe-d3",
            "energy_basis": "gibbs_free_energy",
            "thermochemistry": "harmonic-ideal-gas",
            "solvation": "none",
            "potential_model": "none",
            "compatibility_status": "verified",
            "identity_sha256": "3" * 64,
        },
        "feed_species": ["CO_s", "O_s"],
        "target_products": ["CO2_g"],
        "species": [
            {
                "id": "CO_g", "phase": "gas", "composition": {"C": 1, "O": 1},
                "charge": 0, "sites": {}, "formation_energy": _energy(0.0),
                "frequencies_cm1": [2143.0],
                "activity": {"value": 0.5, "unit": "bar", "source": _source("condition")},
            },
            {
                "id": "O2_g", "phase": "gas", "composition": {"O": 2},
                "charge": 0, "sites": {}, "formation_energy": _energy(0.0),
                "frequencies_cm1": [1556.0],
                "activity": {"value": 0.5, "unit": "bar", "source": _source("condition")},
            },
            {
                "id": "CO2_g", "phase": "gas", "composition": {"C": 1, "O": 2},
                "charge": 0, "sites": {}, "formation_energy": _energy(-2.0),
                "frequencies_cm1": [667.0, 1333.0, 2349.0],
                "activity": {"value": 0.0, "unit": "bar", "source": _source("condition")},
            },
            {
                "id": "star_s", "phase": "surface", "composition": {},
                "charge": 0, "sites": {"s": 1}, "formation_energy": _energy(0.0),
                "frequencies_cm1": [],
            },
            {
                "id": "CO_s", "phase": "adsorbate", "composition": {"C": 1, "O": 1},
                "charge": 0, "sites": {"s": 1}, "formation_energy": _energy(-1.0),
                "frequencies_cm1": [350.0, 1800.0],
            },
            {
                "id": "O_s", "phase": "adsorbate", "composition": {"O": 1},
                "charge": 0, "sites": {"s": 1}, "formation_energy": _energy(-1.0),
                "frequencies_cm1": [500.0],
            },
            {
                "id": "COO_ts", "phase": "transition_state",
                "composition": {"C": 1, "O": 2}, "charge": 0,
                "sites": {"s": 1}, "formation_energy": _energy(0.2),
                "frequencies_cm1": [-450.0, 300.0, 700.0],
            },
        ],
        "elementary_steps": [
            {
                "id": "co_oxidation",
                "reactants": {"CO_s": 1, "O_s": 1},
                "transition_state": {"COO_ts": 1, "star_s": 1},
                "products": {"CO2_g": 1, "star_s": 2},
                "reversible": True,
                "delta_g": _energy(0.0),
                "forward_barrier": _energy(2.2),
                "reverse_barrier": _energy(2.2),
                "prefactors": {
                    "forward": {"value": 1.0e13, "unit": "s^-1", "source": _source("tst")},
                    "reverse": {"value": 1.0e13, "unit": "s^-1", "source": _source("tst")},
                },
                "bep": {"used": False, "source": None, "parameters_sha256": None},
                "scaling": {"used": False, "source": None, "parameters_sha256": None},
                "uncertainty_eV": 0.10,
            },
        ],
        "extensions": {},
    })
    return _freeze(network)


def valid_result(network=None):
    network = network or frozen_network()
    result = {
        "schema": kinetics.RESULT_SCHEMA,
        "input_sha256": network["input_sha256"],
        "adapter": {
            "id": "vcstudio.catmap-process-adapter",
            "version": "2",
            "tool_version": "0.4.0",
            "tool_sha256": "4" * 64,
        },
        "units": copy.deepcopy(kinetics.RESULT_UNITS),
        "points": [
            {
                "conditions": {
                    "temperature": 500.0, "pressure": 1.0, "potential": None,
                },
                "tof": [{"species_id": "CO2_g", "value": 2.5}],
                "coverage": [
                    {"species_id": "CO_s", "site_type": "s", "value": 0.35},
                    {"species_id": "O_s", "site_type": "s", "value": 0.25},
                ],
                "selectivity": [{"species_id": "CO2_g", "value": 1.0}],
                "drc": [{"step_id": "co_oxidation", "value": 0.9}],
                "dsc": [{"step_id": "co_oxidation", "value": 0.1}],
                "reaction_order": [{"species_id": "CO_g", "value": 0.8}],
                "apparent_activation_energy": [{"species_id": "CO2_g", "value": 0.7}],
                "free_energy_diagram": [
                    {"state_id": "reactants", "value": -2.0},
                    {"state_id": "COO_ts", "value": 0.2},
                    {"state_id": "products", "value": -2.0},
                ],
                "convergence": {
                    "converged": True, "residual": 1.0e-12,
                    "iterations": 42, "solver": "numbers",
                },
            },
        ],
        "sensitivity": {
            "analyses": [
                {
                    "kind": "energy_uncertainty", "point_index": 0,
                    "condition_sha256": "", "step_id": "co_oxidation",
                    "perturbation_eV": 0.10, "max_relative_change": 0.12,
                },
            ],
            "warnings": [],
        },
    }
    result["sensitivity"]["analyses"][0]["condition_sha256"] = (
        kinetics.compute_condition_sha256(result["points"][0]["conditions"]))
    return result


def _codes(audit):
    return {item["code"] for item in audit["issues"]}


def test_valid_frozen_projection_passes_all_machine_audits():
    network = frozen_network()
    audit = kinetics.audit_network(network)

    assert audit["schema"] == kinetics.AUDIT_SCHEMA
    assert audit["input_sha256"] == network["input_sha256"]
    assert audit["machine_pass"] is True
    assert audit["export_ready"] is True
    assert audit["scientific_status"] == "diagnostic"
    assert audit["eligible_final"] is False
    assert audit["denominator"] == {"species": 7, "elementary_steps": 1, "checks": 12}
    assert not [item for item in audit["issues"] if item["severity"] == "error"]


def test_protocol_provider_is_consumed_without_owning_upstream_dto():
    class Provider:
        def kinetics_input(self):
            return frozen_network()

        def kinetics_evidence(self, reference):
            return _EVIDENCE[reference]

        def canonical_source_projection(self):
            return frozen_network().canonical_source_projection()

    assert kinetics.audit_network(Provider())["machine_pass"] is True


def test_raw_mapping_fabricated_projection_and_evidence_are_never_trusted():
    network = frozen_network()
    raw_audit = kinetics.audit_network(dict(network))
    assert raw_audit["machine_pass"] is False
    assert "INVALID_INPUT" in _codes(raw_audit)

    network["source_projection"]["projection_sha256"] = "f" * 64
    network["input_sha256"] = kinetics.compute_input_sha256(network)
    fabricated = kinetics.audit_network(network)
    assert fabricated["machine_pass"] is False
    assert "INVALID_INPUT" in _codes(fabricated)


def test_projection_schema_and_real_artifact_hash_are_provider_verified():
    network = frozen_network()
    network["source_projection"]["schema"] = "caller.fabricated/v9"
    network["input_sha256"] = kinetics.compute_input_sha256(network)
    assert "INVALID_INPUT" in _codes(kinetics.audit_network(network))

    class BadEvidence(FrozenNetwork):
        def kinetics_evidence(self, reference):
            if reference == "manifest:job-1":
                return b"different artifact bytes"
            return super().kinetics_evidence(reference)

    network = BadEvidence(frozen_network())
    assert "INVALID_INPUT" in _codes(kinetics.audit_network(network))


def test_nested_energy_source_cannot_fabricate_artifact_sha():
    network = frozen_network()
    network["species"][0]["formation_energy"]["source"][
        "evidence_sha256"] = "f" * 64
    _freeze(network)
    audit = kinetics.audit_network(network)
    assert audit["machine_pass"] is False
    assert "INVALID_INPUT" in _codes(audit)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda n: n["species"][5]["composition"].update({"O": 2}), "ELEMENT_NOT_CONSERVED"),
        (lambda n: n["species"][5].update({"charge": -1}), "CHARGE_NOT_CONSERVED"),
        (lambda n: n["species"][5]["sites"].update({"s": 2}), "SITE_NOT_CONSERVED"),
        (lambda n: n["elementary_steps"][0].update({"reverse_barrier": _energy(1.8)}),
         "DETAILED_BALANCE_MISMATCH"),
        (lambda n: n["elementary_steps"][0].update({"reversible": False}),
         "REVERSE_BARRIER_REQUIRED"),
        (lambda n: n["methodology"].update({"compatibility_status": "unknown"}),
         "METHOD_COMPATIBILITY_UNVERIFIED"),
        (lambda n: n["assumptions"].update({"mechanism_completeness": "unknown"}),
         "MECHANISM_COMPLETENESS_UNRESOLVED"),
        (lambda n: n["operating_range"].update({"temperature_K": [-1, 650]}),
         "INVALID_OPERATING_RANGE"),
        (lambda n: n["species"][4]["formation_energy"].update({"uncertainty_eV": None}),
         "ENERGY_UNCERTAINTY_REQUIRED"),
        (lambda n: n["elementary_steps"][0]["prefactors"].pop("forward"),
         "PREFACTOR_REQUIRED"),
        (lambda n: n["species"][0]["activity"].update({"value": 0.2}),
         "PARTIAL_PRESSURE_SUM_MISMATCH"),
        (lambda n: n["methodology"].update({"potential_model": "che"}),
         "POTENTIAL_RANGE_REQUIRED"),
    ],
)
def test_scientific_input_failures_are_explicit_and_block_export(mutate, code):
    network = frozen_network()
    mutate(network)
    _freeze(network)

    audit = kinetics.audit_network(network)

    assert code in _codes(audit)
    assert audit["machine_pass"] is False
    assert audit["export_ready"] is False
    assert audit["scientific_status"] == "unavailable"


def test_hash_duplicates_missing_species_and_missing_path_are_rejected():
    network = frozen_network()
    network["input_sha256"] = "f" * 64
    network["elementary_steps"].append(copy.deepcopy(network["elementary_steps"][0]))
    network["elementary_steps"][1]["id"] = "duplicate-id"
    network["target_products"] = ["missing_product"]
    _freeze(network)

    audit = kinetics.audit_network(network)

    assert {"DUPLICATE_STEP", "UNKNOWN_TARGET_SPECIES"} <= _codes(audit)


def test_unknown_fields_paths_commands_and_nonfinite_values_fail_closed():
    network = frozen_network()
    network["run_command"] = "python -c __import__('os').system('whoami')"
    network["species"][0]["id"] = "../../escape"
    network["species"][1]["formation_energy"]["value"] = float("nan")

    audit = kinetics.audit_network(network)

    assert "INVALID_INPUT" in _codes(audit)


def test_result_import_is_strict_server_normalized_and_always_diagnostic():
    network = frozen_network()
    normalized = kinetics.import_result(
        valid_result(network), network,
        expected_adapter={
            "id": "vcstudio.catmap-process-adapter", "version": "2",
            "tool_sha256": "4" * 64,
        },
    )

    assert normalized["schema"] == kinetics.NORMALIZED_RESULT_SCHEMA
    assert normalized["input_sha256"] == network["input_sha256"]
    assert normalized["scientific_status"] == "diagnostic"
    assert normalized["eligible_final"] is False
    assert normalized["available"] is True
    assert normalized["points"][0]["tof"][0]["value"] == 2.5
    assert normalized["limitations"]["mean_field"] is True
    assert normalized["limitations"]["browser_solves"] is False


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r.update({"schema": "evil/v9"}), "schema"),
        (lambda r: r.update({"input_sha256": "0" * 64}), "input hash"),
        (lambda r: r["units"].update({"tof": "mol/s"}), "units"),
        (lambda r: r["adapter"].update({"tool_sha256": "5" * 64}), "tool hash"),
        (lambda r: r["points"][0]["coverage"][0].update({"value": 1.2}), "coverage"),
        (lambda r: r["points"][0]["conditions"].update({"temperature": 900.0}),
         "operating range"),
        (lambda r: r["points"][0]["convergence"].update({"solver": "../../cmd.exe"}),
         "safe identifier"),
        (lambda r: r["points"][0]["tof"][0].update({"value": float("inf")}),
         "finite number"),
        (lambda r: r["points"][0]["tof"][0].update({"species_id": "invented"}),
         "outside the frozen network"),
    ],
)
def test_malicious_or_mismatched_results_are_rejected(mutate, match):
    network = frozen_network()
    result = valid_result(network)
    mutate(result)

    with pytest.raises(kinetics.KineticsContractError, match=match):
        kinetics.import_result(
            result, network,
            expected_adapter={
                "id": "vcstudio.catmap-process-adapter", "version": "2",
                "tool_sha256": "4" * 64,
            },
        )


def test_reported_converged_flag_is_ignored_and_server_residual_controls_status():
    network = frozen_network()
    result = valid_result(network)
    result["points"][0]["convergence"]["converged"] = False

    normalized = kinetics.import_result(
        result, network,
        expected_adapter={
            "id": "vcstudio.catmap-process-adapter", "version": "2",
            "tool_sha256": "4" * 64,
        },
    )

    assert normalized["available"] is True
    assert normalized["points"][0]["convergence"]["reported_converged"] is False
    result["points"][0]["convergence"].update({
        "converged": True, "residual": 1.0e-4,
    })
    normalized = kinetics.import_result(
        result, network,
        expected_adapter={
            "id": "vcstudio.catmap-process-adapter", "version": "2",
            "tool_sha256": "4" * 64,
        },
    )
    assert normalized["available"] is False
    assert "NUMERICAL_NOT_CONVERGED" in normalized["reason_codes"]


def test_empty_or_self_reported_sensitivity_is_rejected():
    network = frozen_network()
    result = valid_result(network)
    result["sensitivity"] = {
        "status": "unavailable", "analyses": [], "warnings": ["not computed"],
    }

    with pytest.raises(kinetics.KineticsContractError, match="unknown fields"):
        kinetics.import_result(
            result, network,
            expected_adapter={
                "id": "vcstudio.catmap-process-adapter", "version": "2",
                "tool_sha256": "4" * 64,
            },
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r["sensitivity"].update({"analyses": []}), "non-empty"),
        (lambda r: r["sensitivity"]["analyses"][0].update(
            {"condition_sha256": "0" * 64}), "condition_sha256"),
        (lambda r: r["sensitivity"]["analyses"][0].update(
            {"step_id": "invented"}), "frozen steps/points"),
        (lambda r: r["sensitivity"]["analyses"][0].update(
            {"perturbation_eV": 0.2}), "frozen step uncertainty"),
        (lambda r: r["points"][0]["convergence"].update(
            {"iterations": kinetics.CONVERGENCE_ITERATIONS_MAX + 1}), "iterations"),
    ],
)
def test_result_availability_evidence_is_complete_bounded_and_frozen(mutate, match):
    network = frozen_network()
    result = valid_result(network)
    mutate(result)
    with pytest.raises(kinetics.KineticsContractError, match=match):
        kinetics.import_result(
            result, network,
            expected_adapter={
                "id": "vcstudio.catmap-process-adapter", "version": "2",
                "tool_sha256": "4" * 64,
            },
        )


def test_sensitivity_status_is_server_derived_from_complete_analysis():
    network = frozen_network()
    result = valid_result(network)
    result["sensitivity"]["analyses"][0]["max_relative_change"] = 1.5
    normalized = kinetics.import_result(
        result, network,
        expected_adapter={
            "id": "vcstudio.catmap-process-adapter", "version": "2",
            "tool_sha256": "4" * 64,
        },
    )
    assert normalized["sensitivity"]["status"] == "warning"
    assert normalized["sensitivity"]["coverage"] == {
        "required": 1, "observed": 1, "complete": True,
    }
