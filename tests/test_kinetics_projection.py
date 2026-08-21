"""One-shot frozen Reaction + exact model-spec projection tests."""
from __future__ import annotations

import copy
import hashlib

import pytest

from tests.test_kinetics import _EVIDENCE, _freeze, catmap_ready_network
from vcstudio.external import catmap_adapter
from vcstudio.project import kinetics
from vcstudio.project import kinetics_projection as projection_module
from vcstudio.project import reaction_workbench as reaction_workbench
from vcstudio.project.catalysis_contracts import (
    DomainEnvelope,
    ElementaryStep,
    FluidStandardState,
    FluidState,
    ReactionNetwork,
)
from vcstudio.project.catalysis_runtime import ValidatedReactionSnapshot
from vcstudio.project.kinetics_model_spec import KineticsModelSpec
from vcstudio.project.kinetics_projection import (
    KineticsProjectionError,
    KineticsProjectionProvider,
    ValidatedFrozenReactionV2,
    ValidatedFrozenReactionAuthoritySnapshot,
    required_frozen_v2_evidence_references,
)
from tests import test_reaction_workbench as reaction_fixtures


_DOMAIN_AUTHORITY = "d" * 32


class FrozenReactionFixture:
    """Temporary fixture for the narrow Reaction worker hand-off protocol."""

    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.read_count = 0

    def frozen_reaction_projection(self):
        self.read_count += 1
        return copy.deepcopy(self.value)


class Resolver:
    def __init__(self, values=None):
        self.values = dict(_EVIDENCE if values is None else values)
        self.calls = []

    def __call__(self, reference):
        self.calls.append(reference)
        return self.values[reference]


class MutatedProvider:
    def __init__(self, provider, mutate):
        self.provider = provider
        self.value = copy.deepcopy(provider.kinetics_input())
        mutate(self.value)
        self.value["input_sha256"] = kinetics.compute_input_sha256(self.value)

    def kinetics_input(self):
        return copy.deepcopy(self.value)

    def kinetics_evidence(self, reference):
        return self.provider.kinetics_evidence(reference)

    def canonical_source_projection(self):
        return self.provider.canonical_source_projection()

    def kinetics_model_spec(self):
        return self.provider.kinetics_model_spec()


