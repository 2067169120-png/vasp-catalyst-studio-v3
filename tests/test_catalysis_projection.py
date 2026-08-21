from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os

import pytest

from vcstudio.project import catalysis_domain_store as store_module
from vcstudio.project import catalysis_projection as projection_module
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
from vcstudio.project.catalysis_projection import (
    ActiveBindingAuthoritySnapshot,
    ActiveNetworkBinding,
    CatalysisProjectionAuthority,
    CatalysisProjectionError,
)


def _evidence():
    return (EvidenceRef(
        "calculation_result", "result-observed", "observed", "result-revision-1",
    ),)


def _method():
    return MethodFingerprint(
        method_id="method-001", scope="electronic_structure", sha256="a" * 64,
        evidence_refs=(EvidenceRef(
            "method_record", "method-observed", "observed", "method-revision-1",
        ),),
    )


def _surface(
        surface_id="surface-1", revision="surface-revision-1", *,
        parent=None, expected=None, composition="Pt"):
    return CatalystSurface(
        surface_id=surface_id, composition=composition,
        miller_indices=(1, 1, 1), termination_id="termination-1",
        geometric_site_ids=("site-top",), provenance="observed",
        evidence_refs=_evidence(), method_fingerprint=_method(),
        object_revision_id=revision, parent_revision=parent,
        expected_current_hash=expected,
    )


def _state(
        state_id, *, surface_id="surface-1", charge=0,
        geometric_site_id="site-top", formula="CO"):
    return AdsorbateState(
        state_id=state_id, surface_id=surface_id,
        adsorbate_id=f"adsorbate-{state_id}", chemical_formula=formula,
        geometric_site_id=geometric_site_id, charge=charge, multiplicity=1,
        provenance="observed", evidence_refs=_evidence(),
        method_fingerprint=_method(), object_revision_id=f"{state_id}-revision-1",
    )


def _fluid(state_id="fluid-co", *, phase="gas", formula="CO", charge=0):
    return FluidState(
        state_id=state_id, phase=phase, chemical_formula=formula,
        charge=charge, multiplicity=1, standard_state=FluidStandardState(
            phase=phase, kind="1-bar" if phase == "gas" else "1-molar",
            value=100000.0 if phase == "gas" else 1.0,
            unit="Pa" if phase == "gas" else "mol/L"),
        provenance="observed", evidence_refs=_evidence(),
        method_fingerprint=_method(), object_revision_id=f"{state_id}-revision-1",
    )


def _participant(
        state_id, *, phase="adsorbed", charge=0, site="site-top",
        site_amount=1, extra_site=None):
    amount = (
        site_amount if isinstance(site_amount, ExactRational)
        else ExactRational(site_amount))
    sites = {} if site is None else {site: amount}
    if extra_site is not None:
        sites[extra_site] = ExactRational(1)
    return ReactionParticipant(
        state_id=state_id, coefficient=ExactRational(1), phase=phase,
        charge=charge, site_stoichiometry=sites,
    )


def _step(
        step_id="step-1", *, reactant="state-a", transition="state-ts",
        product="state-b", condition="condition-1", gas=False):
    return ElementaryStep(
        step_id=step_id,
        reactants=(_participant(reactant, phase="gas" if gas else "adsorbed"),),
        transition_state=(_participant(transition),),
        products=(_participant(product),), condition_set_id=condition,
        reversible=True, provenance="observed", evidence_refs=_evidence(),
        method_fingerprint=_method(), object_revision_id=f"{step_id}-revision-1",
    )


def _condition(condition_id="condition-1"):
    return ConditionSet(
        condition_set_id=condition_id, temperature_k=300.0,
        pressure_pa=100_000.0, ph=None, electrode_potential_v=None,
        provenance="observed", evidence_refs=_evidence(),
        method_fingerprint=_method(),
        object_revision_id=f"{condition_id}-revision-1",
    )


