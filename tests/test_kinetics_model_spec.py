"""Strict KineticsModelSpec DTO and project-local revision CAS tests."""
from __future__ import annotations

import hashlib
import multiprocessing
import os
from pathlib import Path

import pytest

from vcstudio.project import kinetics_model_spec as model_spec_module
from vcstudio.project.kinetics_model_spec import (
    AuthoritativeKineticsSourceSnapshot,
    KineticsModelSpec,
    KineticsModelSpecConflict,
    KineticsModelSpecError,
    KineticsModelSpecStore,
    SCHEMA,
    STORE_DIRECTORY,
)


_ARTIFACTS = {
    "model:assumptions": b"explicit microkinetic assumptions",
    "condition:feed": b"frozen feed activity",
    "model:step": b"explicit prefactor and uncertainty policy",
    "model:sites": b"site population normalization",
}
_DOMAIN_AUTHORITY = "d" * 32


class SourceAuthority:
    def __init__(self, snapshots=None):
        self.snapshots = list(snapshots or [AuthoritativeKineticsSourceSnapshot(
            project_id="project-1", domain_authority_id=_DOMAIN_AUTHORITY,
            domain_generation=8, network_revision="network-r7",
            source_projection_sha256="a" * 64, network_id="network-1",
        )])
        self.calls = 0

    def authoritative_kinetics_source_snapshot(self):
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[index]


def _digest(reference: str) -> str:
    return hashlib.sha256(_ARTIFACTS[reference]).hexdigest()


def _evidence(reference: str, kind: str = "model") -> dict:
    return {
        "kind": kind,
        "reference": reference,
        "evidence_sha256": _digest(reference),
    }


def _put(store, value, *, intent, store_snapshot=None, source_authority=None,
         expected_domain_authority_id=_DOMAIN_AUTHORITY,
         expected_source_projection_sha256="a" * 64,
         expected_domain_generation=8):
    snapshot = store_snapshot or store.snapshot()
    return store.put(
        value, confirmed=True, intent=intent,
        expected_domain_authority_id=expected_domain_authority_id,
        expected_source_projection_sha256=expected_source_projection_sha256,
        expected_domain_generation=expected_domain_generation,
        expected_store_snapshot_sha256=snapshot.snapshot_sha256,
        source_authority=source_authority or SourceAuthority(),
        expected_network_id="network-1",
    )


def spec_dict(*, revision="r1", parent_revision=None,
              expected_current_hash=None, source_hash="a" * 64) -> dict:
    return {
        "schema": SCHEMA,
        "project_id": "project-1",
        "spec_id": "co-oxidation-model",
        "revision": revision,
        "parent_revision": parent_revision,
        "expected_current_hash": expected_current_hash,
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
            "mean_field": True,
            "steady_state": True,
            "site_uniformity": "uniform",
            "lateral_interactions": "neglected",
            "mechanism_completeness": "claimed_complete",
            "evidence": [_evidence("model:assumptions")],
        },
        "feed_reservoirs": [
            {
                "species_id": "CO_g",
                "activity": 1.0,
                "unit": "bar",
                "source": "condition:feed",
                "evidence_sha256": _digest("condition:feed"),
            },
        ],
        "target_products": ["CO_s"],
        "steps": [
            {
                "step_id": "co_adsorption",
                "prefactors": {
                    direction: {
                        "value": 1.0e13,
                        "unit": "s^-1",
                        "source": _evidence("model:step", "tst"),
                    }
                    for direction in ("forward", "reverse")
                },
                "bep": {"used": False, "source": None, "parameters_sha256": None},
                "scaling": {
                    "used": False, "source": None, "parameters_sha256": None,
                },
                "uncertainty_eV": 0.1,
                "evidence": [_evidence("model:step")],
            },
        ],
        "site_population_totals": [
            {
                "site_type": "top",
                "value": 2.0,
                "unit": "sites",
                "basis": "surface_unit_cell",
                "evidence": [_evidence("model:sites")],
            },
        ],
        "saddle_selector": None,
    }


def _put_racer(root: str, payload: dict, expected_store_snapshot: str, queue) -> None:
    try:
        result = KineticsModelSpecStore(root).put(
            payload, confirmed=True, intent="advance",
            expected_domain_authority_id=_DOMAIN_AUTHORITY,
            expected_source_projection_sha256="a" * 64,
            expected_domain_generation=8,
            expected_store_snapshot_sha256=expected_store_snapshot,
            source_authority=SourceAuthority(), expected_network_id="network-1",
        )
        queue.put(("ok", result.action, result.spec.revision))
    except KineticsModelSpecConflict as exc:
        queue.put(("conflict", exc.reason, payload["revision"]))