def _real_fluid_projection(*, phase="gas", gas_standard_state="1-bar"):
    source = reaction_fixtures.projection()
    fluid_phase = phase
    if fluid_phase == "gas" and gas_standard_state == "1-atm":
        fluid_states = [
            DomainEnvelope.wrap(FluidState(
                state_id=state_id,
                phase="gas",
                chemical_formula=formula,
                charge=0,
                multiplicity=1,
                standard_state=FluidStandardState(
                    phase="gas", kind="1-atm", value=101325.0, unit="Pa"),
                **reaction_fixtures._domain_common(f"ev-{state_id}"),
            )).to_dict()
            for state_id, formula in (("co-g", "CO"), ("co2-g", "CO2"))
        ]
    else:
        fluid_states = [
            reaction_fixtures._fluid_state("co-g", "CO", phase=fluid_phase),
            reaction_fixtures._fluid_state("co2-g", "CO2", phase=fluid_phase),
        ]
    states = [
        *fluid_states,
        reaction_fixtures._state("o-ads", "O"),
        reaction_fixtures._state("vacancy", "*"),
        reaction_fixtures._state("state-ts", "CO2"),
    ]
    step = DomainEnvelope.wrap(ElementaryStep(
        step_id="step-1",
        reactants=(
            reaction_fixtures._participant(
                "co-g", phase=fluid_phase, site=None),
            reaction_fixtures._participant("o-ads"),
        ),
        transition_state=(reaction_fixtures._participant("state-ts"),),
        products=(
            reaction_fixtures._participant(
                "co2-g", phase=fluid_phase, site=None),
            reaction_fixtures._participant("vacancy"),
        ),
        condition_set_id="condition-1", reversible=True,
        **reaction_fixtures._domain_common("step-evidence"),
    )).to_dict()
    network = DomainEnvelope.wrap(ReactionNetwork(
        network_id="network-1", surface_ids=("surface-1",),
        state_ids=tuple(item["object_id"] for item in states),
        step_ids=("step-1",), condition_set_ids=("condition-1",),
        **reaction_fixtures._domain_common("network-evidence"),
    )).to_dict()
    source.update({"states": states, "steps": [step], "network": network})
    source["authority"].update({
        "network_revision_id": network["object_revision_id"],
        "network_semantic_sha256": network["semantic_sha256"],
    })
    identifiers = (
        "network-1", "surface-1", "condition-1", "step-1",
        *(item["object_id"] for item in states),
    )
    source["bindings"] = {
        identifier: {
            "label": identifier,
            "structure_sha256": reaction_fixtures.H["structure"],
            "evidence_sha256": reaction_fixtures.H["evidence"],
            "scientific_status": "machine_pass", "origin": "observed",
        }
        for identifier in identifiers
    }
    for state_id, energy in (
            ("co-g", -5.0), ("co2-g", -7.0),
            ("o-ads", -2.0), ("vacancy", 0.0)):
        source["bindings"][state_id]["thermochemistry"] = (
            reaction_fixtures._thermo(energy))
    source["bindings"]["step-1"]["saddle"] = {
        "step_semantic_sha256": step["semantic_sha256"],
        "transition_side_sha256": reaction_workbench.semantic_sha256(
            step["payload"]["transition_state"]),
        "label": "CO oxidation saddle",
        "structure_sha256": reaction_fixtures.H["structure"],
        "evidence_sha256": reaction_fixtures.H["evidence"],
        "scientific_status": "machine_pass", "origin": "observed",
        "thermochemistry": reaction_fixtures._thermo(-6.0, ts=True),
    }
    edge_evidence = copy.deepcopy(
        reaction_fixtures.projection()["bindings"]["step-1"]["edge_evidence"])
    reactant_hashes = {
        "co-g": reaction_fixtures.H["structure"],
        "o-ads": reaction_fixtures.H["structure"],
    }
    product_hashes = {
        "co2-g": reaction_fixtures.H["structure"],
        "vacancy": reaction_fixtures.H["structure"],
    }
    edge_evidence["reactant_structure_sha256"] = reactant_hashes
    edge_evidence["product_structure_sha256"] = product_hashes
    edge_evidence["neb"]["reactant_structure_sha256"] = reactant_hashes
    edge_evidence["neb"]["product_structure_sha256"] = product_hashes
    edge_evidence["neb"]["observed_reverse_delta_e_barrier"]["value"] = 0.8
    source["bindings"]["step-1"]["edge_evidence"] = edge_evidence
    return source


def _real_frozen_v2(*, phase="gas", gas_standard_state="1-bar"):
    reaction_projection = _real_fluid_projection(
        phase=phase, gas_standard_state=gas_standard_state)
    conditions = {
        "temperature_k": 300.0, "pressure_pa": 100000.0,
        "ph": 0.0, "electrode_potential_v": 0.0, "coverage": 0.25,
    }
    view = reaction_workbench.build_reaction_workbench_view(
        reaction_projection, project_id="project-1", conditions=conditions)
    frozen = view["frozen_network"]
    assert frozen["readiness"] == "ready"
    identity = ValidatedReactionSnapshot(
        project_id="project-1", projection=reaction_projection,
        source_projection_sha256=frozen["source_projection_sha256"],
        domain_authority_id=frozen["domain_authority"]["authority_id"],
        domain_generation=frozen["domain_authority"]["generation"],
        network_id=frozen["network_id"],
        network_revision=frozen["network_identity"]["object_revision_id"],
    )
    references = required_frozen_v2_evidence_references(frozen)
    artifacts = {
        reference: f"artifact:{reference}".encode("utf-8")
        for reference in references
    }
    bindings = {
        reference: hashlib.sha256(artifact).hexdigest()
        for reference, artifact in artifacts.items()
    }
    frozen_authority = _frozen_authority(frozen)
    adapter = ValidatedFrozenReactionV2(
        frozen, validated_reaction=identity, validated_frozen=frozen_authority,
        evidence_bindings=bindings,
        evidence_resolver=artifacts.__getitem__)
    return frozen, identity, frozen_authority, artifacts, bindings, adapter


def _frozen_authority(frozen, *, conditions_sha256=None):
    return ValidatedFrozenReactionAuthoritySnapshot(
        frozen_network_sha256=frozen["frozen_network_sha256"],
        condition_revision_id=frozen["condition_revision_id"],
        condition_revision_sha256=frozen["condition_revision_sha256"],
        conditions_sha256=(
            conditions_sha256
            or reaction_workbench.semantic_sha256(frozen["conditions"])),
    )


