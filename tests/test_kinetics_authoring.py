"""Strict KineticsModelSpecDraft compiler and durable authoring tests."""
from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from vcstudio.project.kinetics_authoring import (
    ACTIVE_SELECTOR_FILENAME,
    AUTHORING_PENDING_FILENAME,
    AUTHORING_RECEIPT_FILENAME,
    DRAFT_SCHEMA,
    LEGACY_SELECTOR_SCHEMA,
    SELECTOR_ANCHOR_FILENAME,
    ActiveKineticsModelSpecSelection,
    ActiveKineticsModelSpecSelectorStore,
    AuthoringSourceSnapshot,
    KineticsAuthoringCoordinator,
    KineticsAuthoringError,
    KineticsAuthoringInjectedFailure,
    KineticsAuthoringValidationError,
    KineticsModelSpecDraft,
    KineticsModelSpecDraftCompiler,
)
from vcstudio.project.kinetics_model_spec import KineticsModelSpecStore


DOMAIN_AUTHORITY = "d" * 32
DOMAIN_SHA = "1" * 64
NETWORK_SHA = "2" * 64
PROJECTION_SHA = "a" * 64
ARTIFACTS = {
    "assumptions": b"explicit assumptions",
    "feed": b"feed conditions",
    "step": b"step policy",
    "sites": b"site normalization",
    "parameters": b"empirical parameters",
}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_snapshot(**changes) -> AuthoringSourceSnapshot:
    values = {
        "project_id": "project-1",
        "domain_authority_id": DOMAIN_AUTHORITY,
        "domain_generation": 8,
        "domain_snapshot_sha256": DOMAIN_SHA,
        "network_id": "network-1",
        "network_revision": "network-r7",
        "network_semantic_sha256": NETWORK_SHA,
        "source_projection_sha256": PROJECTION_SHA,
        "required_feed_reservoir_ids": ["CO_g"],
        "allowed_target_product_ids": ["CO_s"],
        "required_step_ids": ["co_adsorption"],
        "required_site_type_ids": ["top"],
        "saddle_candidates_by_step": {"co_adsorption": ["CO_ts"]},
        "evidence_catalog": [
            {
                "reference_id": reference,
                "kind": "condition" if reference == "feed" else "model",
                "artifact_sha256": _sha(payload),
            }
            for reference, payload in ARTIFACTS.items()
        ],
        "solver_ready": True,
        "solver_readiness_reasons": [],
    }
    values.update(changes)
    return AuthoringSourceSnapshot(**values)


class SourceAuthority:
    def __init__(self, snapshots=None):
        self.snapshots = list(snapshots or [source_snapshot()])
        self.calls = 0

    def authoring_source_snapshot(self):
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[index]


def draft_dict(*, spec_id="co-model", mode="create") -> dict:
    return {
        "schema": DRAFT_SCHEMA,
        "spec_id": spec_id,
        "mode": mode,
        "rate_law_policy": {
            "activity": "ideal",
            "reversibility": "explicit_reverse",
            "detailed_balance": "enforced",
            "prefactor": "explicit_per_step",
            "electrochemical": "none",
            "reactor": "mean_field_steady_state",
        },
        "assumptions": {
            "mean_field": True,
            "steady_state": True,
            "site_uniformity": "uniform",
            "lateral_interactions": "neglected",
            "mechanism_completeness": "claimed_complete",
            "evidence_ref_ids": ["assumptions"],
        },
        "feed_reservoirs": [
            {
                "species_id": "CO_g",
                "activity": 1.0,
                "unit": "bar",
                "source_ref_id": "feed",
            }
        ],
        "target_product_ids": ["CO_s"],
        "steps": [
            {
                "step_id": "co_adsorption",
                "prefactors": {
                    direction: {
                        "value": 1.0e13,
                        "unit": "s^-1",
                        "source_ref_id": "step",
                    }
                    for direction in ("forward", "reverse")
                },
                "bep": {"used": False, "source_ref_id": None, "parameters_ref_id": None},
                "scaling": {
                    "used": False,
                    "source_ref_id": None,
                    "parameters_ref_id": None,
                },
                "uncertainty_eV": 0.1,
                "evidence_ref_ids": ["step"],
            }
        ],
        "site_population_totals": [
            {
                "site_type": "top",
                "value": 2.0,
                "unit": "sites",
                "basis": "surface_unit_cell",
                "evidence_ref_ids": ["sites"],
            }
        ],
    }


