"""P1 regressions for strict input/output authority and reuse transactions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from vcstudio.generate.method_recipe import bind_method_recipe
from vcstudio.project import calculation_reuse as reuse
from vcstudio.project import result_import, reuse_verification
from vcstudio.shared import manifest as manifest_mod
from vcstudio.shared.execution_environment import (
    EXECUTION_ENVIRONMENT_AUTHORITY,
    EXECUTION_ENVIRONMENT_SCHEMA,
)
from vcstudio.shared.scientific_inputs import record_input_closure


POSCAR = """strict
1
8 0 0
0 8 0
0 0 8
C
1
Direct
0 0 0
"""
KPOINTS = """mesh
0
Gamma
1 1 1
0 0 0
"""
POTCAR = """TITEL = PAW_PBE C 08Apr2002
licensed-content-identity
"""
INCAR = """ENCUT = 500
EDIFF = 1E-6
EDIFFG = -0.05
NSW = 10
IBRION = 2
"""
OUTCAR = """ vasp.6.4.3
 NELM = 60 ; NSW = 10 ; EDIFFG = -0.05
 aborting loop because EDIFF is reached
 reached required accuracy - stopping structural energy minimisation
 FORCES: max atom, RMS 0.010
 energy(sigma->0) = -12.500000
 General timing and accounting informations for this job
"""
OSZICAR = """DAV:  1  -0.125E+02  0.1E-03
 1 F= -.12500000E+02 E0= -.12500000E+02 d E =0.0
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_job(
    root: Path, job_id: str, *, project_id: str = "project-a",
    state: str = "CREATED", task: str = "relax", incar: str = INCAR,
) -> dict:
    root.mkdir(parents=True)
    if task == "neb" and "IMAGES" not in incar:
        incar += "IMAGES = 1\n"
    (root / "INCAR").write_text(incar, encoding="utf-8")
    (root / "KPOINTS").write_text(KPOINTS, encoding="utf-8")
    (root / "POTCAR").write_text(POTCAR, encoding="utf-8")
    inputs = {
        "engine": "vasp", "project_uuid": project_id,
        "method_recipe": bind_method_recipe({"test_recipe": "strict-v1"}),
        "execution_environment": {
            "schema": EXECUTION_ENVIRONMENT_SCHEMA,
            "authority": EXECUTION_ENVIRONMENT_AUTHORITY,
            "engine": "vasp", "vasp_version": "6.4.3",
            "build_identity": "build-attestation-001",
            "evidence": {"kind": "test-attestation", "sha256": "e" * 64},
        },
    }
    if task == "neb":
        inputs["n_images"] = 1
        frames = ("00", "01", "02")
        for frame in frames:
            frame_dir = root / frame
            frame_dir.mkdir()
            (frame_dir / "POSCAR").write_text(POSCAR, encoding="utf-8")
    else:
        (root / "POSCAR").write_text(POSCAR, encoding="utf-8")
    results: dict = {}
    if state == "DONE":
        output_hashes = {}
        if task == "neb":
            for frame in ("00", "01", "02"):
                (root / frame / "OUTCAR").write_text(OUTCAR, encoding="utf-8")
                (root / frame / "OSZICAR").write_text(OSZICAR, encoding="utf-8")
                output_hashes[f"{frame}/OUTCAR"] = _sha(root / frame / "OUTCAR")
                output_hashes[f"{frame}/OSZICAR"] = _sha(root / frame / "OSZICAR")
        else:
            (root / "OUTCAR").write_text(OUTCAR, encoding="utf-8")
            (root / "OSZICAR").write_text(OSZICAR, encoding="utf-8")
            output_hashes = {"OUTCAR": _sha(root / "OUTCAR"),
                             "OSZICAR": _sha(root / "OSZICAR")}
        results = {
            "diagnosis": {
                "failure_class": "CONVERGED", "task_converged": True,
                "electronic_converged": True, "ionic_converged": True,
                "clean_exit": True, "exit_code": 0,
            },
            "fetched_sha256": output_hashes,
        }
    manifest = {
        "schema": 1, "job_id": job_id, "job_uuid": job_id,
        "project_uuid": project_id, "system": "C", "task_type": task,
        "calc_type": "slab", "created_at": "2026-08-20T00:00:00",
        "created_by": "vcstudio-test", "inputs": inputs, "cluster": None,
        "remote_dir": None, "scheduler_job_id": None, "state": state,
        "state_history": [{"state": state, "at": "2026-08-20T00:00:00"}],
        "attempts": [], "results": results, "warnings": [],
    }
    closure = record_input_closure(root, manifest)
    assert closure["schema"] == "vcstudio.scientific-input-closure/v1"
    manifest_mod.save_manifest(root, manifest)
    return manifest


