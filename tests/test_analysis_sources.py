from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import vcstudio.project.analysis_sources as analysis_sources
from vcstudio.project.analysis_registry import normalize_analysis_request
from vcstudio.project.analysis_sources import (
    SourceSnapshotChanged,
    build_property_view,
    build_task_analysis_view,
    capture_source_snapshot,
    resolve_project_targets,
    value_provenance,
    verify_neb_endpoint_record,
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


def test_task_analysis_rejects_over_limit_evidence_before_runner(
        tmp_path, monkeypatch):
    job = tmp_path / "elf-over-limit"
    manifest = _manifest(job, "elf")
    manifest_size = (job / "job.yaml").stat().st_size
    limit = manifest_size + 8
    (job / "ELFCAR").write_bytes(b"x" * (limit + 1))
    target = {
        "path": str(job), "path_key": str(job).lower(),
        "source_id": "job-elf-over-limit", "relation": "descendant",
        "task_type": "elf", "state": "DONE", "manifest": manifest,
    }
    spec = normalize_analysis_request(
        {"analysis_id": "charge-wavefunction"}, project_id="project-opaque")
    monkeypatch.setattr(analysis_sources, "MAX_EVIDENCE_FILE_BYTES", limit)

    view = build_task_analysis_view(
        spec, [target],
        runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("rejected evidence must not reach the runner")),
        parser_identities={"elf": {
            "module": "vcstudio.project.elf", "callable": "summarize_elfcar",
            "version": "4.0-test", "version_source": "test-release"}},
        method_evidence=lambda _target: {"status": "verified"},
    )

    assert view["available"] is False
    assert view["capability_status"] == "unavailable"
    assert view["rows"][0]["status"] == "unavailable"
    assert "bounded evidence rejected" in view["rows"][0]["summary"]


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


def test_property_runner_uses_materialized_operands_and_retries_source_mutation(tmp_path):
    root = tmp_path / "property-source"
    manifest = _manifest(root, "vaspsol")
    (root / "OSZICAR").write_text(
        " 1 F= -1 E0= -1 d E=0\n", encoding="utf-8")
    target = {
        "path": str(root), "path_key": str(root).lower(),
        "source_id": "job-property-source", "relation": "descendant",
        "task_type": "vaspsol", "state": "DONE", "manifest": manifest,
    }
    spec = normalize_analysis_request(
        {"analysis_id": "property-calculators"}, project_id="project-opaque")

    def runner(kind, materialized_targets):
        if kind != "vaspsol":
            return []
        parser_root = Path(materialized_targets[0]["path"])
        assert parser_root != root
        assert (parser_root / "OSZICAR").read_text(
            encoding="utf-8") == " 1 F= -1 E0= -1 d E=0\n"
        (root / "OSZICAR").write_text(
            " 1 F= -99 E0= -99 d E=0\n", encoding="utf-8")
        return [{
            "ok": True, "source_ids": ["job-property-source"],
            "solvation_energy_eV": 98.0,
            "parser_module": "vcstudio.project.vaspsol",
            "parser_callable": "analyze_pair",
        }]

    with pytest.raises(SourceSnapshotChanged, match="changed during analysis"):
        build_property_view(
            spec, [target], runner=runner, parser_version="4.0-test")


def test_value_provenance_keeps_only_hashed_files_and_explicit_parser_identity():
    value = value_provenance(
        "job-opaque",
        [
            {"name": "OSZICAR", "sha256": "A" * 64},
            {"name": "OUTCAR", "sha256": "not-a-hash"},
        ],
        {"module": "parser.module", "callable": "parse", "version": "4.1"},
    )

    assert value == {
        "source_id": "job-opaque",
        "file_hashes": [{"name": "OSZICAR", "sha256": "a" * 64}],
        "parser_module": "parser.module",
        "parser_callable": "parse",
        "parser_version": "4.1",
    }


def test_source_snapshot_hashes_and_parses_one_copy_then_detects_replacement(tmp_path):
    job = tmp_path / "immutable"
    manifest = _manifest(job, "aimd")
    original = b" 1 T= 300 E= -10.0\n"
    (job / "OSZICAR").write_bytes(original)
    target = {
        "path": str(job), "source_id": "job-immutable", "relation": "member",
        "task_type": "aimd", "state": "DONE", "manifest": manifest,
    }

    snapshot = capture_source_snapshot(target, ["OSZICAR"])
    evidence = snapshot.file("OSZICAR")
    captured_stat = (job / "OSZICAR").stat()

    assert snapshot.bytes("OSZICAR") == original
    assert snapshot.text("OSZICAR") is snapshot.text("OSZICAR")
    assert evidence["sha256"] == hashlib.sha256(original).hexdigest()
    replacement = b" 1 T= 999 E= -99.0\n"
    assert len(replacement) == len(original)
    (job / "OSZICAR").write_bytes(replacement)
    os.utime(
        job / "OSZICAR",
        ns=(captured_stat.st_atime_ns, captured_stat.st_mtime_ns),
    )
    assert snapshot.bytes("OSZICAR") == original
    with pytest.raises(SourceSnapshotChanged, match="changed during analysis"):
        snapshot.assert_unchanged()


