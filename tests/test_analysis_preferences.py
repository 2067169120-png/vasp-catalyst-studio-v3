from __future__ import annotations

import json
import multiprocessing
import threading

import pytest

from vcstudio.project import analysis_preferences as preferences
from vcstudio.project.analysis_registry import normalize_analysis_request


PROJECT = "project-a"


def _template(template_id="my-screen", *, name="My screen",
              analysis_id="adsorption-energy", spec=None):
    return {
        "id": template_id,
        "name": name,
        "analysis_id": analysis_id,
        "spec": {
            "data_mode": "stable",
            "precision": 6,
            "sort": {"key": "energy", "direction": "desc"},
            **(spec or {}),
        },
    }


def _process_update(path, template_id, start, output):
    try:
        start.wait(10)
        result = preferences.AnalysisPreferencesStore(
            path, lock_timeout=10.0).update(
                _template(template_id), project_id=PROJECT, expected_revision=0)
        output.put({
            "ok": result["ok"],
            "conflict": result["conflict"],
            "revision": result["revision"],
        })
    except Exception as exc:  # pragma: no cover - reported to the parent assertion
        output.put({"error": f"{type(exc).__name__}: {exc}"})


def test_missing_file_reads_safe_default_without_creating_files(tmp_path):
    path = tmp_path / "analysis-preferences.json"
    store = preferences.AnalysisPreferencesStore(path)

    result = store.read()

    assert result == {
        "ok": True,
        "conflict": False,
        "revision": 0,
        "preferences": {
            "schema": preferences.SCHEMA,
            "templates": [],
            "favorites": [],
            "default_template_by_analysis": {},
        },
        "error": None,
    }
    assert not path.exists()
    assert not store.lock_path.exists()


def test_template_roundtrip_strips_every_cross_project_identity(tmp_path):
    path = tmp_path / "analysis-preferences.json"

    def clock():
        return "2026-08-11T12:00:00.000000Z"

    store = preferences.AnalysisPreferencesStore(path, clock=clock)
    request = {
        "id": "comparison-screen",
        "name": "Reusable comparison",
        "analysis_id": "multi-project-comparison",
        "spec": {
            "schema": "vcstudio.analysis-spec/v1",
            "analysis_id": "multi-project-comparison",
            "project_id": PROJECT,
            "comparison_project_ids": [PROJECT, "project-b"],
            "baseline_project_id": "project-b",
            "data_mode": "stable",
            "precision": 7,
            "missing_policy": "complete_cases",
            "sensitivity_deadbands_eV": [0.3, 0.1, 0.2, 0.1],
            "view_id": "method-audit",
        },
    }

    result = store.update(
        request, project_id=PROJECT, expected_revision=0, set_default=True)

    assert result["ok"] is True
    assert result["revision"] == 1
    assert result["template"] == {
        "id": "comparison-screen",
        "name": "Reusable comparison",
        "analysis_id": "multi-project-comparison",
        "spec": {
                "data_mode": "stable",
            "precision": 7,
            "missing_policy": "complete_cases",
            "sensitivity_deadbands_eV": [0.1, 0.2, 0.3],
        },
        "created_at": "2026-08-11T12:00:00.000000Z",
        "updated_at": "2026-08-11T12:00:00.000000Z",
    }
    assert result["preferences"]["default_template_by_analysis"] == {
        "multi-project-comparison": "comparison-screen",
    }
    persisted_text = path.read_text(encoding="utf-8")
    assert PROJECT not in persisted_text
    assert "project-b" not in persisted_text
    assert "comparison_project_ids" not in persisted_text
    assert "baseline_project_id" not in persisted_text
    assert "view_id" not in persisted_text

    # The persisted patch is reusable with a different server-resolved project.
    saved = store.read()["preferences"]["templates"][0]
    applied = normalize_analysis_request(
        {"analysis_id": saved["analysis_id"], **saved["spec"]},
        project_id="another-project")
    assert applied.project_id == "another-project"
    assert applied.baseline_project_id is None
    assert applied.comparison_project_ids == ("another-project",)


def test_template_project_binding_must_match_caller_owned_project(tmp_path):
    store = preferences.AnalysisPreferencesStore(tmp_path / "prefs.json")
    request = _template(spec={"project_id": "project-b"})

    with pytest.raises(preferences.AnalysisPreferencesError, match="binding mismatch"):
        store.update(request, project_id=PROJECT, expected_revision=0)

    assert not store.path.exists()


def test_update_returns_canonical_template_and_preserves_created_timestamp(tmp_path):
    timestamps = iter([
        "2026-08-11T12:00:00.000001Z",
        "2026-08-11T12:00:00.000002Z",
    ])
    store = preferences.AnalysisPreferencesStore(
        tmp_path / "prefs.json", clock=lambda: next(timestamps))
    first = store.update(
        _template(), project_id=PROJECT, expected_revision=0)

    edited = _template(name="Edited", spec={"precision": 8})
    second = store.update(
        edited, project_id=PROJECT, expected_revision=1)

    assert second["template"]["name"] == "Edited"
    assert second["template"]["spec"]["precision"] == 8
    assert second["template"]["created_at"] == first["template"]["created_at"]
    assert second["template"]["updated_at"] == "2026-08-11T12:00:00.000002Z"
    assert second["preferences"]["templates"] == [second["template"]]


