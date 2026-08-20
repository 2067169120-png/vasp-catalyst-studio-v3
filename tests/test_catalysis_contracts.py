from __future__ import annotations

import copy

import pytest

from vcstudio.project.catalysis_contracts import (
    AdsorbateState,
    CatalystSurface,
    CatalysisContractError,
    ConditionSet,
    DomainEnvelope,
    ElementaryStep,
    EvidenceRef,
    ExactRational,
    MethodFingerprint,
    AuthoritativeParticipantState,
    ReactionParticipant,
    ReactionNetwork,
    canonical_json_bytes,
    redact_sensitive,
    reject_sensitive,
    validate_elementary_step_conservation,
)


def _refs(origin="observed"):
    return (EvidenceRef(
        ref_type="calculation_result", opaque_id=f"energy-{origin}",
        origin=origin, revision_id="revision-1",
    ),)


def _method(origin="observed"):
    return MethodFingerprint(
        method_id="vasp-method-001", scope="electronic_structure",
        sha256="a" * 64,
        evidence_refs=(EvidenceRef(
            ref_type="method_record", opaque_id=f"method-{origin}",
            origin=origin, revision_id="revision-1",
        ),),
    )


def _participant(state_id, coefficient=1, phase="gas", charge=0, sites=None):
    return ReactionParticipant(
        state_id=state_id, coefficient=coefficient, phase=phase, charge=charge,
        site_stoichiometry={} if sites is None else sites,
    )


def _state(state_id, formula, phase="gas", charge=0, sites=None):
    return AuthoritativeParticipantState(
        state_id=state_id, chemical_formula=formula, phase=phase, charge=charge,
        site_stoichiometry={} if sites is None else sites,
    )


def _step(reactants, transition_state, products, step_id="step-conservation"):
    return ElementaryStep(
        step_id=step_id, reactants=tuple(reactants),
        transition_state=tuple(transition_state), products=tuple(products),
        condition_set_id=None, reversible=False, provenance="observed",
        evidence_refs=_refs(), method_fingerprint=_method(),
        object_revision_id="revision-1",
    )


def _objects():
    common = {"provenance": "observed", "evidence_refs": _refs(),
              "method_fingerprint": _method(), "object_revision_id": "revision-1"}
    return (
        CatalystSurface(
            surface_id="surface-001", composition="Pt", miller_indices=(1, 1, 1),
            termination_id="termination-a", geometric_site_ids=("site-top", "site-fcc"),
            **common,
        ),
        AdsorbateState(
            state_id="state-co-top", surface_id="surface-001", adsorbate_id="ads-co",
            chemical_formula="CO", geometric_site_id="site-top", charge=0, multiplicity=1,
            **common,
        ),
        ElementaryStep(
            step_id="step-001",
            reactants=(_participant("state-a", 2),),
            transition_state=(_participant("state-ts-a"), _participant("state-ts-b")),
            products=(_participant("state-b"),),
            condition_set_id="conditions-298k", reversible=True, **common,
        ),
        ConditionSet(
            condition_set_id="conditions-298k", temperature_k=298.15,
            pressure_pa=101325.0, ph=None, electrode_potential_v=None, **common,
        ),
        ReactionNetwork(
            network_id="network-001", surface_ids=("surface-001",),
            state_ids=("state-a", "state-b"), step_ids=("step-001",),
            condition_set_ids=("conditions-298k",), **common,
        ),
    )


def test_domain_dtos_are_strict_versioned_canonical_and_round_trip():
    for value in _objects():
        payload = value.to_dict()
        restored = type(value).from_dict(copy.deepcopy(payload))
        assert restored == value
        assert restored.semantic_hash() == value.semantic_hash()
        assert len(value.semantic_hash()) == 64
        assert canonical_json_bytes(payload) == canonical_json_bytes(
            dict(reversed(list(payload.items()))))
        assert payload["schema_version"] in {"1.0.0", "3.0.0"}
        assert payload["object_revision_id"] == "revision-1"
        assert payload["provenance"] == "observed"
        assert payload["evidence_refs"][0]["origin"] == "observed"
        assert payload["method_fingerprint"]["sha256"] == "a" * 64