def test_task_parser_consumes_materialized_snapshot_and_retries_source_mutation(tmp_path):
    job = tmp_path / "bands-snapshot"
    manifest = _manifest(job, "bands")
    (job / "vasprun.xml").write_text("OLD", encoding="utf-8")
    target = {
        "path": str(job), "path_key": str(job).lower(),
        "source_id": "job-bands-snapshot", "relation": "descendant",
        "task_type": "bands", "state": "DONE", "manifest": manifest,
    }
    spec = normalize_analysis_request(
        {"analysis_id": "electronic-structure"}, project_id="project-opaque")

    def runner(snapshot_root, _kind):
        assert (Path(snapshot_root) / "vasprun.xml").read_text(
            encoding="utf-8") == "OLD"
        (job / "vasprun.xml").write_text("NEW", encoding="utf-8")
        return {
            "ok": True, "result": {
                "gap": {"value": 99.0, "direct": True, "metal": False}},
            "summary": "must be retried",
        }

    with pytest.raises(SourceSnapshotChanged, match="changed during analysis"):
        build_task_analysis_view(
            spec, [target], runner=runner,
            parser_identities={"bands": {
                "module": "vcstudio.project.bands", "callable": "parse_bands",
                "version": "4.0-test", "version_source": "test-release"}},
            method_evidence=lambda _target, snapshot: {
                "status": "verified",
                "snapshot_sha256": snapshot.file("vasprun.xml")["sha256"],
            },
        )


def test_task_and_property_views_retry_ledger_job_yaml_divergence(tmp_path):
    task_root = tmp_path / "task-manifest-mismatch"
    task_manifest = _manifest(task_root, "bands")
    (task_root / "vasprun.xml").write_text("bands", encoding="utf-8")
    (task_root / "job.yaml").write_text(
        json.dumps({**task_manifest, "state": "FAILED"}), encoding="utf-8")
    task_target = {
        "path": str(task_root), "path_key": str(task_root).lower(),
        "source_id": "task-manifest-mismatch", "relation": "descendant",
        "task_type": "bands", "state": "DONE", "manifest": task_manifest,
    }
    task_spec = normalize_analysis_request(
        {"analysis_id": "electronic-structure"}, project_id="project-opaque")
    with pytest.raises(SourceSnapshotChanged, match="ledger manifest"):
        build_task_analysis_view(
            task_spec, [task_target],
            runner=lambda *_args: (_ for _ in ()).throw(
                AssertionError("mismatched manifest must not reach task runner")),
            parser_identities={"bands": {
                "module": "vcstudio.project.bands", "callable": "parse_bands",
                "version": "4.0-test", "version_source": "test-release"}},
            method_evidence=lambda _target: {"status": "verified"},
        )

    property_root = tmp_path / "property-manifest-mismatch"
    property_manifest = _manifest(property_root, "vaspsol")
    (property_root / "job.yaml").write_text(
        json.dumps({**property_manifest, "state": "FAILED"}), encoding="utf-8")
    property_target = {
        "path": str(property_root), "path_key": str(property_root).lower(),
        "source_id": "property-manifest-mismatch", "relation": "descendant",
        "task_type": "vaspsol", "state": "DONE", "manifest": property_manifest,
    }
    property_spec = normalize_analysis_request(
        {"analysis_id": "property-calculators"}, project_id="project-opaque")
    with pytest.raises(SourceSnapshotChanged, match="ledger manifest"):
        build_property_view(
            property_spec, [property_target],
            runner=lambda *_args: (_ for _ in ()).throw(
                AssertionError("mismatched manifest must not reach property runner")),
            parser_version="4.0-test",
        )


