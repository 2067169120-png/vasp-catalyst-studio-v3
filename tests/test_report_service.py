from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

import vcstudio.project.report_service as report_service_module
from vcstudio.project.report_service import (
    HISTORY_SCHEMA,
    PREVIEW_SCHEMA,
    PREVIEW_TOKEN_SCHEMA,
    ReportService,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_record(path: Path, *, digest_field="sha256") -> dict:
    return {
        "path": path.name,
        digest_field: _file_digest(path),
        "size": path.stat().st_size,
    }


class _Host:
    def __init__(self, root: Path):
        self.root = root
        self.project_id = "project-" + "a" * 32
        self.scientific_fingerprint = _digest("science-v1")
        self.input_fingerprint = _digest("input-v1")
        self.preview_calls = 0
        self.render_calls = 0
        self.marker_calls = 0
        self.fail_marker = False
        self.history_audit_calls = 0
        self.history_audit_result = None
        self.history_audit_error = None
        self.render_extras = {}
        self.current_state_error = None

    def _report_workbench_project_context(self, path):
        if str(path) != "project.yaml":
            raise FileNotFoundError(path)
        return {
            "project": {"name": "Demo", "root": str(self.root)},
            "project_path": "project.yaml",
            "project_root": str(self.root),
            "project_id": self.project_id,
            "project_name": "Demo",
        }

    @staticmethod
    def proj_report_capabilities():
        return {
            "ok": True,
            "formats": {
                fmt: {"available": True, "reason": ""}
                for fmt in ("html", "docx", "pdf")
            },
        }

    @staticmethod
    def proj_report_status(path):
        return {
            "ok": True,
            "artifact_status": "missing",
            "scientific_status": None,
            "path": str(path),
        }

    def _report_workbench_build(self, path, spec, temp_root):
        spec_sha = spec.semantic_sha256
        snapshot_sha = _digest(spec_sha + self.scientific_fingerprint)
        model_sha = _digest(snapshot_sha + "model")
        validation_sha = _digest(snapshot_sha + model_sha)
        contracts = {
            "preset_id": spec.preset_id,
            "scientific_qualification": (
                "adsorption_result_verified"
                if spec.requested_kind == "final" else "diagnostic"
            ),
            "report_spec": spec.to_dict(),
            "report_snapshot": {
                "spec_sha256": spec_sha,
                "input_fingerprint": self.scientific_fingerprint,
                "sources": [{
                    "source_id": "project:demo",
                    "sha256": self.scientific_fingerprint,
                    "locator": r"C:\private\project.yaml",
                }],
                "payload": {"energy_ev": -1.25},
                "evidence": {},
            },
            "validation": {
                "spec_sha256": spec_sha,
                "snapshot_sha256": snapshot_sha,
                "status": "passed",
                "effective_kind": spec.requested_kind,
                "scientific_qualification": "diagnostic",
                "checks": [],
            },
            "contract_refs": {
                "spec": {"sha256": spec_sha},
                "snapshot": {"sha256": snapshot_sha},
                "validation": {"sha256": validation_sha},
            },
        }
        return {
            "project": {"name": "Demo"},
            "project_path": str(path),
            "project_root": str(self.root),
            "project_id": self.project_id,
            "summary": {},
            "report_spec": spec.to_dict(),
            "requested_kind": spec.requested_kind,
            "report_kind": spec.requested_kind,
            "eligible_final": True,
            "gate_reason": "",
            "input_fingerprint": self.input_fingerprint,
            "scientific_fingerprint": self.scientific_fingerprint,
            "report_model_sha256": model_sha,
            "contracts": contracts,
            "model": {"title": "Demo", **contracts},
            "figure_dir": str(Path(temp_root) / "figures"),
            "figure_files": [],
            "default_stem": "demo-report",
        }

    def _report_workbench_render_preview(self, model):
        self.preview_calls += 1
        return {"html": "<!doctype html><title>safe preview</title>"}

    def _report_workbench_current_state(self, path, report_spec=None):
        if self.current_state_error is not None:
            raise self.current_state_error
        return {
            "project_id": self.project_id,
            "input_fingerprint": self.input_fingerprint,
            "scientific_fingerprint": self.scientific_fingerprint,
            "eligible_final": True,
            "gate_reason": "",
        }

    def _report_workbench_render_build(self, build, out_dir, *, stem, revision):
        self.render_calls += 1
        destination = Path(out_dir)
        destination.mkdir(parents=True, exist_ok=True)
        formats = build["contracts"]["report_spec"]["formats"]
        revision_id = str(revision.get("revision_id") or "unrecorded")
        files = {}
        for fmt in formats:
            path = destination / f"{stem}.{fmt}"
            path.write_text(f"{fmt}:{revision_id}", encoding="utf-8")
            files[fmt] = str(path)
        model_file = destination / f"{stem}.model.json"
        model_file.write_text("{}", encoding="utf-8")
        contracts = {}
        contract_records = {}
        contract_payloads = {
            "spec": build["contracts"]["report_spec"],
            "snapshot": build["contracts"]["report_snapshot"],
            "validation": build["contracts"]["validation"],
        }
        for name, payload in contract_payloads.items():
            path = destination / f"{stem}.{name}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            contracts[name] = str(path)
            contract_records[name] = {
                **copy.deepcopy(build["contracts"]["contract_refs"][name]),
                **_file_record(path, digest_field="file_sha256"),
            }
        manifest = destination / f"{stem}.manifest.json"
        manifest_payload = {
            "schema": "vcstudio.paper-report.bundle/v2",
            "artifact_status": "complete",
            "report_id": revision.get("report_id"),
            "preset_id": build["contracts"]["preset_id"],
            "report_kind": build["report_kind"],
            "scientific_status": build["report_kind"],
            "scientific_qualification": build["contracts"][
                "scientific_qualification"
            ],
            "model_sha256": _file_digest(model_file),
            "report_model_sha256": build["report_model_sha256"],
            "revision": copy.deepcopy(revision),
            "formats": list(formats),
            "files": {
                fmt: _file_record(Path(raw_path))
                for fmt, raw_path in files.items()
            },
            "model_file": _file_record(model_file),
            "contracts": contract_records,
            "assets": [],
        }
        manifest.write_text(
            json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        result = {
            "ok": True,
            "artifact_status": "complete",
            "files": {**files, "manifest": str(manifest)},
            "contract_files": contracts,
            "model_file": str(model_file),
            "manifest": str(manifest),
            "report_model_sha256": build["report_model_sha256"],
            "model_sha256": _file_digest(model_file),
            "contracts": contract_records,
            "revision": copy.deepcopy(revision),
            "error": None,
        }
        result.update(copy.deepcopy(self.render_extras))
        return result

    def _report_workbench_persist_build(self, build, rendered, *, revision):
        self.marker_calls += 1
        if self.fail_marker:
            raise RuntimeError("marker disk full")
        return {"revision": copy.deepcopy(revision), "artifact_status": "ready"}

    def _report_workbench_validate_history_entry(self, entry):
        self.history_audit_calls += 1
        if self.history_audit_error is not None:
            raise self.history_audit_error
        if self.history_audit_result is not None:
            return copy.deepcopy(self.history_audit_result)
        return {
            "ok": True,
            "current": True,
            "scientific_status": entry.get("scientific_status"),
            "scientific_qualification": entry.get("scientific_qualification"),
            "error": None,
        }


def _request(project_id: str, *, operation_id="op-1", title_path=False):
    request = {
        "operation_id": operation_id,
        "spec": {
            "preset_id": "diagnostic-repair",
            "formats": ["html"],
            "scope": {
                "kind": "project",
                "project_ids": [project_id],
                "job_ids": [],
                "species": [],
                "configuration_ids": [],
                "stable_only": False,
                "include_failed": True,
            },
        },
    }
    if title_path:
        request["spec"]["scope"]["species"] = [r"C:\secret\species"]
    return request


def test_bootstrap_returns_five_server_presets_without_creating_history(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")

    result = service.bootstrap("project.yaml", "diagnostic-repair")

    assert result["ok"] is True
    assert len(result["catalog"]["presets"]) == 5
    assert result["report_spec"]["preset_id"] == "diagnostic-repair"
    assert result["history"]["revisions"] == []
    assert not (tmp_path / ".vcstudio").exists()


def test_preview_is_bound_path_free_and_writes_no_project_history(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")

    result = service.preview("project.yaml", _request(host.project_id))

    assert result["schema"] == PREVIEW_SCHEMA
    assert result["ok"] is True and result["artifact_status"] == "preview"
    assert result["operation_id"] == "op-1"
    assert result["preview_token"]["schema"] == PREVIEW_TOKEN_SCHEMA
    assert len(result["preview_id"]) == 64
    encoded = json.dumps(result, ensure_ascii=False)
    assert r"C:\private" not in encoded
    assert "<local-path-redacted>" not in result["html"]
    assert not (tmp_path / ".vcstudio").exists()
    assert host.preview_calls == 1 and host.marker_calls == 0


def test_preview_rejects_unsafe_nested_request_before_build(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")

    result = service.preview(
        "project.yaml", _request(host.project_id, title_path=True)
    )

    assert result["ok"] is False
    assert "path" in result["error"].lower()
    assert host.preview_calls == host.render_calls == host.marker_calls == 0


def test_publish_requires_complete_exact_preview_token(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))
    incomplete = {"preview_id": preview["preview_id"]}

    result = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"], incomplete
    )

    assert result["ok"] is False
    assert "missing project_id" in result["error"]
    assert host.render_calls == host.marker_calls == 0


def test_changed_scientific_input_makes_preview_stale_without_artifact(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))
    host.scientific_fingerprint = _digest("science-v2")

    result = service.publish(
        "project.yaml",
        str(tmp_path / "out"),
        preview["preview_id"],
        preview["preview_token"],
    )

    assert result["ok"] is False
    assert result["artifact_status"] == "stale_preview"
    assert result["stale_preview"] is True
    assert host.render_calls == host.marker_calls == 0


def test_publish_allocates_monotonic_revisions_and_preserves_old_files(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    first_preview = service.preview("project.yaml", _request(host.project_id))
    first = service.publish(
        "project.yaml", str(tmp_path / "out"), first_preview["preview_id"],
        first_preview["preview_token"], public=False,
    )
    second_preview = service.preview(
        "project.yaml", _request(host.project_id, operation_id="op-2")
    )
    second = service.publish(
        "project.yaml", str(tmp_path / "out"), second_preview["preview_id"],
        second_preview["preview_token"], public=False,
    )

    assert first["ok"] is second["ok"] is True
    assert first["revision"]["sequence"] == 1
    assert second["revision"]["sequence"] == 2
    assert second["revision"]["parent_manifest_sha256"] == hashlib.sha256(
        Path(first["manifest"]).read_bytes()).hexdigest()
    assert Path(first["files"]["html"]).is_file()
    assert Path(second["files"]["html"]).is_file()
    assert first["files"]["html"] != second["files"]["html"]
    history = service.history("project.yaml")
    assert history["ok"] is True
    assert [item["sequence"] for item in reversed(history["revisions"])] == [1, 2]


def test_two_previews_from_same_base_revision_conflict_after_first_publish(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    left = service.preview("project.yaml", _request(host.project_id, operation_id="left"))
    right = service.preview("project.yaml", _request(host.project_id, operation_id="right"))

    first = service.publish(
        "project.yaml", str(tmp_path / "out"), left["preview_id"], left["preview_token"]
    )
    conflict = service.publish(
        "project.yaml", str(tmp_path / "out"), right["preview_id"], right["preview_token"]
    )

    assert first["ok"] is True
    assert conflict["ok"] is False
    assert conflict["artifact_status"] == "revision_conflict"
    assert conflict["base_revision"] == 0 and conflict["current_revision"] == 1
    assert host.render_calls == 1 and host.marker_calls == 1


def test_marker_failure_retains_generated_unrecorded_revision(tmp_path):
    host = _Host(tmp_path)
    host.fail_marker = True
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))

    result = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=False,
    )

    assert result["ok"] is False
    assert result["artifact_status"] == "generated_unrecorded"
    assert result["marker"] is None
    assert Path(result["manifest"]).is_file()
    history = service.history("project.yaml")
    assert history["revisions"][0]["artifact_status"] == "generated_unrecorded"
    assert "disk full" in history["revisions"][0]["error"]


def test_public_publish_result_exposes_names_not_absolute_paths(tmp_path):
    host = _Host(tmp_path)
    secret_path = tmp_path / "private" / "figure.png"
    host.render_extras = {
        "assets": [str(secret_path)],
        "figures": [{"source": str(secret_path)}],
        "out_dir": str(tmp_path / "out"),
        "recovery_record": {"where": str(tmp_path / "rollback")},
        "diagnostics": {
            "detail": f"renderer failed near {secret_path}",
            "credential": "github_pat_abcdefghijklmnopqrstuvwxyz123456",
        },
    }
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))

    result = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=True,
    )

    assert result["ok"] is True
    assert result["files"]["html"]["available"] is True
    assert not os.path.isabs(result["files"]["html"]["name"])
    assert "marker" not in result
    for forbidden in (
        "assets", "figures", "out_dir", "recovery_record", "marker"
    ):
        assert forbidden not in result
    encoded = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "github_pat_" not in encoded