def test_delete_clears_custom_default_and_favorite_roundtrips(tmp_path):
    store = preferences.AnalysisPreferencesStore(tmp_path / "prefs.json")
    saved = store.update(
        _template(), project_id=PROJECT, expected_revision=0, set_default=True)
    favored = store.favorite(
        "adsorption-energy", favorite=True,
        expected_revision=saved["revision"])

    assert favored["preferences"]["favorites"] == ["adsorption-energy"]
    deleted = store.delete(
        "my-screen", expected_revision=favored["revision"])

    assert deleted["ok"] is True
    assert deleted["deleted_template_id"] == "my-screen"
    assert deleted["preferences"]["templates"] == []
    assert deleted["preferences"]["default_template_by_analysis"] == {}
    assert deleted["preferences"]["favorites"] == ["adsorption-energy"]
    unfavored = store.favorite(
        "adsorption-energy", favorite=False,
        expected_revision=deleted["revision"])
    assert unfavored["preferences"]["favorites"] == []


def test_default_may_reference_matching_builtin_but_builtin_cannot_be_changed(tmp_path):
    store = preferences.AnalysisPreferencesStore(tmp_path / "prefs.json")

    selected = store.set_default(
        "adsorption-energy", "stable-screen", expected_revision=0)
    assert selected["preferences"]["default_template_by_analysis"] == {
        "adsorption-energy": "stable-screen",
    }
    with pytest.raises(preferences.AnalysisPreferencesError, match="built-in"):
        store.update(
            _template("stable-screen"), project_id=PROJECT,
            expected_revision=selected["revision"])
    with pytest.raises(preferences.AnalysisPreferencesError, match="built-in"):
        store.delete("stable-screen", expected_revision=selected["revision"])


def test_revision_conflict_returns_current_safe_snapshot_without_writing(tmp_path):
    path = tmp_path / "prefs.json"
    store = preferences.AnalysisPreferencesStore(path)
    winner = store.update(
        _template("winner"), project_id=PROJECT, expected_revision=0)
    before = path.read_bytes()

    stale = store.update(
        _template("stale"), project_id=PROJECT, expected_revision=0)

    assert stale == {
        "ok": False,
        "conflict": True,
        "revision": 1,
        "preferences": winner["preferences"],
        "error": "analysis preferences revision conflict",
        "template": None,
    }
    assert path.read_bytes() == before
    assert [item["id"] for item in stale["preferences"]["templates"]] == ["winner"]


@pytest.mark.parametrize("mutator,match", [
    (lambda item: {**item, "unknown": True}, "unknown fields"),
    (lambda item: {**item, "id": "../private/template"}, "path"),
    (lambda item: {**item, "name": r"C:\\private\\template.json"}, "path"),
    (lambda item: {**item, "name": "token=top-secret-session"}, "credential"),
    (lambda item: {**item, "spec": {"near_degenerate_eV": float("nan")}}, "finite"),
    (lambda item: {**item, "spec": {"api_token": "abc"}}, "sensitive"),
    (lambda item: {
        **item,
        "spec": {"a": {"b": {"c": {"d": {"e": "too-deep"}}}}},
    }, "nested too deeply"),
])
def test_untrusted_templates_reject_unknown_path_secret_nan_and_nesting(
        tmp_path, mutator, match):
    store = preferences.AnalysisPreferencesStore(tmp_path / "prefs.json")

    with pytest.raises(preferences.AnalysisPreferencesError, match=match):
        store.update(
            mutator(_template()), project_id=PROJECT, expected_revision=0)

    assert not store.path.exists()


def test_corrupt_or_identity_bearing_files_fail_closed_and_are_not_reset(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_bytes(b'{"schema":')
    before = path.read_bytes()
    store = preferences.AnalysisPreferencesStore(path)

    with pytest.raises(preferences.AnalysisPreferencesError, match="cannot read"):
        store.read()
    with pytest.raises(preferences.AnalysisPreferencesError, match="cannot read"):
        store.update(
            _template(), project_id=PROJECT, expected_revision=0)
    assert path.read_bytes() == before

    path.unlink()
    store.update(_template(), project_id=PROJECT, expected_revision=0)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["templates"][0]["spec"]["project_id"] = PROJECT
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(preferences.AnalysisPreferencesError, match="identity"):
        store.read()


def test_atomic_replace_failure_preserves_old_bytes_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "prefs.json"
    store = preferences.AnalysisPreferencesStore(path)
    store.update(_template(), project_id=PROJECT, expected_revision=0)
    before = path.read_bytes()

    def boom(_source, _destination):
        raise OSError("disk full")

    monkeypatch.setattr(preferences.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.update(
            _template(name="must not land"),
            project_id=PROJECT, expected_revision=1)

    assert path.read_bytes() == before
    assert list(tmp_path.glob(".prefs.json.*.tmp")) == []


def test_two_threads_with_same_revision_have_one_winner(tmp_path):
    path = tmp_path / "prefs.json"
    start = threading.Barrier(2)
    results = []

    def worker(template_id):
        start.wait()
        results.append(preferences.AnalysisPreferencesStore(path).update(
            _template(template_id), project_id=PROJECT, expected_revision=0))

    threads = [
        threading.Thread(target=worker, args=("thread-a",)),
        threading.Thread(target=worker, args=("thread-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted((result["ok"], result["conflict"]) for result in results) == [
        (False, True), (True, False),
    ]
    assert preferences.AnalysisPreferencesStore(path).read()["revision"] == 1


def test_two_processes_use_advisory_lock_for_one_cas_winner(tmp_path):
    path = tmp_path / "prefs.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_update, args=(str(path), "process-a", start, output)),
        context.Process(
            target=_process_update, args=(str(path), "process-b", start, output)),
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(15)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert not any("error" in result for result in results), results
    assert sorted((result["ok"], result["conflict"]) for result in results) == [
        (False, True), (True, False),
    ]
    final = preferences.AnalysisPreferencesStore(path).read()
    assert final["revision"] == 1
    assert len(final["preferences"]["templates"]) == 1