def test_source_snapshot_rejects_nonregular_and_symlink_evidence(tmp_path):
    job = tmp_path / "unsafe-evidence"
    manifest = _manifest(job, "aimd")
    (job / "OUTCAR").mkdir()
    outside = tmp_path / "outside-OSZICAR"
    outside.write_text(" 1 T= 300 E= -10\n", encoding="utf-8")
    symlink_supported = True
    try:
        (job / "OSZICAR").symlink_to(outside)
    except OSError:
        symlink_supported = False
    target = {
        "path": str(job), "source_id": "job-unsafe", "relation": "member",
        "task_type": "aimd", "state": "DONE", "manifest": manifest,
    }

    evidence = ["OUTCAR"]
    if symlink_supported:
        evidence.append("OSZICAR")
    snapshot = capture_source_snapshot(target, evidence)

    assert snapshot.bytes("OUTCAR") == b""
    expected = {"OUTCAR", "OSZICAR"} if symlink_supported else {"OUTCAR"}
    assert {item["name"] for item in snapshot.rejected} == expected
    if symlink_supported:
        assert snapshot.bytes("OSZICAR") == b""
    assert all("no-follow regular file" in item["reason"]
               for item in snapshot.rejected)


def test_source_snapshot_enforces_per_file_total_line_and_frame_limits(
        tmp_path, monkeypatch):
    def target_for(name: str) -> tuple[Path, dict]:
        job = tmp_path / name
        manifest = _manifest(job, "aimd")
        return job, {
            "path": str(job), "source_id": f"job-{name}", "relation": "member",
            "task_type": "aimd", "state": "DONE", "manifest": manifest,
        }

    job, target = target_for("per-file")
    manifest_size = (job / "job.yaml").stat().st_size
    per_file_limit = manifest_size + 8
    (job / "OUTCAR").write_bytes(b"x" * (per_file_limit + 1))
    with monkeypatch.context() as scoped:
        scoped.setattr(analysis_sources, "MAX_EVIDENCE_FILE_BYTES", per_file_limit)
        snapshot = capture_source_snapshot(target, ["OUTCAR"])
    assert any("per-file byte limit" in item["reason"]
               for item in snapshot.rejected)

    job, target = target_for("total")
    manifest_size = (job / "job.yaml").stat().st_size
    (job / "OSZICAR").write_bytes(b"1234567890")
    with monkeypatch.context() as scoped:
        scoped.setattr(
            analysis_sources, "MAX_EVIDENCE_TOTAL_BYTES", manifest_size + 2)
        snapshot = capture_source_snapshot(target, ["OSZICAR"])
    assert any("remaining total byte limit" in item["reason"]
               for item in snapshot.rejected)

    job, target = target_for("lines")
    (job / "OSZICAR").write_text("one\ntwo\n", encoding="utf-8")
    with monkeypatch.context() as scoped:
        scoped.setattr(analysis_sources, "MAX_EVIDENCE_LINES", 1)
        snapshot = capture_source_snapshot(target, ["OSZICAR"])
    assert any("line limit" in item["reason"] for item in snapshot.rejected)

    job, target = target_for("bare-cr-lines")
    (job / "OSZICAR").write_bytes(b"a\rb\rc")
    with monkeypatch.context() as scoped:
        scoped.setattr(analysis_sources, "MAX_EVIDENCE_LINES", 2)
        scoped.setattr(analysis_sources, "_READ_CHUNK_BYTES", 2)
        snapshot = capture_source_snapshot(target, ["OSZICAR"])
    assert any("line limit" in item["reason"] for item in snapshot.rejected)

    job, target = target_for("split-crlf-lines")
    (job / "OSZICAR").write_bytes(b"a\r\nb")
    with monkeypatch.context() as scoped:
        scoped.setattr(analysis_sources, "MAX_EVIDENCE_LINES", 2)
        scoped.setattr(analysis_sources, "_READ_CHUNK_BYTES", 2)
        snapshot = capture_source_snapshot(target, ["OSZICAR"])
    assert snapshot.bytes("OSZICAR") == b"a\r\nb"
    assert not any("line limit" in item["reason"] for item in snapshot.rejected)

    splitline_separators = (
        "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
    )
    for index, separator in enumerate(splitline_separators):
        job, target = target_for(f"python-splitlines-{index}")
        text = f"a{separator}b{separator}c"
        assert len(text.splitlines()) == 3
        (job / "OSZICAR").write_bytes(text.encode("utf-8"))
        with monkeypatch.context() as scoped:
            scoped.setattr(analysis_sources, "MAX_EVIDENCE_LINES", 2)
            scoped.setattr(analysis_sources, "_READ_CHUNK_BYTES", 1)
            snapshot = capture_source_snapshot(target, ["OSZICAR"])
        assert any("line limit" in item["reason"] for item in snapshot.rejected)

    job, target = target_for("frames")
    (job / "XDATCAR").write_text(
        "H trajectory\n1\n1 0 0\n0 1 0\n0 0 1\nH\n1\n"
        "Direct configuration= 1\n0 0 0\n"
        "Direct configuration= 2\n0 0 0\n",
        encoding="utf-8",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(analysis_sources, "MAX_XDATCAR_FRAMES", 1)
        snapshot = capture_source_snapshot(target, ["XDATCAR"])
    assert any("frame limit" in item["reason"] for item in snapshot.rejected)

    job, target = target_for("bare-cr-frames")
    (job / "XDATCAR").write_bytes(
        b"H trajectory\r1\r1 0 0\r0 1 0\r0 0 1\rH\r1\r"
        b"Direct configuration= 1\r0 0 0\r"
        b"Direct configuration= 2\r0 0 0\r"
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(analysis_sources, "MAX_XDATCAR_FRAMES", 1)
        scoped.setattr(analysis_sources, "_READ_CHUNK_BYTES", 1)
        snapshot = capture_source_snapshot(target, ["XDATCAR"])
    assert any("frame limit" in item["reason"] for item in snapshot.rejected)

    job, target = target_for("nbsp-frames")
    (job / "XDATCAR").write_text(
        "H trajectory\n1\n1 0 0\n0 1 0\n0 0 1\nH\n1\n"
        "\N{NO-BREAK SPACE}Direct configuration= 1\n0 0 0\n"
        "\N{NO-BREAK SPACE}Direct configuration= 2\n0 0 0\n",
        encoding="utf-8",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(analysis_sources, "MAX_XDATCAR_FRAMES", 1)
        snapshot = capture_source_snapshot(target, ["XDATCAR"])
    assert not any("frame limit" in item["reason"] for item in snapshot.rejected)
    from vcstudio.project.analysis_scientific import _parse_xdatcar
    parsed = _parse_xdatcar(snapshot.text("XDATCAR"))
    assert parsed["frames"] == []
    assert "unexpected content" in parsed["error"]


@pytest.mark.parametrize("outcar", [
    b" energy(sigma->0) = -10.0\n",
    (b" General timing and accounting informations for this job:\n"
     b" aborting loop because EDIFF is reached\n"
     b" energy(sigma->0) = -10.0\n"),
])
def test_neb_endpoint_requires_terminal_current_outcar_footer_even_with_vasprun(
        tmp_path, outcar):
    source = tmp_path / "endpoint-source"
    source_manifest = _manifest(source, "relax")
    source_manifest["results"] = {"diagnosis": {
        "failure_class": "CONVERGED", "exit_code": 0, "clean_exit": True,
    }}
    (source / "job.yaml").write_text(
        json.dumps(source_manifest), encoding="utf-8")
    endpoint_files = {
        "POSCAR": b"H\n1\n1 0 0\n0 1 0\n0 0 1\nH\n1\nDirect\n0 0 0\n",
        "OSZICAR": b" 1 F= -10 E0= -10 d E=0\n",
        "OUTCAR": outcar,
    }
    for name, data in endpoint_files.items():
        (source / name).write_bytes(data)
    (source / "vasprun.xml").write_text(
        "<modeling><calculation></calculation></modeling>", encoding="utf-8")
    source_target = {
        "path": str(source), "source_id": "endpoint-source",
        "relation": "descendant", "task_type": "relax", "state": "DONE",
        "manifest": source_manifest,
    }

    neb = tmp_path / "neb"
    neb_manifest = _manifest(neb, "neb")
    frame = neb / "00"
    frame.mkdir()
    for name, data in endpoint_files.items():
        (frame / name).write_bytes(data)
    neb_target = {
        "path": str(neb), "source_id": "neb-source", "relation": "member",
        "task_type": "neb", "state": "DONE", "manifest": neb_manifest,
    }
    neb_snapshot = capture_source_snapshot(
        neb_target, ["00/POSCAR", "00/OSZICAR", "00/OUTCAR"])
    record = {
        "target_frame": "00", "source_job_id": "endpoint-source",
        "files": [
            {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in endpoint_files.items()
        ],
    }
    fingerprint = {"encut": 500}

    result = verify_neb_endpoint_record(
        record=record, role="start", frame="00", neb_snapshot=neb_snapshot,
        targets_by_source_id={"endpoint-source": source_target},
        method_resolver=lambda _target, _snapshot: {
            "status": "verified", "fingerprint": fingerprint},
        neb_method_fingerprint=fingerprint,
    )

    assert result["ok"] is False
    assert any("OUTCAR timing" in issue for issue in result["issues"])