def _network(
        network_id="network-1", *, states=("state-a", "state-ts", "state-b"),
        steps=("step-1",), conditions=("condition-1",),
        revision=None, parent=None, expected=None):
    return ReactionNetwork(
        network_id=network_id, surface_ids=("surface-1",),
        state_ids=tuple(states), step_ids=tuple(steps),
        condition_set_ids=tuple(conditions), provenance="observed",
        evidence_refs=_evidence(), method_fingerprint=_method(),
        object_revision_id=revision or f"{network_id}-revision-1",
        parent_revision=parent, expected_current_hash=expected,
    )


def _authority(tmp_path, *, second_network=False, gas=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    authority = CatalysisProjectionAuthority(tmp_path)
    for value in (
        _surface(), _state("state-a"), _state("state-ts"), _state("state-b"),
        _step(gas=gas), _condition(), _network(),
    ):
        authority.domain_store.put(DomainEnvelope.wrap(value))
    if second_network:
        authority.domain_store.put(DomainEnvelope.wrap(_network("network-2")))
    return authority


def _mixed_values(
        *, phase="gas", fluid_participant=None, include_fluid=True,
        product_formula="CO", extra_values=()):
    fluid_participant = fluid_participant or _participant(
        "fluid-co", phase=phase, site=None)
    step = ElementaryStep(
        step_id="step-1",
        reactants=(
            fluid_participant,
            _participant("state-vacant"),
        ),
        transition_state=(_participant("state-ts"),),
        products=(_participant("state-product"),),
        condition_set_id="condition-1", reversible=True,
        provenance="observed", evidence_refs=_evidence(),
        method_fingerprint=_method(), object_revision_id="step-1-revision-1",
    )
    values = [
        _surface(),
        _state("state-vacant", formula="*"),
        _state("state-ts", formula="CO"),
        _state("state-product", formula=product_formula),
    ]
    if include_fluid:
        values.append(_fluid("fluid-co", phase=phase))
    values.extend((
        step, _condition(),
        _network(states=(
            "fluid-co", "state-vacant", "state-ts", "state-product")),
        *extra_values,
    ))
    return values


def _bound_values(tmp_path, values, *, intent="mixed-fluid"):
    authority = CatalysisProjectionAuthority(tmp_path)
    for value in values:
        authority.domain_store.put(DomainEnvelope.wrap(value))
    authority.create_binding(
        network_id="network-1", intent_id=intent, confirmed=True,
        **_network_cas_kwargs(authority))
    return authority


def _gap_codes(snapshot):
    return {item["code"] for item in snapshot["evidence_gaps"]}


def _network_cas_kwargs(authority, network_id="network-1"):
    snapshot = authority.network_head_cas(network_id)
    assert snapshot is not None
    assert snapshot.network_status == "available"
    return {
        "expected_domain_authority_id": snapshot.domain_authority_id,
        "expected_domain_generation": snapshot.domain_generation,
        "expected_domain_snapshot_sha256": snapshot.domain_snapshot_sha256,
        "expected_network_revision_id": snapshot.network_revision_id,
        "expected_network_semantic_sha256": snapshot.network_semantic_sha256,
    }


def test_multiple_networks_are_never_selected_without_an_explicit_binding(tmp_path):
    authority = _authority(tmp_path, second_network=True)
    snapshot = authority.build_snapshot()
    assert snapshot["status"] == "unavailable"
    assert snapshot["network"] is None
    assert _gap_codes(snapshot) == {"active_network_binding_missing"}
    assert snapshot["binding_authority"]["revision"] == 0

    selected = authority.create_binding(
        network_id="network-1", intent_id="intent-select-1", confirmed=True,
        **_network_cas_kwargs(authority))
    assert selected.action == "created"
    assert selected.binding.network_id == "network-1"
    replayed = authority.replay_binding(
        network_id="network-1", intent_id="intent-select-1", confirmed=True,
        expected_authority_id=None, expected_revision=0,
        expected_current_hash=None,
        **_network_cas_kwargs(authority),
    )
    assert replayed.action == "replayed"
    assert replayed.snapshot == selected.snapshot
    assert ActiveBindingAuthoritySnapshot.from_dict(
        selected.snapshot.to_dict()) == selected.snapshot
    assert ActiveNetworkBinding.from_dict(
        selected.binding.to_dict()) == selected.binding


def test_binding_cas_is_strict_and_returns_authoritative_conflicts(tmp_path):
    authority = _authority(tmp_path, second_network=True)
    created = authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    stale = authority.advance_binding(
        network_id="network-2", intent_id="intent-stale", confirmed=True,
        expected_authority_id=created.snapshot.authority_id,
        expected_revision=created.snapshot.revision + 1,
        expected_current_hash=created.snapshot.current_hash,
        **_network_cas_kwargs(authority, "network-2"),
    )
    assert stale.action == "conflict"
    assert stale.reason == "stale_cas"
    assert stale.snapshot == created.snapshot
    assert "path" not in stale.to_dict()

    unconfirmed = authority.advance_binding(
        network_id="network-2", intent_id="intent-unconfirmed", confirmed=False,
        expected_authority_id=created.snapshot.authority_id,
        expected_revision=created.snapshot.revision,
        expected_current_hash=created.snapshot.current_hash,
        **_network_cas_kwargs(authority, "network-2"),
    )
    assert unconfirmed.action == "conflict"
    assert unconfirmed.reason == "confirmation_required"


def test_two_binding_writers_from_one_cas_have_exactly_one_winner(tmp_path):
    authority = _authority(tmp_path, second_network=True)
    created = authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    target_cas = _network_cas_kwargs(authority, "network-2")

    def attempt(index):
        return authority.advance_binding(
            network_id="network-2", intent_id=f"intent-{index}", confirmed=True,
            expected_authority_id=created.snapshot.authority_id,
            expected_revision=created.snapshot.revision,
            expected_current_hash=created.snapshot.current_hash,
            **target_cas,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (1, 2)))
    assert [item.action for item in results].count("advanced") == 1
    assert [item.action for item in results].count("conflict") == 1
    assert next(item for item in results if item.action == "conflict").reason == "stale_cas"
    assert authority.binding_snapshot().revision == 2


