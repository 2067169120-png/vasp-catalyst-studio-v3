from __future__ import annotations

import json
from pathlib import Path

from vcstudio.project.analysis_registry import normalize_analysis_request
from vcstudio.project.analysis_sources import (
    build_property_view,
    build_task_analysis_view,
    resolve_project_targets,
)


def _manifest(path: Path, task_type: str, *, parent=None, state="DONE"):
    value = {
        "job_uuid": f"uuid-{path.name}", "task_type": task_type,
        "state": state, "inputs": {"engine": "vasp"},
    }
    if parent is not None:
        value["parent_job"] = str(parent)
    path.mkdir(parents=True)
    (path / "job.yaml").write_text(json.dumps(value), encoding="utf-8")
    return value


def test_resolver_accepts_members_and_ledger_descendants_but_not_nearby_jobs(tmp_path):
    member = tmp_path / "member"
    child = tmp_path / "derived-bands"
    grandchild = tmp_path / "derived-bader"
    sibling = tmp_path / "unrelated"
    manifests = {
        str(member): _manifest(member, "relax"),
        str(child): _manifest(child, "bands", parent=member),
        str(grandchild): _manifest(grandchild, "bader", parent=child),
        str(sibling): _manifest(sibling, "bands"),
    }
    project = {"members": {"clean_slab": str(member), "configs": []}}

    targets = resolve_project_targets(
        project,
        [(path, manifest) for path, manifest in manifests.items() if path != str(member)],
        manifest_loader=lambda path: manifests.get(str(path)),
        opaque_id=lambda _path, manifest: manifest["job_uuid"],
    )

    assert [target["source_id"] for target in targets] == [
        "uuid-derived-bader", "uuid-derived-bands", "uuid-member"]
    assert next(target for target in targets if target["source_id"] ==
                "uuid-derived-bands")["relation"] == "descendant"
    assert "uuid-unrelated" not in {target["source_id"] for target in targets}


