import json

import pytest

from vcstudio.project.candidate_evaluation import (
    DEFAULT_DEADBAND_EV,
    DEFAULT_POLICY_ID,
    evaluate_batch,
    evaluate_candidate,
)


_FE_PROFILE = {
    "Li2S8": [-1.04],
    "Li2S6": [-1.14],
    "Li2S4": [-1.23],
    "Li2S2": [-2.09],
    "Li2S": [-2.81],
}


def _summary(values, *, method="verified", summary_method=None):
    rows = []
    for species, energies in values.items():
        for index, energy in enumerate(energies):
            row = {
                "name": f"{species}_{index}",
                "species": species,
                "delta_e": energy,
                "reference_valid": True,
            }
            if method is not None:
                row["method_check"] = {"status": method}
            rows.append(row)
    status = summary_method if summary_method is not None else method
    summary = {"rows": rows}
    if status is not None:
        summary["method_consistency"] = {
            "status": status,
            "issues": ["method conflict"] if status == "incompatible" else [],
            "warnings": ["method warning"] if status == "unverified" else [],
        }
    return summary


def _fed(eta=0.32, *, corrected=True, solvated=True):
    return {
        "steps": [
            {"label": "S8*", "G": 0.0},
            {"label": "Li2S4*", "G": -0.5},
            {"label": "Li2S*", "G": -0.3},
        ],
        "per_electron": [-0.5, 0.2],
        "pds_index": 1,
        "u_eq": 1.66,
        "u_l": 1.66 - eta,
        "eta": eta,
        "direction": "reduction",
        "reaction_path_id": "lis-balanced-path-v1",
        "thermo_corrected": corrected,
        "solvation_corrected": solvated,
    }


def _by_species(result):
    return {row["species"]: row for row in result["species"]}


def _recommendation_codes(result):
    return {row["code"] for row in result["recommendations"]}


def test_fe_like_profile_advances_only_as_followup_screen():
    result = evaluate_candidate(
        _summary(_FE_PROFILE),
        project={"name": "Fe@B1N3", "project_uuid": "fe-b1n3"},
    )

    assert result["schema"] == "vcstudio.candidate-evaluation/v1"
    assert result["audit"]["policy_id"] == DEFAULT_POLICY_ID
    assert result["profile"]["ranking_deadband_eV"] == DEFAULT_DEADBAND_EV
    assert result["candidate"] == {"project_id": "fe-b1n3", "name": "Fe@B1N3"}
    assert result["decision"]["priority"] == "advance"
    assert result["decision"]["claim_ceiling"] == "electronic_adsorption_screen"
    assert result["profile"]["trend"]["counts"] == {
        "strengthening": 2,
        "plateau": 2,
        "weakening": 0,
    }
    assert result["profile"]["long_chain_anchor"]["status"] == "adequate"
    assert result["profile"]["short_chain_risk"]["status"] == "medium"
    assert {
        row["species"]: row["category"] for row in result["species"]
    } == {
        "Li2S8": "moderate_anchor",
        "Li2S6": "moderate_anchor",
        "Li2S4": "moderate_anchor",
        "Li2S2": "strong_check_kinetics",
        "Li2S": "strong_check_kinetics",
    }
    assert "BUILD_BALANCED_FREE_ENERGY_PATH" in _recommendation_codes(result)
    assert "NEB_LI2S_CHARGE_DECOMPOSITION" in _recommendation_codes(result)


def test_result_is_deterministic_and_json_serialisable():
    summary = _summary(_FE_PROFILE)
    options = {"candidate_name": "same", "geometry_verified": True}
    first = evaluate_candidate(summary, options=options)
    second = evaluate_candidate(summary, options=options)

    assert first == second
    assert json.loads(json.dumps(first, ensure_ascii=False)) == first


def test_deadband_preserves_co_minima_and_boundary_uncertainty():
    result = evaluate_candidate(
        _summary({
            "Li2S8": [-1.04, -0.94, -0.70],
            "Li2S6": [-0.50],
            "Li2S4": [-2.00],
            "Li2S2": [-3.00],
            "Li2S": [-3.01],
        }),
    )
    rows = _by_species(result)

    assert rows["Li2S8"]["co_minima"] == ["Li2S8_0", "Li2S8_1"]
    assert rows["Li2S8"]["unique_minimum"] is False
    assert rows["Li2S8"]["value_eV"] == -1.04
    assert rows["Li2S6"]["category"] == "moderate_anchor"
    assert rows["Li2S4"]["category"] == "moderate_anchor"
    assert rows["Li2S2"]["category"] == "strong_check_kinetics"
    assert rows["Li2S"]["category"] == "overstrong_alert"
    assert all(rows[species]["near_boundary"] for species in (
        "Li2S6", "Li2S4", "Li2S2", "Li2S"))
    assert result["decision"]["priority"] == "lower_priority"