def test_any_domain_head_advance_makes_the_old_binding_fail_closed(tmp_path):
    authority = _authority(tmp_path)
    authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    before = authority.build_snapshot()
    assert before["network"]["object_id"] == "network-1"
    assert before["status"] == "available"
    assert before["evidence_gaps"] == []

    first = authority.domain_store.head("CatalystSurface", "surface-1")
    advanced = _surface(
        revision="surface-revision-2",
        parent=first.object_revision_id, expected=first.semantic_sha256,
        composition="Au",
    )
    authority.domain_store.put(DomainEnvelope.wrap(advanced))
    stale = authority.build_snapshot()
    assert stale["status"] == "unavailable"
    assert stale["network"] is None
    assert "domain_generation_advanced" in _gap_codes(stale)


def test_network_head_advance_is_reported_without_projecting_the_new_head(tmp_path):
    authority = _authority(tmp_path)
    authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    first = authority.domain_store.head("ReactionNetwork", "network-1")
    second = _network(
        revision="network-1-revision-2",
        parent=first.object_revision_id, expected=first.semantic_sha256,
    )
    authority.domain_store.put(DomainEnvelope.wrap(second))
    snapshot = authority.build_snapshot()
    assert snapshot["network"] is None
    assert {
        "active_network_head_advanced", "domain_generation_advanced",
    }.issubset(_gap_codes(snapshot))


