from __future__ import annotations

import copy
import hashlib
import json
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_report_service import _Host, _request  # noqa: E402

from vcstudio.project.report_insights import (  # noqa: E402
    CapsuleDestinations,
    CapsuleExportError,
    CapsuleExportReceipts,
    CapsuleLimits,
    CapsuleQuotaError,
    FrozenRevision,
    build_capsule_bytes,
    capsule_members,
    evidence_graph,
    export_capsule,
    load_frozen_revision,
    redact,
    scientific_diff,
)
from vcstudio.project.report_service import ReportService  # noqa: E402
from vcstudio.gui_web.api import Api  # noqa: E402


def _two_revisions(tmp_path: Path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    first_preview = service.preview(
        "project.yaml", _request(host.project_id, operation_id="insight-1")
    )
    first = service.publish(
        "project.yaml", str(tmp_path / "out"), first_preview["preview_id"],
        first_preview["preview_token"], public=False,
    )
    second_preview = service.preview(
        "project.yaml", _request(host.project_id, operation_id="insight-2")
    )
    second = service.publish(
        "project.yaml", str(tmp_path / "out"), second_preview["preview_id"],
        second_preview["preview_token"], public=False,
    )
    assert first["ok"] is second["ok"] is True
    return host, service, first, second


def _notebook_payload(project_id: str, revision_id: str, *,
                      ledger_revision: int = 0,
                      head_digest: str | None = None):
    records = [
        {
            "record_id": f"rn-{index:032x}",
            "revision": index + 1,
            "record_type": "note",
            "body": f"frozen note {index + 1}",
            "active": True,
        }
        for index in range(ledger_revision)
    ]
    return {
        "schema": "vcstudio.research-notebook-archive/v1",
        "project_id": project_id,
        "bound_report_revision_id": revision_id,
        "ledger_revision": ledger_revision,
        "ledger_head_digest": head_digest,
        "integrity_status": "current",
        "records": records,
        "denominator": {
            "records": len(records), "active": len(records), "review_todo": 0,
        },
        "limitations": [{"code": "report_gate_unchanged"}],
    }


def test_scientific_diff_revalidates_bundles_and_compares_file_hashes(tmp_path):
    host, service, first, second = _two_revisions(tmp_path)

    result = scientific_diff(
        service, "project.yaml", first["revision"]["revision_id"],
        second["revision"]["revision_id"],
    )

    assert result["ok"] is True
    assert result["project_id"] == host.project_id
    assert result["left"]["revision_id"] != result["right"]["revision_id"]
    assert result["files"]
    assert result["changed"] is True
    assert "created_at_utc" not in json.dumps(result)


def test_scientific_diff_reports_structured_method_matrix_changes(
    tmp_path, monkeypatch,
):
    _host, service, first, second = _two_revisions(tmp_path)
    left = load_frozen_revision(service, "project.yaml", first["revision"]["revision_id"])
    right = load_frozen_revision(service, "project.yaml", second["revision"]["revision_id"])
    left_model = copy.deepcopy(left.model)
    right_model = copy.deepcopy(right.model)
    left_model["method_consistency"] = {"functional": "PBE", "encut_ev": 450}
    right_model["method_consistency"] = {"functional": "PBE", "encut_ev": 520}
    bundles = iter((
        FrozenRevision(**{**left.__dict__, "model": left_model}),
        FrozenRevision(**{**right.__dict__, "model": right_model}),
    ))
    monkeypatch.setattr(
        "vcstudio.project.report_insights.load_frozen_revision",
        lambda *_args, **_kwargs: next(bundles),
    )

    result = scientific_diff(service, "project.yaml", left.revision_id, right.revision_id)

    assert result["method_matrix"] == [{
        "key": "model.method_consistency.encut_ev", "left": 450, "right": 520,
    }]
    assert result["changed"] is True


def test_tampered_revision_fails_closed_before_diff(tmp_path):
    _host, service, first, second = _two_revisions(tmp_path)
    Path(first["files"]["html"]).write_text("tampered", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed|hash|audit"):
        scientific_diff(
            service, "project.yaml", first["revision"]["revision_id"],
            second["revision"]["revision_id"],
        )


def test_revision_selection_rejects_missing_and_wrong_project_path(tmp_path):
    _host, service, first, _second = _two_revisions(tmp_path)

    with pytest.raises(LookupError, match="authoritative"):
        load_frozen_revision(service, "project.yaml", "missing-r0001")
    with pytest.raises(FileNotFoundError):
        load_frozen_revision(
            service, "another-project.yaml", first["revision"]["revision_id"])


def test_evidence_graph_is_path_free_and_marks_unresolved_claim_links(
    tmp_path, monkeypatch,
):
    host, service, first, _second = _two_revisions(tmp_path)
    frozen = load_frozen_revision(
        service, "project.yaml", first["revision"]["revision_id"])
    model = copy.deepcopy(frozen.model)
    model["claims"] = [{
        "id": "conclusion-1", "text": "Frozen conclusion",
        "source_refs": ["source:missing"],
    }]
    augmented = FrozenRevision(
        **{**frozen.__dict__, "model": model}
    )
    monkeypatch.setattr(
        "vcstudio.project.report_insights.load_frozen_revision",
        lambda *_args, **_kwargs: augmented,
    )

    graph = evidence_graph(service, "project.yaml", frozen.revision_id)
    encoded = json.dumps(graph, ensure_ascii=False)

    assert graph["project_id"] == host.project_id
    assert graph["status"] == "blocked"
    assert graph["missing_links"][0]["expected"] == "source:missing"
    assert any(node["type"] == "conclusion" for node in graph["nodes"])
    assert all(node.get("route") for node in graph["nodes"])
    assert str(tmp_path) not in encoded
    assert r"C:\private\project.yaml" not in encoded


def test_evidence_graph_redacts_claim_source_figure_labels_and_missing_refs(
    tmp_path, monkeypatch,
):
    _host, service, first, _second = _two_revisions(tmp_path)
    frozen = load_frozen_revision(
        service, "project.yaml", first["revision"]["revision_id"])
    snapshot = copy.deepcopy(frozen.snapshot)
    snapshot["sources"] = [{
        "source_id": r"C:\private\source.dat token=source-secret",
        "sha256": "a" * 64,
    }]
    model = copy.deepcopy(frozen.model)
    model["sections"] = [{
        "blocks": [{
            "type": "figure",
            "figure": {
                "id": "figure-sensitive",
                "caption": "Bearer figure-secret-token",
            },
        }],
    }]
    model["claims"] = [{
        "text": r"C:\private\claim.txt token=claim-secret",
        "source_refs": [r"C:\private\missing.dat token=missing-secret"],
    }]
    augmented = FrozenRevision(
        **{**frozen.__dict__, "snapshot": snapshot, "model": model}
    )
    monkeypatch.setattr(
        "vcstudio.project.report_insights.load_frozen_revision",
        lambda *_args, **_kwargs: augmented,
    )

    graph = evidence_graph(service, "project.yaml", frozen.revision_id)
    encoded = json.dumps(graph, ensure_ascii=False)

    for sensitive in (
        r"C:\private\source.dat",
        "source-secret",
        "figure-secret-token",
        r"C:\private\claim.txt",
        "claim-secret",
        r"C:\private\missing.dat",
        "missing-secret",
    ):
        assert sensitive not in encoded
    sensitive_types = {"source", "figure", "conclusion"}
    labels = [
        node["label"] for node in graph["nodes"]
        if node["type"] in sensitive_types
    ]
    assert labels
    assert all(label == "[redacted-sensitive-value]" for label in labels)
    assert graph["missing_links"][0]["expected"] == "[redacted-sensitive-value]"


@pytest.mark.parametrize("private_value", [
    "path=/home/alice/private/POSCAR",
    r"path=C:\Users\alice\private\POSCAR",
    r"path=\\server\private-share\POSCAR",
    "path=file:///home/alice/private/POSCAR",
    "path=file://server/private-share/POSCAR",
])
def test_report_public_redactor_covers_assignment_local_paths(private_value):
    assert redact(private_value) == "[redacted-sensitive-value]"
    assert redact({"message": private_value}) == {
        "message": "[redacted-sensitive-value]",
    }


@pytest.mark.parametrize("private_value", [
    "access_token=central-classifier-secret",
    "refresh-token:central-classifier-secret",
    "authorization=Bearer central-classifier-secret",
    "https://alice:central-classifier-secret@example.invalid/repository",
])
def test_report_public_redactor_uses_central_credential_classifier(private_value):
    assert redact(private_value) == "[redacted-sensitive-value]"


def test_capsule_is_deterministic_redacted_and_self_checksummed(tmp_path):
    _host, service, first, _second = _two_revisions(tmp_path)
    bundle = load_frozen_revision(
        service, "project.yaml", first["revision"]["revision_id"])

    left = build_capsule_bytes(bundle)
    right = build_capsule_bytes(bundle)

    assert left == right
    assert str(tmp_path).encode() not in left
    assert rb"C:\private\project.yaml" not in left
    with zipfile.ZipFile(BytesIO(left)) as archive:
        names = set(archive.namelist())
        assert "capsule-manifest.json" in names
        assert "SHA256SUMS" in names
        assert "contracts/report-spec.json" in names
        assert "contracts/report-snapshot.json" in names
        assert "contracts/validation-result.json" in names
        assert "inputs/input-manifest.json" in names
        assert "model/report-model.json" in names
        assert "metadata/figures.json" in names
        assert "methods/methods.json" in names
        assert "references/references.bib" in names
        assert "environment/environment.json" in names
        assert b"[redacted-local-path]" in archive.read(
            "contracts/report-snapshot.json")
        sums = archive.read("SHA256SUMS").decode("ascii").splitlines()
        for line in sums:
            digest, name = line.split("  ", 1)
            assert digest == hashlib.sha256(archive.read(name)).hexdigest()


def test_capsule_can_bind_research_notebook_ledger_and_explicit_limitations(tmp_path):
    _host, service, first, _second = _two_revisions(tmp_path)
    bundle = load_frozen_revision(
        service, "project.yaml", first["revision"]["revision_id"])
    notebook_payload = {
        "schema": "vcstudio.research-notebook-archive/v1",
        "project_id": bundle.project_id,
        "bound_report_revision_id": bundle.revision_id,
        "ledger_revision": 2,
        "ledger_head_digest": "a" * 64,
        "integrity_status": "current",
        "records": [{
            "record_id": "rn-review", "record_type": "review",
            "body": r"Reviewed under C:\private\project",
            "review": {"decision": "approved", "cryptographic_signature": False},
        }],
        "denominator": {"records": 1, "active": 1, "review_todo": 0},
        "limitations": [{
            "code": "report_gate_unchanged",
            "en": "Notebook review does not elevate ValidationResult.",
        }],
    }

    data = build_capsule_bytes(bundle, notebook_payload=notebook_payload)

    with zipfile.ZipFile(BytesIO(data)) as archive:
        ledger = json.loads(archive.read("research-notebook/ledger.json"))
        limitations = json.loads(
            archive.read("research-notebook/limitations.json"))
        assert ledger["bound_report_revision_id"] == bundle.revision_id
        assert ledger["ledger_head_digest"] == "a" * 64
        assert ledger["records"][0]["body"] == "[redacted-sensitive-value]"
        assert limitations["limitations"][0]["code"] == "report_gate_unchanged"


def test_capsule_export_requires_preview_and_confirmation_receipt(tmp_path):
    _host, service, first, _second = _two_revisions(tmp_path)

    with pytest.raises(CapsuleExportError, match="preview.*confirmation"):
        export_capsule(
            service, "project.yaml", first["revision"]["revision_id"],
            str(tmp_path),
        )


def test_capsule_preview_freezes_provider_then_confirmation_publishes(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    calls = []

    def notebook_payload(path, project_id, bound_revision_id):
        calls.append((path, project_id, bound_revision_id))
        return _notebook_payload(project_id, bound_revision_id)

    host._research_notebook_archive_payload = notebook_payload
    destination = tmp_path / "with-notebook"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)

    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-operation-1",
        project_id=host.project_id,
    )

    assert calls == [("project.yaml", host.project_id, revision_id)]
    assert preview["status"] == "awaiting_confirmation"
    assert preview["archive"]["sha256"]
    assert not (destination / preview["archive"]["name"]).exists()
    assert str(destination) not in json.dumps(preview)
    transaction_files = list(destination.glob(".vcstudio-capsule-*.json"))
    assert len(transaction_files) == 1
    transaction = json.loads(transaction_files[0].read_text(encoding="utf-8"))
    assert transaction["confirmation_seal_sha256"] != "[redacted-secret]"
    assert transaction["destination_binding_sha256"] == preview[
        "destination_binding_sha256"]
    assert str(destination) not in json.dumps(transaction)

    exported = receipts.confirm(
        service, "project.yaml", preview["receipt_id"],
        preview["receipt_token"], "capsule-operation-1",
        project_id=host.project_id,
    )

    assert calls == [
        ("project.yaml", host.project_id, revision_id),
        ("project.yaml", host.project_id, revision_id),
    ]
    assert exported["file"]["sha256"] == preview["archive"]["sha256"]
    assert exported["file"]["size"] == preview["archive"]["size"]
    with zipfile.ZipFile(destination / exported["file"]["name"]) as archive:
        ledger = json.loads(archive.read("research-notebook/ledger.json"))
        assert ledger["bound_report_revision_id"] == revision_id
        assert ledger["ledger_head_digest"] is None


def test_capsule_preview_never_overwrites_existing_final(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "capsules"
    destination.mkdir()
    target = destination / f"{revision_id}-si-capsule.zip"
    target.write_bytes(b"pre-existing")
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)

    with pytest.raises(FileExistsError, match="already contains"):
        receipts.preview(
            service, "project.yaml", revision_id,
            selected["destination_token"], "capsule-existing",
        )
    assert target.read_bytes() == b"pre-existing"


def test_capsule_confirmation_rejects_live_notebook_drift(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    state = {"revision": 0, "head": None}

    def provider(_path, project_id, bound_revision_id):
        return _notebook_payload(
            project_id, bound_revision_id,
            ledger_revision=state["revision"], head_digest=state["head"],
        )

    host._research_notebook_archive_payload = provider
    destination = tmp_path / "drift"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-drift",
    )
    state.update(revision=1, head="c" * 64)

    with pytest.raises(RuntimeError, match="notebook changed"):
        receipts.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], "capsule-drift",
        )
    assert not (destination / preview["archive"]["name"]).exists()