def test_storage_envelope_rehashes_payload_and_keeps_job_yaml_authoritative():
    surface = _objects()[0]
    envelope = DomainEnvelope.wrap(surface)
    wire = envelope.to_dict()

    assert wire["object_type"] == "CatalystSurface"
    assert wire["schema_version"] == "1.0.0"
    assert wire["object_revision_id"] == "revision-1"
    assert wire["semantic_sha256"] == surface.semantic_hash()
    assert wire["job_source_of_truth"] == "job.yaml"
    assert wire["authorizes_execution"] is False
    assert DomainEnvelope.from_dict(wire) == envelope

    with pytest.raises(TypeError):
        envelope.payload["composition"] = "Au"
    detached = envelope.to_dict()
    detached["payload"]["composition"] = "Au"
    assert envelope.to_dict()["payload"]["composition"] == "Pt"

    tampered = copy.deepcopy(wire)
    tampered["payload"]["composition"] = "Au"
    with pytest.raises(CatalysisContractError, match="hash mismatch"):
        DomainEnvelope.from_dict(tampered)

    for field, replacement in (
        ("schema_version", "9.0.0"),
        ("object_revision_id", "revision-other"),
        ("parent_revision", "revision-parent"),
        ("expected_current_hash", "b" * 64),
    ):
        mismatched = copy.deepcopy(wire)
        mismatched[field] = replacement
        with pytest.raises(CatalysisContractError):
            DomainEnvelope.from_dict(mismatched)


def test_revision_parent_and_expected_hash_are_an_atomic_pair():
    base = _objects()[0]
    with pytest.raises(CatalysisContractError, match="provided together"):
        CatalystSurface(
            surface_id=base.surface_id, composition=base.composition,
            miller_indices=base.miller_indices, termination_id=base.termination_id,
            geometric_site_ids=base.geometric_site_ids, provenance=base.provenance,
            evidence_refs=base.evidence_refs, method_fingerprint=base.method_fingerprint,
            object_revision_id="revision-2", parent_revision="revision-1",
        )
    with pytest.raises(CatalysisContractError, match="own parent"):
        CatalystSurface(
            surface_id=base.surface_id, composition=base.composition,
            miller_indices=base.miller_indices, termination_id=base.termination_id,
            geometric_site_ids=base.geometric_site_ids, provenance=base.provenance,
            evidence_refs=base.evidence_refs, method_fingerprint=base.method_fingerprint,
            object_revision_id="revision-2", parent_revision="revision-2",
            expected_current_hash="b" * 64,
        )


def test_inferred_evidence_cannot_be_mislabelled_as_observed():
    with pytest.raises(CatalysisContractError, match="observed evidence"):
        CatalystSurface(
            surface_id="surface-001", composition="Pt", miller_indices=(1, 1, 1),
            termination_id=None, geometric_site_ids=("site-top",),
            provenance="observed", evidence_refs=_refs("inferred"),
            method_fingerprint=_method("inferred"), object_revision_id="revision-1",
        )

    inferred = CatalystSurface(
        surface_id="surface-001", composition="Pt", miller_indices=(1, 1, 1),
        termination_id=None, geometric_site_ids=("site-top",), provenance="inferred",
        evidence_refs=_refs("inferred"), method_fingerprint=_method("inferred"),
        object_revision_id="revision-1",
    )
    assert inferred.to_dict()["provenance"] == "inferred"

    with pytest.raises(CatalysisContractError, match="observed method"):
        CatalystSurface(
            surface_id="surface-001", composition="Pt", miller_indices=(1, 1, 1),
            termination_id=None, geometric_site_ids=("site-top",),
            provenance="observed", evidence_refs=_refs("observed"),
            method_fingerprint=_method("inferred"), object_revision_id="revision-1",
        )


