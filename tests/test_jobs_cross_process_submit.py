"""Cross-process submission ownership and fail-closed recovery regressions."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from vcstudio.cluster import submitter
from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.shared import manifest


_OPERATION_ID = "jobs-submit-cross-process-001"


class _Sink:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def write(self, _value):
        return None


class _SFTP:
    def put(self, _local, _remote):
        return None

    def file(self, _path, mode="w"):
        del mode
        return _Sink()


def _profile() -> ClusterProfile:
    return ClusterProfile(
        name="race", hostname="host", username="user", auth="key", key_path="key",
        remote_root="/work/jobs", scheduler="PBS", scheduler_bin="/bin",
        queue="batch", nodes=1, ppn=1, walltime="00:10:00", env_lines=[],
        vasp_cmd="", script_mode="auto",
        engine_commands={"cp2k": "cp2k -i {input} -o {stem}.out"},
    )


def _quick_job(root: Path) -> Path:
    job = root / "job"
    job.mkdir()
    (job / "calc.inp").write_text(
        "&GLOBAL\n RUN_TYPE ENERGY\n&END GLOBAL\n"
        "&FORCE_EVAL\n &DFT\n  &MGRID\n   CUTOFF 400\n  &END MGRID\n"
        " &END DFT\n &SUBSYS\n  &CELL\n   ABC 10 10 10\n  &END CELL\n"
        "  &COORD\n   H 0 0 0\n  &END COORD\n"
        "  &KIND H\n  &END KIND\n &END SUBSYS\n&END FORCE_EVAL\n",
        encoding="utf-8",
    )
    data = manifest.new_manifest(
        job_id="race-1", system="race", task_type="quick", calc_type="",
        inputs={"engine": "cp2k", "files": ["calc.inp"]},
    )
    manifest.save_manifest(job, data)
    return job


def _spawn_submit(job, marker_dir, identifier, entered, release, hold, outcomes):
    """Spawn-safe worker with a scheduler seam that records actual submissions."""
    from vcstudio.cluster import submitter as worker_submitter

    def fake_run(_client, command, check=False):
        del check
        if "qsub" not in command:
            return "", ""
        Path(marker_dir, f"remote-{identifier}").write_text(command, encoding="utf-8")
        entered.set()
        if hold:
            release.wait(timeout=15)
        return f"{identifier}.cluster\n", ""

    worker_submitter.run_cmd = fake_run
    try:
        result = worker_submitter.submit_job(
            object(), _SFTP(), _profile(), job, idempotency_key=_OPERATION_ID)
        outcomes.put({
            "kind": "ok",
            "job_id": result.get("scheduler_job_id"),
            "replayed": bool(result.get("_submission_replayed")),
        })
    except worker_submitter.JobOperationBusy as exc:
        outcomes.put({"kind": "busy", "code": exc.code, "message": str(exc)})
    except Exception as exc:  # pragma: no cover - surfaced verbatim to parent assertion
        outcomes.put({"kind": "error", "type": type(exc).__name__, "message": str(exc)})


@pytest.mark.skipif(os.name not in {"nt", "posix"}, reason="requires OS advisory locks")
def test_spawned_submitters_have_one_remote_winner_and_durable_replay(tmp_path):
    job = _quick_job(tmp_path)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    outcomes = context.Queue()

    winner = context.Process(
        target=_spawn_submit,
        args=(str(job), str(tmp_path), "11001", entered, release, True, outcomes),
    )
    winner.start()
    assert entered.wait(timeout=10), "first process did not reach the scheduler seam"

    contender_entered = context.Event()
    contender = context.Process(
        target=_spawn_submit,
        args=(str(job), str(tmp_path), "22002", contender_entered, release, False, outcomes),
    )
    contender.start()
    contender.join(timeout=10)
    assert not contender.is_alive(), "busy contender did not fail promptly"
    release.set()
    winner.join(timeout=10)
    assert not winner.is_alive(), "winning submitter did not finish"

    first_round = [outcomes.get(timeout=3), outcomes.get(timeout=3)]
    assert sorted(item["kind"] for item in first_round) == ["busy", "ok"]
    assert next(item for item in first_round if item["kind"] == "busy")["code"] == "job_busy"
    assert len(list(tmp_path.glob("remote-*"))) == 1

    # A later process with the same durable operation id obtains the lock,
    # re-reads the authoritative manifest, and returns it without qsub.
    replay_entered = context.Event()
    replay = context.Process(
        target=_spawn_submit,
        args=(str(job), str(tmp_path), "33003", replay_entered, release, False, outcomes),
    )
    replay.start()
    replay.join(timeout=10)
    assert not replay.is_alive()
    replay_result = outcomes.get(timeout=3)
    assert replay_result == {"kind": "ok", "job_id": "11001", "replayed": True}
    assert not replay_entered.is_set()
    assert len(list(tmp_path.glob("remote-*"))) == 1

    authoritative = manifest.load_manifest(job)
    assert authoritative is not None
    assert authoritative["state"] == "SUBMITTED"
    assert authoritative["scheduler_job_id"] == "11001"
    assert authoritative["attempts"][-1]["idempotency_key"] == _OPERATION_ID
    assert not (job / ".vcstudio-submit-recovery.json").exists()
    assert not list(job.glob(".job.yaml.*.tmp"))


def test_remote_accept_then_manifest_failure_blocks_automatic_retry(tmp_path, monkeypatch):
    job = _quick_job(tmp_path)
    scheduler_calls = []

    def fake_run(_client, command, check=False):
        del check
        if "qsub" in command:
            scheduler_calls.append(command)
            return "991.cluster\n", ""
        return "", ""

    monkeypatch.setattr(submitter, "run_cmd", fake_run)
    real_save = manifest.save_manifest

    def fail_submitted_write(job_dir, payload):
        if payload.get("state") == "SUBMITTED":
            raise OSError("injected manifest persistence failure")
        return real_save(job_dir, payload)

    monkeypatch.setattr(submitter.manifest_mod, "save_manifest", fail_submitted_write)
    with pytest.raises(submitter.UnknownRemoteSubmission) as failure:
        submitter.submit_job(
            object(), _SFTP(), _profile(), str(job), idempotency_key=_OPERATION_ID)
    assert failure.value.code == "unknown_remote_submission"
    assert failure.value.requires_manual_recovery is True
    assert failure.value.recovery_status == "remote_accepted"
    assert failure.value.scheduler_job_id == "991"
    assert len(scheduler_calls) == 1

    recovery_path = job / ".vcstudio-submit-recovery.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert recovery["status"] == "remote_accepted"
    assert recovery["scheduler_job_id"] == "991"
    assert recovery["idempotency_key"] == _OPERATION_ID
    assert recovery["incar_sha256"] is None  # CP2K has no INCAR authority field.
    assert manifest.load_manifest(job)["state"] == "CREATED"

    monkeypatch.setattr(submitter.manifest_mod, "save_manifest", real_save)
    with pytest.raises(submitter.UnknownRemoteSubmission) as retry:
        submitter.submit_job(
            object(), _SFTP(), _profile(), str(job), idempotency_key=_OPERATION_ID)
    assert retry.value.requires_manual_recovery is True
    assert retry.value.scheduler_job_id == "991"
    assert len(scheduler_calls) == 1
    assert recovery_path.is_file()


def test_created_manifest_with_prior_attempt_is_not_treated_as_fresh(tmp_path, monkeypatch):
    job = _quick_job(tmp_path)
    data = manifest.load_manifest(job)
    data["attempts"] = [{"n": 1, "job_id": "ghost-remote"}]
    manifest.save_manifest(job, data)
    scheduler_calls = []

    def fake_run(_client, command, check=False):
        del check
        scheduler_calls.append(command)
        return "992.cluster\n", ""

    monkeypatch.setattr(submitter, "run_cmd", fake_run)
    with pytest.raises(ValueError, match="提交尝试记录"):
        submitter.submit_job(
            object(), _SFTP(), _profile(), str(job), idempotency_key=_OPERATION_ID)

    assert scheduler_calls == []
    assert manifest.load_manifest(job)["state"] == "CREATED"


def test_manifest_atomic_writer_uses_unique_staging_names(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    created = []
    real_mkstemp = manifest.tempfile.mkstemp

    def record_mkstemp(*args, **kwargs):
        descriptor, path = real_mkstemp(*args, **kwargs)
        created.append(Path(path).name)
        return descriptor, path

    monkeypatch.setattr(manifest.tempfile, "mkstemp", record_mkstemp)
    manifest.save_manifest(job, {"state": "CREATED", "job_id": "one"})
    manifest.save_manifest(job, {"state": "CREATED", "job_id": "two"})

    assert len(created) == 2 and len(set(created)) == 2
    assert all(name.startswith(".job.yaml.") and name.endswith(".tmp") for name in created)
    assert manifest.load_manifest(job)["job_id"] == "two"
    assert not list(job.glob(".job.yaml.*.tmp"))