def test_capsule_preview_response_loss_replays_same_receipt_and_destination(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    calls = []
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        calls.append(bound_revision_id)
        or _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "preview-crash"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    stages = []

    def fault(stage):
        stages.append(stage)
        if stage == "after_temp":
            raise RuntimeError("simulated response loss after temp")

    receipts = CapsuleExportReceipts(destinations, fault_hook=fault)
    with pytest.raises(RuntimeError, match="simulated response loss"):
        receipts.preview(
            service, "project.yaml", revision_id,
            selected["destination_token"], "capsule-preview-loss",
        )
    receipts._fault_hook = None

    replay = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-preview-loss",
    )

    assert replay["replayed"] is True
    assert stages == ["after_temp"]
    assert calls == [revision_id]


def test_capsule_confirm_reconciles_crash_after_atomic_rename(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "rename-crash"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))

    def fault(stage):
        if stage == "after_rename":
            raise RuntimeError("simulated crash after rename")

    receipts = CapsuleExportReceipts(destinations, fault_hook=fault)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-rename-crash",
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        receipts.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], "capsule-rename-crash",
        )
    target = destination / preview["archive"]["name"]
    assert target.is_file()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == preview["archive"]["sha256"]
    receipts._fault_hook = None

    reconciled = receipts.confirm(
        service, "project.yaml", preview["receipt_id"],
        preview["receipt_token"], "capsule-rename-crash",
    )

    assert reconciled["ok"] is True
    assert reconciled["replayed"] is True
    assert reconciled["file"]["sha256"] == preview["archive"]["sha256"]