@pytest.mark.parametrize("mutation", [
    {"project_path": r"C:\\private\\surface"},
    {"credential": "hunter2"},
    {"active_site_id": "site-top"},
])
def test_unknown_paths_secrets_and_active_site_claims_fail_closed(mutation):
    payload = _objects()[0].to_dict()
    payload.update(mutation)
    with pytest.raises(CatalysisContractError):
        CatalystSurface.from_dict(payload)
    assert "geometric_site_ids" in _objects()[0].to_dict()
    assert "active_site_id" not in _objects()[0].to_dict()


def test_recursive_private_fields_are_rejected_inside_method_evidence():
    payload = _objects()[0].to_dict()
    payload["method_fingerprint"]["evidence_refs"][0]["nested"] = {
        "password": "do-not-store",
    }
    with pytest.raises(CatalysisContractError):
        CatalystSurface.from_dict(payload)


@pytest.mark.parametrize("private_value", [
    {"github_api_key": "needle"},
    {"ssh_private_key": "needle"},
    {"project_root": "private"},
    {"browser_directory": "private"},
    {"nested": {"note": "see(/srv/private/job.yaml)"}},
    {"nested": {"note": r"see(C:\\private\\job.yaml)"}},
])
def test_recursive_private_key_and_embedded_path_variants_are_rejected(private_value):
    with pytest.raises(CatalysisContractError):
        reject_sensitive(private_value)


def test_public_redactor_removes_composite_keys_and_embedded_paths():
    projected = redact_sensitive({
        "github_api_key": "needle",
        "nested": ["see(/srv/private/job.yaml)", r"see(C:\\private\\job.yaml)"],
        "safe": "evidence-001",
    })
    rendered = str(projected)
    assert "needle" not in rendered
    assert "/srv/private" not in rendered
    assert r"C:\\private" not in rendered
    assert projected["safe"] == "evidence-001"


def test_evidence_constructor_rejects_secret_ids_and_obvious_origin_mismatch():
    with pytest.raises(CatalysisContractError):
        EvidenceRef("method_record", "github_pat_abcdefghijk", "observed")
    with pytest.raises(CatalysisContractError, match="imported origin"):
        EvidenceRef("imported_record", "external-record-1", "observed")


@pytest.mark.parametrize("field, secret", [
    ("composition", "AKIA1234567890ABCDEF"),
    ("chemical_formula", "sk-proj-abcdefghijklmnopqrstuvwxyz"),
])
def test_direct_domain_constructor_rejects_secret_shaped_scientific_strings(field, secret):
    common = {
        "provenance": "observed", "evidence_refs": _refs(),
        "method_fingerprint": _method(), "object_revision_id": "revision-1",
    }
    if field == "composition":
        with pytest.raises(CatalysisContractError, match="path or secret"):
            CatalystSurface(
                surface_id="surface-1", composition=secret, miller_indices=(1, 1, 1),
                termination_id=None, geometric_site_ids=("site-1",), **common,
            )
    else:
        with pytest.raises(CatalysisContractError, match="path or secret"):
            AdsorbateState(
                state_id="state-1", surface_id="surface-1", adsorbate_id="adsorbate-1",
                chemical_formula=secret, geometric_site_id=None, charge=0, multiplicity=1,
                **common,
            )
        with pytest.raises(CatalysisContractError, match="path or secret"):
            AuthoritativeParticipantState("state-1", secret, "gas", 0, {})


def test_elementary_step_v3_is_canonical_and_explicitly_rejects_v1_v2_shapes():
    step = _objects()[2]
    wire = step.to_dict()
    assert wire["schema"] == "vcstudio.elementary-step/v3"
    assert wire["schema_version"] == "3.0.0"
    assert wire["reactants"][0]["coefficient"] == {
        "numerator": 2, "denominator": 1,
    }
    assert len(wire["transition_state"]) == 2
    assert "transition_state_id" not in wire

    v1 = {"schema": "vcstudio.elementary-step/v1"}
    v2 = {"schema": "vcstudio.elementary-step/v2"}
    for legacy in (v1, v2):
        with pytest.raises(CatalysisContractError, match="migrated to the v3"):
            ElementaryStep.from_dict(legacy)

    legacy_field = copy.deepcopy(wire)
    legacy_field["transition_state_id"] = "state-ts"
    legacy_field.pop("transition_state")
    with pytest.raises(CatalysisContractError):
        ElementaryStep.from_dict(legacy_field)

    with pytest.raises(CatalysisContractError, match="transition_state must not be empty"):
        _step([_participant("reactant")], [], [_participant("product")])


