"""Strict fingerprint, advisory-index and explicit-reuse contract tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import types

import pytest

from vcstudio.cluster.submitter import JobOperationBusy
from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.gui_web.api import Api
from vcstudio.project import calculation_reuse as reuse
from vcstudio.shared import manifest as manifest_mod


POSCAR = """strict structure
1.0
10.0 0 0
0 10 0
0 0 20
C O
1 1
Selective dynamics
Direct
0.0000 0 0 T T T
0.500000 0.5 0.25 F F T
"""
INCAR = """SYSTEM = private local label
GGA = PE ; ENCUT = 500.000
ISPIN = 2
MAGMOM = 1 -1
LDAU = .TRUE.
LDAUL = 2 -1
LDAUU = 3.0 0
LDAUJ = 0 0
IVDW = 12
LSOL = .TRUE.
EB_K = 78.4
EDIFF = 1E-5
"""
KPOINTS = """comment is not scientific
0
Gamma
3 3 1
0 0 0
"""
POTCAR = """PAW data bytes A
TITEL  = PAW_PBE C 08Apr2002
payload-C
TITEL  = PAW_PBE O 08Apr2002
payload-O
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_job(
        root: Path, job_id: str, *, state="CREATED", poscar=POSCAR, incar=INCAR,
        kpoints=KPOINTS, potcar=POTCAR, recipe_hash="a" * 64,
        vasp_version="6.4.3", build_identity="linux-x86_64-oneapi-2025",
        project_uuid="project-a", converged=True, result_content="verified result\n",
        created_by="vcstudio 4.0.0") -> dict:
    root.mkdir(parents=True)
    for name, text in {
            "POSCAR": poscar, "INCAR": incar, "KPOINTS": kpoints,
            "POTCAR": potcar}.items():
        (root / name).write_text(text, encoding="utf-8")
    results = {}
    if state == "DONE":
        (root / "OUTCAR").write_text(
            result_content
            + "\nGeneral timing and accounting informations for this job\n",
            encoding="utf-8")
        (root / "OSZICAR").write_text(
            "  1 F= -.12500000E+02 E0= -.12500000E+02 d E =0.0\n",
            encoding="utf-8")
        results = {
            "energy_e0_eV": -12.5,
            "diagnosis": ({"failure_class": "CONVERGED", "task_converged": True,
                           "clean_exit": True, "exit_code": 0}
                          if converged else {}),
            "fetched_sha256": {
                "OUTCAR": _sha(root / "OUTCAR"), "OSZICAR": _sha(root / "OSZICAR")},
            "resource_usage": {"core_hours": 12.25},
        }
    elif state in {"FAILED", "UNCONVERGED", "NEEDS_HUMAN"}:
        results = {"diagnosis": {"failure_class": state}}
    inputs = {
        "engine": "vasp", "project_uuid": project_uuid,
        "sha256": {name: _sha(root / name) for name in reuse._INPUT_FILES},
        "potcar_sha256": _sha(root / "POTCAR"),
        "method_recipe": {
            "schema": "vcstudio.method-recipe/v1",
            "semantic_sha256": recipe_hash,
        } if recipe_hash is not None else None,
        "execution_environment": {
            "vasp_version": vasp_version, "build_identity": build_identity,
        },
    }
    if inputs["method_recipe"] is None:
        inputs.pop("method_recipe")
    manifest = {
        "schema": 1, "job_id": job_id, "job_uuid": job_id,
        "project_uuid": project_uuid, "system": "CO", "task_type": "relax",
        "calc_type": "slab", "created_at": "2026-08-15T00:00:00",
        "created_by": created_by, "inputs": inputs, "cluster": None,
        "remote_dir": None, "scheduler_job_id": None, "state": state,
        "state_history": [{"state": state, "at": "2026-08-15T00:00:00"}],
        "attempts": [{"n": 1, "cores": 24, "walltime": "02:00:00"}],
        "results": results, "warnings": [],
    }
    manifest_mod.save_manifest(root, manifest)
    return manifest


