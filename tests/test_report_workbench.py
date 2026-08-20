from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vcstudio.gui_web.api import Api
from vcstudio.project.report_insights import (
    CapsuleDestinations,
    OpaqueDestinationRegistry,
)


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05"
    b"\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _Charts:
    @staticmethod
    def _write(path, formats):
        source = Path(path)
        stem = source.with_suffix("")
        outputs = []
        for fmt in formats:
            target = stem.with_suffix(f".{fmt}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_PNG if fmt == "png" else b"%PDF-1.4\n%%EOF\n")
            outputs.append(str(target))
        return outputs

    def adsorption_bar(self, data, path, **kwargs):
        return self._write(path, kwargs.get("formats") or ("png",))

    def energy_matrix_table(self, data, path, **kwargs):
        return self._write(path, kwargs.get("formats") or ("png",))

    def free_energy_ladder(self, data, path, **kwargs):
        return self._write(path, kwargs.get("formats") or ("png",))


def _workbench_api(tmp_path):
    project_path = str(tmp_path / "project.yaml")
    Path(project_path).write_text("schema: vcstudio.project/v1\n", encoding="utf-8")
    clean = str(tmp_path / "clean")
    reference = str(tmp_path / "reference")
    config = str(tmp_path / "config")
    for directory in (clean, reference, config):
        Path(directory).mkdir()
    state = {
        "project": {
            "name": "workbench-demo",
            "project_uuid": "1" * 32,
            "root": str(tmp_path),
            "members": {
                "clean_slab": clean,
                "gas_ref": reference,
                "configs": [config],
            },
        },
        "summary": {
            "slab": ("DONE", -100.0),
            "ref": ("DONE", -10.0),
            "has_ref": True,
            "reference_mode": "single",
            "method_consistency": {
                "status": "verified",
                "issues": [],
                "warnings": [],
            },
            "rows": [{
                "name": "demo_ads_Li2S8",
                "configuration_id": "job-config",
                "species": "Li2S8",
                "job": config,
                "state": "DONE",
                "e_config": -115.0,
                "e_slab": -100.0,
                "e_ref": -10.0,
                "delta_e": -5.0,
                "dd_e": 0.0,
                "reference_valid": True,
                "reference_state": "DONE",
                "reference_job": reference,
                "reference_source": "OSZICAR:E0",
                "method_check": {"status": "verified"},
                "method_status": "verified",
                "note": "",
            }],
        },
        "saved": [],
    }
    manifests = {
        clean: {
            "job_uuid": "job-clean",
            "state": "DONE",
            "inputs": {"engine": "vasp", "formula": "Pt4S",
                       "source_id": "source-clean", "sha256": {"POSCAR": "a" * 64}},
            "attempts": [{"n": 1, "attempt_token": "clean-a1"}],
            "results": {"energy_e0_eV": -100.0, "fetched_sha256": {"OUTCAR": "a" * 64}},
        },
        reference: {
            "job_uuid": "job-reference",
            "state": "DONE",
            "inputs": {"engine": "vasp", "formula": "Li2S8",
                       "source_id": "source-reference",
                       "sha256": {"POSCAR": "b" * 64}},
            "attempts": [{"n": 1, "attempt_token": "ref-a1"}],
            "results": {"energy_e0_eV": -10.0, "fetched_sha256": {"OUTCAR": "b" * 64}},
        },
        config: {
            "job_uuid": "job-config",
            "state": "DONE",
            "inputs": {"engine": "vasp", "formula": "Pt4SLi2S8",
                       "source_id": "source-config", "sha256": {"POSCAR": "c" * 64}},
            "attempts": [{"n": 1, "attempt_token": "config-a1"}],
            "results": {"energy_e0_eV": -115.0, "fetched_sha256": {"OUTCAR": "c" * 64}},
        },
    }

    def load_project(path):
        return state["project"] if str(path) == project_path else None

    def save_project(root, value):
        state["project"] = copy.deepcopy(value)
        state["saved"].append(copy.deepcopy(value))

    adsorption = SimpleNamespace(
        list_projects=lambda: [project_path],
        load_project=load_project,
        save_project=save_project,
        delta_e_rows=lambda project: copy.deepcopy(state["summary"]),
    )
    manifest = SimpleNamespace(load_manifest=lambda path: copy.deepcopy(manifests.get(str(path))))
    api = Api(
        adsorption_mod=adsorption,
        manifest_mod=manifest,
        native_charts_mod=_Charts(),
    )
    return api, project_path, state, manifests


def _project_id(api, project_path):
    return api._workspace_project_id(
        project_path, api._adsorption.load_project(project_path)
    )


def _destination_token(api, project_id, directory):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    return api._workbench_destination_registry().register(
        str(target), binding=project_id
    )["destination_token"]


def _publish(api, project_path, directory, preview, expected=None):
    project_id = _project_id(api, project_path)
    destination_token = _destination_token(api, project_id, directory)
    return api.report_workbench_publish(
        project_id,
        destination_token,
        preview["preview_id"],
        preview["preview_token"] if expected is None else expected,
    )


def _assert_public_path_free(value, *private_paths):
    encoded = json.dumps(value, ensure_ascii=False)
    for private_path in private_paths:
        assert str(private_path) not in encoded
    forbidden_keys = {"path", "project_path", "root", "out_dir", "destination"}

    def visit(item):
        if isinstance(item, dict):
            assert forbidden_keys.isdisjoint(item)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)


