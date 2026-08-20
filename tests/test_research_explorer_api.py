from __future__ import annotations

import json
import os
from types import SimpleNamespace

from vcstudio.gui_web.api import Api
from vcstudio.project import adsorption
from vcstudio.project import energy_gate
from vcstudio.shared import manifest


def _registered_project(tmp_path):
    root = tmp_path / "project"
    job = root / "config"
    job.mkdir(parents=True)
    job_manifest = manifest.new_manifest(
        job_id="api-job-1", system="Pt4S", task_type="static", calc_type="slab",
        inputs={
            "engine": "vasp", "formula": "Pt4S", "facet": "111",
            "sha256": {"POSCAR": "a" * 64}, "source_id": "api-source-1",
        },
    )
    manifest.set_state(job_manifest, "DONE")
    job_manifest["results"] = {
        "energy_e0_eV": -123.4, "barrier_eV": 0.55,
        "hashes": {"OUTCAR": "b" * 64}, "parser": "vcstudio.energy",
        "validation": {"status": "passed"},
    }
    job_manifest["validation"] = {"status": "passed"}
    manifest.save_manifest(job, job_manifest)
    project = {
        "schema": 1,
        "name": "API project",
        "root": str(root),
        "project_uuid": "0123456789abcdef0123456789abcdef",
        "formula": "Pt4S",
        "facet": "111",
        "members": {"clean_slab": None, "gas_ref": None, "configs": [str(job)]},
        "config_species": {str(job): "Li2S8"},
    }
    path = adsorption.save_project(root, project)
    assert adsorption.register_project(path)
    return "project-0123456789abcdef0123456789abcdef"


