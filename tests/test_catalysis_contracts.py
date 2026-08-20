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
            reactants=(ReactionParticipant("state-a", 2, "gas", 0, 0),),
            products=(ReactionParticipant("state-b", 1, "gas", 0, 0),),
            transition_state_id="state-ts",
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
        assert payload["schema_version"] in {"1.0.0", "2.0.0"}
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
            AuthoritativeParticipantState("state-1", secret, "gas", 0, 0)


def test_elementary_step_v2_expresses_coefficients_and_rejects_old_v1_shape():
    step = _objects()[2]
    assert step.to_dict()["reactants"][0]["coefficient"] == 2
    old = step.to_dict()
    old["schema"] = "vcstudio.elementary-step/v1"
    old["schema_version"] = "1.0.0"
    old["reactant_state_ids"] = ["state-a", "state-a"]
    old["product_state_ids"] = ["state-b"]
    old.pop("reactants")
    old.pop("products")
    with pytest.raises(CatalysisContractError):
        ElementaryStep.from_dict(old)


def test_authoritative_resolver_enforces_elements_charge_and_surface_sites():
    step = _objects()[2]
    states = {
        "state-a": AuthoritativeParticipantState("state-a", "H", "gas", 0, 0),
        "state-b": AuthoritativeParticipantState("state-b", "H2", "gas", 0, 0),
    }
    result = validate_elementary_step_conservation(step, states)
    assert result["elements"] == {"H": 2}
    assert result["authorizes_execution"] is False

    h2_to_water = ElementaryStep(
        step_id="step-unbalanced",
        reactants=(ReactionParticipant("h2", 1, "gas", 0, 0),),
        products=(ReactionParticipant("water", 1, "gas", 0, 0),),
        transition_state_id=None, condition_set_id=None, reversible=False,
        provenance="observed", evidence_refs=_refs(), method_fingerprint=_method(),
        object_revision_id="revision-1",
    )
    with pytest.raises(CatalysisContractError, match="elemental conservation"):
        validate_elementary_step_conservation(h2_to_water, {
            "h2": AuthoritativeParticipantState("h2", "H2", "gas", 0, 0),
            "water": AuthoritativeParticipantState("water", "H2O", "gas", 0, 0),
        })


@pytest.mark.parametrize(("products", "records", "message"), [
    (
        (ReactionParticipant("product", 1, "adsorbed", 1, 1),),
        {
            "reactant": AuthoritativeParticipantState("reactant", "H", "adsorbed", 0, 1),
            "product": AuthoritativeParticipantState("product", "H", "adsorbed", 1, 1),
        },
        "charge conservation",
    ),
    (
        (ReactionParticipant("product", 1, "adsorbed", 0, 2),),
        {
            "reactant": AuthoritativeParticipantState("reactant", "H", "adsorbed", 0, 1),
            "product": AuthoritativeParticipantState("product", "H", "adsorbed", 0, 2),
        },
        "surface-site conservation",
    ),
])
def test_authoritative_resolver_rejects_charge_and_site_imbalance(products, records, message):
    step = ElementaryStep(
        step_id="step-conservation",
        reactants=(ReactionParticipant("reactant", 1, "adsorbed", 0, 1),),
        products=products, transition_state_id=None, condition_set_id=None,
        reversible=False, provenance="observed", evidence_refs=_refs(),
        method_fingerprint=_method(), object_revision_id="revision-1",
    )
    with pytest.raises(CatalysisContractError, match=message):
        validate_elementary_step_conservation(step, records)


def test_participant_declaration_cannot_override_authoritative_state_metadata():
    step = ElementaryStep(
        step_id="step-metadata",
        reactants=(ReactionParticipant("reactant", 1, "gas", 0, 0),),
        products=(ReactionParticipant("product", 1, "gas", 0, 0),),
        transition_state_id=None, condition_set_id=None, reversible=False,
        provenance="observed", evidence_refs=_refs(), method_fingerprint=_method(),
        object_revision_id="revision-1",
    )
    records = {
        "reactant": AuthoritativeParticipantState("reactant", "H2", "gas", 0, 0),
        "product": AuthoritativeParticipantState("product", "H2", "adsorbed", 0, 1),
    }
    with pytest.raises(CatalysisContractError, match="disagrees with authoritative"):
        validate_elementary_step_conservation(step, records)