def test_public_publish_failure_redacts_paths_and_credentials(tmp_path):
    host = _Host(tmp_path)
    host.current_state_error = RuntimeError(
        rf"failed at {tmp_path}\private; token=super-secret-value"
    )
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))

    result = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=True,
    )

    assert result["ok"] is False
    encoded = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "super-secret-value" not in encoded


def test_expired_preview_cannot_publish(tmp_path):
    now = [1000.0]
    host = _Host(tmp_path)
    service = ReportService(
        host,
        preview_ttl_seconds=60,
        clock=lambda: now[0],
        temp_root=tmp_path / "previews",
    )
    preview = service.preview("project.yaml", _request(host.project_id))
    now[0] += 61

    result = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"],
    )

    assert result["ok"] is False
    assert "expired" in result["error"]


def test_history_schema_is_stable(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    history = service.history("project.yaml")
    assert history == {
        "schema": HISTORY_SCHEMA,
        "ok": True,
        "project_id": host.project_id,
        "generation": 0,
        "revisions": [],
        "error": None,
    }


def test_history_ready_requires_strict_host_ok_and_current(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))
    published = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=False,
    )
    assert published["ok"] is True

    ready = service.history("project.yaml")
    assert ready["revisions"][0]["artifact_status"] == "ready"
    assert ready["revisions"][0]["current"] is True
    assert host.history_audit_calls == 1

    host.history_audit_result = {
        "ok": True,
        "current": 1,
        "scientific_status": "diagnostic",
        "scientific_qualification": "diagnostic",
        "error": "non-boolean current",
    }
    stale = service.history("project.yaml")
    assert stale["ok"] is True
    assert stale["revisions"][0]["artifact_status"] == "stale"
    assert stale["revisions"][0]["current"] is False