def test_strict_dto_round_trip_and_evidence_index():
    spec = KineticsModelSpec.from_dict(spec_dict())

    assert spec.to_dict() == spec_dict()
    assert len(spec.semantic_sha256) == 64
    assert spec.evidence_bindings() == [
        {"reference": reference, "artifact_sha256": _digest(reference)}
        for reference in sorted(_ARTIFACTS)
    ]


def test_source_binding_optionally_seals_network_identity():
    payload = spec_dict()
    payload["source_binding"]["network_id"] = "network-1"

    spec = KineticsModelSpec.from_dict(payload)

    assert spec.source_binding["network_id"] == "network-1"
    assert spec.to_dict() == payload


def test_dto_is_deeply_immutable_and_detached_from_caller_values():
    payload = spec_dict()
    spec = KineticsModelSpec.from_dict(payload)
    original_hash = spec.semantic_sha256

    payload["assumptions"]["mean_field"] = False
    payload["steps"][0]["prefactors"]["forward"]["value"] = 9.0
    with pytest.raises(TypeError, match="immutable"):
        spec.assumptions["mean_field"] = False
    with pytest.raises(TypeError, match="immutable"):
        spec.steps[0]["prefactors"]["forward"]["value"] = 9.0
    with pytest.raises(TypeError):
        dict.__setitem__(spec.assumptions, "mean_field", False)
    with pytest.raises(TypeError):
        spec.assumptions._values["mean_field"] = False

    assert spec.assumptions["mean_field"] is True
    assert spec.steps[0]["prefactors"]["forward"]["value"] == 1.0e13
    assert spec.semantic_sha256 == original_hash


@pytest.mark.parametrize("mutate", [
    lambda value: value.update({"unknown": True}),
    lambda value: value.update({"project_id": "C:\\Users\\person\\project"}),
    lambda value: value["assumptions"]["evidence"][0].update({
        "reference": "token=super-secret-value",
    }),
    lambda value: value["steps"][0].update({"uncertainty_eV": float("nan")}),
    lambda value: value["site_population_totals"][0].update({"value": float("inf")}),
    lambda value: value["assumptions"]["evidence"][0].update({
        "reference": "evidence=C:\\Users\\person\\artifact.json",
    }),
    lambda value: value["feed_reservoirs"][0].update({
        "source": "key=/scratch/private/artifact",
    }),
    lambda value: value["assumptions"]["evidence"][0].update({
        "reference": "https://user:password@example.invalid/evidence",
    }),
    lambda value: value["assumptions"]["evidence"][0].update({
        "reference": "https://user@example.invalid/evidence",
    }),
    lambda value: value["assumptions"]["evidence"][0].update({
        "reference": "artifact;/home/private/artifact.json",
    }),
    lambda value: value["assumptions"]["evidence"][0].update({
        "reference": "//user:password@example.invalid",
    }),
])
def test_dto_rejects_unknown_path_secret_and_nonfinite(mutate):
    value = spec_dict()
    mutate(value)

    with pytest.raises(KineticsModelSpecError):
        KineticsModelSpec.from_dict(value)


@pytest.mark.parametrize("leaked", [
    "artifact;/home/private-person/artifact.json",
    "//user:password@example.invalid",
])
def test_sensitive_text_is_rejected_without_echoing_it(leaked):
    value = spec_dict()
    value["assumptions"]["evidence"][0].update({"reference": leaked})

    with pytest.raises(KineticsModelSpecError) as captured:
        KineticsModelSpec.from_dict(value)
    assert leaked not in str(captured.value)


@pytest.mark.parametrize("reference", [
    "artifact:opaque/relative", "doi:10.1000/opaque", "urn:sha256:opaque",
    "https://example.invalid/evidence",
])
def test_legal_opaque_evidence_references_remain_available(reference):
    value = spec_dict()
    value["assumptions"]["evidence"][0].update({
        "reference": reference,
        "evidence_sha256": hashlib.sha256(reference.encode()).hexdigest(),
    })

    spec = KineticsModelSpec.from_dict(value)
    assert reference in {
        item["reference"] for item in spec.evidence_bindings()
    }


def test_evidence_reference_has_one_global_hash_and_global_count_limit():
    conflict = spec_dict()
    conflict["site_population_totals"][0]["evidence"] = [{
        "kind": "model",
        "reference": "model:assumptions",
        "evidence_sha256": _digest("model:sites"),
    }]
    with pytest.raises(KineticsModelSpecError, match="multiple artifact hashes"):
        KineticsModelSpec.from_dict(conflict)

    excessive = spec_dict()
    excessive["assumptions"]["evidence"] = [
        {
            "kind": "model", "reference": f"artifact:{index}",
            "evidence_sha256": hashlib.sha256(str(index).encode()).hexdigest(),
        }
        for index in range(300)
    ]
    with pytest.raises(KineticsModelSpecError, match="bounded evidence array"):
        KineticsModelSpec.from_dict(excessive)