def test_fresh_capsule_registry_recovers_authoritative_after_temp_receipt(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "fresh-after-temp"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))

    def fault(stage):
        if stage == "after_temp":
            raise RuntimeError("simulated crash after durable receipt")

    crashed = CapsuleExportReceipts(destinations, fault_hook=fault)
    with pytest.raises(RuntimeError, match="durable receipt"):
        crashed.preview(
            service, "project.yaml", revision_id,
            selected["destination_token"], "capsule-fresh-after-temp",
        )
    transaction_path = next(destination.glob(".vcstudio-capsule-*.json"))
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))

    recovered = CapsuleExportReceipts(CapsuleDestinations())
    replay = recovered.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-fresh-after-temp",
    )

    assert replay["receipt_id"] == transaction["receipt_id"]
    assert replay["receipt_token"] == transaction["receipt_token"]
    assert replay["replayed"] is True
    exported = recovered.confirm(
        service, "project.yaml", replay["receipt_id"],
        replay["receipt_token"], "capsule-fresh-after-temp",
    )
    assert exported["file"]["sha256"] == replay["archive"]["sha256"]


def test_fresh_capsule_registry_reconciles_after_rename_receipt(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "fresh-after-rename"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))

    def fault(stage):
        if stage == "after_rename":
            raise RuntimeError("simulated crash after durable rename")

    crashed = CapsuleExportReceipts(destinations, fault_hook=fault)
    preview = crashed.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-fresh-after-rename",
    )
    with pytest.raises(RuntimeError, match="durable rename"):
        crashed.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], "capsule-fresh-after-rename",
        )

    recovered = CapsuleExportReceipts(CapsuleDestinations())
    exported = recovered.confirm(
        service, "project.yaml", preview["receipt_id"],
        preview["receipt_token"], "capsule-fresh-after-rename",
    )

    assert exported["replayed"] is True
    assert exported["file"]["sha256"] == preview["archive"]["sha256"]
    assert len(list(destination.glob("*.zip"))) == 1