def _rehash_frozen(frozen):
    frozen["frozen_network_sha256"] = reaction_workbench.semantic_sha256({
        key: value for key, value in frozen.items()
        if key != "frozen_network_sha256"
    })


def _v2_spec(frozen, bindings, *, phase="gas"):
    evidence_reference = next(iter(sorted(bindings)))
    evidence_digest = bindings[evidence_reference]
    source_record = {
        "kind": "frozen-v2-artifact", "reference": evidence_reference,
        "evidence_sha256": evidence_digest,
    }
    fluid_unit = "bar" if phase == "gas" else "mol/L"
    return KineticsModelSpec.from_dict({
        "schema": KineticsModelSpec.schema,
        "project_id": "project-1", "spec_id": "fluid-model",
        "revision": "spec-r1", "parent_revision": None,
        "expected_current_hash": None,
        "source_binding": {
            "domain_authority_id": frozen["domain_authority"]["authority_id"],
            "domain_generation": frozen["domain_authority"]["generation"],
            "network_id": frozen["network_id"],
            "network_revision": frozen["network_identity"]["object_revision_id"],
            "source_projection_sha256": frozen["frozen_network_sha256"],
        },
        "rate_law_policy": {
            "activity": "ideal", "reversibility": "explicit_reverse",
            "detailed_balance": "enforced", "prefactor": "explicit_per_step",
            "electrochemical": "none", "reactor": "mean_field_steady_state",
        },
        "assumptions": {
            "mean_field": True, "steady_state": True,
            "site_uniformity": "uniform", "lateral_interactions": "neglected",
            "mechanism_completeness": "claimed_complete",
            "evidence": [source_record],
        },
        "feed_reservoirs": [
            {
                "species_id": "co-g", "activity": 1.0,
                "unit": fluid_unit, "source": evidence_reference,
                "evidence_sha256": evidence_digest,
            },
            {
                "species_id": "co2-g", "activity": 0.0,
                "unit": fluid_unit, "source": evidence_reference,
                "evidence_sha256": evidence_digest,
            },
            {
                "species_id": "o-ads", "activity": 1.0,
                "unit": "dimensionless", "source": evidence_reference,
                "evidence_sha256": evidence_digest,
            },
        ],
        "target_products": ["co2-g"],
        "steps": [{
            "step_id": "step-1",
            "prefactors": {
                direction: {
                    "value": 1.0e13, "unit": "s^-1", "source": source_record,
                }
                for direction in ("forward", "reverse")
            },
            "bep": {"used": False, "source": None, "parameters_sha256": None},
            "scaling": {
                "used": False, "source": None, "parameters_sha256": None},
            "uncertainty_eV": 0.05, "evidence": [source_record],
        }],
        "site_population_totals": [{
            "site_type": "top", "value": 1.0, "unit": "sites",
            "basis": "surface_unit_cell", "evidence": [source_record],
        }],
        "saddle_selector": None,
    })


def _v2_provider(*, phase="gas", gas_standard_state="1-bar"):
    frozen, identity, frozen_authority, artifacts, bindings, adapter = (
        _real_frozen_v2(
            phase=phase, gas_standard_state=gas_standard_state))
    spec = _v2_spec(frozen, bindings, phase=phase)
    provider = KineticsProjectionProvider(
        adapter, spec, adapter.kinetics_evidence)
    return (
        provider, frozen, identity, frozen_authority, artifacts, bindings,
        adapter)