def coordinator(root: Path, authority=None, *, fault=None, resolver=None):
    store = KineticsModelSpecStore(root)
    if not store.path.exists():
        store.snapshot()
    return KineticsAuthoringCoordinator(
        root,
        source_authority=authority or SourceAuthority(),
        evidence_resolver=resolver,
        model_store=store,
        fault_injector=fault,
    )


def preview_and_confirmation(service, draft, intent="intent-1"):
    preview = service.preview(draft, intent_id=intent)
    assert preview.can_confirm, preview.to_dict()
    return preview, service.confirmation_from_preview(preview)


def test_empty_draft_has_no_scientific_defaults_and_preview_writes_no_authorities(tmp_path):
    service = coordinator(tmp_path)
    store_before = service._model_store.path.read_bytes()

    result = service.preview({}, intent_id="empty-draft")

    assert result.can_confirm is False
    assert result.solver_ready is False
    assert result.authorizes_execution is False
    assert {issue.code for issue in result.issues} == {"invalid_fields"}
    assert service._model_store.path.read_bytes() == store_before
    directory = tmp_path / ".vcstudio" / "kinetics"
    assert not (directory / ACTIVE_SELECTOR_FILENAME).exists()
    assert not (directory / AUTHORING_PENDING_FILENAME).exists()
    assert not (directory / AUTHORING_RECEIPT_FILENAME).exists()


def test_missing_source_or_uninitialized_store_is_structured_unavailable(tmp_path):
    service = KineticsAuthoringCoordinator(tmp_path, source_authority=None)

    missing_source = service.preview(draft_dict(), intent_id="missing-source")

    assert missing_source.can_confirm is False
    assert "source_unavailable" in {issue.code for issue in missing_source.issues}
    assert not service._model_store.path.exists()

    with_source = KineticsAuthoringCoordinator(tmp_path, source_authority=SourceAuthority())
    missing_store = with_source.preview(draft_dict(), intent_id="missing-store")
    assert missing_store.can_confirm is False
    assert "model_store_unavailable" in {issue.code for issue in missing_store.issues}
    assert not with_source._model_store.path.exists()


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda value: value.update({"project_id": "project-1"}), "invalid_fields"),
        (lambda value: value.update({"revision": "r1"}), "invalid_fields"),
        (lambda value: value["steps"][0].update({"uncertainty_eV": float("nan")}), "invalid_number"),
        (lambda value: value["feed_reservoirs"][0].update({"unit": "atm"}), "unsupported_unit"),
        (lambda value: value["steps"].append(copy.deepcopy(value["steps"][0])), "duplicate_id"),
        (lambda value: value["target_product_ids"].append("CO_s"), "duplicate_id"),
        (
            lambda value: value["assumptions"].update(
                {"evidence_ref_ids": ["C:\\Users\\person\\evidence"]}
            ),
            "invalid_identifier",
        ),
    ],
)
def test_draft_strict_fields_ids_finite_numbers_and_units(mutate, code):
    value = draft_dict()
    mutate(value)

    with pytest.raises(KineticsAuthoringValidationError) as caught:
        KineticsModelSpecDraft.from_dict(value)

    assert caught.value.issues[0].code == code


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda value: value["feed_reservoirs"][0].update({"species_id": "wrong"}), "coverage_mismatch"),
        (lambda value: value["target_product_ids"].__setitem__(0, "wrong"), "membership_mismatch"),
        (lambda value: value["steps"][0].update({"step_id": "wrong"}), "coverage_mismatch"),
        (
            lambda value: value["site_population_totals"][0].update({"site_type": "wrong"}),
            "coverage_mismatch",
        ),
        (
            lambda value: value["assumptions"].update({"evidence_ref_ids": ["unknown"]}),
            "unknown_evidence_ref",
        ),
    ],
)
def test_compiler_rejects_membership_coverage_and_unknown_evidence(mutate, code):
    value = draft_dict()
    mutate(value)

    with pytest.raises(KineticsAuthoringValidationError) as caught:
        KineticsModelSpecDraftCompiler().compile(value, source_snapshot(), None)

    assert caught.value.issues[0].code == code


def test_saddle_membership_is_server_authoritative():
    value = draft_dict()
    value["saddle_selector"] = {
        "mode": "explicit_species_by_step",
        "by_step": {"co_adsorption": "wrong-ts"},
        "evidence_ref_ids": ["step"],
    }

    with pytest.raises(KineticsAuthoringValidationError) as caught:
        KineticsModelSpecDraftCompiler().compile(value, source_snapshot(), None)

    assert caught.value.issues[0].code == "membership_mismatch"