def test_electronic_view_projects_server_numbers_hashes_and_parser_identity(tmp_path):
    job = tmp_path / "bands"
    manifest = _manifest(job, "bands")
    (job / "EIGENVAL").write_text("authoritative-eigenval", encoding="utf-8")
    target = {
        "path": str(job), "path_key": str(job).lower(),
        "source_id": "job-bands-opaque", "relation": "descendant",
        "task_type": "bands", "state": "DONE", "manifest": manifest,
    }
    spec = normalize_analysis_request(
        {"analysis_id": "electronic-structure", "precision": 6},
        project_id="project-opaque",
    )

    view = build_task_analysis_view(
        spec, [target],
        runner=lambda _path, _kind: {
            "ok": True, "result": {
                "gap": {"value": 1.23456789, "direct": True, "metal": False}},
            "summary": "parser finalized band gap", "error": None,
        },
        parser_identities={
            "bands": {"module": "vcstudio.project.bands",
                      "callable": "parse_bands", "version": "4.0-test",
                      "version_source": "test-release"}},
        method_evidence=lambda _target: {
            "status": "verified", "fingerprint": "method-sha256"},
    )

    assert view["available"] is True
    assert view["scientific_status"] == "verified"
    assert view["capability_status"] == "available"
    assert view["denominator"] == {
        "resolved_targets": 1, "matching_targets": 1,
        "parser_ready_targets": 1, "available_results": 1,
        "blocked_results": 0, "evidence_files": 2, "visible_rows": 1,
    }
    row = view["rows"][0]
    assert row["values"][0]["value"] == 1.23456789
    assert row["values"][0]["display"] == "1.234568"
    assert row["parser"] == {
        "module": "vcstudio.project.bands", "callable": "parse_bands",
        "version": "4.0-test", "version_source": "test-release",
    }
    assert {item["name"] for item in row["source"]["files"]} == {
        "job.yaml", "EIGENVAL"}
    encoded = json.dumps(view, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "path" not in row["source"]


def test_elf_quantitative_distribution_parser_is_available_without_topology_claim(tmp_path):
    job = tmp_path / "elf"
    manifest = _manifest(job, "elf")
    (job / "ELFCAR").write_text(
        "ELF\n1\n1 0 0\n0 1 0\n0 0 1\nSi\n1\nDirect\n0 0 0\n\n"
        "2 1 1\n0.25 0.75\n",
        encoding="utf-8")
    target = {
        "path": str(job), "path_key": str(job).lower(), "source_id": "job-elf",
        "relation": "descendant", "task_type": "elf", "state": "DONE",
        "manifest": manifest,
    }
    spec = normalize_analysis_request(
        {"analysis_id": "charge-wavefunction"}, project_id="project-opaque")

    view = build_task_analysis_view(
        spec, [target], runner=lambda path, _kind: __import__(
            "vcstudio.project.task_analysis", fromlist=["analyze_elf"]
        ).analyze_elf(path),
        parser_identities={"elf": {
            "module": "vcstudio.project.elf", "callable": "summarize_elfcar",
            "version": "4.0-test", "version_source": "test-release"}},
        method_evidence=lambda _target: {"status": "verified"},
    )

    assert view["available"] is True
    assert view["scientific_status"] == "verified"
    assert view["capability_status"] == "available"
    assert view["rows"][0]["status"] == "available"
    assert view["rows"][0]["result"]["analysis_kind"] == "elf_distribution_summary"
    assert view["rows"][0]["values"][2]["display"] == "0.2500"
    assert view["rows"][0]["result"]["denominator"]["grid_points"] == 2
    assert view["rows"][0]["result"]["display_quantiles"][3] == {
        "fraction": "0.5000", "value": "0.5000"}
    assert sum(int(item["count"]) for item in
               view["rows"][0]["result"]["display_histogram"]) == 2
    assert "topology conclusion" in view["rows"][0]["result"]["interpretation"]
    assert view["denominator"]["available_results"] == 1


def test_elf_missing_file_and_non_done_are_missing_prerequisites(tmp_path):
    job = tmp_path / "elf"
    manifest = _manifest(job, "elf", state="RUNNING")
    target = {
        "path": str(job), "path_key": str(job).lower(), "source_id": "job-elf",
        "relation": "descendant", "task_type": "elf", "state": "RUNNING",
        "manifest": manifest,
    }
    spec = normalize_analysis_request(
        {"analysis_id": "charge-wavefunction"}, project_id="project-opaque")
    view = build_task_analysis_view(
        spec, [target], runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("non-DONE parser must not run")),
        parser_identities={"elf": {
            "module": "vcstudio.project.elf", "callable": "summarize_elfcar",
            "version": "4.0-test", "version_source": "test-release"}},
        method_evidence=lambda _target: {"status": "verified"},
    )
    assert view["available"] is False
    assert view["capability_status"] == "missing_prerequisite"
    assert view["rows"][0]["status"] == "missing_prerequisite"


def test_property_view_keeps_calculator_operands_opaque_and_numbers_server_finalized(
        tmp_path):
    spec = normalize_analysis_request(
        {"analysis_id": "property-calculators", "precision": 5},
        project_id="project-opaque")

    targets = []
    for source_id in ("job-vac", "job-sol"):
        root = tmp_path / source_id
        manifest = _manifest(root, "vaspsol")
        targets.append({
            "path": str(root), "path_key": str(root).lower(),
            "source_id": source_id, "relation": "descendant",
            "task_type": "vaspsol", "state": "DONE", "manifest": manifest,
        })
    view = build_property_view(
        spec, targets, parser_version="4.0-test",
        runner=lambda kind, _targets: ([{
            "ok": True, "source_ids": ["job-vac", "job-sol"],
            "solvation_energy_eV": -0.3456789,
            "parser_module": "vcstudio.project.vaspsol",
            "parser_callable": "analyze_pair",
            "summary": "verified pair",
        }] if kind == "vaspsol" else []),
    )

    assert view["available"] is True
    row = view["rows"][0]
    assert row["source_ids"] == ["job-vac", "job-sol"]
    assert row["values"][0]["value"] == -0.3456789
    assert row["values"][0]["display"] == "-0.34568"
    assert view["denominator"]["available_results"] == 1