def test_closure_pins_only_explicit_heads_and_exposes_model_gaps(tmp_path):
    authority = _authority(tmp_path, gas=True)
    authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    snapshot = authority.build_snapshot()
    assert snapshot["domain_authority"]["generation"] == 7
    assert snapshot["network"]["object_revision_id"] == "network-1-revision-1"
    assert {item["object_id"] for item in snapshot["surfaces"]} == {"surface-1"}
    assert {item["object_id"] for item in snapshot["states"]} == {
        "state-a", "state-ts", "state-b",
    }
    assert {item["object_id"] for item in snapshot["steps"]} == {"step-1"}
    assert {item["object_id"] for item in snapshot["conditions"]} == {"condition-1"}
    assert snapshot["transition_states"] == []
    assert _gap_codes(snapshot) == {
        "participant_phase_model_unavailable", "formal_conservation_failed"}
    assert snapshot["authorizes_execution"] is False


def test_all_adsorbed_unit_site_authority_is_available_including_transition(tmp_path):
    authority = _authority(tmp_path)
    authority.create_binding(
        network_id="network-1", intent_id="all-adsorbed", confirmed=True,
        **_network_cas_kwargs(authority))
    snapshot = authority.build_snapshot()
    assert snapshot["status"] == "available"
    assert snapshot["evidence_gaps"] == []
    assert snapshot["gap_summary"] == {
        "total": 0, "returned": 0, "omitted": 0, "by_code": {}}
    assert {item["object_id"] for item in snapshot["states"]} == {
        "state-a", "state-ts", "state-b"}


def test_same_site_name_on_different_surfaces_fails_authority_conservation(tmp_path):
    values = (
        _surface("surface-a"),
        _surface("surface-b"),
        _state("state-a", surface_id="surface-a"),
        _state("state-ts", surface_id="surface-a"),
        _state("state-b", surface_id="surface-b"),
        _step(),
        _condition(),
        ReactionNetwork(
            network_id="network-1", surface_ids=("surface-a", "surface-b"),
            state_ids=("state-a", "state-ts", "state-b"),
            step_ids=("step-1",), condition_set_ids=("condition-1",),
            provenance="observed", evidence_refs=_evidence(),
            method_fingerprint=_method(), object_revision_id="network-revision-1",
        ),
    )
    authority = _bound_values(tmp_path, values, intent="cross-surface-site")

    snapshot = authority.build_snapshot()

    assert snapshot["status"] == "unavailable"
    assert "formal_conservation_failed" in _gap_codes(snapshot)


@pytest.mark.parametrize("phase", ["gas", "liquid", "aqueous"])
def test_mixed_fluid_and_adsorbate_closure_is_exact_and_available(tmp_path, phase):
    authority = _bound_values(tmp_path, _mixed_values(phase=phase))
    snapshot = authority.build_snapshot()
    assert snapshot["status"] == "available"
    assert snapshot["evidence_gaps"] == []
    states = {item["object_id"]: item for item in snapshot["states"]}
    assert states["fluid-co"]["object_type"] == "FluidState"
    assert states["fluid-co"]["payload"]["phase"] == phase
    assert states["fluid-co"]["payload"]["standard_state"]["kind"] == (
        "1-bar" if phase == "gas" else "1-molar")
    assert states["state-product"]["object_type"] == "AdsorbateState"