def test_unbound_catalog_hash_requires_injected_byte_resolver():
    catalog = source_snapshot().to_dict()["evidence_catalog"]
    for entry in catalog:
        if entry["reference_id"] == "step":
            entry["artifact_sha256"] = None
    source = source_snapshot(evidence_catalog=catalog)

    with pytest.raises(KineticsAuthoringValidationError) as caught:
        KineticsModelSpecDraftCompiler().compile(draft_dict(), source, None)
    assert caught.value.issues[0].code == "evidence_resolver_unavailable"

    spec = KineticsModelSpecDraftCompiler(lambda reference: ARTIFACTS[reference]).compile(
        draft_dict(), source, None
    )
    assert spec.steps[0]["evidence"][0]["evidence_sha256"] == _sha(ARTIFACTS["step"])


def test_compile_maps_catalog_refs_to_hashes_and_never_accepts_draft_hashes():
    draft = KineticsModelSpecDraft.from_dict(draft_dict())
    spec = KineticsModelSpecDraftCompiler().compile(draft, source_snapshot(), None)

    assert spec.project_id == "project-1"
    assert spec.revision.startswith("authoring-")
    assert spec.source_binding["network_id"] == "network-1"
    assert spec.source_binding["source_projection_sha256"] == PROJECTION_SHA
    assert spec.assumptions["evidence"][0]["reference"] == "assumptions"
    assert spec.assumptions["evidence"][0]["evidence_sha256"] == _sha(
        ARTIFACTS["assumptions"]
    )
    assert "source_binding" not in draft.to_dict()
    assert "revision" not in draft.to_dict()


def test_preview_is_read_only_and_confirm_commits_exact_spec_then_selector(tmp_path):
    authority = SourceAuthority()
    service = coordinator(tmp_path, authority)
    store_before = service._model_store.path.read_bytes()
    preview, confirmation = preview_and_confirmation(service, draft_dict())
    directory = tmp_path / ".vcstudio" / "kinetics"

    assert service._model_store.path.read_bytes() == store_before
    assert not (directory / ACTIVE_SELECTOR_FILENAME).exists()
    assert not (directory / AUTHORING_PENDING_FILENAME).exists()
    assert not (directory / AUTHORING_RECEIPT_FILENAME).exists()
    assert preview.authorizes_execution is False

    result = service.confirm(draft_dict(), confirmation)

    assert result.action == "committed"
    assert result.spec["spec_id"] == "co-model"
    committed_head = service._model_store.head("project-1", "co-model")
    assert result.spec == committed_head.to_dict()
    assert result.selector["spec_sha256"] == committed_head.semantic_sha256
    assert service.selector_snapshot().selection.spec_sha256 == result.selector["spec_sha256"]
    assert not (directory / AUTHORING_PENDING_FILENAME).exists()
    assert (directory / AUTHORING_RECEIPT_FILENAME).exists()
    # Preview read + confirm preview/source check + the store's mandatory live two reads.
    assert authority.calls >= 5


def test_contract_valid_diagnostic_spec_can_save_without_claiming_solver_readiness(tmp_path):
    diagnostic_source = source_snapshot(
        solver_ready=False,
        solver_readiness_reasons=["thermochemistry_incomplete"],
    )
    service = coordinator(tmp_path, SourceAuthority([diagnostic_source]))

    preview, confirmation = preview_and_confirmation(
        service, draft_dict(), "diagnostic-spec"
    )

    assert preview.can_confirm is True
    assert preview.solver_ready is False
    assert preview.solver_readiness_reasons == ("thermochemistry_incomplete",)
    assert preview.authorizes_execution is False
    assert service.confirm(draft_dict(), confirmation).action == "committed"


def test_source_drift_on_model_store_second_live_read_fails_closed(tmp_path):
    base = source_snapshot()
    changed = source_snapshot(domain_generation=9)
    authority = SourceAuthority([base, base, base, base, changed])
    service = coordinator(tmp_path, authority)
    _preview, confirmation = preview_and_confirmation(service, draft_dict())

    result = service.confirm(draft_dict(), confirmation)

    assert result.action == "conflict"
    assert result.conflict.retry_automatically is False
    assert service._model_store.head("project-1", "co-model") is None
    assert service.selector_snapshot().selection is None