def test_capsule_confirm_rejects_destination_directory_replacement(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "replace-destination"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-replaced-destination",
    )
    moved = tmp_path / "original-destination"
    destination.rename(moved)
    destination.mkdir()

    with pytest.raises(CapsuleExportError, match="identity changed"):
        receipts.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], "capsule-replaced-destination",
        )

    assert not list(destination.glob("*.zip"))
    assert not list(moved.glob("*.zip"))


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_capsule_confirm_requires_untampered_authoritative_transaction(
    tmp_path, mutation,
):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / f"receipt-{mutation}"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], f"capsule-receipt-{mutation}",
    )
    transaction = next(destination.glob(".vcstudio-capsule-*.json"))
    if mutation == "missing":
        transaction.unlink()
    else:
        value = json.loads(transaction.read_text(encoding="utf-8"))
        value["archive"]["size"] += 1
        transaction.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CapsuleExportError, match="receipt|transaction"):
        receipts.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], f"capsule-receipt-{mutation}",
        )
    assert not list(destination.glob("*.zip"))


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_fresh_registry_requires_untampered_receipt_locator(tmp_path, mutation):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / f"locator-{mutation}"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], f"capsule-locator-{mutation}",
    )
    locator = receipts._locator_path(preview["receipt_id"])
    if mutation == "missing":
        locator.unlink()
    else:
        value = json.loads(locator.read_text(encoding="utf-8"))
        value["destination_path"] = str(tmp_path / "redirected")
        locator.write_text(json.dumps(value), encoding="utf-8")

    recovered = CapsuleExportReceipts(CapsuleDestinations())
    with pytest.raises(CapsuleExportError, match="receipt|locator"):
        recovered.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], f"capsule-locator-{mutation}",
        )
    assert not list(destination.glob("*.zip"))


