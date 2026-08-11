"""Focused contracts for clone, move, and copied-folder adoption."""
from __future__ import annotations

import json
import multiprocessing
import shutil
from pathlib import Path

import pytest
import yaml

import vcstudio.project.project_lifecycle as lifecycle
from vcstudio.project.project_lifecycle import (
    ProjectLifecycleError,
    ProjectLifecyclePartialRollbackError,
    ProjectLifecycleService,
)


UUID_A = "a" * 32


class _SimulatedCrash(BaseException):
    pass


def _process_apply(registry: str, plan, held, release, slow: bool, outcomes) -> None:
    """Independent-process lifecycle caller used by the lock contract test."""
    try:
        service = ProjectLifecycleService(registry)
        if slow:
            real_copytree = service._copytree

            def held_copytree(source, destination, **kwargs):
                result = real_copytree(source, destination, **kwargs)
                held.set()
                if not release.wait(20):
                    raise RuntimeError("test release gate timed out")
                return result

            service._copytree = held_copytree
        result = service.apply(plan)
        outcomes.put(("ok", result["action"]))
    except BaseException as exc:  # child must report lock errors and test crashes
        outcomes.put(("error", getattr(exc, "code", type(exc).__name__)))


def _write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _project(root: Path, project_uuid: str = UUID_A, *, recorded_root: Path | None = None,
             external_reference: Path | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    clean = root / "clean"
    config = root / "configs" / "site-1"
    molecule = root / "molecules" / "Li2S8"
    for job in (clean, config, molecule):
        job.mkdir(parents=True, exist_ok=True)
        _write_yaml(job / "job.yaml", {
            "job_id": f"job-{job.name}",
            "state": "DONE",
            "inputs": {"remote_namespace": f"study-{project_uuid}"},
        })
    recorded = recorded_root or root
    external = external_reference or molecule
    project = {
        "schema": 1,
        "name": "study",
        "root": str(recorded),
        "project_uuid": project_uuid,
        "preparation": {"project_uuid": project_uuid, "request_sha256": "1" * 64},
        "remote_namespace": f"study-{project_uuid}",
        "members": {
            "clean_slab": str(recorded / "clean"),
            "gas_ref": None,
            "configs": [str(recorded / "configs" / "site-1")],
        },
        "config_species": {str(recorded / "configs" / "site-1"): "Li2S8"},
        "config_species_evidence": {
            str(recorded / "configs" / "site-1"): {
                "status": "exact", "clean_member": str(recorded / "clean")
            }
        },
        "species_ref_jobs": {"Li2S8": str(external)},
        "molecules_dir": str(recorded / "molecules"),
        "dataset_groups": [{
            "group_id": "composition:Li:2|S:8",
            "configs": [str(recorded / "configs" / "site-1")],
            "reference_job": str(external),
        }],
        # This resembles a path but is provenance, not a live member/reference locator.
        "audit_note": f"Source was {recorded / 'configs' / 'site-1'}",
    }
    _write_yaml(root / "project.yaml", project)
    return root / "project.yaml"


def _registry(path: Path, entries: list[Path]) -> Path:
    path.write_text(json.dumps({"projects": [str(item) for item in entries]}, indent=2),
                    encoding="utf-8")
    return path


def _read_project(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _read_registry(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["projects"]


def _ledger(path: Path, entries: list[Path]) -> Path:
    path.write_text(json.dumps({"job_dirs": [str(item) for item in entries]}, indent=2),
                    encoding="utf-8")
    return path


def _read_ledger(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["job_dirs"]


def test_clone_preflight_is_dry_run_and_apply_mints_identity_and_rebases_only_local_locators(
        tmp_path):
    external = tmp_path / "shared-reference"
    external.mkdir()
    source = _project(tmp_path / "source", external_reference=external)
    registry = _registry(tmp_path / "projects.json", [source])
    source_jobs = [source.parent / "clean", source.parent / "configs" / "site-1",
                   source.parent / "molecules" / "Li2S8"]
    ledger = _ledger(tmp_path / "jobs.json", source_jobs)
    destination = tmp_path / "clone"
    service = ProjectLifecycleService(registry)

    plan = service.preflight_clone(source, destination)

    assert plan.ready and plan.target_project_uuid != UUID_A
    assert not destination.exists()
    assert _read_registry(registry) == [str(source)]
    public = plan.public_summary()
    assert str(tmp_path) not in json.dumps(public)
    assert "project_path" not in json.dumps(public)
    assert public["impact"]["project_uuid_reminted"] is True
    assert public["jobs"]["count"] == 3

    result = service.apply(plan)

    cloned = _read_project(destination / "project.yaml")
    assert result["ok"] and cloned["project_uuid"] == plan.target_project_uuid
    assert cloned["preparation"]["project_uuid"] == plan.target_project_uuid
    assert cloned["root"] == str(destination)
    assert cloned["members"]["clean_slab"] == str(destination / "clean")
    config = str(destination / "configs" / "site-1")
    assert cloned["config_species"] == {config: "Li2S8"}
    assert cloned["config_species_evidence"][config]["clean_member"] == str(
        destination / "clean"
    )
    assert cloned["dataset_groups"][0]["configs"] == [config]
    assert cloned["species_ref_jobs"]["Li2S8"] == str(external)
    assert cloned["dataset_groups"][0]["reference_job"] == str(external)
    assert cloned["audit_note"] == _read_project(source)["audit_note"]
    manifest = _read_project(destination / "clean" / "job.yaml")
    assert manifest["inputs"]["remote_namespace"] == cloned["remote_namespace"]
    assert manifest["job_uuid"] != "job-clean"
    assert _read_project(source)["project_uuid"] == UUID_A
    assert _read_registry(registry) == [str(source), str(destination / "project.yaml")]
    assert _read_ledger(ledger) == [
        *(str(path) for path in source_jobs),
        str(destination / "clean"),
        str(destination / "configs" / "site-1"),
        str(destination / "molecules" / "Li2S8"),
    ]


def test_clone_rejects_existing_destination_without_writing(tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    destination = tmp_path / "occupied"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    service = ProjectLifecycleService(registry)

    plan = service.preflight_clone(source, destination)

    assert not plan.ready
    assert {item["code"] for item in plan.conflicts} == {"destination_exists"}
    with pytest.raises(ProjectLifecycleError, match="unresolved conflicts"):
        service.apply(plan)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_clone_preflight_blocks_relative_member_that_escapes_project_root(tmp_path):
    source = _project(tmp_path / "source")
    project = _read_project(source)
    project["members"]["clean_slab"] = "../shared-job"
    _write_yaml(source, project)
    registry = _registry(tmp_path / "projects.json", [source])
    service = ProjectLifecycleService(registry)

    plan = service.preflight_clone(source, tmp_path / "clone")

    assert not plan.ready
    assert "unsafe_relative_locator" in {item["code"] for item in plan.conflicts}
    assert not (tmp_path / "clone").exists()


def test_job_manifest_change_after_preflight_is_stale_and_does_not_mutate(tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    ledger = _ledger(tmp_path / "jobs.json", [source.parent / "clean"])
    destination = tmp_path / "clone"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_clone(source, destination)
    job_file = source.parent / "clean" / "job.yaml"
    changed = _read_project(job_file)
    changed["state"] = "FAILED"
    _write_yaml(job_file, changed)

    with pytest.raises(ProjectLifecycleError) as failure:
        service.apply(plan)

    assert failure.value.code == "preflight_stale"
    assert not destination.exists()
    assert _read_registry(registry) == [str(source)]
    assert _read_ledger(ledger) == [str(source.parent / "clean")]


def test_move_preflight_blocks_cross_volume_or_conflicting_job_authority(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    destination = tmp_path / "moved"
    duplicate = tmp_path / "other-job"
    _write_yaml(duplicate / "job.yaml", {"job_id": "job-clean", "state": "DONE"})
    registry = _registry(tmp_path / "projects.json", [source])
    _ledger(tmp_path / "jobs.json", [
        source.parent / "clean", duplicate, destination / "configs" / "site-1",
    ])
    service = ProjectLifecycleService(registry)

    conflict_plan = service.preflight_move(source, destination)

    codes = {item["code"] for item in conflict_plan.conflicts}
    assert "duplicate_job_identity" in codes
    assert "target_job_already_registered" in codes

    monkeypatch.setattr(lifecycle, "_same_filesystem", lambda _source, _parent: False)
    cross_volume = service.preflight_move(source, tmp_path / "different-volume")
    assert "cross_volume_move_unsupported" in {
        item["code"] for item in cross_volume.conflicts
    }


def test_preflight_blocks_stale_project_local_jobs_ledger_path(tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    _ledger(tmp_path / "jobs.json", [source.parent / "missing-job"])
    service = ProjectLifecycleService(registry)

    plan = service.preflight_clone(source, tmp_path / "clone")

    assert "ledger_job_unreadable" in {item["code"] for item in plan.conflicts}


def test_clone_registry_failure_removes_unregistered_destination(tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    destination = tmp_path / "clone"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_clone(source, destination)
    monkeypatch.setattr(
        service, "_write_registry",
        lambda _payload, _entries: (_ for _ in ()).throw(OSError("registry unavailable")),
    )

    with pytest.raises(ProjectLifecycleError, match="failed safely"):
        service.apply(plan)

    assert not destination.exists()
    assert source.is_file()
    assert _read_registry(registry) == [str(source)]


def test_clone_commit_time_registry_cas_preserves_concurrent_registration(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    destination = tmp_path / "clone"
    concurrent = tmp_path / "concurrent" / "project.yaml"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_clone(source, destination)
    real_copytree = service._copytree

    def copy_then_register(src, dst, **kwargs):
        result = real_copytree(src, dst, **kwargs)
        registry.write_text(
            json.dumps({"projects": [str(source), str(concurrent)]}), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(service, "_copytree", copy_then_register)

    with pytest.raises(ProjectLifecycleError) as failure:
        service.apply(plan)

    assert failure.value.code == "preflight_stale"
    assert not destination.exists()
    assert _read_registry(registry) == [str(source), str(concurrent)]


def test_clone_commit_time_ledger_cas_preserves_concurrent_job_registration(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    source_jobs = [source.parent / "clean", source.parent / "configs" / "site-1"]
    ledger = _ledger(tmp_path / "jobs.json", source_jobs)
    destination = tmp_path / "clone"
    concurrent = tmp_path / "unrelated-job"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_clone(source, destination)
    real_copytree = service._copytree

    def copy_then_register(src, dst, **kwargs):
        result = real_copytree(src, dst, **kwargs)
        ledger.write_text(json.dumps({
            "job_dirs": [*(str(path) for path in source_jobs), str(concurrent)]
        }), encoding="utf-8")
        return result

    monkeypatch.setattr(service, "_copytree", copy_then_register)

    with pytest.raises(ProjectLifecycleError) as failure:
        service.apply(plan)

    assert failure.value.code == "preflight_stale"
    assert not destination.exists()
    assert _read_registry(registry) == [str(source)]
    assert _read_ledger(ledger) == [*(str(path) for path in source_jobs), str(concurrent)]


def test_clone_never_overwrites_destination_that_appears_during_copy(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    destination = tmp_path / "clone"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_clone(source, destination)
    real_copytree = service._copytree

    def copy_then_conflict(src, dst, **kwargs):
        result = real_copytree(src, dst, **kwargs)
        destination.mkdir()
        (destination / "keep.txt").write_text("keep", encoding="utf-8")
        return result

    monkeypatch.setattr(service, "_copytree", copy_then_conflict)

    with pytest.raises(ProjectLifecycleError) as failure:
        service.apply(plan)

    assert failure.value.code == "destination_exists"
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert source.is_file() and _read_registry(registry) == [str(source)]


def test_move_preserves_uuid_rebases_local_paths_and_atomically_replaces_registry_entry(
        tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    source_jobs = [source.parent / "clean", source.parent / "configs" / "site-1",
                   source.parent / "molecules" / "Li2S8"]
    ledger = _ledger(tmp_path / "jobs.json", source_jobs)
    destination = tmp_path / "moved"
    service = ProjectLifecycleService(registry)

    plan = service.preflight_move(source, destination)
    result = service.apply(plan)

    assert result["ok"] and not source.parent.exists()
    moved = _read_project(destination / "project.yaml")
    assert moved["project_uuid"] == UUID_A
    assert moved["preparation"]["project_uuid"] == UUID_A
    assert moved["members"]["clean_slab"] == str(destination / "clean")
    assert _read_registry(registry) == [str(destination / "project.yaml")]
    assert _read_ledger(ledger) == [
        str(destination / "clean"),
        str(destination / "configs" / "site-1"),
        str(destination / "molecules" / "Li2S8"),
    ]
    assert _read_project(destination / "clean" / "job.yaml")["job_id"] == "job-clean"


def test_move_replaces_legacy_registry_root_locator_after_source_disappears(tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source.parent])
    destination = tmp_path / "moved"
    service = ProjectLifecycleService(registry)

    plan = service.preflight_move(source, destination)
    result = service.apply(plan)

    assert result["ok"]
    assert _read_registry(registry) == [str(destination / "project.yaml")]
    assert not source.parent.exists() and (destination / "project.yaml").is_file()


def test_move_registry_failure_rolls_filesystem_and_project_yaml_back(tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    original = source.read_bytes()
    registry = _registry(tmp_path / "projects.json", [source])
    destination = tmp_path / "moved"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_move(source, destination)

    monkeypatch.setattr(
        service, "_write_registry",
        lambda _payload, _entries: (_ for _ in ()).throw(OSError("registry unavailable")),
    )

    with pytest.raises(ProjectLifecycleError, match="failed safely"):
        service.apply(plan)

    assert source.is_file() and source.read_bytes() == original
    assert not destination.exists()
    assert _read_registry(registry) == [str(source)]


def test_move_recovery_preserves_owned_copy_when_source_path_is_recreated_empty(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    original = source.read_bytes()
    registry = _registry(tmp_path / "projects.json", [source])
    source_jobs = [source.parent / "clean", source.parent / "configs" / "site-1"]
    ledger = _ledger(tmp_path / "jobs.json", source_jobs)
    registry_before = registry.read_bytes()
    ledger_before = ledger.read_bytes()
    destination = tmp_path / "moved"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_move(source, destination)

    def recreate_source_then_fail(_payload, _entries):
        source.parent.mkdir()
        (source.parent / "external.txt").write_text("not the project", encoding="utf-8")
        raise OSError("registry unavailable")

    monkeypatch.setattr(service, "_write_registry", recreate_source_then_fail)

    with pytest.raises(ProjectLifecyclePartialRollbackError) as failure:
        service.apply(plan)

    assert failure.value.code == "partial_rollback"
    assert source.parent.is_dir() and not source.exists()
    assert (source.parent / "external.txt").read_text(encoding="utf-8") == (
        "not the project"
    )
    assert (destination / "project.yaml").read_bytes() == original
    assert (destination / ".vcstudio-lifecycle-owner.json").is_file()
    assert service.journal_path.is_file()
    assert registry.read_bytes() == registry_before
    assert ledger.read_bytes() == ledger_before

    with pytest.raises(ProjectLifecyclePartialRollbackError) as restart_failure:
        ProjectLifecycleService(registry)

    assert restart_failure.value.code == "partial_rollback"
    assert (destination / "project.yaml").read_bytes() == original
    assert service.journal_path.is_file()

    shutil.rmtree(source.parent)
    recovered = service.recover_pending()

    assert recovered["status"] == "rolled_back"
    assert source.read_bytes() == original and not destination.exists()
    assert not (source.parent / ".vcstudio-lifecycle-owner.json").exists()
    assert not service.journal_path.exists()


def test_ledger_write_failure_rolls_back_filesystem_registry_and_ledger(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    source_jobs = [source.parent / "clean", source.parent / "configs" / "site-1"]
    ledger = _ledger(tmp_path / "jobs.json", source_jobs)
    registry_before = registry.read_bytes()
    ledger_before = ledger.read_bytes()
    destination = tmp_path / "clone"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_clone(source, destination)
    monkeypatch.setattr(
        service, "_write_ledger",
        lambda _payload, _entries: (_ for _ in ()).throw(OSError("ledger unavailable")),
    )

    with pytest.raises(ProjectLifecycleError, match="failed safely"):
        service.apply(plan)

    assert source.is_file() and not destination.exists()
    assert registry.read_bytes() == registry_before
    assert ledger.read_bytes() == ledger_before


def test_move_reports_partial_rollback_when_original_location_cannot_be_restored(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    destination = tmp_path / "moved"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_move(source, destination)
    calls = 0

    def first_move_only(src, dst):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("rollback device unavailable")
        return shutil.move(src, dst)

    monkeypatch.setattr(service, "_move_path", first_move_only)
    monkeypatch.setattr(
        service, "_write_registry",
        lambda _payload, _entries: (_ for _ in ()).throw(OSError("registry unavailable")),
    )

    with pytest.raises(ProjectLifecyclePartialRollbackError) as failure:
        service.apply(plan)

    assert failure.value.code == "partial_rollback"
    assert not source.parent.exists() and destination.is_dir()
    assert _read_registry(registry) == [str(source)]


def test_move_rolls_back_without_touching_destination_that_appears_during_stage(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    original = source.read_bytes()
    registry = _registry(tmp_path / "projects.json", [source])
    destination = tmp_path / "moved"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_move(source, destination)
    calls = 0

    def move_then_conflict(src, dst):
        nonlocal calls
        calls += 1
        result = shutil.move(src, dst)
        if calls == 1:
            destination.mkdir()
            (destination / "keep.txt").write_text("keep", encoding="utf-8")
        return result

    monkeypatch.setattr(service, "_move_path", move_then_conflict)

    with pytest.raises(ProjectLifecycleError) as failure:
        service.apply(plan)

    assert failure.value.code == "destination_exists"
    assert source.read_bytes() == original
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert _read_registry(registry) == [str(source)]


def test_adopt_copy_defaults_to_remint_and_rebases_recorded_original_root(tmp_path):
    original = _project(tmp_path / "original")
    copied_root = tmp_path / "copied"
    shutil.copytree(original.parent, copied_root)
    copied = copied_root / "project.yaml"
    registry = _registry(tmp_path / "projects.json", [original])
    original_jobs = [original.parent / "clean", original.parent / "configs" / "site-1",
                     original.parent / "molecules" / "Li2S8"]
    ledger = _ledger(tmp_path / "jobs.json", original_jobs)
    service = ProjectLifecycleService(registry)

    preserve = service.preflight_adopt(copied, "preserve")
    assert not preserve.ready
    assert "duplicate_project_uuid" in {item["code"] for item in preserve.conflicts}

    remint = service.preflight_adopt(copied)
    assert remint.ready and remint.identity_mode == "remint"
    result = service.apply(remint)

    adopted = _read_project(copied)
    assert result["identity_mode"] == "remint"
    assert adopted["project_uuid"] not in {None, UUID_A}
    assert adopted["root"] == str(copied_root)
    assert adopted["members"]["clean_slab"] == str(copied_root / "clean")
    assert _read_project(original)["project_uuid"] == UUID_A
    assert _read_registry(registry) == [str(original), str(copied)]
    adopted_job = _read_project(copied_root / "clean" / "job.yaml")
    assert adopted_job["job_uuid"] != "job-clean"
    assert _read_ledger(ledger) == [
        *(str(path) for path in original_jobs),
        str(copied_root / "clean"),
        str(copied_root / "configs" / "site-1"),
        str(copied_root / "molecules" / "Li2S8"),
    ]


def test_adopt_duplicate_path_is_blocked_even_when_remint_requested(tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    service = ProjectLifecycleService(registry)

    plan = service.preflight_adopt(source, "remint")

    assert not plan.ready
    assert "path_already_registered" in {item["code"] for item in plan.conflicts}


def test_adopt_registry_failure_restores_exact_project_and_job_metadata(tmp_path, monkeypatch):
    source = _project(tmp_path / "copy")
    registry = _registry(tmp_path / "projects.json", [])
    project_before = source.read_bytes()
    job = source.parent / "clean" / "job.yaml"
    job_before = job.read_bytes()
    service = ProjectLifecycleService(registry)
    plan = service.preflight_adopt(source)
    monkeypatch.setattr(
        service, "_write_registry",
        lambda _payload, _entries: (_ for _ in ()).throw(OSError("registry unavailable")),
    )

    with pytest.raises(ProjectLifecycleError, match="not adopted"):
        service.apply(plan)

    assert source.read_bytes() == project_before
    assert job.read_bytes() == job_before
    assert _read_registry(registry) == []


def test_apply_rejects_project_or_registry_changes_after_preflight(tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    service = ProjectLifecycleService(registry)
    project_plan = service.preflight_clone(source, tmp_path / "clone-a")
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ProjectLifecycleError) as changed_project:
        service.apply(project_plan)
    assert changed_project.value.code == "preflight_stale"

    registry_plan = service.preflight_clone(source, tmp_path / "clone-b")
    registry.write_text(json.dumps({"projects": [str(source)], "revision": 2}),
                        encoding="utf-8")
    with pytest.raises(ProjectLifecycleError) as changed_registry:
        service.apply(registry_plan)
    assert changed_registry.value.code == "preflight_stale"


def test_restart_recovery_rolls_back_move_after_registry_projection(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    original_project = source.read_bytes()
    registry = _registry(tmp_path / "projects.json", [source])
    source_jobs = [source.parent / "clean", source.parent / "configs" / "site-1"]
    ledger = _ledger(tmp_path / "jobs.json", source_jobs)
    registry_before = registry.read_bytes()
    ledger_before = ledger.read_bytes()
    destination = tmp_path / "moved"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_move(source, destination)
    real_phase = service._journal_phase

    def crash_after_registry(record, status):
        real_phase(record, status)
        if status == "registry_written":
            raise _SimulatedCrash()

    monkeypatch.setattr(service, "_journal_phase", crash_after_registry)

    with pytest.raises(_SimulatedCrash):
        service.apply(plan)
    assert destination.is_dir() and not source.parent.exists()

    recovered = ProjectLifecycleService(registry)

    assert source.read_bytes() == original_project
    assert not destination.exists()
    assert registry.read_bytes() == registry_before
    assert ledger.read_bytes() == ledger_before
    assert recovered.recover_pending()["status"] == "clean"
    assert not service.journal_path.exists()


def test_restart_recovery_finalizes_committed_clone_without_rolling_it_back(
        tmp_path, monkeypatch):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    ledger = _ledger(tmp_path / "jobs.json", [source.parent / "clean"])
    destination = tmp_path / "clone"
    service = ProjectLifecycleService(registry)
    plan = service.preflight_clone(source, destination)
    monkeypatch.setattr(
        service, "_cleanup_committed_journal",
        lambda _record: (_ for _ in ()).throw(OSError("simulated process exit")),
    )

    result = service.apply(plan)

    assert result["ok"] and service.journal_path.is_file()
    ProjectLifecycleService(registry)
    assert (destination / "project.yaml").is_file()
    assert str(destination / "project.yaml") in _read_registry(registry)
    assert str(destination / "clean") in _read_ledger(ledger)
    assert not service.journal_path.exists()
    assert not (destination / ".vcstudio-lifecycle-owner.json").exists()


def test_two_processes_share_one_global_lifecycle_lock_and_only_one_wins(tmp_path):
    source = _project(tmp_path / "source")
    registry = _registry(tmp_path / "projects.json", [source])
    ledger = _ledger(tmp_path / "jobs.json", [source.parent / "clean"])
    planner = ProjectLifecycleService(registry)
    winner_plan = planner.preflight_clone(source, tmp_path / "winner")
    loser_plan = planner.preflight_clone(source, tmp_path / "loser")
    context = multiprocessing.get_context("spawn")
    held = context.Event()
    release = context.Event()
    outcomes = context.Queue()
    winner = context.Process(
        target=_process_apply,
        args=(str(registry), winner_plan, held, release, True, outcomes),
    )
    loser = context.Process(
        target=_process_apply,
        args=(str(registry), loser_plan, held, release, False, outcomes),
    )
    winner.start()
    try:
        assert held.wait(20), "winner never acquired the lifecycle transaction"
        loser.start()
        loser.join(20)
        assert not loser.is_alive(), "busy contender did not fail promptly"
    finally:
        release.set()
        winner.join(20)
        if loser.pid and loser.is_alive():
            loser.terminate()
            loser.join(5)
        if winner.is_alive():
            winner.terminate()
            winner.join(5)

    results = {outcomes.get(timeout=5), outcomes.get(timeout=5)}
    assert ("ok", "clone") in results
    assert ("error", "lifecycle_busy") in results
    assert winner.exitcode == 0 and loser.exitcode == 0
    assert (tmp_path / "winner" / "project.yaml").is_file()
    assert not (tmp_path / "loser").exists()
    json.loads(registry.read_text(encoding="utf-8"))
    json.loads(ledger.read_text(encoding="utf-8"))
    yaml.safe_load((tmp_path / "winner" / "project.yaml").read_text(encoding="utf-8"))
    yaml.safe_load((tmp_path / "winner" / "clean" / "job.yaml").read_text(
        encoding="utf-8"))