def test_actual_input_closure_dependency_tamper_fails_closed(tmp_path):
    job = tmp_path / "job"
    _write_job(job, "job-closure", incar=INCAR + "ICHARG = 11\n")
    assert reuse.build_scientific_fingerprint(job)["status"] == "incomplete"

    (job / "CHGCAR").write_bytes(b"charge-density")
    manifest = manifest_mod.load_manifest(job)
    record_input_closure(job, manifest)
    manifest_mod.save_manifest(job, manifest)
    assert reuse.build_scientific_fingerprint(job)["status"] == "complete"
    (job / "CHGCAR").write_bytes(b"tampered-density")
    fingerprint = reuse.build_scientific_fingerprint(job)
    assert fingerprint["status"] == "incomplete"
    assert "current scientific input closure no longer matches job.yaml" in \
        fingerprint["integrity_issues"]


def test_neb_binds_every_image_structure_and_hash_bound_output(tmp_path):
    job = tmp_path / "neb"
    _write_job(job, "job-neb", task="neb", state="DONE")
    fingerprint = reuse.build_scientific_fingerprint(job)
    verification = reuse.source_verification(job, fingerprint)

    assert fingerprint["status"] == "complete"
    assert fingerprint["components"]["structure"]["summary"]["image_count"] == 3
    assert verification["reusable"] is True
    assert verification["parser"]["matrix"] == "all-images-complete"
    (job / "01" / "POSCAR").write_text(POSCAR.replace("0 0 0", "0.1 0 0", 1))
    assert reuse.build_scientific_fingerprint(job)["status"] == "incomplete"


def test_manifest_is_parsed_from_one_bounded_byte_snapshot(tmp_path, monkeypatch):
    job = tmp_path / "job"
    _write_job(job, "job-one-read")
    monkeypatch.setattr(manifest_mod, "load_manifest", lambda *_args: pytest.fail(
        "fingerprint must not re-open job.yaml through load_manifest"
    ))
    assert reuse.build_scientific_fingerprint(job)["status"] == "complete"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [("failed", True, "failed"), ("max_steps_hit", True, "unconverged"),
     ("electronic_converged", False, "unconverged"),
     ("ionic_converged", False, "unconverged"),
     ("clean_exit", False, "failed"), ("exit_code", 9, "failed")],
)
def test_explicit_contradiction_dominates_current_positive_output(
    tmp_path, field, value, expected,
):
    job = tmp_path / field
    manifest = _write_job(job, f"job-{field}", state="DONE")
    manifest["results"]["diagnosis"][field] = value
    manifest_mod.save_manifest(job, manifest)
    verification = reuse.source_verification(job)
    assert verification["status"] == expected
    assert verification["reusable"] is False


def test_output_parser_binding_and_result_copy_allowlist(tmp_path):
    job = tmp_path / "job"
    manifest = _write_job(job, "job-output", state="DONE")
    verification = reuse.source_verification(job)
    assert verification["reusable"] is True
    assert verification["parser"]["schema"] == "vcstudio.reuse-output-parser/v1"
    assert verification["parser"]["observed_vasp_version"] == "6.4.3"
    assert verification["parser"]["source_sha256"] == \
        manifest["results"]["fetched_sha256"]

    (job / "secret.key").write_text("must-not-copy")
    manifest["results"]["fetched_sha256"]["secret.key"] = _sha(job / "secret.key")
    manifest_mod.save_manifest(job, manifest)
    rejected = reuse.source_verification(job)
    assert rejected["reusable"] is False
    assert any("allowlisted" in issue for issue in rejected["issues"])


def test_observed_vasp_version_conflict_blocks_reuse(tmp_path):
    job = tmp_path / "job"
    manifest = _write_job(job, "job-version-conflict", state="DONE")
    manifest["inputs"]["execution_environment"]["vasp_version"] = "6.3.2"
    manifest_mod.save_manifest(job, manifest)

    verification = reuse.source_verification(job)

    assert verification["reusable"] is False
    assert verification["parser"]["observed_vasp_version"] == "6.4.3"
    assert any("conflicts with planned 6.3.2" in issue
               for issue in verification["issues"])