def test_real_frozen_v2_gas_adapts_to_network_v3_without_mixed_authority():
    provider, frozen, _, _, _, _, adapter = _v2_provider(phase="gas")

    source = adapter.frozen_reaction_projection()
    network = provider.kinetics_input()
    audit = kinetics.audit_network(provider)
    bundle = catmap_adapter.build_export_bundle(provider, tool_path=None)

    assert source["authoritative_source"] == {
        "schema": "vcstudio.frozen-reaction-network/v2", "version": "2",
        "frozen_network_sha256": frozen["frozen_network_sha256"],
    }
    assert all("activity" not in item for item in source["species"])
    assert all(not ({
        "prefactors", "bep", "scaling", "uncertainty_eV", "evidence",
    } & set(item)) for item in source["elementary_steps"])
    assert network["schema"] == "vcstudio.kinetics-network/v3"
    assert network["source_projection"]["schema"] == (
        "vcstudio.frozen-reaction-network/v2")
    assert network["source_projection"]["projection_sha256"] == (
        frozen["frozen_network_sha256"])
    assert network["model_spec"]["source_projection_sha256"] == (
        frozen["frozen_network_sha256"])
    assert network["feed_species"] == ["co-g", "o-ads"]
    assert next(
        item for item in network["species"] if item["id"] == "co2-g"
    )["activity"] == {
        "value": 0.0, "unit": "bar",
        "source": next(
            item for item in network["species"] if item["id"] == "co2-g"
        )["activity"]["source"],
    }
    frozen_types = {
        item["state_id"]: item["object_type"]
        for item in frozen["state_catalog"]
    }
    assert all(
        frozen_types[item["id"]] == "FluidState"
        for item in network["species"] if item["phase"] == "gas")
    assert audit["machine_pass"] is True
    assert bundle["contract_ready"] is True
    assert "model.mkm" in bundle["files"]


def test_real_frozen_v2_one_atm_is_core_valid_but_catmap_is_audit_only():
    provider, *_ = _v2_provider(
        phase="gas", gas_standard_state="1-atm")

    network = provider.kinetics_input()
    gas_states = [
        item for item in network["species"] if item["phase"] == "gas"
    ]
    audit = kinetics.audit_network(provider)
    bundle = catmap_adapter.build_export_bundle(provider, tool_path=None)

    assert gas_states
    assert all(item["standard_state"] == {
        "schema": "vcstudio.fluid-standard-state/v1",
        "phase": "gas",
        "kind": "1-atm",
        "value": 101325.0,
        "unit": "Pa",
    } for item in gas_states)
    assert audit["machine_pass"] is True
    assert bundle["contract_ready"] is False
    assert bundle["export_ready"] is False
    assert {item["code"] for item in bundle["adapter_issues"]} == {
        "CATMAP_PHASE1_CONTRACT_UNSUPPORTED"}
    assert "1-atm requires" in bundle["adapter_issues"][0]["message"]


def test_frozen_v2_rejects_multiple_surface_axes_before_site_ids_are_lost():
    frozen, identity, _, artifacts, bindings, _ = _real_frozen_v2()
    multi_surface = copy.deepcopy(frozen)
    adsorbates = [
        item for item in multi_surface["state_catalog"]
        if item["object_type"] == "AdsorbateState"
    ]
    assert len(adsorbates) >= 2
    adsorbates[-1]["surface_id"] = "surface-2"
    _rehash_frozen(multi_surface)

    with pytest.raises(
            KineticsProjectionError, match="exactly one catalyst surface axis"):
        ValidatedFrozenReactionV2(
            multi_surface,
            validated_reaction=identity,
            validated_frozen=_frozen_authority(multi_surface),
            evidence_bindings=bindings,
            evidence_resolver=artifacts.__getitem__,
        )


@pytest.mark.parametrize(("phase", "mapped_phase"), [
    ("aqueous", "solution"),
    ("liquid", "liquid"),
])
def test_real_frozen_v2_condensed_fluid_core_passes_but_catmap_blocks(
        phase, mapped_phase):
    provider, *_ = _v2_provider(phase=phase)

    network = provider.kinetics_input()
    fluids = [
        item for item in network["species"]
        if item["phase"] in {"liquid", "solution"}
    ]
    audit = kinetics.audit_network(provider)
    bundle = catmap_adapter.build_export_bundle(provider, tool_path=None)

    assert fluids
    assert {item["phase"] for item in fluids} == {mapped_phase}
    assert all(item["activity"]["unit"] == "mol/L" for item in fluids)
    assert all(item["standard_state"] == {
        "schema": "vcstudio.fluid-standard-state/v1", "phase": phase,
        "kind": "1-molar", "value": 1.0, "unit": "mol/L",
    } for item in fluids)
    assert audit["machine_pass"] is True
    assert bundle["contract_ready"] is False
    assert bundle["export_ready"] is False
    assert {item["code"] for item in bundle["adapter_issues"]} == {
        "CATMAP_PHASE1_CONTRACT_UNSUPPORTED"}
    assert "gas" not in {item["phase"] for item in fluids}