def test_differences_within_deadband_are_not_over_ranked():
    profile = {
        "Li2S8": [-1.00],
        "Li2S6": [-1.10],
        "Li2S4": [-1.25],
        "Li2S2": [-1.40],
        "Li2S": [-1.54],
    }
    result = evaluate_candidate(_summary(profile))

    assert [row["verdict"] for row in result["profile"]["trend"]["pairs"]] == [
        "plateau", "plateau", "plateau", "plateau",
    ]


def test_missing_method_evidence_holds_candidate():
    result = evaluate_candidate(_summary(_FE_PROFILE, method=None))

    assert result["comparability"]["status"] == "unverified"
    assert result["decision"]["priority"] == "hold_for_evidence"
    assert "METHOD_PROVENANCE_MISSING" in result["audit"]["reason_codes"]
    assert "AUDIT_METHOD_PROVENANCE" in _recommendation_codes(result)


def test_verified_summary_is_allowed_as_historical_fallback():
    result = evaluate_candidate(
        _summary(_FE_PROFILE, method=None, summary_method="verified"),
    )

    assert result["comparability"]["status"] == "verified"
    assert result["decision"]["priority"] == "advance"
    assert {row["method_status"] for row in result["species"]} == {"missing"}


def test_incompatible_summary_without_row_evidence_blocks():
    result = evaluate_candidate(
        _summary(_FE_PROFILE, method=None, summary_method="incompatible"),
    )

    assert result["comparability"]["status"] == "incompatible"
    assert result["decision"]["priority"] == "blocked"
    assert "METHOD_INCOMPATIBLE" in result["audit"]["reason_codes"]
    assert "RECONCILE_METHOD_CONFLICTS" in _recommendation_codes(result)


def test_unused_incompatible_config_does_not_poison_selected_operands():
    summary = _summary(_FE_PROFILE)
    summary["rows"].append({
        "name": "Li2S_bad_method",
        "species": "Li2S",
        "delta_e": -5.0,
        "method_check": {"status": "incompatible"},
    })
    summary["method_consistency"] = {
        "status": "incompatible",
        "issues": ["only Li2S_bad_method conflicts"],
        "warnings": [],
    }
    result = evaluate_candidate(summary)

    assert result["comparability"]["status"] == "verified"
    assert result["decision"]["priority"] == "advance"
    assert result["comparability"]["excluded_incompatible_configs"] == [
        "Li2S_bad_method",
    ]
    assert result["comparability"]["notices"][0]["code"] == (
        "UNUSED_INCOMPATIBLE_CONFIGS"
    )
    assert _by_species(result)["Li2S"]["value_eV"] == -2.81


def test_unverified_near_degenerate_config_lowers_method_confidence():
    summary = _summary(_FE_PROFILE)
    summary["rows"].append({
        "name": "Li2S8_near",
        "species": "Li2S8",
        "delta_e": -0.94,
        "reference_valid": True,
        "method_check": {"status": "unverified"},
    })
    result = evaluate_candidate(summary)

    assert _by_species(result)["Li2S8"]["co_minima"] == [
        "Li2S8_0", "Li2S8_near",
    ]
    assert _by_species(result)["Li2S8"]["method_status"] == "unverified"
    assert result["comparability"]["status"] == "unverified"
    assert result["decision"]["priority"] == "hold_for_evidence"


def test_all_incompatible_rows_block_evaluation():
    result = evaluate_candidate(
        _summary({"Li2S": [-2.5]}, method="incompatible"),
    )

    assert result["species"] == []
    assert result["decision"]["priority"] == "blocked"
    assert "NO_USABLE_ADSORPTION_ENERGY" in result["audit"]["reason_codes"]


def test_energy_without_adsorbate_reference_is_not_treated_as_adsorption_energy():
    summary = _summary(_FE_PROFILE)
    summary.update({"has_ref": False, "reference_mode": "none"})
    for row in summary["rows"]:
        row["reference_valid"] = False
        row["note"] = "未设气相参考:此值为 E(slab+ads)-E(slab)"
    result = evaluate_candidate(summary)

    assert result["comparability"]["status"] == "incompatible"
    assert result["decision"]["priority"] == "blocked"
    assert "ADSORBATE_REFERENCE_MISSING" in result["audit"]["reason_codes"]
    assert "CALC_VALID_ADSORBATE_REFERENCE" in _recommendation_codes(result)