def test_capsule_temp_entity_replacement_is_rejected_even_with_same_bytes(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "temp-entity-replacement"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-temp-replacement",
    )
    transaction = json.loads(next(
        destination.glob(".vcstudio-capsule-*.json")).read_text(encoding="utf-8"))
    temporary = destination / transaction["temporary_name"]
    identical = temporary.read_bytes()
    temporary.unlink()
    temporary.write_bytes(identical)

    with pytest.raises(CapsuleExportError, match="prepared archive"):
        receipts.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], "capsule-temp-replacement",
        )
    assert not list(destination.glob("*.zip"))


def test_capsule_concurrent_fresh_confirms_publish_once_and_replay(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "concurrent-confirm"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-concurrent-confirm",
    )
    registries = (
        CapsuleExportReceipts(CapsuleDestinations()),
        CapsuleExportReceipts(CapsuleDestinations()),
    )
    barrier = threading.Barrier(2)

    def publish(registry):
        barrier.wait()
        return registry.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], "capsule-concurrent-confirm",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, registries))

    assert sorted(result["replayed"] for result in results) == [False, True]
    assert results[0]["file"] == results[1]["file"]
    assert len(list(destination.glob("*.zip"))) == 1


def test_capsule_idempotency_replays_same_payload_and_rejects_different(tmp_path):
    host, service, first, second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "idempotent"
    destination.mkdir()
    destinations = CapsuleDestinations()
    first_destination = destinations.register(str(destination))
    second_destination = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        first_destination["destination_token"], "capsule-idempotent",
    )

    replay = receipts.preview(
        service, "project.yaml", revision_id,
        first_destination["destination_token"], "capsule-idempotent",
    )
    assert replay["receipt_id"] == preview["receipt_id"]
    assert replay["replayed"] is True
    with pytest.raises(ValueError, match="different input"):
        receipts.preview(
            service, "project.yaml", second["revision"]["revision_id"],
            second_destination["destination_token"], "capsule-idempotent",
        )

    exported = receipts.confirm(
        service, "project.yaml", preview["receipt_id"],
        preview["receipt_token"], "capsule-idempotent",
    )
    replayed_export = receipts.confirm(
        service, "project.yaml", preview["receipt_id"],
        preview["receipt_token"], "capsule-idempotent",
    )
    assert exported["replayed"] is False
    assert replayed_export["replayed"] is True
    assert replayed_export["file"] == exported["file"]