def test_stale_store_and_selector_previews_adopt_and_stop(tmp_path):
    service = coordinator(tmp_path)
    _preview, confirmation = preview_and_confirmation(service, draft_dict(), "stale-store")
    other = KineticsModelSpecDraftCompiler().compile(
        draft_dict(spec_id="other-model"), source_snapshot(), None
    )
    snapshot = service._model_store.snapshot()
    service._model_store.put(
        other,
        confirmed=True,
        intent="create",
        expected_domain_authority_id=DOMAIN_AUTHORITY,
        expected_source_projection_sha256=PROJECTION_SHA,
        expected_domain_generation=8,
        expected_store_snapshot_sha256=snapshot.snapshot_sha256,
        source_authority=_ModelSourceAuthority(),
        expected_network_id="network-1",
    )

    stale_store = service.confirm(draft_dict(), confirmation)
    assert stale_store.action == "conflict"
    assert stale_store.conflict.reason == "stale_preview"
    assert stale_store.conflict.retry_automatically is False

    service2 = coordinator(tmp_path)
    _preview2, confirmation2 = preview_and_confirmation(service2, draft_dict(), "stale-selector")
    selector_snapshot = service2.selector_store.snapshot()
    manual = ActiveKineticsModelSpecSelection(
        authority_id="b" * 32,
        revision=1,
        parent_revision=None,
        expected_current_hash=None,
        project_id="project-1",
        spec_id="manual",
        spec_revision="r1",
        spec_sha256="c" * 64,
        intent_id="manual-intent",
        transaction_id="manual-transaction",
        confirmed=True,
    )
    service2.selector_store.compare_and_swap(
        manual, expected_snapshot_sha256=selector_snapshot.snapshot_sha256
    )

    stale_selector = service2.confirm(draft_dict(), confirmation2)
    assert stale_selector.action == "conflict"
    assert stale_selector.conflict.retry_automatically is False


class _ModelSourceAuthority:
    def authoritative_kinetics_source_snapshot(self):
        from vcstudio.project.kinetics_model_spec import AuthoritativeKineticsSourceSnapshot

        return AuthoritativeKineticsSourceSnapshot(
            project_id="project-1",
            domain_authority_id=DOMAIN_AUTHORITY,
            domain_generation=8,
            network_revision="network-r7",
            source_projection_sha256=PROJECTION_SHA,
            network_id="network-1",
        )


def test_success_replays_exact_intent_and_rejects_intent_reuse(tmp_path):
    service = coordinator(tmp_path)
    _preview, confirmation = preview_and_confirmation(service, draft_dict(), "durable-intent")
    first = service.confirm(draft_dict(), confirmation)
    assert first.action == "committed"

    replay = service.confirm(draft_dict(), confirmation)
    assert replay.action == "replayed"
    assert replay.receipt == first.receipt

    changed = draft_dict(mode="advance")
    changed["steps"][0]["uncertainty_eV"] = 0.2
    next_preview = service.preview(changed, intent_id="durable-intent")
    assert next_preview.can_confirm
    reused = service.confirm(changed, service.confirmation_from_preview(next_preview))
    assert reused.action == "conflict"
    assert reused.conflict.reason == "intent_reused"
    assert reused.conflict.retry_automatically is False


@pytest.mark.parametrize(
    "stage, expected_recovery, committed",
    [
        ("after_pending", "discarded_requires_repreview", False),
        ("after_spec", "completed", True),
        ("after_selector_prepare", "completed", True),
        ("after_selector_replace", "completed", True),
        ("after_selector", "completed", True),
        ("after_receipt", "completed", True),
    ],
)
def test_fresh_coordinator_recovers_every_durable_fault_boundary(
    tmp_path, stage, expected_recovery, committed
):
    def fail(selected):
        if selected == stage:
            raise KineticsAuthoringInjectedFailure(selected)

    crashing = coordinator(tmp_path, fault=fail)
    _preview, confirmation = preview_and_confirmation(crashing, draft_dict(), f"fault-{stage}")
    with pytest.raises(KineticsAuthoringInjectedFailure):
        crashing.confirm(draft_dict(), confirmation)

    fresh = KineticsAuthoringCoordinator(
        tmp_path,
        source_authority=SourceAuthority(),
        model_store=KineticsModelSpecStore(tmp_path),
    )
    assert fresh.recover() == expected_recovery
    head = fresh._model_store.head("project-1", "co-model")
    selector = fresh.selector_snapshot().selection
    assert (head is not None) is committed
    assert (selector is not None) is committed
    if committed:
        assert selector.spec_revision == head.revision
        assert selector.spec_sha256 == head.semantic_sha256
        assert (tmp_path / ".vcstudio" / "kinetics" / AUTHORING_RECEIPT_FILENAME).exists()
    assert not (tmp_path / ".vcstudio" / "kinetics" / AUTHORING_PENDING_FILENAME).exists()