def test_weak_long_chain_and_overbound_terminal_are_lower_priority():
    weak = dict(_FE_PROFILE)
    weak["Li2S8"] = [-0.20]
    weak_result = evaluate_candidate(_summary(weak))
    assert weak_result["profile"]["long_chain_anchor"]["status"] == (
        "weak_or_unfavorable"
    )
    assert weak_result["decision"]["priority"] == "lower_priority"

    overbound = dict(_FE_PROFILE)
    overbound["Li2S"] = [-3.30]
    overbound_result = evaluate_candidate(_summary(overbound))
    assert overbound_result["profile"]["short_chain_risk"]["status"] == "high"
    assert overbound_result["decision"]["priority"] == "lower_priority"
    assert "NEB_LI2S_CHARGE_DECOMPOSITION" in _recommendation_codes(
        overbound_result
    )


def test_partial_species_profile_is_held_for_more_evidence():
    result = evaluate_candidate(
        _summary({
            "Li2S8": [-1.0],
            "Li2S4": [-1.2],
            "Li2S": [-2.5],
        }),
    )

    assert result["decision"]["priority"] == "hold_for_evidence"
    assert result["evidence"]["coverage"]["missing_species"] == [
        "Li2S6", "Li2S2",
    ]
    missing = next(
        row for row in result["recommendations"]
        if row["code"] == "CALC_MISSING_LIS_SPECIES"
    )
    assert missing["targets"] == ["Li2S6", "Li2S2"]


def test_unicode_species_are_canonical_and_nonfinite_rows_are_audited():
    summary = _summary({"Li₂S₈": [-1.0], "Li₂S": [-2.4]})
    summary["rows"].append({
        "name": "nan-row",
        "species": "Li₂S₄",
        "delta_e": float("nan"),
        "method_check": {"status": "verified"},
    })
    result = evaluate_candidate(summary)

    assert [row["species"] for row in result["species"]] == ["Li2S8", "Li2S"]
    assert result["evidence"]["invalid_or_unresolved_rows"] == [{
        "name": "nan-row",
        "species": "Li2S4",
        "reason": "missing_or_nonfinite_delta_e",
    }]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"policy_id": "unknown"}, "policy_id"),
        ({"deadband_eV": -0.1}, "deadband_eV"),
        ({"quantity": "delta_G_ads"}, "quantity=delta_E_ads"),
        ({"sign_convention": "positive_is_stronger"}, "negative_is_stronger"),
    ],
)
def test_invalid_policy_contract_fails_closed(options, message):
    with pytest.raises(ValueError, match=message):
        evaluate_candidate(_summary(_FE_PROFILE), options=options)


def test_paper_volcano_strategy_requires_exact_free_energy_context():
    base = {
        "id": "zhang_lis2_assoc_volcano_v1",
        "reaction_path_id": "LIS_ASSOC_LIS2",
        "descriptor_species": "*LiS₂",
        "descriptor_value_eV": -1.65,
        "quantity": "delta_G_ads",
        "sign_convention": "negative_is_stronger",
    }
    result = evaluate_candidate(
        _summary(_FE_PROFILE),
        options={"paper_strategy": base},
    )
    paper = result["paper_volcano_strategy"]

    assert paper["status"] == "applicable"
    assert paper["descriptor"]["optimum_eV"] == -1.90
    assert paper["descriptor"]["distance_to_context_optimum_eV"] == 0.25
    assert paper["source"]["figure"] == "图3.13"

    wrong = dict(base)
    wrong.update({
        "quantity": "delta_E_ads",
        "reaction_path_id": "some-other-path",
        "descriptor_species": "Li2S2",
        "sign_convention": "positive_is_stronger",
    })
    blocked = evaluate_candidate(
        _summary(_FE_PROFILE),
        options={"paper_strategy": wrong},
    )["paper_volcano_strategy"]
    assert blocked["status"] == "not_applicable"
    assert set(blocked["reason_codes"]) == {
        "PAPER_STRATEGY_REQUIRES_DELTA_G_ADS",
        "PAPER_STRATEGY_PATH_MISMATCH",
        "PAPER_STRATEGY_DESCRIPTOR_MISMATCH",
        "PAPER_STRATEGY_SIGN_CONVENTION_MISMATCH",
    }