def test_frozen_v2_rejects_hash_blocked_and_stale_source_identities():
    frozen, identity, _, artifacts, bindings, _ = _real_frozen_v2()

    tampered = copy.deepcopy(frozen)
    tampered["conditions"]["temperature_k"] = 301.0
    with pytest.raises(KineticsProjectionError, match="hash does not match"):
        ValidatedFrozenReactionV2(
            tampered, validated_reaction=identity,
            validated_frozen=_frozen_authority(frozen),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)

    blocked = copy.deepcopy(frozen)
    blocked.update({"readiness": "blocked", "microkinetics_ready": False})
    _rehash_frozen(blocked)
    with pytest.raises(KineticsProjectionError, match="blocked"):
        ValidatedFrozenReactionV2(
            blocked, validated_reaction=identity,
            validated_frozen=_frozen_authority(blocked),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)

    forged = copy.deepcopy(frozen)
    forged["domain_authority"]["authority_id"] = "e" * 32
    _rehash_frozen(forged)
    with pytest.raises(KineticsProjectionError, match="stale or forged"):
        ValidatedFrozenReactionV2(
            forged, validated_reaction=identity,
            validated_frozen=_frozen_authority(forged),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)

    forged = copy.deepcopy(frozen)
    forged["network_id"] = "forged-network"
    forged["network_identity"]["network_id"] = "forged-network"
    _rehash_frozen(forged)
    with pytest.raises(KineticsProjectionError, match="stale or forged"):
        ValidatedFrozenReactionV2(
            forged, validated_reaction=identity,
            validated_frozen=_frozen_authority(forged),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)

    forged = copy.deepcopy(frozen)
    forged["source_projection_sha256"] = "f" * 64
    _rehash_frozen(forged)
    with pytest.raises(KineticsProjectionError, match="stale or forged"):
        ValidatedFrozenReactionV2(
            forged, validated_reaction=identity,
            validated_frozen=_frozen_authority(forged),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)


def test_frozen_v2_rejects_stale_condition_authority_and_missing_evidence():
    frozen, identity, _, artifacts, bindings, _ = _real_frozen_v2()
    old_conditions_hash = reaction_workbench.semantic_sha256(frozen["conditions"])
    stale = copy.deepcopy(frozen)
    stale["conditions"]["temperature_k"] = 301.0
    _rehash_frozen(stale)

    with pytest.raises(KineticsProjectionError, match="condition identity"):
        ValidatedFrozenReactionV2(
            stale, validated_reaction=identity,
            validated_frozen=_frozen_authority(
                stale, conditions_sha256=old_conditions_hash),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)

    calls = []
    incomplete = dict(bindings)
    incomplete.pop(next(iter(incomplete)))
    with pytest.raises(KineticsProjectionError, match="incomplete or excessive"):
        ValidatedFrozenReactionV2(
            frozen, validated_reaction=identity,
            validated_frozen=_frozen_authority(frozen),
            evidence_bindings=incomplete,
            evidence_resolver=lambda reference: calls.append(reference))
    assert calls == []

    excessive = {f"opaque-{index}": "a" * 64 for index in range(300)}
    with pytest.raises(KineticsProjectionError, match="binding index is invalid"):
        ValidatedFrozenReactionV2(
            frozen, validated_reaction=identity,
            validated_frozen=_frozen_authority(frozen),
            evidence_bindings=excessive,
            evidence_resolver=lambda reference: calls.append(reference))
    assert calls == []

    bad_artifacts = dict(artifacts)
    reference = next(iter(sorted(bad_artifacts)))
    bad_artifacts[reference] = b"semantic hash is not an artifact byte hash"
    with pytest.raises(KineticsProjectionError, match="bytes do not match"):
        ValidatedFrozenReactionV2(
            frozen, validated_reaction=identity,
            validated_frozen=_frozen_authority(frozen),
            evidence_bindings=bindings,
            evidence_resolver=bad_artifacts.__getitem__)