def test_api_bootstrap_query_rebuild_save_and_live_provenance_are_path_free(tmp_path):
    project_id = _registered_project(tmp_path)
    api = Api()
    api._analysis_workbench_method_evidence = lambda _target: {
        "status": "verified", "engine": "vasp",
        "schema": "vcstudio.method-fingerprint/vasp/v1",
        "fingerprint": {"functional": "PBE", "dispersion": "D3"},
        "missing": [],
    }

    boot = api.research_explorer_bootstrap()

    assert boot["ok"] is True
    assert boot["status"] == "ready"
    assert boot["contracts"] == {
        "index_is_authority": False,
        "scientific_values_server_finalized": True,
        "default_method_compatible": True,
        "live_graph_kind": "live_derived",
        "frozen_report_graph_separate": True,
    }
    assert boot["freshness"]["registry_total"] == 1
    assert boot["table"]["sample_count"] == 1
    row = boot["table"]["rows"][0]
    assert row["project_id"] == project_id
    assert row["job_id"] == "api-job-1"
    assert row["energy_eV"] == -123.4
    assert row["barrier_eV"] == 0.55
    assert row["units"] == {"energy_eV": "eV", "barrier_eV": "eV"}

    views = boot["saved_views"]
    saved = api.research_view_save({
        "id": "api-screen", "name": "API screen",
        "filters": {"elements": ["Pt"], "method_compatible": True},
        "sort": {"key": "energy_eV", "direction": "asc"},
        "axes": {"x": "energy_eV", "y": "barrier_eV"},
    }, views["authority_id"], views["revision"])
    assert saved["ok"] is True
    assert saved["revision"] == 1

    queried = api.research_explorer_query({
        "filters": {"project_ids": [project_id], "method_compatible": True},
        "sort": {"key": "job", "direction": "asc"},
        "axes": {"x": "energy_eV", "y": "barrier_eV"},
        "limit": 25,
    })
    assert queried["ok"] is True
    assert queried["table"]["sample_count"] == 1
    assert queried["histogram"]["unit"] == "eV"
    assert queried["scatter"]["units"] == {
        "energy_eV": "eV", "barrier_eV": "eV"}

    graph = api.research_explorer_provenance(
        project_id, row["job_id"], row["source_id"])
    assert graph["ok"] is True
    assert graph["graph_kind"] == "live_derived"
    assert graph["report_frozen_graph_included"] is False
    assert {node["layer"] for node in graph["nodes"]} == {"data", "logical"}

    rebuilt = api.research_explorer_rebuild({"limit": 10})
    assert rebuilt["rebuild"] == {
        "performed": True, "fact_source_changed": False,
        "remote_side_effects": False,
    }

    encoded = json.dumps(
        {"boot": boot, "saved": saved, "query": queried,
         "graph": graph, "rebuild": rebuilt}, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "project.yaml" not in encoded


def test_api_rejects_path_identity_and_client_scientific_rows_without_echoing_them(tmp_path):
    _registered_project(tmp_path)
    api = Api()

    invalid_project = api.research_explorer_provenance(
        str(tmp_path / "project" / "project.yaml"))
    invalid_query = api.research_explorer_query({
        "scientific_rows": [{"energy_eV": -999.0}]})

    assert invalid_project["ok"] is False
    assert invalid_project["status"] == "unavailable"
    assert invalid_query["ok"] is False
    assert invalid_query["status"] == "unavailable"
    encoded = json.dumps(
        {"project": invalid_project, "query": invalid_query}, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "-999" not in encoded


def test_non_vasp_editable_method_fields_cannot_create_verified_adapter_evidence():
    api = Api()

    evidence = api._analysis_workbench_method_evidence({
        "path": "unused",
        "source_id": "source-qe",
        "manifest": {
            "inputs": {
                "engine": "quantum-espresso",
                "method": {"functional": "PBE"},
                "method_fingerprint": "user-claims-this-is-complete",
            },
            "results": {"hashes": {"output": "a" * 64}},
        },
    })

    assert evidence["status"] == "unverified"
    assert evidence["engine"] == "quantum-espresso"
    assert evidence["schema"] == ""
    assert evidence["fingerprint"] == {}
    assert evidence["adapter"] is None
    assert "versioned server method adapter" in evidence["missing"][0]


def test_vasp_method_adapter_emits_an_engine_versioned_schema(monkeypatch):
    api = Api()
    monkeypatch.setattr(energy_gate, "method_record", lambda *_args: {
        "known": {key: True for key in (
            "functional", "dispersion", "encut", "spin",
            "u_values", "u_by_element", "kpoints_scheme", "potcar_ids")},
        "fingerprint": {"functional": "PBE", "encut": 520},
        "evidence_warnings": [],
    })

    evidence = api._analysis_workbench_method_evidence({
        "path": "unused", "source_id": "source-vasp",
        "manifest": {"inputs": {"engine": "vasp"}},
    })

    assert evidence["status"] == "verified"
    assert evidence["schema"] == "vcstudio.method-fingerprint/vasp/v1"
    assert evidence["fingerprint"]["schema"] == evidence["schema"]
    assert evidence["adapter"].endswith("/vasp/v1")


def test_source_hash_detects_equal_size_equal_mtime_replacement_and_retries_toctou(
        tmp_path, monkeypatch):
    _registered_project(tmp_path)
    api = Api()
    api._analysis_workbench_method_evidence = lambda _target: {
        "status": "verified", "engine": "vasp",
        "schema": "vcstudio.method-fingerprint/vasp/v1",
        "fingerprint": {"functional": "PBE", "dispersion": "D3"},
        "missing": [],
    }
    job_yaml = tmp_path / "project" / "config" / "job.yaml"
    first = api.research_explorer_query()
    first_snapshot = first["freshness"]["snapshot_id"]
    original_stat = job_yaml.stat()
    original = job_yaml.read_bytes()
    replaced = original.replace(b"-123.4", b"-223.4")
    assert len(replaced) == len(original) and replaced != original
    job_yaml.write_bytes(replaced)
    os.utime(job_yaml, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    second = api.research_explorer_query()
    assert second["table"]["rows"][0]["energy_eV"] == -223.4
    assert second["freshness"]["snapshot_id"] != first_snapshot

    # Mutate after the pre-build hash and after manifest read.  Post-build
    # revalidation must reject that publication and retry against new bytes.
    original_loader = api._manifest.load_manifest
    mutated = False

    def drifting_loader(path):
        nonlocal mutated
        value = original_loader(path)
        if not mutated and os.path.normcase(str(path)) == os.path.normcase(str(job_yaml.parent)):
            before = job_yaml.stat()
            current = job_yaml.read_bytes()
            changed = current.replace(b"-223.4", b"-923.4")
            assert len(changed) == len(current) and changed != current
            job_yaml.write_bytes(changed)
            os.utime(job_yaml, ns=(before.st_atime_ns, before.st_mtime_ns))
            mutated = True
        return value

    monkeypatch.setattr(api._adsorption, "delta_e_rows", lambda _project: {"rows": []})
    monkeypatch.setattr(api._manifest, "load_manifest", drifting_loader)
    final = api.research_explorer_rebuild()

    assert final["ok"] is True
    assert final["table"]["rows"][0]["energy_eV"] == -923.4
    assert mutated is True


def test_api_rejects_20000_member_project_before_manifest_or_summary_expansion(tmp_path):
    project_path = tmp_path / "project.yaml"
    project = {
        "name": "Huge API project", "project_uuid": "9" * 32,
        "members": {"clean_slab": None, "gas_ref": None, "configs": [
            str(tmp_path / f"config-{index:05d}") for index in range(20_000)]},
    }
    manifest_calls = 0
    summary_calls = 0

    def load_manifest(_path):
        nonlocal manifest_calls
        manifest_calls += 1
        return {}

    def delta_e_rows(_project):
        nonlocal summary_calls
        summary_calls += 1
        return {"rows": []}

    api = Api(
        adsorption_mod=SimpleNamespace(
            list_projects=lambda: [str(project_path)],
            load_project=lambda _path: project,
            delta_e_rows=delta_e_rows,
        ),
        manifest_mod=SimpleNamespace(load_manifest=load_manifest),
    )
    result = api.research_explorer_query({"limit": 1})

    assert result["ok"] is False
    assert result["status"] == "partial"
    assert manifest_calls == 0
    assert summary_calls == 0
    assert result["table"]["rows"] == []