def test_capsule_receipt_ttl_expires_and_cleans_prepared_bytes(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    host._research_notebook_archive_payload = (
        lambda _path, project_id, bound_revision_id:
        _notebook_payload(project_id, bound_revision_id))
    destination = tmp_path / "receipt-ttl"
    destination.mkdir()
    now = [10.0]
    destinations = CapsuleDestinations(clock=lambda: now[0], ttl_seconds=20)
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(
        destinations, clock=lambda: now[0], ttl_seconds=5)
    preview = receipts.preview(
        service, "project.yaml", revision_id,
        selected["destination_token"], "capsule-expiring",
    )
    assert list(destination.glob("*.tmp"))
    now[0] = 15.0

    with pytest.raises(CapsuleExportError, match="invalid or expired"):
        receipts.confirm(
            service, "project.yaml", preview["receipt_id"],
            preview["receipt_token"], "capsule-expiring",
        )
    assert not list(destination.glob("*.tmp"))
    assert not list(destination.glob(".vcstudio-capsule-*.json"))
    assert not list(destination.glob("*.zip"))


def test_capsule_preview_rejects_missing_provider_and_wrong_revision_binding(tmp_path):
    host, service, first, second = _two_revisions(tmp_path)
    destination = tmp_path / "wrong-revision"
    destination.mkdir()
    destinations = CapsuleDestinations()
    missing = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)
    with pytest.raises(CapsuleExportError, match="provider is unavailable"):
        receipts.preview(
            service, "project.yaml", first["revision"]["revision_id"],
            missing["destination_token"], "capsule-missing-provider",
        )

    host._research_notebook_archive_payload = (
        lambda _path, project_id, _bound_revision_id:
        _notebook_payload(project_id, second["revision"]["revision_id"]))
    wrong = destinations.register(str(destination))
    with pytest.raises(CapsuleExportError, match="snapshot is invalid"):
        receipts.preview(
            service, "project.yaml", first["revision"]["revision_id"],
            wrong["destination_token"], "capsule-wrong-revision",
        )
    assert not list(destination.glob("*.zip"))


def test_capsule_preview_failure_never_echoes_path_or_credential(tmp_path):
    host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    private_failure = (
        r"path=C:\Users\alice\private\POSCAR "
        "path=/home/alice/private/POSCAR access_token=central-secret"
    )

    def failed_provider(*_args):
        raise RuntimeError(private_failure)

    host._research_notebook_archive_payload = failed_provider
    destination = tmp_path / "redacted-failure"
    destination.mkdir()
    destinations = CapsuleDestinations()
    selected = destinations.register(str(destination))
    receipts = CapsuleExportReceipts(destinations)

    with pytest.raises(CapsuleExportError) as caught:
        receipts.preview(
            service, "project.yaml", revision_id,
            selected["destination_token"], "capsule-redacted-failure",
        )
    encoded = str(caught.value)
    assert "Users" not in encoded
    assert "/home/alice" not in encoded
    assert "central-secret" not in encoded


