"""Active ReactionNetwork bootstrap, signed preview and confirm tests."""
from __future__ import annotations

import copy
import json
import multiprocessing
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from vcstudio.project import catalysis_projection as projection_module
from vcstudio.project.catalysis_authoring import (
    PREVIEW_REQUEST_SCHEMA,
    CatalysisActiveNetworkAuthoringService,
    CatalysisActiveNetworkPreviewRequest,
    CatalysisAuthoringBootstrap,
    CatalysisAuthoringCandidateSnapshot,
    CatalysisAuthoringDomainCAS,
    CatalysisAuthoringGap,
    CatalysisAuthoringGapSummary,
    CatalysisAuthoringValidationError,
)
from vcstudio.project.catalysis_contracts import (
    AdsorbateState,
    CatalystSurface,
    ConditionSet,
    DomainEnvelope,
    ElementaryStep,
    EvidenceRef,
    ExactRational,
    MethodFingerprint,
    ReactionNetwork,
    ReactionParticipant,
)
from vcstudio.project.catalysis_projection import CatalysisProjectionAuthority


SEAL_KEY = b"server-owned-catalysis-authoring-key-v1" * 2


def _evidence():
    return (
        EvidenceRef(
            "calculation_result", "result-observed", "observed", "result-revision-1"
        ),
    )


def _method():
    return MethodFingerprint(
        method_id="method-001",
        scope="electronic_structure",
        sha256="a" * 64,
        evidence_refs=(
            EvidenceRef(
                "method_record", "method-observed", "observed", "method-revision-1"
            ),
        ),
    )


def _surface(surface_id="surface-1"):
    return CatalystSurface(
        surface_id=surface_id,
        composition="Pt",
        miller_indices=(1, 1, 1),
        termination_id="termination-1",
        geometric_site_ids=("site-top",),
        provenance="observed",
        evidence_refs=_evidence(),
        method_fingerprint=_method(),
        object_revision_id=f"{surface_id}-revision-1",
    )


def _state(state_id):
    return AdsorbateState(
        state_id=state_id,
        surface_id="surface-1",
        adsorbate_id=f"adsorbate-{state_id}",
        chemical_formula="CO",
        geometric_site_id="site-top",
        charge=0,
        multiplicity=1,
        provenance="observed",
        evidence_refs=_evidence(),
        method_fingerprint=_method(),
        object_revision_id=f"{state_id}-revision-1",
    )


def _participant(state_id):
    return ReactionParticipant(
        state_id=state_id,
        coefficient=ExactRational(1),
        phase="adsorbed",
        charge=0,
        site_stoichiometry={"site-top": ExactRational(1)},
    )


def _step():
    return ElementaryStep(
        step_id="step-1",
        reactants=(_participant("state-a"),),
        transition_state=(_participant("state-ts"),),
        products=(_participant("state-b"),),
        condition_set_id="condition-1",
        reversible=True,
        provenance="observed",
        evidence_refs=_evidence(),
        method_fingerprint=_method(),
        object_revision_id="step-1-revision-1",
    )


def _condition():
    return ConditionSet(
        condition_set_id="condition-1",
        temperature_k=300.0,
        pressure_pa=100_000.0,
        ph=None,
        electrode_potential_v=None,
        provenance="observed",
        evidence_refs=_evidence(),
        method_fingerprint=_method(),
        object_revision_id="condition-1-revision-1",
    )


def _network(
    network_id="network-1",
    *,
    surfaces=("surface-1",),
    states=("state-a", "state-ts", "state-b"),
    steps=("step-1",),
    conditions=("condition-1",),
    revision=None,
    parent=None,
    expected=None,
):
    return ReactionNetwork(
        network_id=network_id,
        surface_ids=tuple(surfaces),
        state_ids=tuple(states),
        step_ids=tuple(steps),
        condition_set_ids=tuple(conditions),
        provenance="observed",
        evidence_refs=_evidence(),
        method_fingerprint=_method(),
        object_revision_id=revision or f"{network_id}-revision-1",
        parent_revision=parent,
        expected_current_hash=expected,
    )