def test_selector_reader_recovery_preserves_requires_repreview_tombstone(tmp_path):
    def fail(stage):
        if stage == "after_pending":
            raise KineticsAuthoringInjectedFailure(stage)

    crashing = coordinator(tmp_path, fault=fail)
    _preview, confirmation = preview_and_confirmation(
        crashing, draft_dict(), "reader-recovery-intent"
    )
    with pytest.raises(KineticsAuthoringInjectedFailure):
        crashing.confirm(draft_dict(), confirmation)

    reader = KineticsAuthoringCoordinator(
        tmp_path,
        source_authority=SourceAuthority(),
        model_store=KineticsModelSpecStore(tmp_path),
    )
    assert reader.selector_snapshot().selection is None

    retried_without_repreview = reader.confirm(draft_dict(), confirmation)
    assert retried_without_repreview.action == "needs_repreview"
    assert retried_without_repreview.conflict.retry_automatically is False


def _race_confirm(root: str, confirmation: dict, intent: str, queue) -> None:
    service = KineticsAuthoringCoordinator(
        root,
        source_authority=SourceAuthority(),
        model_store=KineticsModelSpecStore(root),
    )
    result = service.confirm(draft_dict(), {**confirmation, "intent_id": intent})
    queue.put((result.action, None if result.conflict is None else result.conflict.reason))


def test_two_spawned_processes_on_one_base_have_exactly_one_winner(tmp_path):
    service = coordinator(tmp_path)
    preview_a = service.preview(draft_dict(), intent_id="race-a")
    preview_b = service.preview(draft_dict(), intent_id="race-b")
    confirm_a = service.confirmation_from_preview(preview_a).to_dict()
    confirm_b = service.confirmation_from_preview(preview_b).to_dict()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_race_confirm, args=(str(tmp_path), confirm_a, "race-a", queue)),
        context.Process(target=_race_confirm, args=(str(tmp_path), confirm_b, "race-b", queue)),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    results = [queue.get(timeout=5) for _ in processes]

    assert [item[0] for item in results].count("committed") == 1
    assert [item[0] for item in results].count("conflict") == 1
    selector = service.selector_snapshot().selection
    head = service._model_store.head("project-1", "co-model")
    assert selector.spec_revision == head.revision
    assert selector.spec_sha256 == head.semantic_sha256


def _manual_selector(
    *, revision, authority="b" * 32, previous=None, intent=None,
):
    return ActiveKineticsModelSpecSelection(
        authority_id=authority,
        revision=revision,
        parent_revision=None if previous is None else previous.revision,
        expected_current_hash=(
            None if previous is None else previous.selection_sha256),
        project_id="project-1",
        spec_id=f"manual-{revision}",
        spec_revision=f"r{revision}",
        spec_sha256=hashlib.sha256(f"spec-{revision}".encode()).hexdigest(),
        intent_id=intent or f"manual-intent-{revision}",
        transaction_id=f"manual-transaction-{revision}",
        confirmed=True,
    )


def test_selector_independent_anchor_rejects_restored_old_current_bytes(tmp_path):
    store = KineticsModelSpecStore(tmp_path)
    store.snapshot()
    selector = ActiveKineticsModelSpecSelectorStore(tmp_path)
    base = selector.snapshot()
    first = _manual_selector(revision=1)
    selector.compare_and_swap(
        first, expected_snapshot_sha256=base.snapshot_sha256)
    old_current = selector.path.read_bytes()
    second = _manual_selector(revision=2, previous=first)
    selector.compare_and_swap(
        second, expected_snapshot_sha256=selector.snapshot().snapshot_sha256)

    selector.path.write_bytes(old_current)

    with pytest.raises(KineticsAuthoringError, match="independent anchor"):
        selector.snapshot()
    with pytest.raises(KineticsAuthoringError, match="independent anchor"):
        ActiveKineticsModelSpecSelectorStore(tmp_path).snapshot()