def test_frozen_v2_rejects_forged_edge_stoichiometry_and_condition_binding():
    frozen, identity, _, artifacts, bindings, _ = _real_frozen_v2()
    forged = copy.deepcopy(frozen)
    forged["edges"][0]["stoichiometry"]["co-g"] = {
        "numerator": -2, "denominator": 1}
    _rehash_frozen(forged)
    with pytest.raises(KineticsProjectionError, match="stoichiometry"):
        ValidatedFrozenReactionV2(
            forged, validated_reaction=identity,
            validated_frozen=_frozen_authority(forged),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)

    forged = copy.deepcopy(frozen)
    forged["edges"][0]["condition_set_sha256"] = "f" * 64
    _rehash_frozen(forged)
    with pytest.raises(KineticsProjectionError, match="domain evidence identity"):
        ValidatedFrozenReactionV2(
            forged, validated_reaction=identity,
            validated_frozen=_frozen_authority(forged),
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)


def test_frozen_v2_feed_units_are_phase_exact_and_v1_is_not_formal():
    frozen, identity, authority, artifacts, bindings, adapter = _real_frozen_v2()
    invalid = _v2_spec(frozen, bindings).to_dict()
    invalid["feed_reservoirs"][0]["unit"] = "dimensionless"
    invalid_spec = KineticsModelSpec.from_dict(invalid)
    with pytest.raises(KineticsProjectionError, match="unit is incompatible"):
        KineticsProjectionProvider(
            adapter, invalid_spec, adapter.kinetics_evidence)

    incomplete = _v2_spec(frozen, bindings).to_dict()
    incomplete["feed_reservoirs"] = [
        item for item in incomplete["feed_reservoirs"]
        if item["species_id"] != "co2-g"
    ]
    with pytest.raises(KineticsProjectionError, match="cover every fluid"):
        KineticsProjectionProvider(
            adapter, KineticsModelSpec.from_dict(incomplete),
            adapter.kinetics_evidence)

    invalid = _v2_spec(frozen, bindings).to_dict()
    invalid["source_binding"]["network_id"] = "forged-network"
    with pytest.raises(KineticsProjectionError, match="network_id"):
        KineticsProjectionProvider(
            adapter, KineticsModelSpec.from_dict(invalid),
            adapter.kinetics_evidence)

    legacy = copy.deepcopy(frozen)
    legacy.update({
        "schema": "vcstudio.frozen-reaction-network/v1", "version": "1"})
    _rehash_frozen(legacy)
    with pytest.raises(KineticsProjectionError, match="only .*v2 is formal"):
        ValidatedFrozenReactionV2(
            legacy, validated_reaction=identity, validated_frozen=authority,
            evidence_bindings=bindings, evidence_resolver=artifacts.__getitem__)


def _reaction_source():
    network = catmap_ready_network()
    source = network.canonical_source_projection()
    hash_field = "projection_sha256"
    source.update({
        "project_id": "project-1",
        "domain_authority_id": _DOMAIN_AUTHORITY,
        "domain_generation": 8,
        "network_revision": "network-r7",
    })
    source[hash_field] = ""
    source[hash_field] = kinetics.compute_source_projection_sha256(source)
    return source, network


def _source(kind, reference):
    return {
        "kind": kind,
        "reference": reference,
        "evidence_sha256": hashlib.sha256(_EVIDENCE[reference]).hexdigest(),
    }


def _model_spec(source, network, *, source_hash=None, site_total=2.0):
    source_hash = source_hash or source["projection_sha256"]
    species = {item["id"]: item for item in network["species"]}
    return KineticsModelSpec.from_dict({
        "schema": KineticsModelSpec.schema,
        "project_id": "project-1",
        "spec_id": "adsorption-model",
        "revision": "spec-r1",
        "parent_revision": None,
        "expected_current_hash": None,
        "source_binding": {
            "domain_authority_id": _DOMAIN_AUTHORITY,
            "domain_generation": 8,
            "network_revision": "network-r7",
            "source_projection_sha256": source_hash,
        },
        "rate_law_policy": {
            "activity": "ideal",
            "reversibility": "explicit_reverse",
            "detailed_balance": "enforced",
            "prefactor": "explicit_per_step",
            "electrochemical": "none",
            "reactor": "mean_field_steady_state",
        },
        "assumptions": {
            **copy.deepcopy(network["assumptions"]),
            "evidence": [_source("assumption", "manifest:job-1")],
        },
        "feed_reservoirs": [
            {
                "species_id": species_id,
                "activity": species[species_id]["activity"]["value"],
                "unit": species[species_id]["activity"]["unit"],
                "source": species[species_id]["activity"]["source"]["reference"],
                "evidence_sha256": species[species_id]["activity"]["source"][
                    "evidence_sha256"],
            }
            for species_id in ("CO_g", "O2_g")
        ] + [{
            "species_id": "star_s",
            "activity": 1.0,
            "unit": "dimensionless",
            "source": "manifest:job-1",
            "evidence_sha256": hashlib.sha256(
                _EVIDENCE["manifest:job-1"]).hexdigest(),
        }],
        "target_products": ["CO_s", "O2_s"],
        "steps": [
            {
                "step_id": step["id"],
                "prefactors": copy.deepcopy(step["prefactors"]),
                "bep": copy.deepcopy(step["bep"]),
                "scaling": copy.deepcopy(step["scaling"]),
                "uncertainty_eV": step["uncertainty_eV"],
                "evidence": [_source("model", "manifest:job-1")],
            }
            for step in network["elementary_steps"]
        ],
        "site_population_totals": [{
            "site_type": "s", "value": site_total, "unit": "sites",
            "basis": "surface_unit_cell",
            "evidence": [_source("site-policy", "manifest:job-1")],
        }],
        "saddle_selector": None,
    })