def _authority(root: Path, *, second_network=False, incomplete=False):
    root.mkdir(parents=True, exist_ok=True)
    authority = CatalysisProjectionAuthority(root)
    values = [_network()]
    if not incomplete:
        values = [
            _surface(),
            _state("state-a"),
            _state("state-ts"),
            _state("state-b"),
            _step(),
            _condition(),
            *values,
        ]
    if second_network:
        values.append(_network("network-2"))
    for value in values:
        authority.domain_store.put(DomainEnvelope.wrap(value))
    return authority


class LockedSnapshotAdapter:
    """Test implementation proving one authority/domain lock per snapshot."""

    def __init__(self, authority: CatalysisProjectionAuthority, project_id="project-1"):
        self.authority = authority
        self.project_id = project_id
        self.bootstrap_reads = 0
        self.candidate_reads = 0

    @staticmethod
    def _gap(raw):
        return CatalysisAuthoringGap(
            status=raw["status"],
            code=raw["code"],
            object_type=raw["object_type"],
            object_id=raw["object_id"],
            # Context is deliberately suppressed: the authoring boundary needs
            # diagnostic identity/counts, never formulas, phases or energies.
            context={},
        )

    def _candidate(self, binding, domain, index, network_id):
        network_cas = projection_module._network_cas(domain, index, network_id)
        network = index.head("ReactionNetwork", network_id)
        if network is None:
            raw_gaps = [
                {
                    "status": "missing",
                    "code": "network_missing",
                    "object_type": "ReactionNetwork",
                    "object_id": network_id,
                }
            ]
            status = "unavailable"
        else:
            parsed = projection_module.ReactionNetwork.from_dict(network.payload)
            resource = projection_module._network_resource_gap(parsed)
            if resource is not None:
                raw_gaps = [resource]
                status = "unavailable"
            else:
                projection = projection_module._build_closure(binding, domain, network, index)
                raw_gaps = projection["evidence_gaps"]
                status = projection["status"]
        gaps = tuple(sorted(
            (self._gap(item) for item in raw_gaps[:64]),
            key=lambda item: json.dumps(item.to_dict(), sort_keys=True),
        ))
        by_code = {}
        for item in raw_gaps:
            by_code[item["code"]] = by_code.get(item["code"], 0) + 1
        summary = CatalysisAuthoringGapSummary(
            total=len(raw_gaps),
            returned=len(gaps),
            omitted=len(raw_gaps) - len(gaps),
            by_code=by_code,
        )
        return CatalysisAuthoringCandidateSnapshot(
            project_id=self.project_id,
            binding=binding,
            network_head=network_cas,
            projection_status=status,
            gaps=gaps,
            gap_summary=summary,
        )

    def authoring_bootstrap_snapshot(self):
        self.bootstrap_reads += 1
        with self.authority._locked():
            store = self.authority._read_binding_store()
            binding = projection_module._snapshot_from_store(store)
            with self.authority.domain_store.locked_snapshot_heads(initialize=False) as domain:
                if domain is None:
                    return CatalysisAuthoringBootstrap(
                        project_id=self.project_id,
                        status="missing",
                        reason="domain_authority_missing",
                        domain_cas=None,
                        binding_cas=binding,
                        candidates=(),
                    )
                index = projection_module._HeadIndex.build(domain)
                network_ids = sorted(
                    envelope.object_id
                    for envelope in domain.heads
                    if envelope.object_type == "ReactionNetwork"
                )
                candidates = tuple(
                    self._candidate(binding, domain, index, network_id).to_candidate()
                    for network_id in network_ids
                )
                return CatalysisAuthoringBootstrap(
                    project_id=self.project_id,
                    status="available",
                    reason=None,
                    domain_cas=CatalysisAuthoringDomainCAS(
                        authority_id=domain.authority_id,
                        generation=domain.generation,
                        snapshot_sha256=domain.snapshot_sha256,
                    ),
                    binding_cas=binding,
                    candidates=candidates,
                )

    def authoring_candidate_snapshot(self, network_id):
        self.candidate_reads += 1
        with self.authority._locked():
            store = self.authority._read_binding_store()
            binding = projection_module._snapshot_from_store(store)
            with self.authority.domain_store.locked_snapshot_heads(initialize=False) as domain:
                if domain is None:
                    return None
                index = projection_module._HeadIndex.build(domain)
                return self._candidate(binding, domain, index, network_id)