def test_selector_v2_without_anchor_is_never_adopted_as_genesis(tmp_path):
    store = KineticsModelSpecStore(tmp_path)
    store.snapshot()
    selector = ActiveKineticsModelSpecSelectorStore(tmp_path)
    first = _manual_selector(revision=1)
    selector.path.write_text(json.dumps(first.to_dict()), encoding="utf-8")

    with pytest.raises(KineticsAuthoringError, match="without its independent anchor"):
        ActiveKineticsModelSpecSelectorStore(tmp_path).snapshot()


def test_selector_anchor_tamper_and_entity_replacement_fail_closed(tmp_path):
    store = KineticsModelSpecStore(tmp_path)
    store.snapshot()
    selector = ActiveKineticsModelSpecSelectorStore(tmp_path)
    first = _manual_selector(revision=1)
    selector.compare_and_swap(
        first, expected_snapshot_sha256=selector.snapshot().snapshot_sha256)
    anchor = tmp_path / ".vcstudio" / "kinetics" / SELECTOR_ANCHOR_FILENAME
    original = anchor.read_bytes()

    anchor.write_bytes(original.replace(b'"kind":"commit"', b'"kind":"tamper"', 1))
    with pytest.raises(KineticsAuthoringError, match="anchor"):
        ActiveKineticsModelSpecSelectorStore(tmp_path).snapshot()

    anchor.write_bytes(original)
    pinned = ActiveKineticsModelSpecSelectorStore(tmp_path)
    assert pinned.snapshot().revision == 1
    replacement = anchor.with_suffix(".replacement")
    replacement.write_bytes(original)
    replacement.replace(anchor)
    with pytest.raises(KineticsAuthoringError, match="entity changed"):
        pinned.snapshot()


def test_legacy_v1_selector_is_readable_but_never_write_authority(tmp_path):
    store = KineticsModelSpecStore(tmp_path)
    store.snapshot()
    directory = tmp_path / ".vcstudio" / "kinetics"
    material = {
        "schema": LEGACY_SELECTOR_SCHEMA,
        "authority_id": "e" * 32,
        "revision": 1,
        "parent_revision": None,
        "expected_current_hash": None,
        "project_id": "project-1",
        "spec_id": "legacy",
        "spec_revision": "r1",
        "spec_sha256": "f" * 64,
        "confirmed": True,
    }
    material["selection_sha256"] = hashlib.sha256(
        json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    (directory / ACTIVE_SELECTOR_FILENAME).write_text(
        json.dumps(material, sort_keys=True), encoding="utf-8"
    )
    selector_store = ActiveKineticsModelSpecSelectorStore(tmp_path)

    snapshot = selector_store.snapshot()

    assert snapshot.migration_state == "legacy_v1_read_only"
    assert snapshot.writable is False
    assert selector_store.read_compatible()["schema"] == LEGACY_SELECTOR_SCHEMA
    target = ActiveKineticsModelSpecSelection(
        authority_id="e" * 32,
        revision=2,
        parent_revision=1,
        expected_current_hash=material["selection_sha256"],
        project_id="project-1",
        spec_id="new",
        spec_revision="r2",
        spec_sha256="a" * 64,
        intent_id="migration-write",
        transaction_id="migration-transaction",
        confirmed=True,
    )
    with pytest.raises(KineticsAuthoringError, match="read-only"):
        selector_store.compare_and_swap(
            target, expected_snapshot_sha256=snapshot.snapshot_sha256
        )


def _assert_public_tree_safe(value):
    if isinstance(value, Mapping):
        forbidden = {"path", "project_root", "raw", "raw_facts", "secret", "token", "actor"}
        assert not (set(value) & forbidden)
        for key, item in value.items():
            assert not isinstance(key, Path)
            _assert_public_tree_safe(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _assert_public_tree_safe(item)
    else:
        assert not isinstance(value, Path)
        if isinstance(value, str):
            assert str(tmp_path_placeholder()) not in value


def tmp_path_placeholder():
    return Path("C:/Users/private/project")


def test_public_dtos_recursively_contain_no_path_secret_actor_or_raw_facts(tmp_path):
    service = coordinator(tmp_path)
    preview, confirmation = preview_and_confirmation(service, draft_dict(), "public-dto")
    result = service.confirm(draft_dict(), confirmation)

    for value in (
        KineticsModelSpecDraft.from_dict(draft_dict()).to_dict(),
        source_snapshot().to_dict(),
        preview.to_dict(),
        confirmation.to_dict(),
        result.to_dict(),
        service.selector_snapshot().to_dict(),
    ):
        _assert_public_tree_safe(value)