def test_capsule_member_count_and_byte_quotas_have_exact_boundaries(tmp_path):
    _host, service, first, _second = _two_revisions(tmp_path)
    bundle = load_frozen_revision(
        service, "project.yaml", first["revision"]["revision_id"])
    notebook = _notebook_payload(bundle.project_id, bundle.revision_id)
    members = capsule_members(bundle, notebook_payload=notebook)
    baseline = build_capsule_bytes(bundle, notebook_payload=notebook)
    count = len(members)
    largest = max(map(len, members.values()))
    total = sum(map(len, members.values()))
    exact = CapsuleLimits(
        max_members=count,
        max_member_bytes=largest,
        max_total_bytes=total,
        max_archive_bytes=len(baseline),
    )

    assert build_capsule_bytes(
        bundle, notebook_payload=notebook, limits=exact) == baseline
    with pytest.raises(CapsuleQuotaError, match="member count"):
        build_capsule_bytes(
            bundle, notebook_payload=notebook,
            limits=CapsuleLimits(
                max_members=count - 1, max_member_bytes=largest,
                max_total_bytes=total, max_archive_bytes=len(baseline)),
        )
    with pytest.raises(CapsuleQuotaError, match="member byte"):
        build_capsule_bytes(
            bundle, notebook_payload=notebook,
            limits=CapsuleLimits(
                max_members=count, max_member_bytes=largest - 1,
                max_total_bytes=total, max_archive_bytes=len(baseline)),
        )
    with pytest.raises(CapsuleQuotaError, match="total byte"):
        build_capsule_bytes(
            bundle, notebook_payload=notebook,
            limits=CapsuleLimits(
                max_members=count, max_member_bytes=largest,
                max_total_bytes=total - 1, max_archive_bytes=len(baseline)),
        )
    with pytest.raises(CapsuleQuotaError, match="archive byte"):
        build_capsule_bytes(
            bundle, notebook_payload=notebook,
            limits=CapsuleLimits(
                max_members=count, max_member_bytes=largest,
                max_total_bytes=total, max_archive_bytes=len(baseline) - 1),
        )


def test_capsule_destination_tokens_expire_and_never_disclose_paths(
    tmp_path, monkeypatch,
):
    now = [100.0]
    tokens = iter(("token-one", "token-two"))
    monkeypatch.setattr(
        "vcstudio.project.report_insights.secrets.token_urlsafe",
        lambda _length: next(tokens),
    )
    destinations = CapsuleDestinations(
        clock=lambda: now[0], ttl_seconds=900, max_items=64)
    private = tmp_path / "private-destination"
    private.mkdir()

    first = destinations.register(str(private))
    now[0] = 1000.0
    second = destinations.register(str(private))

    assert first["destination_token"] == "capsule.token-one"
    assert second["destination_token"] == "capsule.token-two"
    assert len(destinations._items) == 1
    with pytest.raises(ValueError) as caught:
        destinations.consume(first["destination_token"])
    assert str(private) not in str(caught.value)
    assert destinations.consume(second["destination_token"]) == str(private.resolve())


def test_capsule_destination_registry_evicts_oldest_at_hard_limit(
    tmp_path, monkeypatch,
):
    now = [0.0]
    tokens = iter(("oldest", "middle", "newest"))
    monkeypatch.setattr(
        "vcstudio.project.report_insights.secrets.token_urlsafe",
        lambda _length: next(tokens),
    )
    destinations = CapsuleDestinations(
        clock=lambda: now[0], ttl_seconds=900, max_items=2)
    selected = []
    for index in range(3):
        directory = tmp_path / f"destination-{index}"
        directory.mkdir()
        now[0] = float(index)
        selected.append(destinations.register(str(directory)))

    assert len(destinations._items) == 2
    assert set(destinations._items) == {"capsule.middle", "capsule.newest"}
    with pytest.raises(ValueError, match="invalid or expired"):
        destinations.consume(selected[0]["destination_token"])
    assert destinations.consume(selected[1]["destination_token"]) == str(
        (tmp_path / "destination-1").resolve())