def _service(authority, *, clock=None):
    adapter = LockedSnapshotAdapter(authority)
    return CatalysisActiveNetworkAuthoringService(
        project_id="project-1",
        owner_id="owner-1",
        snapshot_authority=adapter,
        binding_authority=authority,
        preview_seal_key=SEAL_KEY,
        time_source=(lambda: 1000.0) if clock is None else clock,
    ), adapter


def _request(bootstrap, network_id, intent_id):
    candidate = next(item for item in bootstrap.candidates if item.network_id == network_id)
    return CatalysisActiveNetworkPreviewRequest(
        network_id=network_id,
        intent_id=intent_id,
        expected_binding=bootstrap.binding_cas,
        expected_network_head=candidate.network_head,
    )


def test_bootstrap_lists_multiple_networks_without_auto_select_or_raw_payload(tmp_path):
    service, adapter = _service(_authority(tmp_path, second_network=True))

    bootstrap = service.bootstrap()

    assert bootstrap.status == "available"
    assert [item.network_id for item in bootstrap.candidates] == ["network-1", "network-2"]
    assert bootstrap.active_network_id is None
    assert bootstrap.active_status == "none"
    assert adapter.bootstrap_reads == 1
    wire = json.dumps(bootstrap.to_dict()).lower()
    assert "chemical_formula" not in wire
    assert "energy" not in wire
    assert "payload" not in wire
    assert "composition" not in wire
    assert str(tmp_path).lower() not in wire


def test_strict_preview_request_rejects_unknown_fields_paths_and_axis_mismatch(tmp_path):
    service, _adapter = _service(_authority(tmp_path))
    request = _request(service.bootstrap(), "network-1", "intent-1").to_dict()

    unknown = {**request, "actor": "person"}
    with pytest.raises(CatalysisAuthoringValidationError):
        CatalysisActiveNetworkPreviewRequest.from_dict(unknown)

    path = copy.deepcopy(request)
    path["network_id"] = "C:\\Users\\person\\network"
    with pytest.raises(CatalysisAuthoringValidationError):
        CatalysisActiveNetworkPreviewRequest.from_dict(path)

    mismatch = copy.deepcopy(request)
    mismatch["network_id"] = "network-2"
    with pytest.raises(CatalysisAuthoringValidationError):
        CatalysisActiveNetworkPreviewRequest.from_dict(mismatch)


def test_preview_revalidates_one_candidate_snapshot_and_never_writes_binding(tmp_path):
    authority = _authority(tmp_path)
    service, adapter = _service(authority)
    request = _request(service.bootstrap(), "network-1", "preview-only")
    binding_path = authority._binding_path
    domain_before = authority.domain_store.path.read_bytes()

    preview = service.preview(request)

    assert preview.status == "ready"
    assert preview.action == "create"
    assert preview.confirmed is False
    assert preview.authorizes_execution is False
    assert preview.can_confirm is True
    assert adapter.candidate_reads == 1
    assert not binding_path.exists()
    assert authority.domain_store.path.read_bytes() == domain_before


def test_confirm_create_and_response_loss_replay_are_exactly_idempotent(tmp_path):
    authority = _authority(tmp_path)
    service, _adapter = _service(authority)
    request = _request(service.bootstrap(), "network-1", "select-network-1")
    preview = service.preview(request)
    confirmation = service.confirmation_from_preview(preview)

    created = service.confirm(request, confirmation)
    replayed = service.confirm(request, confirmation)

    assert created.action == "created"
    assert replayed.action == "replayed"
    assert replayed.binding_cas == created.binding_cas
    assert replayed.network_cas == created.network_cas