def test_exact_rational_normalizes_and_rejects_float_or_nonpositive_amounts():
    assert ExactRational(2, 4) == ExactRational(1, 2)
    assert ExactRational.from_dict({"numerator": 2, "denominator": 4}) == (
        ExactRational(1, 2))
    participant = _participant(
        "state-a", ExactRational(2, 4), "adsorbed", sites={
            "top": ExactRational(2, 4),
        },
    )
    assert participant.to_dict()["coefficient"] == {
        "numerator": 1, "denominator": 2,
    }
    assert participant.to_dict()["site_stoichiometry"]["top"] == {
        "numerator": 1, "denominator": 2,
    }
    canonical_participant = _participant(
        "state-a", ExactRational(1, 2), "adsorbed", sites={
            "top": ExactRational(1, 2),
        },
    )
    assert participant.to_dict() == canonical_participant.to_dict()
    state = _state("state-a", "H", "adsorbed", sites={
        "top": ExactRational(1, 2),
    })
    assert AuthoritativeParticipantState.from_dict(state.to_dict()) == state

    with pytest.raises(CatalysisContractError, match="never a floating-point"):
        _participant("state-a", 0.5)
    with pytest.raises(CatalysisContractError, match="never a floating-point"):
        _participant("state-a", sites={"top": 0.5})
    with pytest.raises(CatalysisContractError, match="must be positive"):
        _participant("state-a", 0)
    with pytest.raises(CatalysisContractError, match="must be positive"):
        _participant("state-a", sites={"top": 0})
    with pytest.raises(CatalysisContractError):
        ExactRational(1, 0)
    with pytest.raises(CatalysisContractError):
        ExactRational(-1, 2)
    with pytest.raises(CatalysisContractError):
        ExactRational(0.5, 1)
    with pytest.raises(CatalysisContractError):
        ExactRational(1, 2.0)
    with pytest.raises(CatalysisContractError, match="outside the supported range"):
        ExactRational(1_000_000_001, 1)
    with pytest.raises(CatalysisContractError):
        _participant("state-a", sites={"github_api_key": 1})

    old_v2_participant = participant.to_dict()
    old_v2_participant["coefficient"] = 1
    with pytest.raises(CatalysisContractError):
        ReactionParticipant.from_dict(old_v2_participant)


def test_half_o2_uses_fraction_exactly_across_reactants_ts_and_products():
    sites_reactant = {"bridge": 2}
    sites_single = {"bridge": 1}
    step = _step(
        [_participant("o2", ExactRational(1, 2), "adsorbed", 2, sites_reactant)],
        [_participant("ts-o", 1, "adsorbed", 1, sites_single)],
        [_participant("o", 1, "adsorbed", 1, sites_single)],
        "step-half-o2",
    )
    result = validate_elementary_step_conservation(step, {
        "o2": _state("o2", "O2", "adsorbed", 2, sites_reactant),
        "ts-o": _state("ts-o", "O", "adsorbed", 1, sites_single),
        "o": _state("o", "O", "adsorbed", 1, sites_single),
    })
    assert result["elements"] == {"O": {"numerator": 1, "denominator": 1}}
    assert result["charge"] == {"numerator": 1, "denominator": 1}
    assert result["site_stoichiometry"] == {
        "bridge": {"numerator": 1, "denominator": 1},
    }
    assert result["authorizes_execution"] is False
    assert canonical_json_bytes(result)


def test_multi_participant_ts_is_complete_and_missing_ts_record_fails_closed():
    step = _objects()[2]
    states = {
        "state-a": _state("state-a", "H"),
        "state-ts-a": _state("state-ts-a", "H"),
        "state-ts-b": _state("state-ts-b", "H"),
        "state-b": _state("state-b", "H2"),
    }
    result = validate_elementary_step_conservation(step, states)
    assert result["elements"] == {"H": {"numerator": 2, "denominator": 1}}

    states.pop("state-ts-b")
    with pytest.raises(CatalysisContractError, match="has no state state-ts-b"):
        validate_elementary_step_conservation(step, states)


