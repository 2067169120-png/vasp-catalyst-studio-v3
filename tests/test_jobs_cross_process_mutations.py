"""Spawned-process at-most-once regressions for continue and cancel."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.shared import manifest


_OPERATION_ID = "jobop-cross-process-mutation-001"
_CONTCAR = (
    "C restart\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n"
)


def _profile() -> ClusterProfile:
    return ClusterProfile(
        name="cluster-a", hostname="host", username="user", auth="key",
        key_path="key", remote_root="/work/jobs", scheduler="PBS",
        scheduler_bin="/bin", queue="batch", nodes=1, ppn=1,
        walltime="00:10:00", env_lines=[], vasp_cmd="vasp_std",
        script_mode="auto",
    )


def _job(root: Path, action: str) -> Path:
    job = root / action
    job.mkdir()
    (job / "POSCAR").write_text(_CONTCAR, encoding="utf-8")
    (job / "INCAR").write_text("ENCUT = 400\n", encoding="utf-8")
    data = manifest.new_manifest(
        job_id=f"{action}-local", system=action, task_type="relax",
        calc_type="", inputs={
            "sha256": {"INCAR": manifest.sha256_file(job / "INCAR")},
        },
    )
    data.update({
        "cluster": "cluster-a", "remote_dir": f"/work/jobs/{action}",
        "scheduler_job_id": "100", "state": "RUNNING" if action == "cancel"
        else "UNCONVERGED",
    })
    data["state_history"] = [{"state": data["state"], "at": "before"}]
    if action == "continue":
        data.setdefault("results", {})["diagnosis"] = {
            "failure_class": "NONCONVERGED", "restartable": True,
        }
    manifest.save_manifest(job, data)
    return job


def _spawn_mutation(action, job, marker_dir, identifier, entered, release, hold,
                    outcomes):
    from vcstudio.cluster import submitter

    def fake_run(_client, command, timeout=30, check=False):
        del timeout, check
        if command.startswith("cat "):
            return _CONTCAR, ""
        if "stat -c" in command:
            return "OUTCAR 10 1000\nOSZICAR 10 1000\n", ""
        scheduler = "qsub" in command or "qdel" in command
        if scheduler:
            Path(marker_dir, f"remote-{action}-{identifier}").write_text(
                command, encoding="utf-8")
            entered.set()
            if hold:
                release.wait(timeout=15)
            return (f"{identifier}.cluster\n", "") if "qsub" in command else ("", "")
        return "", ""

    submitter.run_cmd = fake_run
    try:
        if action == "continue":
            result = submitter.continue_from_contcar(
                object(), _profile(), job, idempotency_key=_OPERATION_ID)
            replayed = bool(result.get("_continue_replayed"))
        else:
            result = submitter.cancel_job(
                object(), _profile(), job, idempotency_key=_OPERATION_ID)
            replayed = bool(result.get("_cancel_replayed"))
        outcomes.put({
            "kind": "ok", "job_id": result.get("scheduler_job_id"),
            "replayed": replayed,
        })
    except submitter.JobOperationBusy as exc:
        outcomes.put({"kind": "busy", "code": exc.code})
    except Exception as exc:  # pragma: no cover - surfaced to the parent assertion
        outcomes.put({
            "kind": "error", "type": type(exc).__name__, "message": str(exc),
        })


@pytest.mark.skipif(os.name not in {"nt", "posix"}, reason="requires OS advisory locks")
@pytest.mark.parametrize("action", ["continue", "cancel"])
def test_spawned_continue_and_cancel_have_one_remote_winner_and_restart_replay(
        tmp_path, action):
    job = _job(tmp_path, action)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    outcomes = context.Queue()

    winner = context.Process(
        target=_spawn_mutation,
        args=(action, str(job), str(tmp_path), "201", entered, release, True, outcomes),
    )
    winner.start()
    assert entered.wait(timeout=10), "winner did not reach the scheduler seam"

    contender_entered = context.Event()
    contender = context.Process(
        target=_spawn_mutation,
        args=(action, str(job), str(tmp_path), "202", contender_entered,
              release, False, outcomes),
    )
    contender.start()
    contender.join(timeout=10)
    assert not contender.is_alive()
    release.set()
    winner.join(timeout=10)
    assert not winner.is_alive()

    first_round = [outcomes.get(timeout=3), outcomes.get(timeout=3)]
    assert sorted(item["kind"] for item in first_round) == ["busy", "ok"]
    assert next(item for item in first_round if item["kind"] == "busy")["code"] == \
        "job_busy"
    assert not contender_entered.is_set()
    assert len(list(tmp_path.glob(f"remote-{action}-*"))) == 1

    # A fresh process obtains the lock and replays durable job.yaml/journal
    # evidence without issuing qsub/qdel again.
    replay_entered = context.Event()
    replay = context.Process(
        target=_spawn_mutation,
        args=(action, str(job), str(tmp_path), "203", replay_entered,
              release, False, outcomes),
    )
    replay.start()
    replay.join(timeout=10)
    assert not replay.is_alive()
    replay_result = outcomes.get(timeout=3)
    assert replay_result["kind"] == "ok" and replay_result["replayed"] is True
    assert replay_result["job_id"] == ("201" if action == "continue" else "100")
    assert not replay_entered.is_set()
    assert len(list(tmp_path.glob(f"remote-{action}-*"))) == 1

    authoritative = manifest.load_manifest(job)
    expected_state = "SUBMITTED" if action == "continue" else "FAILED"
    assert authoritative["state"] == expected_state
    assert authoritative["attempts"][-1]["idempotency_key"] == _OPERATION_ID


def test_cancel_transport_interruption_is_durable_and_restart_fails_closed(
        tmp_path, monkeypatch):
    from vcstudio.cluster import submitter

    job = _job(tmp_path, "cancel")
    calls = []

    def interrupted(_client, command, timeout=30, check=False):
        del timeout, check
        calls.append(command)
        raise OSError("connection lost before scheduler acknowledgement")

    monkeypatch.setattr(submitter, "run_cmd", interrupted)
    with pytest.raises(submitter.UnknownRemoteJobOperation) as failure:
        submitter.cancel_job(
            object(), _profile(), job, idempotency_key=_OPERATION_ID)

    assert failure.value.requires_manual_recovery is True
    assert len(calls) == 1
    journal = submitter._read_job_action_journal(job)
    assert journal["operations"][-1]["status"] == "unknown_remote_outcome"

    calls.clear()
    with pytest.raises(submitter.UnknownRemoteJobOperation):
        submitter.cancel_job(
            object(), _profile(), job, idempotency_key=_OPERATION_ID)
    assert calls == []


def test_completed_cancel_key_cannot_replay_against_a_new_scheduler_generation(
        tmp_path, monkeypatch):
    from vcstudio.cluster import submitter

    job = _job(tmp_path, "cancel")
    commands = []
    monkeypatch.setattr(
        submitter, "run_cmd",
        lambda _client, command, **_kwargs: commands.append(command) or ("", ""),
    )
    submitter.cancel_job(
        object(), _profile(), job, idempotency_key=_OPERATION_ID)
    assert len(commands) == 1

    data = manifest.load_manifest(job)
    data["scheduler_job_id"] = "999"
    data["state"] = "RUNNING"
    manifest.save_manifest(job, data)
    commands.clear()

    with pytest.raises(submitter.UnknownRemoteJobOperation, match="代次"):
        submitter.cancel_job(
            object(), _profile(), job, idempotency_key=_OPERATION_ID)
    assert commands == []