def test_draft_revision_remains_a_valid_distinct_scientific_status(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    request = _request(host.project_id)
    request["spec"]["preset_id"] = "manuscript-materials"
    preview = service.preview("project.yaml", request)
    assert preview["scientific_status"] == "draft"
    published = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=False,
    )

    history = service.history("project.yaml")

    assert published["scientific_status"] == "draft"
    assert history["revisions"][0]["scientific_status"] == "draft"
    assert history["revisions"][0]["artifact_status"] == "ready"


@pytest.mark.parametrize(
    "member",
    ("format", "model", "spec", "snapshot", "validation"),
)
def test_history_marks_any_changed_bundle_member_stale(tmp_path, member):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))
    published = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=False,
    )
    targets = {
        "format": published["files"]["html"],
        "model": published["model_file"],
        **published["contract_files"],
    }
    Path(targets[member]).write_bytes(Path(targets[member]).read_bytes() + b"tamper")

    history = service.history("project.yaml")

    assert history["ok"] is True
    assert history["revisions"][0]["artifact_status"] == "stale"
    assert history["revisions"][0]["current"] is False


def test_history_rejects_manifest_revision_tamper_even_with_updated_manifest_hash(
    tmp_path,
):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))
    published = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=False,
    )
    manifest_path = Path(published["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["revision"]["sequence"] = 99
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    history_path = tmp_path / ".vcstudio" / "reports" / "history.json"
    history_payload = json.loads(history_path.read_text(encoding="utf-8"))
    report_id = published["revision"]["report_id"]
    new_manifest_sha = _file_digest(manifest_path)
    history_payload["reports"][report_id]["latest_manifest_sha256"] = new_manifest_sha
    history_payload["reports"][report_id]["revisions"][0][
        "manifest_sha256"
    ] = new_manifest_sha
    history_path.write_text(
        json.dumps(history_payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    history = service.history("project.yaml")

    assert history["ok"] is True
    assert history["revisions"][0]["artifact_status"] == "stale"
    assert "revision" in history["revisions"][0]["error"]


def test_first_history_write_failure_preserves_generated_artifacts(
    tmp_path, monkeypatch,
):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))

    def fail_history_write(path, value):
        raise OSError("history volume is read only")

    monkeypatch.setattr(report_service_module, "_atomic_json", fail_history_write)
    result = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=False,
    )

    assert result["ok"] is False
    assert result["artifact_status"] == "generated_unrecorded"
    assert result["recovery_state"] == "history_entry_write_failed"
    assert result["history_recorded"] is False
    assert host.marker_calls == 0
    assert Path(result["manifest"]).is_file()
    assert Path(result["files"]["html"]).is_file()


def test_final_history_write_failure_after_marker_preserves_recoverable_bundle(
    tmp_path, monkeypatch,
):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))
    original_atomic_json = report_service_module._atomic_json
    writes = 0

    def fail_second_history_write(path, value):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("history finalization failed")
        return original_atomic_json(path, value)

    monkeypatch.setattr(
        report_service_module, "_atomic_json", fail_second_history_write
    )
    result = service.publish(
        "project.yaml", str(tmp_path / "out"), preview["preview_id"],
        preview["preview_token"], public=False,
    )

    assert result["ok"] is False
    assert result["artifact_status"] == "generated_unrecorded"
    assert result["partial_success"] is True
    assert result["marker_recorded"] is True
    assert result["history_recorded"] is True
    assert result["history_ready_recorded"] is False
    assert result["recovery_state"] == "marker_ready_history_finalize_failed"
    assert Path(result["manifest"]).is_file()
    assert Path(result["files"]["html"]).is_file()
    history = service.history("project.yaml")
    assert history["revisions"][0]["artifact_status"] == "generated_unrecorded"


def test_missing_history_never_overwrites_same_lineage_first_revision(tmp_path):
    host = _Host(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview("project.yaml", _request(host.project_id))
    record = service._previews[preview["preview_id"]]
    destination = tmp_path / "out"
    destination.mkdir()
    orphan = destination / "orphan.manifest.json"
    orphan.write_text(
        json.dumps({
            "report_id": record.report_id,
            "revision": {
                "report_id": record.report_id,
                "revision_id": f"{record.report_id}-r0001",
                "sequence": 1,
            },
        }),
        encoding="utf-8",
    )
    original = orphan.read_bytes()

    result = service.publish(
        "project.yaml", str(destination), preview["preview_id"],
        preview["preview_token"], public=False,
    )

    assert result["ok"] is False
    assert result["artifact_status"] == "revision_conflict"
    assert result["current_revision"] is None
    assert host.render_calls == host.marker_calls == 0
    assert orphan.read_bytes() == original
    assert not (destination / "demo-report_r0001.html").exists()