def test_h2_to_h2o_is_rejected_even_when_ts_matches_reactants():
    step = _step(
        [_participant("h2")], [_participant("ts-h2")], [_participant("water")],
        "step-unbalanced",
    )
    with pytest.raises(
            CatalysisContractError,
            match="transition_state/products violates elemental conservation"):
        validate_elementary_step_conservation(step, {
            "h2": _state("h2", "H2"),
            "ts-h2": _state("ts-h2", "H2"),
            "water": _state("water", "H2O"),
        })


def test_a_site_to_b_site_is_rejected_per_site_type_not_scalar_total():
    step = _step(
        [_participant("reactant", 1, "adsorbed", sites={"A-site": 1})],
        [_participant("ts", 1, "adsorbed", sites={"A-site": 1})],
        [_participant("product", 1, "adsorbed", sites={"B-site": 1})],
    )
    with pytest.raises(
            CatalysisContractError,
            match="transition_state/products violates site-type conservation"):
        validate_elementary_step_conservation(step, {
            "reactant": _state("reactant", "H", "adsorbed", sites={"A-site": 1}),
            "ts": _state("ts", "H", "adsorbed", sites={"A-site": 1}),
            "product": _state("product", "H", "adsorbed", sites={"B-site": 1}),
        })


@pytest.mark.parametrize(("ts_participant", "states", "message"), [
    (
        _participant("ts-element"),
        {
            "reactant": _state("reactant", "H2"),
            "ts-element": _state("ts-element", "H"),
            "product": _state("product", "H2"),
        },
        "reactants/transition_state violates elemental conservation",
    ),
    (
        _participant("ts-charge", charge=1),
        {
            "reactant": _state("reactant", "H"),
            "ts-charge": _state("ts-charge", "H", charge=1),
            "product": _state("product", "H"),
        },
        "reactants/transition_state violates charge conservation",
    ),
    (
        _participant("ts-site", phase="adsorbed", sites={"top": 2}),
        {
            "reactant": _state("reactant", "H", "adsorbed", sites={"top": 1}),
            "ts-site": _state("ts-site", "H", "adsorbed", sites={"top": 2}),
            "product": _state("product", "H", "adsorbed", sites={"top": 1}),
        },
        "reactants/transition_state violates site-type conservation",
    ),
])
def test_transition_state_elements_charge_and_sites_are_each_authoritative(
        ts_participant, states, message):
    reactant = next(item for key, item in states.items() if key == "reactant")
    product = next(item for key, item in states.items() if key == "product")
    step = _step(
        [_participant(
            "reactant", phase=reactant.phase, charge=reactant.charge,
            sites=reactant.site_stoichiometry,
        )],
        [ts_participant],
        [_participant(
            "product", phase=product.phase, charge=product.charge,
            sites=product.site_stoichiometry,
        )],
    )
    with pytest.raises(CatalysisContractError, match=message):
        validate_elementary_step_conservation(step, states)


def test_participant_declaration_cannot_override_authoritative_ts_metadata():
    step = _step(
        [_participant("reactant")], [_participant("ts")], [_participant("product")],
        "step-metadata",
    )
    records = {
        "reactant": _state("reactant", "H2"),
        "ts": _state("ts", "H2", "adsorbed", sites={"top": 1}),
        "product": _state("product", "H2"),
    }
    with pytest.raises(CatalysisContractError, match="disagrees with authoritative"):
        validate_elementary_step_conservation(step, records)


def test_callable_resolver_is_snapshotted_once_per_state_id():
    step = _step(
        [_participant("shared")], [_participant("shared")], [_participant("shared")],
        "step-shared-state",
    )
    calls = 0

    def resolver(state_id):
        nonlocal calls
        calls += 1
        return _state(state_id, "H" if calls == 1 else "H2")

    validate_elementary_step_conservation(step, resolver)
    assert calls == 1