def test_project_local_create_advance_replay_and_stale_conflict(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    first = KineticsModelSpec.from_dict(spec_dict())

    empty = store.snapshot()
    created = _put(store, first, intent="create", store_snapshot=empty)
    current = store.snapshot()
    replayed = _put(store, first, intent="create", store_snapshot=current)
    second = KineticsModelSpec.from_dict(spec_dict(
        revision="r2", parent_revision="r1",
        expected_current_hash=first.semantic_sha256))
    advanced = _put(
        store, second, intent="advance", store_snapshot=store.snapshot())

    assert tuple(store.directory.parts[-3:]) == STORE_DIRECTORY
    assert store.path == project / ".vcstudio" / "kinetics" / "model-spec" / "store.json"
    assert (created.action, replayed.action, advanced.action) == (
        "created", "replayed", "advanced")
    detached_receipt = created.spec.to_dict()
    detached_receipt["assumptions"]["mean_field"] = False
    with pytest.raises(TypeError):
        dict.__setitem__(created.spec.assumptions, "mean_field", False)
    assert created.current_hash == first.semantic_sha256
    assert created.spec.to_dict()["assumptions"]["mean_field"] is True
    assert store.get(
        "project-1", "co-oxidation-model", "r1").assumptions["mean_field"] is True
    assert store.head("project-1", "co-oxidation-model").revision == "r2"
    assert store.get("project-1", "co-oxidation-model", "r1") == first

    stale = KineticsModelSpec.from_dict(spec_dict(
        revision="r3", parent_revision="r1",
        expected_current_hash=first.semantic_sha256))
    with pytest.raises(KineticsModelSpecConflict, match="stale_parent_revision"):
        _put(store, stale, intent="advance", store_snapshot=store.snapshot())


def test_write_requires_confirmation_intent_and_current_source_binding(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    value = spec_dict()
    empty = store.snapshot()

    with pytest.raises(KineticsModelSpecError, match="confirmation"):
        store.put(
            value, confirmed=False, intent="create",
            expected_domain_authority_id=_DOMAIN_AUTHORITY,
            expected_source_projection_sha256="a" * 64,
            expected_domain_generation=8,
            expected_store_snapshot_sha256=empty.snapshot_sha256,
            source_authority=SourceAuthority())
    with pytest.raises(KineticsModelSpecError, match="intent"):
        store.put(
            value, confirmed=True, intent="advance",
            expected_domain_authority_id=_DOMAIN_AUTHORITY,
            expected_source_projection_sha256="a" * 64,
            expected_domain_generation=8,
            expected_store_snapshot_sha256=empty.snapshot_sha256,
            source_authority=SourceAuthority())
    with pytest.raises(
            KineticsModelSpecConflict, match="stale_expected_domain_authority"):
        _put(
            store, value, intent="create", store_snapshot=empty,
            expected_domain_authority_id="e" * 32)
    with pytest.raises(
            KineticsModelSpecConflict, match="stale_expected_source_projection"):
        _put(
            store, value, intent="create", store_snapshot=empty,
            expected_source_projection_sha256="b" * 64)
    with pytest.raises(KineticsModelSpecError, match="validator"):
        store.put(
            value, confirmed=True, intent="create",
            expected_domain_authority_id=_DOMAIN_AUTHORITY,
            expected_source_projection_sha256="a" * 64,
            expected_domain_generation=8,
            expected_store_snapshot_sha256=empty.snapshot_sha256)
    assert store.snapshot().generation == 0


def test_empty_authority_is_persisted_and_snapshot_seals_full_chain(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)

    first = store.snapshot()
    raw_bytes = store.path.read_bytes()
    second = store.snapshot()

    assert first.to_dict() == second.to_dict()
    assert first.generation == 0 and first.heads == ()
    assert first.authority_id == second.authority_id
    assert store.path.read_bytes() == raw_bytes
    assert len(first.chain_sha256) == len(first.store_sha256) == 64
    assert len(first.anchor_chain_sha256) == 64
    assert store.anchor_path.is_file()


def test_real_source_is_revalidated_twice_inside_store_lock(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    empty = store.snapshot()
    valid = SourceAuthority().snapshots[0]
    advanced_source = AuthoritativeKineticsSourceSnapshot(
        project_id="project-1", domain_authority_id=_DOMAIN_AUTHORITY,
        domain_generation=9, network_revision="network-r8",
        source_projection_sha256="b" * 64, network_id="network-1",
    )
    changing = SourceAuthority([valid, advanced_source])

    with pytest.raises(
            KineticsModelSpecConflict, match="authoritative_source_changed"):
        _put(
            store, spec_dict(), intent="create", store_snapshot=empty,
            source_authority=changing)
    assert changing.calls == 2
    assert store.snapshot().generation == 0

    with pytest.raises(KineticsModelSpecConflict, match="stale_domain_generation"):
        _put(
            store, spec_dict(), intent="create", store_snapshot=store.snapshot(),
            source_authority=SourceAuthority([advanced_source]))
    assert store.snapshot().generation == 0


def test_independent_anchor_rejects_old_store_bytes_with_old_snapshot(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    empty = store.snapshot()
    empty_bytes = store.path.read_bytes()
    first = KineticsModelSpec.from_dict(spec_dict())
    _put(store, first, intent="create", store_snapshot=empty)
    committed_anchor = store.anchor_path.read_bytes()
    replacement_root = spec_dict(revision="replacement-root")

    store.path.write_bytes(empty_bytes)
    with pytest.raises(KineticsModelSpecError, match="independent anchor"):
        _put(
            store, replacement_root, intent="create", store_snapshot=empty)
    with pytest.raises(KineticsModelSpecError, match="independent anchor"):
        store.snapshot()
    assert store.anchor_path.read_bytes() == committed_anchor
    assert store.path.read_bytes() == empty_bytes


def test_first_snapshot_crash_forward_reconciles_from_anchor_prepare(
        tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    original_write_store = model_spec_module._write_store

    def crash_before_store(*_args, **_kwargs):
        raise RuntimeError("simulated initialization crash")

    monkeypatch.setattr(model_spec_module, "_write_store", crash_before_store)
    with pytest.raises(RuntimeError, match="initialization crash"):
        store.snapshot()
    assert store.anchor_path.is_file()
    assert store.pending_path.is_file()
    assert not store.path.exists()

    monkeypatch.setattr(model_spec_module, "_write_store", original_write_store)
    recovered = store.snapshot()
    assert recovered.generation == 0
    assert store.path.is_file()
    assert not store.pending_path.exists()
    assert store.snapshot().to_dict() == recovered.to_dict()


def test_committed_store_crash_forward_reconciles_pending_anchor(
        tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    empty = store.snapshot()
    original_append = model_spec_module._append_anchor_record

    def crash_before_commit(path, record, boundary):
        if record.get("kind") == "commit" and record.get("sequence") > 2:
            raise RuntimeError("simulated commit-marker crash")
        return original_append(path, record, boundary)

    monkeypatch.setattr(
        model_spec_module, "_append_anchor_record", crash_before_commit)
    with pytest.raises(RuntimeError, match="commit-marker crash"):
        _put(store, spec_dict(), intent="create", store_snapshot=empty)
    assert store.path.is_file() and store.pending_path.is_file()

    monkeypatch.setattr(model_spec_module, "_append_anchor_record", original_append)
    recovered = store.snapshot()
    assert recovered.generation == 1
    assert store.head("project-1", "co-oxidation-model").revision == "r1"
    assert not store.pending_path.exists()


def test_project_root_entity_replacement_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    store.snapshot()
    moved = tmp_path / "moved-project"

    os.replace(project, moved)
    project.mkdir()

    with pytest.raises(KineticsModelSpecError, match="entity changed"):
        store.snapshot()


def test_fixed_anchor_wal_entity_replacement_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    store.snapshot()
    anchor_bytes = store.anchor_path.read_bytes()
    moved_anchor = store.directory / ".moved-anchor.wal"

    os.replace(store.anchor_path, moved_anchor)
    store.anchor_path.write_bytes(anchor_bytes)

    with pytest.raises(KineticsModelSpecError, match="anchor WAL entity changed"):
        store.snapshot()


def test_cross_process_competing_advances_have_exactly_one_winner(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    first = KineticsModelSpec.from_dict(spec_dict())
    _put(store, first, intent="create", store_snapshot=store.snapshot())
    expected_store_snapshot = store.snapshot().snapshot_sha256
    candidates = [
        spec_dict(
            revision=revision, parent_revision="r1",
            expected_current_hash=first.semantic_sha256)
        for revision in ("r2-a", "r2-b")
    ]
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_put_racer,
            args=(str(project), value, expected_store_snapshot, queue))
        for value in candidates
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=5) for _ in processes]

    assert [item[0] for item in outcomes].count("ok") == 1
    assert [item[0] for item in outcomes].count("conflict") == 1
    assert store.head("project-1", "co-oxidation-model").revision in {"r2-a", "r2-b"}


def test_store_does_not_accept_a_caller_selected_storage_path(tmp_path):
    missing_project = tmp_path / "missing"
    with pytest.raises(KineticsModelSpecError, match="project_root"):
        KineticsModelSpecStore(missing_project)

    project = tmp_path / "project"
    project.mkdir()
    store = KineticsModelSpecStore(project)
    assert Path(store.path).is_relative_to(project)
