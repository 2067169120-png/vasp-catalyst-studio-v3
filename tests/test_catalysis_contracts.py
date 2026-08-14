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
    ReactionNetwork,
    canonical_json_bytes,
    redact_sensitive,
    reject_sensitive,
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
              "method_fingerprint": _method()}
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
            step_id="step-001", reactant_state_ids=("state-a",),
            product_state_ids=("state-b",), transition_state_id="state-ts",
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
        assert payload["model_version"] == "1.0.0"
        assert payload["provenance"] == "observed"
        assert payload["evidence_refs"][0]["origin"] == "observed"
        assert payload["method_fingerprint"]["sha256"] == "a" * 64


def test_storage_envelope_rehashes_payload_and_keeps_job_yaml_authoritative():
    surface = _objects()[0]
    envelope = DomainEnvelope.wrap(surface)
    wire = envelope.to_dict()

    assert wire["object_type"] == "CatalystSurface"
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


def test_inferred_evidence_cannot_be_mislabelled_as_observed():
    with pytest.raises(CatalysisContractError, match="observed evidence"):
        CatalystSurface(
            surface_id="surface-001", composition="Pt", miller_indices=(1, 1, 1),
            termination_id=None, geometric_site_ids=("site-top",),
            provenance="observed", evidence_refs=_refs("inferred"),
            method_fingerprint=_method("inferred"),
        )

    inferred = CatalystSurface(
        surface_id="surface-001", composition="Pt", miller_indices=(1, 1, 1),
        termination_id=None, geometric_site_ids=("site-top",), provenance="inferred",
        evidence_refs=_refs("inferred"), method_fingerprint=_method("inferred"),
    )
    assert inferred.to_dict()["provenance"] == "inferred"

    with pytest.raises(CatalysisContractError, match="observed method"):
        CatalystSurface(
            surface_id="surface-001", composition="Pt", miller_indices=(1, 1, 1),
            termination_id=None, geometric_site_ids=("site-top",),
            provenance="observed", evidence_refs=_refs("observed"),
            method_fingerprint=_method("inferred"),
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