def test_other_paper_path_has_its_own_contextual_optimum():
    paper = evaluate_candidate(
        _summary(_FE_PROFILE),
        options={"paper_strategy": {
            "id": "zhang_lis_assoc_volcano_v1",
            "reaction_path_id": "LIS_ASSOC_LIS",
            "descriptor_species": "LiS2",
            "descriptor_value_eV": -2.64,
            "quantity": "delta_G_ads",
        }},
    )["paper_volcano_strategy"]

    assert paper["applicable"] is True
    assert paper["descriptor"]["optimum_eV"] == -2.85
    assert paper["descriptor"]["distance_to_context_optimum_eV"] == 0.21


def test_free_energy_identity_and_claim_ceiling_are_audited():
    result = evaluate_candidate(_summary(_FE_PROFILE), fed=_fed())

    thermo = result["thermodynamics"]
    assert thermo["potential_identity"]["ok"] is True
    assert thermo["delta_g_max_step_eV"] == 0.2
    assert thermo["delta_g_max_per_electron_eV"] == 0.2
    assert result["evidence"]["claim_ceiling"] == "solvated_thermodynamics"
    assert result["decision"]["priority"] == "advance"


def test_inconsistent_reported_potential_holds_otherwise_promising_profile():
    fed = _fed()
    fed.update({"u_l": 1.98, "eta": 0.32})
    result = evaluate_candidate(_summary(_FE_PROFILE), fed=fed)

    assert result["thermodynamics"]["status"] == "inconsistent"
    assert result["thermodynamics"]["potential_identity"]["ok"] is False
    assert result["decision"]["priority"] == "hold_for_evidence"
    assert "POTENTIAL_IDENTITY_FAILED" in result["audit"]["reason_codes"]
    assert "AUDIT_FREE_ENERGY_METRICS" in _recommendation_codes(result)


def test_dac_candidates_request_required_baselines():
    result = evaluate_candidate(
        _summary(_FE_PROFILE),
        options={"catalyst_kind": "DAC"},
    )

    assert "ADD_DAC_BASELINES" in _recommendation_codes(result)


def test_batch_without_explicit_protocol_never_emits_precise_ranking():
    batch = evaluate_batch([
        {
            "candidate_name": "A",
            "delta_summary": _summary(_FE_PROFILE),
            "fed": _fed(eta=0.30),
        },
        {
            "candidate_name": "B",
            "delta_summary": _summary(_FE_PROFILE),
            "fed": _fed(eta=0.10),
        },
    ])

    assert batch["n_candidates"] == 2
    assert len(batch["cohorts"]) == 1
    assert batch["cohorts"][0]["comparison_protocol_verified"] is False
    assert batch["cohorts"][0]["ranking"]["status"] == "tiered_only"


def test_batch_ranks_eta_only_inside_explicit_verified_cohort():
    batch = evaluate_batch([
        {
            "candidate_name": "A",
            "comparison_protocol_id": "same-protocol",
            "delta_summary": _summary(_FE_PROFILE),
            "fed": _fed(eta=0.30),
        },
        {
            "candidate_name": "B",
            "comparison_protocol_id": "same-protocol",
            "delta_summary": _summary(_FE_PROFILE),
            "fed": _fed(eta=0.10),
        },
    ])

    ranking = batch["cohorts"][0]["ranking"]
    assert ranking["status"] == "ranked_by_eta"
    assert [(row["rank"], row["name"], row["eta_V"]) for row in ranking["order"]] == [
        (1, "B", 0.1),
        (2, "A", 0.3),
    ]


def test_batch_keeps_different_protocols_in_separate_cohorts():
    batch = evaluate_batch([
        {
            "candidate_name": "A",
            "comparison_protocol_id": "protocol-a",
            "delta_summary": _summary(_FE_PROFILE),
            "fed": _fed(eta=0.30),
        },
        {
            "candidate_name": "B",
            "comparison_protocol_id": "protocol-b",
            "delta_summary": _summary(_FE_PROFILE),
            "fed": _fed(eta=0.10),
        },
    ])

    assert len(batch["cohorts"]) == 2
    assert {cohort["members"][0] for cohort in batch["cohorts"]} == {"A", "B"}


@pytest.mark.parametrize("items", [None, "not-a-list", [None], [{}]])
def test_invalid_batch_inputs_fail_closed(items):
    with pytest.raises(ValueError):
        evaluate_batch(items)