@pytest.mark.parametrize(("case", "expected"), [
    ("collision", "network_state_type_collision"),
    ("phase", "participant_fluid_phase_disagrees"),
    ("site", "participant_fluid_site_stoichiometry_disagrees"),
    ("charge", "participant_state_charge_disagrees"),
    ("missing", "network_member_missing"),
    ("wrong_type", "network_member_wrong_type"),
    ("conservation", "formal_conservation_failed"),
])
def test_fluid_union_and_participant_authority_fail_closed(
        tmp_path, case, expected):
    kwargs = {}
    extra = ()
    if case == "collision":
        extra = (_state("fluid-co"),)
    elif case == "phase":
        kwargs["fluid_participant"] = _participant(
            "fluid-co", phase="liquid", site=None)
    elif case == "site":
        kwargs["fluid_participant"] = _participant(
            "fluid-co", phase="gas", site="site-top")
    elif case == "charge":
        kwargs["fluid_participant"] = _participant(
            "fluid-co", phase="gas", charge=1, site=None)
    elif case == "missing":
        kwargs["include_fluid"] = False
    elif case == "wrong_type":
        kwargs["include_fluid"] = False
        extra = (_condition("fluid-co"),)
    elif case == "conservation":
        kwargs["product_formula"] = "H2"
    values = _mixed_values(extra_values=extra, **kwargs)
    authority = _bound_values(tmp_path, values, intent=f"invalid-{case}")
    snapshot = authority.build_snapshot()
    assert snapshot["status"] == "unavailable"
    assert expected in _gap_codes(snapshot)


@pytest.mark.parametrize(("case", "expected_code"), [
    ("top_two", "participant_site_stoichiometry_disagrees"),
    ("top_half", "participant_site_stoichiometry_disagrees"),
    ("multiple_sites", "participant_site_stoichiometry_disagrees"),
    ("empty_sites", "participant_site_stoichiometry_disagrees"),
    ("state_site_missing", "participant_site_authority_unavailable"),
    ("charge_mismatch", "participant_state_charge_disagrees"),
    ("state_missing", "network_member_missing"),
    ("gas_phase", "participant_phase_model_unavailable"),
    ("liquid_phase", "participant_phase_model_unavailable"),
])
def test_transition_participant_requires_state_authorized_unit_adsorbed_site(
        tmp_path, case, expected_code):
    authority = CatalysisProjectionAuthority(tmp_path)
    participant = _participant("state-ts")
    transition_state = _state("state-ts")
    include_transition_state = True
    if case == "top_two":
        participant = _participant("state-ts", site_amount=2)
    elif case == "top_half":
        participant = _participant(
            "state-ts", site_amount=ExactRational(1, 2))
    elif case == "multiple_sites":
        participant = _participant("state-ts", extra_site="bridge")
    elif case == "empty_sites":
        participant = _participant("state-ts", site=None)
    elif case == "state_site_missing":
        transition_state = _state("state-ts", geometric_site_id=None)
    elif case == "charge_mismatch":
        participant = _participant("state-ts", charge=1)
    elif case == "state_missing":
        include_transition_state = False
    elif case == "gas_phase":
        participant = _participant("state-ts", phase="gas")
    else:
        participant = _participant("state-ts", phase="liquid")
    step = replace(_step(), transition_state=(participant,))
    values = [_surface(), _state("state-a"), _state("state-b")]
    if include_transition_state:
        values.append(transition_state)
    values.extend((_condition(), step, _network()))
    for value in values:
        authority.domain_store.put(DomainEnvelope.wrap(value))
    authority.create_binding(
        network_id="network-1", intent_id=f"invalid-{case}", confirmed=True,
        **_network_cas_kwargs(authority))
    snapshot = authority.build_snapshot()
    assert snapshot["status"] == "unavailable"
    assert expected_code in _gap_codes(snapshot)


def test_missing_wrong_type_and_membership_errors_fail_closed(tmp_path):
    authority = CatalysisProjectionAuthority(tmp_path)
    values = (
        _surface(), _state("state-a"), _state("state-b"),
        _step(transition="condition-1"), _condition(),
        _network(states=("state-a", "condition-1", "state-b")),
    )
    for value in values:
        authority.domain_store.put(DomainEnvelope.wrap(value))
    authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    snapshot = authority.build_snapshot()
    assert "network_member_wrong_type" in _gap_codes(snapshot)
    assert snapshot["status"] == "unavailable"

    other_root = tmp_path / "other"
    other_root.mkdir()
    other = CatalysisProjectionAuthority(other_root)
    for value in (
        _surface(), _state("state-a"), _state("state-ts"), _state("state-b"),
        _step(product="state-outside"), _condition(), _network(),
    ):
        other.domain_store.put(DomainEnvelope.wrap(value))
    other.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(other))
    membership = other.build_snapshot()
    assert "participant_state_not_in_network" in _gap_codes(membership)