def test_target_generation_drift_remains_prepared_conflict(tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")

    def drift(_name, count):
        if count == 1:
            manifest = manifest_mod.load_manifest(target)
            manifest["warnings"].append("non-cooperating writer")
            manifest_mod.save_manifest(target, manifest)

    with pytest.raises(reuse.ReuseConflictError, match="target manifest drifted"):
        reuse.record_reuse_reference(
            target, source, decision_id="reuse-target-drift", reason="duplicate",
            target_project_id="project-a", source_project_id="project-a",
            after_materialize=drift,
        )
    persisted = manifest_mod.load_manifest(target)
    assert persisted["state"] == "CREATED"
    assert persisted["reuse_decisions"][0]["status"] == "prepared"


def test_unknown_project_identity_cannot_write_provenance(tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")
    with pytest.raises(reuse.ScientificFingerprintError, match="project identity"):
        reuse.record_reuse_reference(
            target, source, decision_id="reuse-unknown-project", reason="duplicate"
        )
    assert not manifest_mod.load_manifest(target).get("provenance")


def test_authoritative_lookup_is_distinct_from_bounded_advisory(tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")
    entries = [(str(target), None), (str(source), None)]
    lookup = reuse.authoritative_reuse_lookup(
        entries, ["job-target"],
        job_id=lambda _path, manifest: manifest["job_uuid"],
        project_id=lambda _path, manifest: manifest["project_uuid"],
    )
    assert lookup["authoritative"] is True and lookup["complete"] is True
    assert lookup["authorizes_submission"] is False
    assert lookup["targets"][0]["match_count"] == 1
    assert lookup["targets"][0]["exact_matches"][0]["project_relation"] == "same"


def test_incomplete_target_never_gets_authoritative_absence_conclusion(tmp_path):
    target = tmp_path / "target"
    manifest = _write_job(target, "job-target")
    manifest["inputs"].pop("method_recipe")
    manifest_mod.save_manifest(target, manifest)

    lookup = reuse.authoritative_reuse_lookup(
        [(str(target), manifest)], ["job-target"],
        job_id=lambda _path, current: current["job_uuid"],
        project_id=lambda _path, current: current["project_uuid"],
    )

    result = lookup["targets"][0]
    assert lookup["complete"] is True
    assert result["fingerprint"]["status"] == "incomplete"
    assert result["absence_authoritative"] is False


def test_force_recalculation_record_binds_schema_and_input_closure(tmp_path):
    job = tmp_path / "job"
    _write_job(job, "job-force")
    result = reuse.record_force_recalculation(
        job, decision_id="force-001", reason="independent replication"
    )
    decision = result["decision"]
    fingerprint = reuse.build_scientific_fingerprint(job)
    persisted = manifest_mod.load_manifest(job)
    assert decision["schema"] == reuse.FORCE_DECISION_SCHEMA
    assert decision["status"] == "succeeded"
    assert decision["input_closure_digest"] == fingerprint["input_closure_digest"]
    assert reuse.has_current_force_recalculation(persisted, fingerprint) is True


def test_same_reuse_decision_is_idempotent_across_processes_and_restart(tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")
    code = (
        "import json,sys; from vcstudio.project.calculation_reuse import "
        "record_reuse_reference; "
        "print(json.dumps(record_reuse_reference(sys.argv[1],sys.argv[2],"
        "decision_id='reuse-multiprocess',reason='duplicate',"
        "target_project_id='project-a',source_project_id='project-a')))"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    commands = [
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", code,
             str(target), str(source)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
        ) for _ in range(2)
    ]
    completed = [process.communicate(timeout=30) for process in commands]
    successes = [json.loads(stdout.strip().splitlines()[-1]) for stdout, _stderr in completed
                 if stdout.strip()]
    assert successes and any(item["ok"] for item in successes)
    assert len(manifest_mod.load_manifest(target)["reuse_decisions"]) == 1

    restarted = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code,
         str(target), str(source)], check=True,
        capture_output=True, text=True, env=environment, timeout=30,
    )
    assert json.loads(restarted.stdout.strip().splitlines()[-1])["replayed"] is True


def test_restart_repairs_journal_after_done_manifest_commit(tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")
    arguments = {
        "decision_id": "reuse-done-journal-repair", "reason": "duplicate",
        "target_project_id": "project-a", "source_project_id": "project-a",
    }
    reuse.record_reuse_reference(target, source, **arguments)
    journal_path = next(target.glob(".reuse-journal-*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["status"] = "prepared"
    journal.pop("done_manifest_sha256")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    replay = reuse.record_reuse_reference(target, source, **arguments)

    repaired = json.loads(journal_path.read_text(encoding="utf-8"))
    assert replay["replayed"] is True
    assert repaired["status"] == "succeeded"
    assert repaired["done_manifest_sha256"] == manifest_mod.sha256_file(
        target / manifest_mod.MANIFEST_NAME)


def test_output_symlink_or_reparse_point_is_never_reusable(tmp_path):
    job = tmp_path / "job"
    manifest = _write_job(job, "job-linked-output", state="DONE")
    outside = tmp_path / "outside-OUTCAR"
    outside.write_text(OUTCAR, encoding="utf-8")
    (job / "OUTCAR").unlink()
    try:
        (job / "OUTCAR").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable on this platform: {exc}")
    manifest["results"]["fetched_sha256"]["OUTCAR"] = _sha(outside)
    manifest_mod.save_manifest(job, manifest)

    verification = reuse.source_verification(job)

    assert verification["reusable"] is False
    assert "OUTCAR" not in verification["result_files"]
    assert any("unavailable/unsafe:OUTCAR" in issue
               for issue in verification["issues"])


def test_output_hash_and_parser_share_one_open_entity_during_replace(
    tmp_path, monkeypatch,
):
    job = tmp_path / "job"
    manifest = _write_job(job, "job-output-replace", state="DONE")
    bad_outcar = b"incomplete output from the declared run\n"
    (job / "OUTCAR").write_bytes(bad_outcar)
    bad_digest = hashlib.sha256(bad_outcar).hexdigest()
    manifest["results"]["fetched_sha256"]["OUTCAR"] = bad_digest
    manifest_mod.save_manifest(job, manifest)
    replacement = tmp_path / "replacement-OUTCAR"
    replacement.write_text(OUTCAR, encoding="utf-8")
    original_parser = result_import._parse_outcar_handle
    replaced = False

    def replace_path_then_parse(handle, size):
        nonlocal replaced
        if not replaced:
            replaced = True
            try:
                os.replace(replacement, job / "OUTCAR")
            except PermissionError:
                # Windows may deny rename-over-open without delete sharing.  An
                # in-place rewrite is the same TOCTOU class and must also fail.
                (job / "OUTCAR").write_text(OUTCAR, encoding="utf-8")
        return original_parser(handle, size)

    monkeypatch.setattr(
        result_import, "_parse_outcar_handle", replace_path_then_parse
    )

    verification = reuse.source_verification(job)

    assert replaced is True
    assert verification["reusable"] is False
    assert verification["result_files"]["OUTCAR"] == bad_digest
    assert verification["parser"]["source_sha256"]["OUTCAR"] == bad_digest
    assert any("during verification:OUTCAR" in issue
               for issue in verification["issues"])


def test_oversized_declared_output_fails_before_hash_or_parse(tmp_path, monkeypatch):
    job = tmp_path / "job"
    manifest = _write_job(job, "job-oversized-output", state="DONE")
    manifest["results"]["fetched_sha256"] = {
        "OUTCAR": manifest["results"]["fetched_sha256"]["OUTCAR"]
    }
    monkeypatch.setattr(reuse_verification, "MAX_RESULT_FILE_BYTES", 128)

    def fail_resource_bypass(*_args, **_kwargs):
        pytest.fail("oversized output must be rejected before hashing or parsing")

    monkeypatch.setattr(reuse_verification.hashlib, "sha256", fail_resource_bypass)
    monkeypatch.setattr(
        result_import, "_parse_outcar_handle", fail_resource_bypass
    )

    verification = reuse_verification.verify_outputs(job, manifest)

    assert "OUTCAR" not in verification["result_files"]
    assert any("result file exceeds resource limit:OUTCAR" in issue
               for issue in verification["issues"])


def test_reuse_output_verification_never_reopens_parser_paths(tmp_path, monkeypatch):
    job = tmp_path / "job"
    _write_job(job, "job-one-output-entity", state="DONE")

    def fail_path_reopen(*_args, **_kwargs):
        pytest.fail("reuse verification must parse its already-open file entity")

    monkeypatch.setattr(result_import, "_parse_outcar", fail_path_reopen)
    monkeypatch.setattr(result_import, "_parse_oszicar", fail_path_reopen)
    monkeypatch.setattr(manifest_mod, "sha256_file", fail_path_reopen)

    verification = reuse.source_verification(job)

    assert verification["reusable"] is True
