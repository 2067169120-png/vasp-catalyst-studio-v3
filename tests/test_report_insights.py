from __future__ import annotations

import copy
import hashlib
import json
import sys
import threading
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_report_service import _Host, _request  # noqa: E402

from vcstudio.project.report_insights import (  # noqa: E402
    CapsuleDestinations,
    FrozenRevision,
    build_capsule_bytes,
    evidence_graph,
    export_capsule,
    load_frozen_revision,
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


def test_capsule_export_never_overwrites_existing_file(tmp_path):
    _host, service, first, _second = _two_revisions(tmp_path)
    revision_id = first["revision"]["revision_id"]
    destination = tmp_path / "capsules"
    destination.mkdir()

    exported = export_capsule(service, "project.yaml", revision_id, str(destination))
    target = destination / exported["file"]["name"]
    original = target.read_bytes()

    with pytest.raises(FileExistsError):
        export_capsule(service, "project.yaml", revision_id, str(destination))
    assert target.read_bytes() == original


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

    exported_to = []
    monkeypatch.setattr(
        "vcstudio.project.report_insights.export_capsule",
        lambda service, path, revision, destination: exported_to.append(destination) or {
            "schema": "vcstudio.report-si-capsule/v1", "ok": True,
            "project_id": project_id, "status": "ready",
            "revision": {"revision_id": revision},
            "file": {"name": "report-r0001-si-capsule.zip", "sha256": "a" * 64, "size": 1},
            "error": None,
        },
    )
    exported = api.report_capsule_export(
        project_id, "report-r0001", selected["destination_token"])
    assert exported["ok"] is True
    assert exported_to == [str(tmp_path.resolve())]
    replay = api.report_capsule_export(
        project_id, "report-r0001", selected["destination_token"])
    assert replay["ok"] is False
    assert replay["status"] == "unavailable"


def test_api_bridge_rejects_unknown_opaque_project_without_leaking_path():
    api = Api.__new__(Api)
    api._report_insight_record = lambda _project_id: (_ for _ in ()).throw(
        ValueError(r"unknown project at C:\private\project.yaml")
    )

    result = api.report_evidence_graph("wrong-project", "report-r0001")

    assert result["ok"] is False
    assert result["nodes"] == []
    assert r"C:\private\project.yaml" not in json.dumps(result)