def test_corrupt_binding_or_domain_authority_is_never_overwritten(tmp_path):
    authority = _authority(tmp_path)
    created = authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    binding_path = (
        tmp_path / ".vcstudio" / "catalysis" / "active-network-binding.json")
    binding_path.write_text("{corrupt", encoding="utf-8")
    before = binding_path.read_bytes()
    with pytest.raises(CatalysisProjectionError, match="invalid"):
        authority.advance_binding(
            network_id="network-1", intent_id="intent-next", confirmed=True,
            expected_authority_id=created.snapshot.authority_id,
            expected_revision=created.snapshot.revision,
            expected_current_hash=created.snapshot.current_hash,
            **_network_cas_kwargs(authority),
        )
    assert binding_path.read_bytes() == before

    other = _authority(tmp_path / "other")
    other.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(other))
    domain_path = (
        tmp_path / "other" / ".vcstudio" / "catalysis" / "domain-envelopes.json")
    domain_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(CatalysisProjectionError, match="domain authority"):
        other.build_snapshot()
    assert domain_path.read_text(encoding="utf-8") == "{corrupt"


def test_projection_outputs_no_paths_or_secrets_and_refuses_secret_intents(tmp_path):
    authority = _authority(tmp_path)
    with pytest.raises(CatalysisProjectionError, match="opaque identifier"):
        authority.create_binding(
            network_id="network-1",
            intent_id="github_pat_1234567890abcdefghijklmnop",
            confirmed=True,
            **_network_cas_kwargs(authority),
        )
    authority.create_binding(
        network_id="network-1", intent_id="intent-safe", confirmed=True,
        **_network_cas_kwargs(authority))
    wire = json.dumps(authority.build_snapshot(), sort_keys=True)
    assert str(tmp_path) not in wire
    assert "domain-envelopes.json" not in wire
    assert "active-network-binding.json" not in wire
    assert "github_pat_1234567890abcdefghijklmnop" not in wire