def test_same_intent_with_a_different_target_is_rejected_without_retry(tmp_path):
    authority = _authority(tmp_path, second_network=True)
    service, _adapter = _service(authority)
    first_request = _request(service.bootstrap(), "network-1", "one-intent")
    first_preview = service.preview(first_request)
    assert service.confirm(
        first_request, service.confirmation_from_preview(first_preview)
    ).action == "created"

    latest = service.bootstrap()
    second_request = _request(latest, "network-2", "one-intent")
    second_preview = service.preview(second_request)
    result = service.confirm(
        second_request, service.confirmation_from_preview(second_preview)
    )

    assert result.action == "conflict"
    assert result.conflict.reason == "intent_reused"
    assert result.conflict.retry_automatically is False


def test_stale_binding_domain_and_network_are_stable_conflicts(tmp_path):
    authority = _authority(tmp_path, second_network=True)
    service, _adapter = _service(authority)
    base = service.bootstrap()
    stale_binding_request = _request(base, "network-1", "stale-binding")
    preview = service.preview(stale_binding_request)

    competing = _request(base, "network-2", "competing")
    competing_preview = service.preview(competing)
    assert service.confirm(
        competing, service.confirmation_from_preview(competing_preview)
    ).action == "created"

    stale_binding = service.confirm(
        stale_binding_request, service.confirmation_from_preview(preview)
    )
    assert stale_binding.action == "conflict"
    assert stale_binding.conflict.reason == "stale_cas"
    assert stale_binding.conflict.retry_automatically is False

    latest = service.bootstrap()
    domain_request = _request(latest, "network-1", "stale-domain")
    domain_preview = service.preview(domain_request)
    authority.domain_store.put(DomainEnvelope.wrap(_surface("surface-2")))
    stale_domain = service.confirm(
        domain_request, service.confirmation_from_preview(domain_preview)
    )
    assert stale_domain.action == "conflict"
    assert stale_domain.conflict.reason == "stale_domain_snapshot"

    current = service.bootstrap()
    candidate = next(item for item in current.candidates if item.network_id == "network-1")
    wrong_head = candidate.network_head.to_dict()
    wrong_head["network_revision_id"] = "network-1-old-revision"
    wrong_head.pop("snapshot_sha256")
    wrong_head = projection_module.NetworkHeadCAS(**wrong_head)
    stale_network_request = CatalysisActiveNetworkPreviewRequest(
        network_id="network-1",
        intent_id="stale-network",
        expected_binding=current.binding_cas,
        expected_network_head=wrong_head,
    )
    stale_network = service.preview(stale_network_request)
    assert stale_network.status == "conflict"
    assert stale_network.reason == "stale_network_head"


def test_gap_network_is_bindable_but_resource_limit_is_not(tmp_path):
    incomplete = _authority(tmp_path / "incomplete", incomplete=True)
    incomplete_service, _adapter = _service(incomplete)
    incomplete_request = _request(
        incomplete_service.bootstrap(), "network-1", "diagnostic-network"
    )
    incomplete_preview = incomplete_service.preview(incomplete_request)
    assert incomplete_preview.projection_status == "unavailable"
    assert incomplete_preview.gap_summary["total"] > 0
    assert incomplete_preview.can_confirm is True

    resource_root = tmp_path / "resource"
    resource_root.mkdir()
    resource_authority = CatalysisProjectionAuthority(resource_root)
    resource_network = _network(
        surfaces=tuple(f"surface-{index}" for index in range(257))
    )
    resource_authority.domain_store.put(DomainEnvelope.wrap(resource_network))
    resource_service, _adapter = _service(resource_authority)
    resource_request = _request(
        resource_service.bootstrap(), "network-1", "resource-network"
    )
    resource_preview = resource_service.preview(resource_request)
    assert resource_preview.projection_status == "unavailable"
    assert resource_preview.can_confirm is False
    assert resource_preview.reason == "network_resource_limit"