def test_api_bridge_resolves_opaque_project_and_never_accepts_browser_path(
    tmp_path, monkeypatch,
):
    api = Api.__new__(Api)
    api._dialog_fn = lambda kind: str(tmp_path) if kind == "dir" else None
    api._report_insight_destinations = None
    api._report_capsule_receipts = None
    api._report_capsule_receipts_lock = threading.RLock()
    api._report_service_instance = object()
    api._reports = lambda: api._report_service_instance
    api._project_identity_lock = threading.RLock()
    api._project_identity_local = threading.local()

    project_uuid = "0123456789abcdef0123456789abcdef"
    project_id = f"project-{project_uuid}"
    server_path = str(tmp_path / "server-only.yaml")
    loaded_project = {"project_uuid": project_uuid, "name": "Server project"}
    api._adsorption = SimpleNamespace(
        load_project=lambda path: (
            loaded_project if str(path) == server_path else None),
    )
    record = {
        "project_id": project_id,
        "request_project_id": project_id,
        "path": server_path,
        "identity_fingerprint": api._project_identity_fingerprint(
            server_path, loaded_project),
    }
    registry_calls = []
    api._project_registry_snapshot = lambda: (
        registry_calls.append(True) or {"by_id": {project_id: [record]}})
    resolved_paths = []

    monkeypatch.setattr(
        "vcstudio.project.report_insights.scientific_diff",
        lambda service, path, left, right: resolved_paths.append(path) or {
            "schema": "vcstudio.report-scientific-diff/v1", "ok": True,
            "project_id": project_id, "status": "ready",
            "left": {"revision_id": left}, "right": {"revision_id": right},
            "changed": False, "error": None,
        },
    )
    diff = api.report_revision_scientific_diff(
        project_id, "report-r0001", "report-r0002")
    assert diff["ok"] is True
    assert registry_calls
    assert resolved_paths == [server_path]

    browser_path = str(tmp_path / "browser-supplied.yaml")
    rejected = api.report_revision_scientific_diff(
        browser_path, "report-r0001", "report-r0002")
    assert rejected["ok"] is False
    assert resolved_paths == [server_path]
    assert browser_path not in json.dumps(rejected)

    selected = api.report_capsule_pick_destination()
    assert selected["ok"] is True
    assert selected["destination_token"]
    assert str(tmp_path) not in json.dumps(selected)

    preview_calls = []
    confirm_calls = []

    class _Receipts:
        def preview(self, service, path, revision, destination_token,
                    idempotency_key, **bindings):
            preview_calls.append((
                service, path, revision, destination_token,
                idempotency_key, bindings,
            ))
            return {
                "schema": "vcstudio.report-si-capsule-preview/v1",
                "ok": True, "status": "awaiting_confirmation",
                "project_id": project_id,
                "revision": {"revision_id": revision},
                "notebook": {"ledger_revision": 0, "ledger_head_digest": None},
                "provider_plan": {"mode": "frozen-preview-cas"},
                "destination_binding_sha256": "b" * 64,
                "receipt_id": "capsule-receipt.test",
                "receipt_token": "capsule-confirm.test",
                "archive": {"name": "report-r0001-si-capsule.zip",
                            "sha256": "a" * 64, "size": 1, "member_count": 1},
                "ttl_seconds": 900, "replayed": False, "file": None,
                "error": None,
            }

        def confirm(self, service, path, receipt_id, receipt_token,
                    idempotency_key, **bindings):
            confirm_calls.append((
                service, path, receipt_id, receipt_token,
                idempotency_key, bindings,
            ))
            return {
                "schema": "vcstudio.report-si-capsule/v1", "ok": True,
                "project_id": project_id, "status": "ready",
                "revision": {"revision_id": "report-r0001"},
                "file": {"name": "report-r0001-si-capsule.zip",
                         "sha256": "a" * 64, "size": 1},
                "error": None,
            }

    api._report_capsule_receipts = _Receipts()
    preview = api.report_capsule_preview(
        project_id, "report-r0001", selected["destination_token"],
        "capsule-operation")
    assert preview["ok"] is True
    assert preview_calls[0][1:5] == (
        server_path, "report-r0001", selected["destination_token"],
        "capsule-operation")
    assert str(tmp_path) not in json.dumps(preview)

    exported = api.report_capsule_export(
        project_id, preview["receipt_id"], preview["receipt_token"],
        "capsule-operation")
    assert exported["ok"] is True
    assert confirm_calls[0][1:5] == (
        server_path, "capsule-receipt.test", "capsule-confirm.test",
        "capsule-operation")
    assert str(tmp_path) not in json.dumps(exported)


def test_api_bridge_rejects_unknown_opaque_project_without_leaking_path():
    api = Api.__new__(Api)
    api._report_insight_record = lambda _project_id: (_ for _ in ()).throw(
        ValueError(r"unknown project at C:\private\project.yaml")
    )

    result = api.report_evidence_graph("wrong-project", "report-r0001")

    assert result["ok"] is False
    assert result["nodes"] == []
    assert r"C:\private\project.yaml" not in json.dumps(result)