def _provider(*, resolver=None, site_total=2.0):
    source, network = _reaction_source()
    fixture = FrozenReactionFixture(source)
    resolver = resolver or Resolver()
    provider = KineticsProjectionProvider(
        fixture, _model_spec(source, network, site_total=site_total), resolver)
    return provider, fixture, resolver


def test_happy_path_is_one_snapshot_and_hash_binds_source_spec_and_evidence():
    provider, fixture, resolver = _provider(site_total=2.0)

    first = provider.kinetics_input()
    audit = kinetics.audit_network(provider)
    bundle = catmap_adapter.build_export_bundle(provider, tool_path=None)
    second = provider.kinetics_input()

    assert fixture.read_count == 1
    assert len(resolver.calls) == len(set(resolver.calls))
    assert first == second
    assert first["schema"] == "vcstudio.kinetics-network/v3"
    assert first["model_spec"]["spec_sha256"] == (
        KineticsModelSpec.from_dict(provider.kinetics_model_spec()).semantic_sha256)
    assert first["model_spec"]["source_projection_sha256"] == (
        first["source_projection"]["projection_sha256"])
    assert audit["machine_pass"] is True
    assert bundle["contract_ready"] is True
    assert "'total': 2.0" in bundle["files"]["model.mkm"]
    assert "'total': 1.0" not in bundle["files"]["model.mkm"]


def test_stale_source_binding_is_rejected_before_projection():
    source, network = _reaction_source()
    fixture = FrozenReactionFixture(source)
    spec = _model_spec(source, network, source_hash="f" * 64)

    with pytest.raises(KineticsProjectionError, match="stale"):
        KineticsProjectionProvider(fixture, spec, Resolver())


def test_evidence_byte_mismatch_is_rejected_and_never_re_read():
    values = dict(_EVIDENCE)
    values["manifest:job-1"] = b"changed artifact bytes"
    resolver = Resolver(values)

    with pytest.raises(KineticsProjectionError, match="evidence byte mismatch"):
        _provider(resolver=resolver)
    assert resolver.calls.count("manifest:job-1") == 1


def test_excessive_or_conflicting_evidence_is_rejected_before_resolver():
    source, network = _reaction_source()
    source["evidence_refs"].extend({
        "reference": f"opaque-evidence:{index}",
        "artifact_sha256": hashlib.sha256(str(index).encode()).hexdigest(),
    } for index in range(300))
    source["projection_sha256"] = ""
    source["projection_sha256"] = kinetics.compute_source_projection_sha256(source)
    resolver = Resolver()
    with pytest.raises(KineticsProjectionError, match="bounded evidence array"):
        KineticsProjectionProvider(
            FrozenReactionFixture(source), _model_spec(source, network), resolver)
    assert resolver.calls == []

    source, network = _reaction_source()
    source["evidence_refs"].append({
        "reference": source["evidence_refs"][0]["reference"],
        "artifact_sha256": "f" * 64,
    })
    source["projection_sha256"] = ""
    source["projection_sha256"] = kinetics.compute_source_projection_sha256(source)
    resolver = Resolver()
    with pytest.raises(KineticsProjectionError, match="conflicting hashes"):
        KineticsProjectionProvider(
            FrozenReactionFixture(source), _model_spec(source, network), resolver)
    assert resolver.calls == []