def _spawn_confirm(root: str, request: dict, confirmation: dict, queue) -> None:
    authority = CatalysisProjectionAuthority(root)
    adapter = LockedSnapshotAdapter(authority)
    service = CatalysisActiveNetworkAuthoringService(
        project_id="project-1",
        owner_id="owner-1",
        snapshot_authority=adapter,
        binding_authority=authority,
        preview_seal_key=SEAL_KEY,
        time_source=lambda: 1000.0,
    )
    result = service.confirm(request, confirmation)
    queue.put((result.action, None if result.conflict is None else result.conflict.reason))


def test_two_spawned_processes_from_one_binding_cas_have_one_winner(tmp_path):
    authority = _authority(tmp_path, second_network=True)
    service, _adapter = _service(authority)
    bootstrap = service.bootstrap()
    requests = [
        _request(bootstrap, "network-1", "race-one"),
        _request(bootstrap, "network-2", "race-two"),
    ]
    confirmations = [
        service.confirmation_from_preview(service.preview(request)) for request in requests
    ]
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_spawn_confirm,
            args=(str(tmp_path), request.to_dict(), confirmation.to_dict(), queue),
        )
        for request, confirmation in zip(requests, confirmations)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    results = [queue.get(timeout=5) for _ in processes]
    assert [item[0] for item in results].count("created") == 1
    assert [item[0] for item in results].count("conflict") == 1


def test_missing_authorities_and_expired_or_tampered_seals_fail_closed(tmp_path):
    missing = CatalysisActiveNetworkAuthoringService(
        project_id="project-1",
        owner_id="owner-1",
        snapshot_authority=None,
        binding_authority=None,
        preview_seal_key=SEAL_KEY,
        time_source=lambda: 1000.0,
    )
    assert missing.bootstrap().status == "unavailable"

    authority = _authority(tmp_path)
    now = [1000.0]
    service, _adapter = _service(authority, clock=lambda: now[0])
    request = _request(service.bootstrap(), "network-1", "ttl-intent")
    preview = service.preview(request)
    confirmation = service.confirmation_from_preview(preview)
    now[0] += 301
    expired = service.confirm(request, confirmation)
    assert expired.action == "conflict"
    assert expired.conflict.reason == "preview_expired"

    now[0] = 1000.0
    preview = service.preview(request)
    tampered = service.confirmation_from_preview(preview).to_dict()
    tampered["preview_sha256"] = "0" * 64
    invalid = service.confirm(request, tampered)
    assert invalid.action == "conflict"
    assert invalid.conflict.reason == "invalid_preview_seal"


def _assert_public(value):
    if isinstance(value, Mapping):
        forbidden = {
            "actor",
            "chemical_formula",
            "composition",
            "energy",
            "envelope",
            "path",
            "payload",
            "project_root",
            "raw",
            "secret",
            "token",
        }
        assert not (set(value) & forbidden)
        for item in value.values():
            _assert_public(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _assert_public(item)
    else:
        assert not isinstance(value, Path)


def test_public_dtos_recursively_exclude_paths_raw_facts_actors_and_secrets(tmp_path):
    authority = _authority(tmp_path)
    service, _adapter = _service(authority)
    bootstrap = service.bootstrap()
    request = _request(bootstrap, "network-1", "public-boundary")
    preview = service.preview(request)
    confirmation = service.confirmation_from_preview(preview)
    result = service.confirm(request, confirmation)

    for value in (
        bootstrap.to_dict(),
        request.to_dict(),
        preview.to_dict(),
        confirmation.to_dict(),
        result.to_dict(),
    ):
        _assert_public(value)


def test_preview_request_schema_is_canonical_lowercase():
    assert PREVIEW_REQUEST_SCHEMA == "vcstudio.catalysis-active-network-preview-request/v1"