def test_project_internal_directory_cannot_alias_outside_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    internal = tmp_path / "project" / ".vcstudio"
    internal.parent.mkdir()
    try:
        os.symlink(outside, internal, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with pytest.raises(CatalysisProjectionError, match="link|reparse"):
        CatalysisProjectionAuthority(tmp_path / "project")
    assert not (outside / "catalysis").exists()


def test_same_generation_member_aba_changes_snapshot_hash_and_stales_binding(tmp_path):
    authority = _authority(tmp_path)
    created = authority.create_binding(
        network_id="network-1", intent_id="intent-create", confirmed=True,
        **_network_cas_kwargs(authority))
    pinned = created.binding
    assert pinned.domain_snapshot_sha256 == created.network_cas.domain_snapshot_sha256

    path = tmp_path / ".vcstudio" / "catalysis" / "domain-envelopes.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    original = DomainEnvelope.wrap(_state("state-a"))
    replacement = DomainEnvelope.wrap(replace(
        _state("state-a"), chemical_formula="NO"))
    revision_key = store_module._revision_key(original)
    assert store_module._revision_key(replacement) == revision_key
    stored["revisions"][revision_key] = replacement.to_dict()
    path.write_text(json.dumps(stored), encoding="utf-8")

    current = authority.domain_store.snapshot_heads()
    assert current.generation == pinned.domain_generation
    assert current.snapshot_sha256 != pinned.domain_snapshot_sha256
    snapshot = authority.build_snapshot()
    assert snapshot["network"] is None
    assert "domain_snapshot_changed" in _gap_codes(snapshot)


def test_confirmation_of_revision_one_conflicts_after_network_advances(tmp_path):
    authority = _authority(tmp_path)
    expected_revision_one = _network_cas_kwargs(authority)
    first = authority.domain_store.head("ReactionNetwork", "network-1")
    authority.domain_store.put(DomainEnvelope.wrap(_network(
        revision="network-1-revision-2",
        parent=first.object_revision_id, expected=first.semantic_sha256,
    )))

    conflict = authority.create_binding(
        network_id="network-1", intent_id="confirm-revision-one", confirmed=True,
        **expected_revision_one)
    assert conflict.action == "conflict"
    assert conflict.reason == "stale_domain_snapshot"
    assert conflict.snapshot.revision == 0
    assert conflict.network_cas.network_revision_id == "network-1-revision-2"
    assert conflict.network_cas.network_semantic_sha256 != (
        expected_revision_one["expected_network_semantic_sha256"])


def test_project_root_a_to_b_replacement_is_rejected_before_any_new_write(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    authority = _authority(project)
    moved = tmp_path / "project-a"
    project.rename(moved)
    project.mkdir()

    with pytest.raises(CatalysisProjectionError, match="identity changed"):
        authority.binding_snapshot()
    assert not (project / ".vcstudio").exists()
    assert (moved / ".vcstudio" / "catalysis" / "domain-envelopes.json").is_file()


def test_ten_thousand_large_refs_fail_before_closure_and_public_bytes_are_bounded(
        tmp_path, monkeypatch):
    large_root = tmp_path / "large"
    large_root.mkdir()
    authority = CatalysisProjectionAuthority(large_root)
    state_ids = tuple(
        f"state-{index:05d}-" + "x" * 140 for index in range(10_000))
    for value in (
        _surface(), _step(), _condition(), _network(states=state_ids),
    ):
        authority.domain_store.put(DomainEnvelope.wrap(value))
    result = authority.create_binding(
        network_id="network-1", intent_id="large-network", confirmed=True,
        **_network_cas_kwargs(authority))
    assert result.action == "conflict"
    assert result.reason == "network_resource_limit"
    assert result.snapshot.revision == 0
    assert result.network_cas.network_status == "available"

    normal = _authority(tmp_path / "normal")
    normal.create_binding(
        network_id="network-1", intent_id="normal-network", confirmed=True,
        **_network_cas_kwargs(normal))
    baseline = normal.build_snapshot()
    baseline_size = len(json.dumps(
        baseline, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    monkeypatch.setattr(
        projection_module, "MAX_PUBLIC_SNAPSHOT_BYTES", baseline_size - 1)
    bounded = normal.build_snapshot()
    assert _gap_codes(bounded) == {"projection_snapshot_resource_limit"}
    assert len(json.dumps(
        bounded, sort_keys=True, separators=(",", ":")).encode("utf-8")) \
        <= projection_module.MAX_PUBLIC_SNAPSHOT_BYTES


def test_projection_gaps_are_bounded_with_complete_summary_counts(tmp_path):
    authority = CatalysisProjectionAuthority(tmp_path)
    state_ids = ("state-a", "state-ts", "state-b") + tuple(
        f"missing-state-{index:03d}" for index in range(297))
    for value in (
        _surface(), _step(), _condition(), _network(states=state_ids),
    ):
        authority.domain_store.put(DomainEnvelope.wrap(value))
    authority.create_binding(
        network_id="network-1", intent_id="bounded-gaps", confirmed=True,
        **_network_cas_kwargs(authority))
    snapshot = authority.build_snapshot()
    assert len(snapshot["evidence_gaps"]) == projection_module.MAX_PROJECTION_GAPS
    assert snapshot["gap_summary"]["returned"] == (
        projection_module.MAX_PROJECTION_GAPS)
    assert snapshot["gap_summary"]["omitted"] > 0
    assert snapshot["gap_summary"]["by_code"]["network_member_missing"] == 300
