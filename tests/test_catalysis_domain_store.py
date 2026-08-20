from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from vcstudio.project import catalysis_domain_store as store_module
from vcstudio.project.catalysis_contracts import (
    CatalystSurface,
    CatalysisContractError,
    DomainEnvelope,
    EvidenceRef,
    MethodFingerprint,
)
from vcstudio.project.catalysis_domain_store import (
    DomainEnvelopeStore,
    DomainRevisionConflict,
)


def _surface(revision="revision-1", *, parent=None, expected=None, composition="Pt"):
    evidence = (EvidenceRef(
        "calculation_result", "energy-observed", "observed", "evidence-revision-1",
    ),)
    method = MethodFingerprint(
        method_id="vasp-method-001", scope="electronic_structure", sha256="a" * 64,
        evidence_refs=(EvidenceRef(
            "method_record", "method-observed", "observed", "method-revision-1",
        ),),
    )
    return CatalystSurface(
        surface_id="surface-001", composition=composition, miller_indices=(1, 1, 1),
        termination_id="termination-a", geometric_site_ids=("site-top",),
        provenance="observed", evidence_refs=evidence, method_fingerprint=method,
        object_revision_id=revision, parent_revision=parent,
        expected_current_hash=expected,
    )


def test_store_creates_replays_and_advances_with_immutable_revision_cas(tmp_path):
    store = DomainEnvelopeStore(tmp_path / "domain-store.json")
    first = DomainEnvelope.wrap(_surface())
    created = store.put(first)
    assert created.action == "created"
    assert created.generation == 1
    assert created.authorizes_execution is False
    assert created.job_source_of_truth == "job.yaml"

    replayed = store.put(first)
    assert replayed.action == "replayed"
    assert replayed.generation == 1

    second_object = _surface(
        "revision-2", parent="revision-1", expected=first.semantic_sha256,
        composition="Au",
    )
    second = DomainEnvelope.wrap(second_object)
    advanced = store.put(second)
    assert advanced.action == "advanced"
    assert advanced.generation == 2
    assert store.head("CatalystSurface", "surface-001") == second
    assert store.get("CatalystSurface", "surface-001", "revision-1") == first
    assert not list(tmp_path.rglob("job.yaml"))


def test_same_type_id_revision_with_different_hash_is_an_immutable_conflict(tmp_path):
    store = DomainEnvelopeStore(tmp_path / "domain-store.json")
    original = DomainEnvelope.wrap(_surface())
    store.put(original)
    conflicting = DomainEnvelope.wrap(replace(_surface(), composition="Au"))
    with pytest.raises(DomainRevisionConflict, match="immutable_revision_reused"):
        store.put(conflicting)
    assert store.head("CatalystSurface", "surface-001") == original


@pytest.mark.parametrize(("parent", "expected", "reason"), [
    ("revision-0", "b" * 64, "unexpected_parent_for_create"),
])
def test_initial_create_rejects_parent_or_expected_current_hash(
        tmp_path, parent, expected, reason):
    store = DomainEnvelopeStore(tmp_path / "domain-store.json")
    with pytest.raises(DomainRevisionConflict, match=reason):
        store.put(DomainEnvelope.wrap(_surface(
            "revision-1", parent=parent, expected=expected,
        )))


def test_stale_parent_and_hash_writers_never_replace_current_head(tmp_path):
    store = DomainEnvelopeStore(tmp_path / "domain-store.json")
    first = DomainEnvelope.wrap(_surface())
    store.put(first)
    second = DomainEnvelope.wrap(_surface(
        "revision-2", parent="revision-1", expected=first.semantic_sha256,
        composition="Au",
    ))
    store.put(second)

    stale_parent = DomainEnvelope.wrap(_surface(
        "revision-3", parent="revision-1", expected=first.semantic_sha256,
        composition="Ag",
    ))
    with pytest.raises(DomainRevisionConflict, match="stale_parent_revision"):
        store.put(stale_parent)
    wrong_hash = DomainEnvelope.wrap(_surface(
        "revision-3", parent="revision-2", expected="c" * 64,
        composition="Ag",
    ))
    with pytest.raises(DomainRevisionConflict, match="stale_current_hash"):
        store.put(wrong_hash)
    assert store.head("CatalystSurface", "surface-001") == second


def test_two_writers_from_one_head_have_exactly_one_cas_winner(tmp_path):
    store = DomainEnvelopeStore(tmp_path / "domain-store.json")
    first = DomainEnvelope.wrap(_surface())
    store.put(first)
    candidates = [DomainEnvelope.wrap(_surface(
        f"revision-{index}", parent="revision-1", expected=first.semantic_sha256,
        composition=composition,
    )) for index, composition in ((2, "Au"), (3, "Ag"))]

    def attempt(candidate):
        try:
            return store.put(candidate).action
        except DomainRevisionConflict as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, candidates))
    assert results.count("advanced") == 1
    assert results.count("stale_parent_revision") == 1


def test_corrupt_authority_and_failed_replace_never_overwrite_existing_bytes(
        tmp_path, monkeypatch):
    path = tmp_path / "domain-store.json"
    path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(CatalysisContractError, match="refusing overwrite"):
        DomainEnvelopeStore(path).put(DomainEnvelope.wrap(_surface()))
    assert path.read_text(encoding="utf-8") == "{corrupt"

    path.unlink()
    store = DomainEnvelopeStore(path)
    first = DomainEnvelope.wrap(_surface())
    store.put(first)
    before = path.read_bytes()
    second = DomainEnvelope.wrap(_surface(
        "revision-2", parent="revision-1", expected=first.semantic_sha256,
        composition="Au",
    ))

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.put(second)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".domain-store.json.*.tmp"))


def test_structurally_valid_head_rewind_and_fork_are_rejected(tmp_path):
    path = tmp_path / "domain-store.json"
    store = DomainEnvelopeStore(path)
    first = DomainEnvelope.wrap(_surface())
    store.put(first)
    second = DomainEnvelope.wrap(_surface(
        "revision-2", parent="revision-1", expected=first.semantic_sha256,
        composition="Au",
    ))
    store.put(second)

    value = json.loads(path.read_text(encoding="utf-8"))
    identity_key = store_module._identity_key("CatalystSurface", "surface-001")
    first_key = store_module._revision_key(first)
    value["heads"][identity_key] = first_key
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CatalysisContractError, match="chain leaf"):
        store.head("CatalystSurface", "surface-001")

    value["heads"][identity_key] = store_module._revision_key(second)
    fork = DomainEnvelope.wrap(_surface(
        "revision-3", parent="revision-1", expected=first.semantic_sha256,
        composition="Ag",
    ))
    value["revisions"][store_module._revision_key(fork)] = fork.to_dict()
    value["generation"] += 1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CatalysisContractError, match="fork"):
        store.head("CatalystSurface", "surface-001")