def test_evidence_total_byte_limit_is_cumulative(monkeypatch):
    total = sum(len(value) for value in _EVIDENCE.values())
    assert total > max(map(len, _EVIDENCE.values()))
    monkeypatch.setattr(
        projection_module, "_MAX_TOTAL_EVIDENCE_BYTES", total - 1)
    resolver = Resolver()

    with pytest.raises(KineticsProjectionError, match="total byte limit"):
        _provider(resolver=resolver)


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda value: value.pop("rate_law_policy"), "INVALID_OBJECT"),
    (lambda value: value.pop("site_population_totals"),
     "SITE_POPULATION_TOTALS_REQUIRED"),
])
def test_missing_rate_or_site_policy_is_audit_only(mutate, code):
    provider, _, _ = _provider()
    broken = MutatedProvider(provider, mutate)
    audit = kinetics.audit_network(broken)
    bundle = catmap_adapter.build_export_bundle(broken, tool_path=None)

    assert audit["machine_pass"] is False
    assert code in {item["code"] for item in audit["issues"]}
    assert bundle["contract_ready"] is False
    assert set(bundle["files"]) == {"kinetics-audit.json"}


def test_nonideal_and_multisite_inputs_remain_unsupported_audit_only():
    nonideal = catmap_ready_network()
    nonideal["rate_law_policy"]["activity"] = "nonideal"
    _freeze(nonideal)
    multisite = catmap_ready_network()
    multisite["species"][3]["sites"] = {"s": 2}
    _freeze(multisite)

    assert "NONIDEAL_ACTIVITY_UNSUPPORTED" in {
        item["code"] for item in kinetics.audit_network(nonideal)["issues"]
    }
    assert "MULTISITE_SPECIES_UNSUPPORTED" in {
        item["code"] for item in kinetics.audit_network(multisite)["issues"]
    }
    assert set(catmap_adapter.build_export_bundle(
        nonideal, tool_path=None)["files"]) == {"kinetics-audit.json"}
    assert set(catmap_adapter.build_export_bundle(
        multisite, tool_path=None)["files"]) == {"kinetics-audit.json"}


def test_raw_or_missing_spec_provider_is_never_promoted_to_model_export():
    provider, _, _ = _provider()

    class MissingSpec:
        def kinetics_input(self):
            return provider.kinetics_input()

        def kinetics_evidence(self, reference):
            return provider.kinetics_evidence(reference)

        def canonical_source_projection(self):
            return provider.canonical_source_projection()

    audit = kinetics.audit_network(MissingSpec())
    bundle = catmap_adapter.build_export_bundle(MissingSpec(), tool_path=None)
    assert audit["machine_pass"] is False
    assert "INVALID_INPUT" in {item["code"] for item in audit["issues"]}
    assert set(bundle["files"]) == {"kinetics-audit.json"}


@pytest.mark.parametrize(("leaked", "extension"), [
    (
        "evidence=C:\\Users\\private-person\\secret\\artifact.json",
        lambda leaked: {"debug": leaked},
    ),
    (
        "key=/scratch/private-person/artifact.json",
        lambda leaked: {leaked: "opaque-value"},
    ),
    (
        "https://user:password@example.invalid/evidence",
        lambda leaked: {"debug": leaked},
    ),
    (
        "https://user@example.invalid/evidence",
        lambda leaked: {"debug": leaked},
    ),
    (
        "file:///home/private-person/artifact.json",
        lambda leaked: {"debug": leaked},
    ),
])
def test_projection_rejects_paths_without_echoing_them(leaked, extension):
    source, network = _reaction_source()
    source["extensions"] = extension(leaked)
    source["projection_sha256"] = ""
    source["projection_sha256"] = kinetics.compute_source_projection_sha256(source)

    with pytest.raises(KineticsProjectionError) as captured:
        KineticsProjectionProvider(
            FrozenReactionFixture(source), _model_spec(source, network), Resolver())
    assert leaked not in str(captured.value)


def test_provider_rejects_raw_mapping_even_when_bytes_look_valid():
    source, network = _reaction_source()
    with pytest.raises(KineticsProjectionError, match="raw mappings are untrusted"):
        KineticsProjectionProvider(source, _model_spec(source, network), Resolver())