def test_canonical_fingerprint_normalises_float_spelling_and_comments(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_job(left, "job-left")
    _write_job(
        right, "job-right",
        poscar=POSCAR.replace("1.0", "1", 1).replace("10.0", "1E1", 1)
                     .replace("0.500000 0.5", "5e-1 .500"),
        incar=INCAR.replace("500.000", "5E2").replace("1E-5", "0.000010000")
                   .replace("private local label", "PRIVATE LOCAL LABEL"),
        kpoints=KPOINTS.replace("comment is not scientific", "different comment")
                       .replace("3 3 1", "03 3.0 1.000"),
    )

    first = reuse.build_scientific_fingerprint(left)
    second = reuse.build_scientific_fingerprint(right)

    assert first["status"] == second["status"] == "complete"
    assert first["digest"] == second["digest"]
    assert first["components"]["scientific_controls"]["values"]["spin"]["ISPIN"] == "2"
    constraints = first["components"]["scientific_controls"]["values"]["constraints"]
    assert constraints["selective_dynamics"] is True and constraints["constrained_atoms"] == 1


def test_structure_identity_normalises_scale_and_cartesian_coordinates(tmp_path):
    direct = tmp_path / "direct"
    cartesian = tmp_path / "cartesian"
    scaled = tmp_path / "scaled"
    _write_job(direct, "job-direct")
    cartesian_poscar = POSCAR.replace(
        "Direct\n0.0000 0 0 T T T\n0.500000 0.5 0.25 F F T",
        "Cartesian\n0 0 0 T T T\n5.0 5.000 5 F F T")
    scaled_poscar = (POSCAR.replace("1.0\n10.0 0 0\n0 10 0\n0 0 20",
                                    "2.0\n5 0 0\n0 5 0\n0 0 10"))
    _write_job(cartesian, "job-cartesian", poscar=cartesian_poscar)
    _write_job(scaled, "job-scaled", poscar=scaled_poscar)

    fingerprints = [reuse.build_scientific_fingerprint(path)
                    for path in (direct, cartesian, scaled)]

    assert {item["status"] for item in fingerprints} == {"complete"}
    assert len({item["digest"] for item in fingerprints}) == 1
    structure = fingerprints[0]["components"]["structure"]
    assert structure["summary"]["coordinate_mode"] == "fractional-canonical"


@pytest.mark.parametrize("change", ["structure", "potcar", "version", "recipe"])
def test_strict_fingerprint_changes_on_scientific_identity(change, tmp_path):
    baseline = tmp_path / "baseline"
    changed = tmp_path / change
    _write_job(baseline, "job-baseline")
    kwargs = {}
    if change == "structure":
        kwargs["poscar"] = POSCAR.replace("0.25 F F T", "0.26 F F T")
    elif change == "potcar":
        # Same TITEL metadata but different licensed-content identity.
        kwargs["potcar"] = POTCAR.replace("payload-O", "different-payload-O")
    elif change == "version":
        kwargs["vasp_version"] = "6.5.0"
    else:
        kwargs["recipe_hash"] = "b" * 64
    _write_job(changed, f"job-{change}", **kwargs)

    left = reuse.build_scientific_fingerprint(baseline)
    right = reuse.build_scientific_fingerprint(changed)

    assert left["status"] == right["status"] == "complete"
    assert left["digest"] != right["digest"]


def test_legacy_recipe_or_missing_version_is_incomplete_not_exact(tmp_path):
    legacy = tmp_path / "legacy"
    no_version = tmp_path / "no-version"
    _write_job(legacy, "job-legacy", recipe_hash=None)
    _write_job(no_version, "job-version", vasp_version="")

    legacy_fp = reuse.build_scientific_fingerprint(legacy)
    version_fp = reuse.build_scientific_fingerprint(no_version)

    assert legacy_fp["status"] == "incomplete" and legacy_fp["digest"] is None
    assert legacy_fp["recipe_status"] == "explicit_legacy"
    assert "method_recipe.semantic_sha256" in legacy_fp["missing"]
    assert version_fp["status"] == "incomplete" and version_fp["digest"] is None
    assert "execution_environment.vasp_version" in version_fp["missing"]


def test_manifest_input_tamper_fails_closed(tmp_path):
    job = tmp_path / "tampered"
    _write_job(job, "job-tampered")
    (job / "INCAR").write_text(INCAR + "\nNELM=200\n", encoding="utf-8")

    fingerprint = reuse.build_scientific_fingerprint(job)

    assert fingerprint["status"] == "incomplete" and fingerprint["digest"] is None
    assert "INCAR no longer matches job.yaml" in fingerprint["integrity_issues"]


def _index(paths, *, limit=512):
    entries = [(str(path), manifest_mod.load_manifest(path)) for path in paths]
    return reuse.CalculationReuseIndex(limit=limit).rebuild(
        entries, job_id=lambda _path, manifest: manifest["job_uuid"],
        project_id=lambda _path, manifest: manifest.get("project_uuid"),
    )


def test_bounded_advisory_separates_exact_near_and_bad_outcomes(tmp_path):
    target = tmp_path / "target"
    exact = tmp_path / "exact"
    failed = tmp_path / "failed"
    near = tmp_path / "near"
    _write_job(target, "job-target")
    _write_job(exact, "job-exact", state="DONE", project_uuid="project-b")
    _write_job(failed, "job-failed", state="FAILED")
    _write_job(near, "job-near", state="DONE", vasp_version="6.5.0")

    result = _index([target, exact, failed, near]).advisory(["job-target"])
    row = result["targets"][0]

    assert result["automatic_reuse"] is False and result["equivalence_claim"] is False
    assert result["authorizes_submission"] is False
    assert row["default_action"] == "recalculate"
    exact_by_id = {item["source_job_id"]: item for item in row["exact_matches"]}
    assert exact_by_id["job-exact"]["verification"]["reusable"] is True
    assert exact_by_id["job-exact"]["cross_project"] is True
    assert exact_by_id["job-exact"]["saved_estimate"] == {
        "status": "measured", "core_hours": 12.25}
    assert exact_by_id["job-failed"]["verification"]["status"] == "failed"
    near_row = next(item for item in row["near_matches"]
                    if item["source_job_id"] == "job-near")
    assert near_row["not_equivalent"] is True
    assert any(item["field"] == "environment" for item in near_row["differences"])
    assert row["requires_explicit_choice"] is True


def test_index_is_rebuildable_bounded_and_not_authoritative(tmp_path):
    jobs = []
    for index in range(3):
        path = tmp_path / f"job-{index}"
        _write_job(path, f"job-{index}")
        jobs.append(path)

    index = _index(jobs, limit=2)
    result = index.advisory(["job-2"])

    assert result["index"] == {
        "schema": reuse.INDEX_SCHEMA, "capacity": 2, "indexed": 2,
        "observed": 3, "truncated": True, "rebuildable": True,
        "authoritative": False,
    }


def test_explicit_cross_project_reuse_creates_new_lineage_without_final_inheritance(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target", project_uuid="project-a")
    _write_job(source, "job-source", state="DONE", project_uuid="project-b")

    result = reuse.record_reuse_reference(
        target, source, decision_id="reuse-decision-001", reason="same strict method",
        target_project_id="project-a", source_project_id="project-b")
    manifest = manifest_mod.load_manifest(target)

    assert result["ok"] is True and result["state"] == "DONE"
    assert manifest["results"]["energy_e0_eV"] == -12.5
    assert (target / "OUTCAR").is_file() and (target / "OSZICAR").is_file()
    assert manifest["results"]["diagnosis"]["evidence_source"] == \
        "verified_reuse_reference"
    decision = manifest["reuse_decisions"][0]
    assert decision["cross_project"] is True
    assert decision["accepted_inherited"] is False and decision["final_inherited"] is False
    assert manifest["provenance"]["schema"] == reuse.PROVENANCE_SCHEMA
    assert manifest["provenance"]["links"] == [{
        "id": "reuse-decision-001", "type": "reuses", "from": "job-target",
        "to": "job-source", "decision_id": "reuse-decision-001", "cross_project": True,
    }]
    assert "accepted" not in manifest and "final" not in manifest


def test_source_accepted_and_final_like_authority_is_never_copied(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    source_manifest = _write_job(source, "job-source", state="DONE")
    source_manifest.update({
        "accepted": True,
        "final": {"allowed": True, "qualification": "human_scientific_reviewed"},
        "autopilot_report": {"report_kind": "final", "ready": True},
        "rung": "accepted",
    })
    source_manifest["results"]["diagnosis"].update({
        "accepted": True, "final": {"allowed": True},
    })
    manifest_mod.save_manifest(source, source_manifest)

    reuse.record_reuse_reference(
        target, source, decision_id="reuse-no-authority", reason="same calculation")
    persisted = manifest_mod.load_manifest(target)

    for key in ("accepted", "final", "autopilot_report", "rung"):
        assert key not in persisted
    assert "accepted" not in persisted["results"]["diagnosis"]
    assert "final" not in persisted["results"]["diagnosis"]
    decision = persisted["reuse_decisions"][0]
    assert decision["accepted_inherited"] is False
    assert decision["final_inherited"] is False


def test_full_kpoints_line_mode_and_weights_affect_strict_identity(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    line_mode_a = "k path\n4\nLine-mode\nReciprocal\n0 0 0 1\n0.5 0 0 1\n"
    line_mode_b = line_mode_a.replace("0.5 0 0 1", "0.25 0 0 1")
    _write_job(first, "job-first", kpoints=line_mode_a)
    _write_job(second, "job-second", kpoints=line_mode_b)

    left = reuse.build_scientific_fingerprint(first)
    right = reuse.build_scientific_fingerprint(second)

    assert left["status"] == right["status"] == "complete"
    assert left["components"]["kpoints"]["sha256"] != \
        right["components"]["kpoints"]["sha256"]
    assert left["digest"] != right["digest"]


@pytest.mark.parametrize("state", ["FAILED", "UNCONVERGED", "NEEDS_HUMAN"])
def test_bad_results_cannot_be_reused(tmp_path, state):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state=state)

    with pytest.raises(reuse.ScientificFingerprintError, match="not complete"):
        reuse.record_reuse_reference(
            target, source, decision_id=f"reuse-{state.lower()}", reason="try")
    assert manifest_mod.load_manifest(target)["state"] == "CREATED"


def test_reuse_revalidates_result_bytes_at_commit_to_close_toctou(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")

    def tamper():
        (source / "OUTCAR").write_text("tampered after choice\n", encoding="utf-8")

    with pytest.raises(reuse.ScientificFingerprintError, match="changed during"):
        reuse.record_reuse_reference(
            target, source, decision_id="reuse-toctou", reason="same", before_commit=tamper)
    assert manifest_mod.load_manifest(target)["state"] == "CREATED"
    assert not manifest_mod.load_manifest(target).get("reuse_decisions")


def test_reuse_decision_is_restart_idempotent_and_conflicts_fail(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")

    first = reuse.record_reuse_reference(
        target, source, decision_id="reuse-idempotent", reason="same")
    replay = reuse.record_reuse_reference(
        target, source, decision_id="reuse-idempotent", reason="same")

    assert first["replayed"] is False and replay["replayed"] is True
    assert len(manifest_mod.load_manifest(target)["reuse_decisions"]) == 1
    with pytest.raises((reuse.ReuseConflictError, reuse.ScientificFingerprintError)):
        reuse.record_reuse_reference(
            target, source, decision_id="reuse-idempotent", reason="different")


def test_replay_revalidates_source_instead_of_trusting_old_success(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")
    reuse.record_reuse_reference(
        target, source, decision_id="reuse-stale-replay", reason="same")
    (source / "OUTCAR").write_text("source changed later\n", encoding="utf-8")

    with pytest.raises(reuse.ScientificFingerprintError, match="not complete"):
        reuse.record_reuse_reference(
            target, source, decision_id="reuse-stale-replay", reason="same")
    assert manifest_mod.load_manifest(target)["state"] == "DONE"


def test_prepared_reuse_recovers_after_restart_between_file_and_manifest_commit(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")

    def crash_after_first(_name, count):
        if count == 1:
            raise RuntimeError("simulated process interruption")

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        reuse.record_reuse_reference(
            target, source, decision_id="reuse-recover-001", reason="same",
            after_materialize=crash_after_first)
    prepared = manifest_mod.load_manifest(target)
    assert prepared["state"] == "CREATED"
    assert prepared["reuse_decisions"][0]["status"] == "prepared"
    assert len([name for name in ("OUTCAR", "OSZICAR") if (target / name).exists()]) == 1

    recovered = reuse.record_reuse_reference(
        target, source, decision_id="reuse-recover-001", reason="same")
    persisted = manifest_mod.load_manifest(target)

    assert recovered["ok"] is True and recovered["replayed"] is False
    assert persisted["state"] == "DONE"
    assert persisted["reuse_decisions"][0]["status"] == "succeeded"
    assert (target / "OUTCAR").is_file() and (target / "OSZICAR").is_file()


def test_reuse_rechecks_source_after_result_materialization(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")
    oszicar = source / "OSZICAR"
    original = oszicar.read_bytes()

    def tamper_after_first_copy(_name, count):
        if count == 1:
            oszicar.write_bytes(b"tampered after its verified copy\n")

    with pytest.raises(reuse.ScientificFingerprintError, match="source changed"):
        reuse.record_reuse_reference(
            target, source, decision_id="reuse-final-toctou-001",
            reason="duplicate", after_materialize=tamper_after_first_copy)

    prepared = manifest_mod.load_manifest(target)
    assert prepared["state"] == "CREATED"
    assert prepared["reuse_decisions"][0]["status"] == "prepared"

    oszicar.write_bytes(original)
    recovered = reuse.record_reuse_reference(
        target, source, decision_id="reuse-final-toctou-001", reason="duplicate")
    assert recovered["ok"] is True
    assert manifest_mod.load_manifest(target)["state"] == "DONE"


def test_concurrent_reuse_never_creates_two_decisions(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "job-target")
    _write_job(source, "job-source", state="DONE")

    def invoke():
        try:
            return reuse.record_reuse_reference(
                target, source, decision_id="reuse-concurrent", reason="same")
        except JobOperationBusy:
            return {"busy": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: invoke(), range(2)))

    assert any(item.get("ok") for item in results)
    assert len(manifest_mod.load_manifest(target)["reuse_decisions"]) == 1


def test_force_recalculation_reason_is_durable_and_idempotent(tmp_path):
    target = tmp_path / "target"
    _write_job(target, "job-target")

    first = reuse.record_force_recalculation(
        target, decision_id="force-001", reason="independent validation run")
    replay = reuse.record_force_recalculation(
        target, decision_id="force-001", reason="independent validation run")
    manifest = manifest_mod.load_manifest(target)

    assert first["replayed"] is False and replay["replayed"] is True
    assert len(manifest["reuse_decisions"]) == 1
    assert reuse.has_current_force_recalculation(
        manifest, reuse.build_scientific_fingerprint(target)) is True
    with pytest.raises(ValueError, match="requires a reason"):
        reuse.record_force_recalculation(target, decision_id="force-002", reason="")


def test_public_projection_contains_no_paths_secrets_or_potcar_content(tmp_path):
    job = tmp_path / "private" / "job"
    _write_job(job, "job-public", state="DONE")

    public = reuse.public_fingerprint(reuse.build_scientific_fingerprint(job))
    serialized = reuse._canonical_json(public)

    assert str(tmp_path) not in serialized
    assert "payload-C" not in serialized and "PAW data bytes" not in serialized
    assert "password" not in serialized.lower() and "secret" not in serialized.lower()
    assert public["status"] == "complete" and len(public["fields"]) == 7


class _LiveLedger:
    def __init__(self, directories):
        self.directories = list(directories)

    def load_all(self):
        return [(str(path), manifest_mod.load_manifest(path)) for path in self.directories]


def _profiles(store):
    return types.SimpleNamespace(
        load_profiles=lambda: dict(store), ClusterProfile=ClusterProfile)


def test_api_advisory_accepts_only_opaque_ids_and_redacts_locators(tmp_path):
    target = tmp_path / "private" / "target"
    source = tmp_path / "private" / "source"
    _write_job(target, "opaque-target")
    _write_job(source, "opaque-source", state="DONE")
    api = Api(ledger_mod=_LiveLedger([target, source]))
    api._project_role_map = lambda: {}

    result = api.jobs_reuse_advisory(["opaque-target"])
    rejected = api.jobs_reuse_advisory([str(target)])
    rendered = reuse._canonical_json(result)

    assert result["ok"] is True
    assert result["targets"][0]["exact_matches"][0]["source_job_id"] == "opaque-source"
    assert str(tmp_path) not in rendered and "dir" not in result["targets"][0]
    assert rejected["ok"] is False and str(tmp_path) not in rejected["error"]


def test_api_explicit_reference_resolves_ids_server_side(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "opaque-target")
    _write_job(source, "opaque-source", state="DONE")
    api = Api(ledger_mod=_LiveLedger([target, source]))
    api._project_role_map = lambda: {}

    result = api.jobs_reference_existing_result(
        "opaque-target", "opaque-source", "api-reuse-001", "verified duplicate")

    assert result["ok"] is True and result["target_job_id"] == "opaque-target"
    assert result["accepted_inherited"] is False and result["final_inherited"] is False
    assert manifest_mod.load_manifest(target)["state"] == "DONE"


def test_submit_api_requires_force_reason_for_reusable_exact_match(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "opaque-target")
    _write_job(source, "opaque-source", state="DONE")
    calls = []
    batch = types.SimpleNamespace(submit_batch=lambda _profile, _password, dirs, _trust:
                                  calls.append(list(dirs)) or {
                                      "needs_trust": False, "results": []})
    profile = ClusterProfile(name="hpc", hostname="cluster", auth="key")
    api = Api(
        ledger_mod=_LiveLedger([target, source]),
        profiles_mod=_profiles({"hpc": profile}), batch_ops_mod=batch)
    api._project_role_map = lambda: {}

    blocked = api.submit_jobs([str(target)], "hpc", None, False, "submit-guard-001")
    forced = api.jobs_force_recalculation(
        ["opaque-target"], "force-api-001", "independent reproducibility check")
    submitted = api.submit_jobs(
        [str(target)], "hpc", None, False, "submit-guard-001")

    assert blocked["code"] == "reuse_decision_required" and calls == [
        [str(target)]]
    assert blocked["blocked_job_ids"] == ["opaque-target"]
    assert forced["ok"] is True and forced["reason_retained"] is True
    assert submitted.get("error") is None


def test_api_force_recalculation_rejects_missing_reason_and_is_restart_idempotent(tmp_path):
    target = tmp_path / "target"
    _write_job(target, "opaque-target")
    ledger = _LiveLedger([target])
    first_api = Api(ledger_mod=ledger)
    second_api = Api(ledger_mod=ledger)
    first_api._project_role_map = lambda: {}
    second_api._project_role_map = lambda: {}

    missing = first_api.jobs_force_recalculation(["opaque-target"], "force-api-002", "")
    first = first_api.jobs_force_recalculation(
        ["opaque-target"], "force-api-002", "repeat with different random seed")
    replay = second_api.jobs_force_recalculation(
        ["opaque-target"], "force-api-002", "repeat with different random seed")

    assert missing["ok"] is False and missing["reason_retained"] is False
    assert first["results"][0]["replayed"] is False
    assert replay["results"][0]["replayed"] is True


def test_project_submission_uses_same_guard_and_request_idempotency(tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    _write_job(target, "opaque-target")
    _write_job(source, "opaque-source", state="DONE")
    calls = []
    batch = types.SimpleNamespace(submit_batch=lambda _profile, _password, dirs, _trust,
                                  **_kwargs: calls.append(list(dirs)) or {
                                      "needs_trust": False,
                                      "results": [(str(target), True, "job-42")],
                                  })
    profile = ClusterProfile(name="hpc", hostname="cluster", auth="key")
    project = {"name": "strict-project", "launch": {}, "root": str(tmp_path)}
    adsorption = types.SimpleNamespace(save_project=lambda _root, _project: None)
    config = types.SimpleNamespace(set_ui_state=lambda **_kwargs: None)
    api = Api(
        ledger_mod=_LiveLedger([target, source]), profiles_mod=_profiles({"hpc": profile}),
        batch_ops_mod=batch, adsorption_mod=adsorption, config_mod=config)
    api._project_role_map = lambda: {}
    api._load_project_for_path = lambda _path: project
    api._project_member_dirs = lambda _project: [str(target)]

    blocked = api._submit_project_with_resources_for_path(
        str(tmp_path / "project.yaml"), "hpc", 16, "01:00:00",
        idempotency_key="project-submit-001")
    forced = api.jobs_force_recalculation(
        ["opaque-target"], "project-force-001", "independent project rerun")
    first = api._submit_project_with_resources_for_path(
        str(tmp_path / "project.yaml"), "hpc", 16, "01:00:00",
        idempotency_key="project-submit-001")
    replay = api._submit_project_with_resources_for_path(
        str(tmp_path / "project.yaml"), "hpc", 16, "01:00:00",
        idempotency_key="project-submit-001")

    assert blocked["code"] == "reuse_decision_required" and calls == [[str(target)]]
    assert forced["ok"] is True
    assert first["ok"] is True and replay["ok"] is True
    assert len(calls) == 1
