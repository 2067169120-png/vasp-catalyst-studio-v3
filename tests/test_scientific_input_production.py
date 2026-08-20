"""Production wiring for strict input closure, recipe and build evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vcstudio.cluster import quick_submit, submitter
from vcstudio.cluster.profiles import ClusterProfile, save_profiles
from vcstudio.generate import bands_builder, neb_builder
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate.method_recipe import (
    METHOD_RECIPE_AUTHORITY, METHOD_RECIPE_SCHEMA, bind_semantic_recipe,
)
from vcstudio.project.calculation_reuse import (
    CalculationReuseIndex, build_scientific_fingerprint, record_reuse_reference,
)
from vcstudio.shared import manifest as manifest_mod
from vcstudio.shared.execution_environment import (
    EXECUTION_ENVIRONMENT_AUTHORITY, EXECUTION_ENVIRONMENT_SCHEMA,
)
from vcstudio.shared.scientific_inputs import (
    record_input_closure, resolve_input_closure,
)


POSCAR = (
    "C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nDirect\n0 0 0\n"
)
KPOINTS = "Automatic\n0\nGamma\n1 1 1\n0 0 0\n"
POTCAR = "TITEL = PAW_PBE C\nENMAX = 300; ENMIN = 200\n"
ENV_SHA = hashlib.sha256(b"vasp-6.4.3-build-evidence").hexdigest()


def _environment(version="6.4.3", identity="vasp_std-gcc13-openmpi4"):
    return {
        "schema": EXECUTION_ENVIRONMENT_SCHEMA,
        "authority": EXECUTION_ENVIRONMENT_AUTHORITY,
        "engine": "vasp",
        "vasp_version": version,
        "build_identity": identity,
        "evidence": {"kind": "binary-sha256", "sha256": ENV_SHA},
    }


def _profile(**changes):
    values = {
        "name": "hpc", "hostname": "cluster", "username": "user",
        "remote_root": "/work/jobs", "scheduler": "PBS", "queue": "batch",
        "ppn": 8, "vasp_cmd": "mpirun -np 8 vasp_std",
        "vasp_version": "6.4.3",
        "vasp_build_identity": "vasp_std-gcc13-openmpi4",
        "vasp_build_evidence_sha256": ENV_SHA,
    }
    values.update(changes)
    return ClusterProfile(**values)


def _potlib(root: Path) -> Path:
    target = root / "C"
    target.mkdir(parents=True)
    (target / "POTCAR").write_text(POTCAR, encoding="utf-8")
    return root


def _manifest_job(root: Path, incar: str, *, task="static", inputs=None):
    root.mkdir()
    for name, text in {
            "POSCAR": POSCAR, "INCAR": incar, "KPOINTS": KPOINTS,
            "POTCAR": POTCAR}.items():
        (root / name).write_text(text, encoding="utf-8")
    payload = {"engine": "vasp", **(inputs or {})}
    manifest = manifest_mod.new_manifest(
        job_id=f"{root.name}-job", system="C", task_type=task,
        calc_type="slab", inputs=payload)
    return manifest


@pytest.mark.parametrize(
    "incar,task,inputs,dependency",
    [
        ("ICHARG = 11\n", "static", {}, "CHGCAR"),
        ("ISTART = 1\n", "static", {}, "WAVECAR"),
        ("LUSE_VDW = .TRUE.\n", "static", {}, "vdw_kernel.bindat"),
        ("IBRION = 0\nSHAKEMAXITER = 100\n", "aimd", {}, "ICONST"),
        ("ML_MODE = run\n", "aimd", {}, "ML_FF"),
        ("ENCUT = 400\n", "static", {"uses_kpoints_opt": True}, "KPOINTS_OPT"),
        ("IBRION = 3\nNSW = 20\n", "dimer", {}, "MODECAR"),
    ],
)
def test_actual_input_closure_is_task_and_incar_aware(
        tmp_path, incar, task, inputs, dependency):
    job = tmp_path / dependency.replace(".", "-")
    manifest = _manifest_job(job, incar, task=task, inputs=inputs)

    incomplete = resolve_input_closure(job, manifest)
    assert incomplete["status"] == "incomplete"
    assert dependency in incomplete["missing"]

    (job / dependency).write_text("scientific dependency\n", encoding="utf-8")
    complete = record_input_closure(job, manifest)
    assert complete["status"] == "complete"
    assert dependency in complete["files"]


def test_unread_side_outputs_are_not_silently_promoted_to_inputs(tmp_path):
    job = tmp_path / "plain"
    manifest = _manifest_job(job, "ISTART = 0\nICHARG = 2\n")
    (job / "CHGCAR").write_text("output charge\n", encoding="utf-8")
    (job / "WAVECAR").write_text("output wavefunction\n", encoding="utf-8")

    closure = resolve_input_closure(job, manifest)

    assert closure["status"] == "complete"
    assert "CHGCAR" not in closure["files"]
    assert "WAVECAR" not in closure["files"]


def test_generic_builder_manifest_and_pre_submit_fingerprint_are_authoritative(tmp_path):
    source = tmp_path / "POSCAR"
    source.write_text(POSCAR, encoding="utf-8")
    recipe = bind_semantic_recipe("4" * 64, metadata={"source": "method-recipe"})
    out = tmp_path / "job"
    result = build_job_dir(
        source, "ENCUT = 400\n", out, calc_type="slab",
        lib_root=str(_potlib(tmp_path / "potlib")),
        execution_environment=_environment(), method_recipe=recipe,
    )
    manifest_mod.create_from_build(out, result, poscar_path=source)

    loaded = manifest_mod.load_manifest(out)
    assert loaded["inputs"]["method_recipe"] == recipe
    assert loaded["inputs"]["input_closure"]["status"] == "complete"
    fingerprint = build_scientific_fingerprint(out)
    assert fingerprint["status"] == "complete"
    assert not any(item.startswith("method_recipe") for item in fingerprint["missing"])
    assert not any(item.startswith("execution_environment") for item in fingerprint["missing"])
    assert not any(item.startswith("input_closure") for item in fingerprint["missing"])


def test_production_builder_to_advisory_to_explicit_reuse(tmp_path):
    source_poscar = tmp_path / "POSCAR"
    source_poscar.write_text(POSCAR, encoding="utf-8")
    recipe = bind_semantic_recipe("5" * 64, metadata={"source": "method-recipe"})
    potlib = _potlib(tmp_path / "potlib")
    directories = [tmp_path / "source", tmp_path / "target"]
    for directory in directories:
        result = build_job_dir(
            source_poscar,
            "ENCUT=400\nNSW=10\nIBRION=2\nEDIFFG=-0.05\n",
            directory,
            calc_type="slab",
            lib_root=str(potlib),
            execution_environment=_environment(),
            method_recipe=recipe,
        )
        manifest = manifest_mod.create_from_build(
            directory, result, poscar_path=source_poscar,
        )
        manifest["project_uuid"] = "production-project"
        manifest["inputs"]["project_uuid"] = "production-project"
        manifest_mod.save_manifest(directory, manifest)

    source, target = directories
    (source / "OUTCAR").write_text(
        "NELM = 60 ; NSW = 10 ; EDIFFG = -0.05\n"
        "aborting loop because EDIFF is reached\n"
        "reached required accuracy - stopping structural energy minimisation\n"
        "energy(sigma->0) = -12.500000\n"
        "General timing and accounting informations for this job\n",
        encoding="utf-8",
    )
    (source / "OSZICAR").write_text(
        "DAV: 1 -0.125E+02 0.1E-03\n"
        "1 F= -.12500000E+02 E0= -.12500000E+02 d E =0.0\n",
        encoding="utf-8",
    )
    source_manifest = manifest_mod.load_manifest(source)
    source_manifest["state"] = "DONE"
    source_manifest["results"] = {
        "diagnosis": {
            "failure_class": "CONVERGED", "task_converged": True,
            "electronic_converged": True, "ionic_converged": True,
            "clean_exit": True, "exit_code": 0,
        },
        "fetched_sha256": {
            "OUTCAR": manifest_mod.sha256_file(source / "OUTCAR"),
            "OSZICAR": manifest_mod.sha256_file(source / "OSZICAR"),
        },
    }
    manifest_mod.save_manifest(source, source_manifest)
    entries = [
        (str(directory), manifest_mod.load_manifest(directory))
        for directory in directories
    ]
    index = CalculationReuseIndex().rebuild(
        entries,
        job_id=lambda _directory, manifest: manifest["job_id"],
        project_id=lambda _directory, _manifest: "production-project",
    )
    target_id = manifest_mod.load_manifest(target)["job_id"]
    source_id = source_manifest["job_id"]

    advisory = index.advisory([target_id])
    reused = record_reuse_reference(
        target, source, decision_id="production-reuse-001",
        reason="explicitly selected verified duplicate",
        target_project_id="production-project",
        source_project_id="production-project",
    )

    assert advisory["targets"][0]["exact_matches"][0]["source_job_id"] == source_id
    assert advisory["targets"][0]["requires_explicit_choice"] is True
    assert reused["ok"] is True and reused["state"] == "DONE"
    reused_manifest = manifest_mod.load_manifest(target)
    assert reused_manifest["provenance"]["links"][0]["type"] == "reuses"
    assert "accepted" not in reused_manifest and "final" not in reused_manifest


def test_neb_closure_binds_every_ordered_image_and_excludes_outputs(tmp_path):
    initial = POSCAR.replace("0 0 0", "0.1 0 0")
    final = POSCAR.replace("0 0 0", "0.2 0 0")
    out = tmp_path / "neb"
    neb_builder.build_neb_dir(
        out, initial, final, "ENCUT=400\nISYM=0\nNSW=100\n",
        n_images=2, kpoints=[1, 1, 1], potcar_fn=lambda _elements: POTCAR,
        execution_environment=_environment(),
    )
    (out / "01" / "OUTCAR").write_text("old output\n", encoding="utf-8")
    loaded = manifest_mod.load_manifest(out)
    closure = resolve_input_closure(out, loaded)

    assert closure["status"] == "complete"
    assert [name for name in closure["files"] if name.endswith("/POSCAR")] == [
        "00/POSCAR", "01/POSCAR", "02/POSCAR", "03/POSCAR"]
    assert "01/OUTCAR" not in closure["files"]
    names, issues = submitter._declared_input_files(str(out), loaded)
    assert issues == [] and "01/OUTCAR" not in names


def test_neb_restart_dependencies_are_bound_per_intermediate_image(tmp_path):
    job = tmp_path / "neb-restart"
    job.mkdir()
    for name, text in {
            "INCAR": "IMAGES=2\nISTART=1\nICHARG=11\n", "KPOINTS": KPOINTS,
            "POTCAR": POTCAR}.items():
        (job / name).write_text(text, encoding="utf-8")
    for index in range(4):
        frame = job / f"{index:02d}"
        frame.mkdir()
        (frame / "POSCAR").write_text(POSCAR, encoding="utf-8")
    manifest = manifest_mod.new_manifest(
        job_id="neb-restart", system="C", task_type="neb", calc_type="slab",
        inputs={"engine": "vasp", "n_images": 2})

    closure = resolve_input_closure(job, manifest)

    assert closure["status"] == "incomplete"
    assert set(closure["missing"]) == {
        "01/CHGCAR", "01/WAVECAR", "02/CHGCAR", "02/WAVECAR"}
    assert "00/WAVECAR" not in closure["requirements"]


def test_profile_environment_binding_is_atomic_idempotent_and_conflict_closed(tmp_path):
    job = tmp_path / "bind"
    manifest = _manifest_job(job, "ENCUT=400\n")
    record_input_closure(job, manifest)
    manifest_mod.save_manifest(job, manifest)

    first = submitter.bind_execution_environment(str(job), _profile())
    first_bytes = (job / "job.yaml").read_bytes()
    second = submitter.bind_execution_environment(str(job), _profile())
    assert first["inputs"]["execution_environment"]["vasp_version"] == "6.4.3"
    assert first["inputs"]["execution_environment"]["build_identity"] == \
        "vasp_std-gcc13-openmpi4"
    assert first["inputs"]["execution_environment"]["evidence"]["sha256"] == ENV_SHA
    assert second == first
    assert (job / "job.yaml").read_bytes() == first_bytes

    with pytest.raises(ValueError, match="冲突"):
        submitter.bind_execution_environment(
            str(job), _profile(vasp_build_identity="different-build"))
    assert (job / "job.yaml").read_bytes() == first_bytes


def test_environment_binding_fail_closed_when_only_one_side_has_authority(tmp_path):
    job = tmp_path / "planned"
    manifest = _manifest_job(job, "ENCUT=400\n")
    manifest["inputs"]["execution_environment"] = _environment()
    record_input_closure(job, manifest)
    manifest_mod.save_manifest(job, manifest)
    legacy_profile = _profile(
        vasp_version="", vasp_build_identity="", vasp_build_evidence_sha256="")

    assert any("没有可核验" in issue
               for issue in submitter.preflight(legacy_profile, str(job)))

    partial = _profile(vasp_build_evidence_sha256="")
    with pytest.raises(ValueError, match="执行环境证据无效"):
        save_profiles({"hpc": partial}, tmp_path / "clusters.yaml")


def test_direct_bands_and_quick_generate_use_recipe_and_closure(tmp_path):
    source = tmp_path / "scf"
    source.mkdir()
    for name, text in {
            "CONTCAR": POSCAR, "INCAR": "ENCUT=400\n",
            "KPOINTS": KPOINTS, "POTCAR": POTCAR, "CHGCAR": "charge\n"}.items():
        (source / name).write_text(text, encoding="utf-8")
    bands = tmp_path / "bands"
    bands_builder.build_bands_job(source, bands, lattice="cubic", npoints=12)
    band_inputs = manifest_mod.load_manifest(bands)["inputs"]
    assert band_inputs["method_recipe"]["authority"] == METHOD_RECIPE_AUTHORITY
    assert band_inputs["input_closure"]["status"] == "complete"
    assert "CHGCAR" in band_inputs["input_closure"]["files"]

    poscar = tmp_path / "quick-source" / "POSCAR"
    poscar.parent.mkdir()
    poscar.write_text(POSCAR, encoding="utf-8")
    result = quick_submit.build_quick_jobs(
        [str(poscar)], str(tmp_path / "quick-jobs"),
        shared_incar="ENCUT=400\n", lib_root=str(_potlib(tmp_path / "quick-lib")),
    )
    assert result["ok"] and len(result["jobs"]) == 1
    quick_manifest = manifest_mod.load_manifest(result["jobs"][0]["dir"])
    assert quick_manifest["inputs"]["method_recipe"]["schema"] == METHOD_RECIPE_SCHEMA
    assert quick_manifest["inputs"]["input_closure"]["status"] == "complete"


def test_large_dependency_is_rejected_before_hashing_beyond_cap(tmp_path, monkeypatch):
    from vcstudio.shared import scientific_inputs

    job = tmp_path / "bounded"
    manifest = _manifest_job(job, "ISTART=1\n")
    (job / "WAVECAR").write_bytes(b"0123456789")
    monkeypatch.setattr(scientific_inputs, "_MAX_LARGE_BYTES", 8)

    closure = scientific_inputs.resolve_input_closure(job, manifest)

    assert closure["status"] == "incomplete"
    assert closure["missing"] == ["WAVECAR"]
    assert closure["resource_limits"]