def _html_request(project_id, **overrides):
    spec = {
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
    }
    spec.update(overrides)
    if spec.get("requested_kind") == "final" and "outline" not in overrides:
        spec["outline"] = [
            "executive_summary", "adsorption_table", "methods", "limitations",
        ]
    return {"operation_id": "preview-1", "spec": spec}


def test_workbench_preview_is_self_contained_and_has_no_project_side_effect(tmp_path):
    api, project_path, state, _ = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    before = copy.deepcopy(state["project"])

    preview = api.report_workbench_preview(
        project_id,
        _html_request(
            boot["project_id"],
            outline=["executive_summary", "figures", "methods", "limitations"],
        ),
    )

    assert preview["ok"] is True
    assert preview["artifact_status"] == "preview"
    assert preview["scientific_status"] == "diagnostic"
    assert "data:image/png;base64," in preview["html"]
    assert str(tmp_path) not in json.dumps(preview, ensure_ascii=False)
    assert state["project"] == before and state["saved"] == []
    assert not (tmp_path / ".vcstudio").exists()


def test_workbench_final_request_is_gate_owned_and_publishes_bound_revision(tmp_path):
    api, project_path, state, _ = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    request = _html_request(
        boot["project_id"], requested_kind="final", outline=[
            "executive_summary", "adsorption_table", "methods", "limitations"
        ]
    )
    preview = api.report_workbench_preview(project_id, request)
    preview_record = api._reports()._previews[preview["preview_id"]]
    preview_temp_root = Path(preview_record.temp_root)
    out_dir = tmp_path / "published"

    published = _publish(api, project_path, out_dir, preview)

    assert preview["scientific_status"] == "final"
    assert preview["scientific_qualification"] == "adsorption_result_verified"
    assert published["ok"] is True and published["artifact_status"] == "complete"
    assert preview["preview_id"] not in api._reports()._previews
    assert not preview_temp_root.exists()
    assert published["revision"]["sequence"] == 1
    assert published["revision"]["spec_sha256"] == preview["preview_token"]["spec_sha256"]
    assert published["files"]["html"]["available"] is True
    marker = state["project"]["autopilot_report"]
    assert marker["kind"] == "final"
    assert marker["revision"] == published["revision"]
    manifest_path = Path(marker["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["revision"] == published["revision"]
    assert manifest["report_model_sha256"] == preview["report_model_sha256"]


def test_research_live_report_edge_requires_revalidated_frozen_analysis_binding(tmp_path):
    api, project_path, state, manifests = _workbench_api(tmp_path)
    output_payloads = {}
    for path, manifest in manifests.items():
        payload = f"current output for {manifest['job_uuid']}\n".encode()
        output_payloads[path] = payload
        (Path(path) / "OUTCAR").write_bytes(payload)
        manifest["results"]["fetched_sha256"] = {
            "OUTCAR": hashlib.sha256(payload).hexdigest(),
        }
    project_id = _project_id(api, project_path)
    api._analysis_workbench_method_evidence = lambda _target: {
        "status": "verified", "engine": "vasp",
        "schema": "vcstudio.method-fingerprint/vasp/v1",
        "fingerprint": {"functional": "PBE", "dispersion": "D3"},
        "missing": [],
    }
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    preview = api.report_workbench_preview(
        project_id,
        _html_request(boot["project_id"], requested_kind="final"),
    )
    published = _publish(api, project_path, tmp_path / "published", preview)
    assert published["ok"] is True

    research = api.research_explorer_rebuild({"limit": 200})
    config = next(
        row for row in research["table"]["rows"] if row["job_id"] == "job-config")
    graph = api.research_explorer_provenance(
        project_id, config["job_id"], config["source_id"])

    assert config["evidence_level"] == "verified"
    assert any(edge["type"] == "reported_in" for edge in graph["edges"])
    report = next(node for node in graph["nodes"] if node["type"] == "report")
    assert report["origin_status"] == "observed"
    assert report["record"]["revision_id"] == published["revision"]["revision_id"]

    history = json.loads((tmp_path / ".vcstudio" / "reports" / "history.json")
                         .read_text(encoding="utf-8"))
    revision_entry = next(
        entry for lineage in history["reports"].values()
        for entry in lineage["revisions"]
        if entry["revision_id"] == published["revision"]["revision_id"])
    frozen_snapshot = Path(revision_entry["contract_files"]["snapshot"])
    frozen_bytes = frozen_snapshot.read_bytes()
    frozen_stat = frozen_snapshot.stat()
    frozen_snapshot.write_bytes(frozen_bytes + b" ")
    os.utime(
        frozen_snapshot,
        ns=(frozen_stat.st_atime_ns, frozen_stat.st_mtime_ns))
    damaged = api.research_explorer_query({"limit": 200})
    assert damaged["ok"] is False
    assert damaged["status"] in {"partial", "unavailable"}
    assert damaged["table"]["rows"] == []
    frozen_snapshot.write_bytes(frozen_bytes)
    os.utime(
        frozen_snapshot,
        ns=(frozen_stat.st_atime_ns, frozen_stat.st_mtime_ns))

    config_path = state["project"]["members"]["configs"][0]
    manifests[config_path]["results"]["barrier_eV"] = 0.72
    mixed = api.research_explorer_rebuild({"limit": 200})
    mixed_config = next(
        row for row in mixed["table"]["rows"] if row["job_id"] == "job-config")
    assert mixed_config["evidence_level"] == "unverified"
    assert mixed_config["quantity_evidence"] == {
        "energy_eV": "verified", "barrier_eV": "unverified"}
    quantity_graph = api.research_explorer_provenance(
        project_id, config["job_id"], config["source_id"])
    reported = [edge for edge in quantity_graph["edges"]
                if edge["type"] == "reported_in"]
    assert [edge["quantity"] for edge in reported] == ["energy_eV"]
    barrier = next(
        node for node in quantity_graph["nodes"]
        if node["id"] == "analysis:job-config:barrier_eV")
    assert barrier["record"]["evidence_level"] == "unverified"
    assert any(item["from"] == "analysis:job-config:barrier_eV"
               for item in quantity_graph["missing"])

    (Path(config_path) / "OUTCAR").write_bytes(b"tampered output\n")
    stale_output = api.research_explorer_provenance(
        project_id, config["job_id"], config["source_id"])
    assert stale_output["ok"] is False
    assert stale_output["status"] in {"partial", "unavailable"}
    assert not any(edge["type"] == "reported_in" for edge in stale_output["edges"])
    assert stale_output["nodes"] == []

    (Path(config_path) / "OUTCAR").write_bytes(output_payloads[config_path])
    state["project"]["autopilot_report"]["revision"]["revision_id"] = "not-real"
    unbound = api.research_explorer_provenance(
        project_id, config["job_id"], config["source_id"])
    assert unbound["ok"] is False
    assert unbound["status"] in {"partial", "unavailable"}
    assert not any(edge["type"] == "reported_in" for edge in unbound["edges"])
    assert unbound["nodes"] == []


def test_workbench_publish_rejects_source_change_after_preview(tmp_path):
    api, project_path, state, manifests = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    preview = api.report_workbench_preview(
        project_id, _html_request(boot["project_id"], requested_kind="final")
    )
    config = state["project"]["members"]["configs"][0]
    manifests[config]["attempts"].append({"n": 2, "attempt_token": "config-a2"})
    manifests[config]["results"]["energy_e0_eV"] = -114.0

    result = _publish(api, project_path, tmp_path / "out", preview)

    assert result["ok"] is False
    assert result["artifact_status"] == "stale_preview"
    assert "autopilot_report" not in state["project"]
    assert list((tmp_path / "out").iterdir()) == []


def test_workbench_scope_rejects_member_from_another_project(tmp_path):
    api, project_path, _, _ = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    request = _html_request(boot["project_id"])
    request["spec"]["scope"]["configuration_ids"] = ["job-foreign"]

    preview = api.report_workbench_preview(project_id, request)

    assert preview["ok"] is False
    assert "configuration_id" in preview["error"]


def test_workbench_publish_rejects_tampered_binding_and_creates_no_history(tmp_path):
    api, project_path, _, _ = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    preview = api.report_workbench_preview(
        project_id, _html_request(boot["project_id"])
    )
    token = copy.deepcopy(preview["preview_token"])
    token["report_model_sha256"] = "0" * 64

    result = _publish(api, project_path, tmp_path / "out", preview, token)

    assert result["ok"] is False
    assert "report_model_sha256" in result["error"]
    history = api.report_workbench_history(project_id)
    assert history["revisions"] == []
    assert list((tmp_path / "out").iterdir()) == []


def test_public_workbench_journey_uses_only_opaque_inputs_and_path_free_dtos(tmp_path):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    destination = tmp_path / "private-publication-directory"
    destination.mkdir()
    api._dialog_fn = lambda kind: str(destination) if kind == "dir" else None

    bootstrap = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    preview = api.report_workbench_preview(
        project_id, _html_request(bootstrap["project_id"])
    )
    selected = api.report_workbench_pick_destination(project_id)
    published = api.report_workbench_publish(
        project_id,
        selected["destination_token"],
        preview["preview_id"],
        preview["preview_token"],
    )
    history = api.report_workbench_history(project_id)

    assert all(
        result["ok"] is True
        for result in (bootstrap, preview, selected, published, history)
    )
    assert selected["destination_token"].startswith("report-workbench.")
    assert api._report_workbench_destinations._max_items == 64
    assert api._report_workbench_destinations._ttl_seconds == 15 * 60
    for result in (bootstrap, preview, selected, published, history):
        _assert_public_path_free(result, tmp_path, project_path, destination)

    replay = api.report_workbench_publish(
        project_id,
        selected["destination_token"],
        preview["preview_id"],
        preview["preview_token"],
    )
    assert replay["ok"] is False
    assert "invalid or expired" in replay["error"]
    _assert_public_path_free(replay, tmp_path, project_path, destination)


def test_workbench_rejects_raw_unknown_and_duplicate_project_identity_without_leak(
    tmp_path,
):
    api, project_path, state, _manifests = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)

    for untrusted in (project_path, "project-" + "f" * 32):
        responses = (
            api.report_workbench_bootstrap(untrusted),
            api.report_workbench_preview(untrusted, {}),
            api.report_workbench_history(untrusted),
            api.report_workbench_pick_destination(untrusted),
        )
        assert all(result["ok"] is False for result in responses)
        for result in responses:
            _assert_public_path_free(result, tmp_path, project_path)

    duplicate_path = str(tmp_path / "duplicate" / "project.yaml")
    Path(duplicate_path).parent.mkdir()
    Path(duplicate_path).write_text("schema: vcstudio.project/v1\n", encoding="utf-8")
    original_load = api._adsorption.load_project

    def duplicate_load(path):
        if str(path) == duplicate_path:
            clone = copy.deepcopy(state["project"])
            clone["root"] = str(Path(duplicate_path).parent)
            return clone
        return original_load(path)

    api._adsorption.load_project = duplicate_load
    api._adsorption.list_projects = lambda: [project_path, duplicate_path]

    duplicate = api.report_workbench_bootstrap(project_id)
    assert duplicate["ok"] is False
    assert "missing or ambiguous" in duplicate["error"]
    _assert_public_path_free(duplicate, tmp_path, project_path, duplicate_path)


def test_workbench_destination_tokens_are_ttl_bounded_project_bound_and_isolated(
    tmp_path,
):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    now = [10.0]
    registry = OpaqueDestinationRegistry(
        clock=lambda: now[0],
        ttl_seconds=5,
        max_items=2,
        schema="vcstudio.report-workbench-destination/v1",
        purpose="report-workbench",
        token_prefix="report-workbench.",
    )
    api._report_workbench_destinations = registry
    directories = []
    tokens = []
    for index in range(3):
        directory = tmp_path / f"destination-{index}"
        directory.mkdir()
        directories.append(directory)
        now[0] += 1
        tokens.append(registry.register(str(directory), binding=project_id))

    assert len(registry._items) == 2
    assert tokens[0]["destination_token"] not in registry._items
    with pytest.raises(ValueError, match="invalid or expired"):
        registry.consume(tokens[0]["destination_token"], expected_binding=project_id)

    wrong_binding = registry.register(str(directories[0]), binding="project-other")
    with pytest.raises(ValueError, match="binding mismatch"):
        registry.consume(
            wrong_binding["destination_token"], expected_binding=project_id
        )
    assert wrong_binding["destination_token"] not in registry._items

    expiring = registry.register(str(directories[0]), binding=project_id)
    now[0] += 6
    with pytest.raises(ValueError, match="invalid or expired"):
        registry.consume(expiring["destination_token"], expected_binding=project_id)

    capsule = CapsuleDestinations()
    capsule_token = capsule.register(str(directories[0]))["destination_token"]
    assert capsule_token.startswith("capsule.")
    with pytest.raises(ValueError, match="invalid or expired"):
        registry.consume(capsule_token, expected_binding=project_id)
    workbench_token = registry.register(
        str(directories[0]), binding=project_id
    )["destination_token"]
    with pytest.raises(ValueError, match="invalid or expired"):
        capsule.consume(workbench_token)


def test_legacy_bundle_is_adapter_to_revisioned_service(tmp_path):
    api, project_path, state, _ = _workbench_api(tmp_path)

    result = api._proj_report_bundle_for_path(
        project_path,
        str(tmp_path / "legacy"),
        formats=["html"],
        final=False,
        requested_kind="diagnostic",
    )

    assert result["ok"] is True
    assert result["revision"]["sequence"] == 1
    assert state["project"]["autopilot_report"]["revision"] == result["revision"]
    history = api.report_workbench_history(_project_id(api, project_path))
    assert len(history["revisions"]) == 1


def test_public_legacy_bundle_rejects_unrecorded_mode_before_render(tmp_path):
    api, project_path, state, _ = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    api._reports = lambda: (_ for _ in ()).throw(
        AssertionError("report service must not be reached"))

    result = api.proj_report_bundle(
        project_id,
        str(tmp_path / "must-not-exist"),
        formats=["html"],
        record_artifact=False,
    )

    assert result["ok"] is False
    assert result["artifact_status"] == "failed"
    assert result["files"] == {} and result["marker"] is None
    assert result["record_artifact_required"] is True
    assert state["saved"] == []
    assert not (tmp_path / "must-not-exist").exists()


def test_scoped_workbench_marker_replays_frozen_scope_for_current_status(tmp_path):
    api, project_path, state, manifests = _workbench_api(tmp_path)
    second = str(tmp_path / "config-second")
    Path(second).mkdir()
    state["project"]["members"]["configs"].append(second)
    manifests[second] = {
        "job_uuid": "job-config-second",
        "state": "DONE",
        "attempts": [{"n": 1, "attempt_token": "config-second-a1"}],
        "results": {"energy_e0_eV": -113.0, "fetched_sha256": {"OUTCAR": "d" * 64}},
    }
    state["summary"]["rows"].append({
        "name": "demo_ads_Li2S6",
        "configuration_id": "job-config-second",
        "species": "Li2S6",
        "job": second,
        "state": "DONE",
        "e_config": -113.0,
        "e_slab": -100.0,
        "e_ref": -10.0,
        "delta_e": -3.0,
        "dd_e": 0.0,
        "reference_valid": True,
        "reference_state": "DONE",
        "method_status": "verified",
        "note": "",
    })
    # Real adsorption.delta_e_rows rows expose the managed-directory basename,
    # not the local job path.  The workbench must still resolve that basename to
    # the server-owned opaque configuration id.
    state["summary"]["rows"][0]["name"] = Path(
        state["project"]["members"]["configs"][0]
    ).name
    state["summary"]["rows"][1]["name"] = Path(second).name
    state["summary"]["rows"][0].pop("job", None)
    state["summary"]["rows"][1].pop("job", None)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    request = _html_request(boot["project_id"], requested_kind="final")
    request["spec"]["scope"]["configuration_ids"] = ["job-config"]
    preview = api.report_workbench_preview(project_id, request)

    assert preview["ok"] is True
    assert preview["report_spec"]["scope"]["configuration_ids"] == ["job-config"]
    resolved = preview["public_snapshot"]["resolved_scope"]
    assert resolved["selected_configuration_ids"] == ["job-config"]
    assert resolved["dependency_job_ids"] == ["job-clean", "job-reference"]
    assert resolved["excluded_configuration_ids"] == ["job-config-second"]
    assert set(resolved["job_ids"]) == {
        "job-clean", "job-reference", "job-config",
    }
    adsorption = preview["public_snapshot"]["payload"]["adsorption_summary"]
    assert {item["member_id"] for item in adsorption["members"]} == {
        "job-clean", "job-reference", "job-config",
    }
    assert [row["configuration_id"] for row in adsorption["rows"]] == ["job-config"]

    published = _publish(api, project_path, tmp_path / "scoped", preview)
    status = api.proj_report_status(project_id)

    assert published["ok"] is True
    assert status["ok"] is True
    assert status["artifact_status"] == "ready"
    assert status["artifact_current"] is True
    assert status["revision"]["sequence"] == 1


def test_english_precision_and_theme_are_visible_content_bindings(tmp_path):
    api, project_path, state, _ = _workbench_api(tmp_path)
    state["summary"]["rows"][0]["delta_e"] = -5.123456789
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    base = _html_request(
        boot["project_id"],
        requested_kind="final",
        locale="en-US",
        outline=[
            "executive_summary", "key_findings", "candidate_evaluations",
            "adsorption_table", "figures", "methods", "limitations",
            "recommendations",
        ],
        options={"precision": 8},
        theme_id="diagnostic-a4",
    )

    diagnostic = api.report_workbench_preview(project_id, base)
    academic_request = copy.deepcopy(base)
    academic_request["spec"]["theme_id"] = "academic-a4"
    academic_request["operation_id"] = "preview-theme-2"
    academic = api.report_workbench_preview(project_id, academic_request)

    assert diagnostic["ok"] is True and academic["ok"] is True
    assert "-5.12345679" in diagnostic["html"]
    assert re.search(r"[\u4e00-\u9fff]", diagnostic["html"]) is None
    assert diagnostic["report_spec"]["options"]["precision"] == 8
    assert diagnostic["report_spec"]["template_ref"]["id"] == (
        "vcstudio-diagnostic-a4")
    assert academic["report_spec"]["template_ref"]["id"] == (
        "vcstudio-academic-a4")
    assert diagnostic["report_model_sha256"] != academic["report_model_sha256"]


def test_history_reaudits_sidecars_and_exact_revision(tmp_path):
    api, project_path, _, _ = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    preview = api.report_workbench_preview(
        project_id, _html_request(boot["project_id"], requested_kind="final")
    )
    published = _publish(api, project_path, tmp_path / "history", preview)
    assert published["ok"] is True
    history_path = tmp_path / ".vcstudio" / "reports" / "history.json"
    raw = json.loads(history_path.read_text(encoding="utf-8"))
    entry = next(iter(raw["reports"].values()))["revisions"][0]

    audit = api._report_workbench_validate_history_entry(copy.deepcopy(entry))
    assert audit["ok"] is True and audit["current"] is True
    assert audit["scientific_status"] == entry["scientific_status"]
    assert audit["scientific_qualification"] == entry["scientific_qualification"]

    wrong_revision = copy.deepcopy(entry)
    wrong_revision["sequence"] = 99
    rejected = api._report_workbench_validate_history_entry(wrong_revision)
    assert rejected["ok"] is False and rejected["current"] is False
    assert "revision" in rejected["error"]

    Path(entry["contract_files"]["validation"]).write_text(
        "{}", encoding="utf-8")
    history = api.report_workbench_history(project_id)
    assert history["ok"] is True and len(history["revisions"]) == 1
    assert history["revisions"][0]["artifact_status"] == "stale"
    assert history["revisions"][0]["current"] is False


def test_current_status_rejects_marker_revision_that_conflicts_with_manifest(tmp_path):
    api, project_path, state, _ = _workbench_api(tmp_path)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "diagnostic-repair")
    preview = api.report_workbench_preview(
        project_id, _html_request(boot["project_id"], requested_kind="final")
    )
    published = _publish(api, project_path, tmp_path / "out", preview)
    assert published["ok"] is True
    state["project"]["autopilot_report"]["revision"]["sequence"] = 99

    status = api.proj_report_status(project_id)

    assert status["artifact_status"] == "stale"
    assert status["artifact_current"] is False
    assert "revision" in status["report_reason"]
