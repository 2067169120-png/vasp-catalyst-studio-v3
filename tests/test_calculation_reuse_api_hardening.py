"""API/UI resource and authority boundaries for strict calculation reuse."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.gui_web.api import Api
from vcstudio.project import calculation_reuse as reuse


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"


class _Ledger:
    def __init__(self, entries=()):
        self.entries = list(entries)

    def load_all(self):
        return list(self.entries)


def _profiles(store):
    return types.SimpleNamespace(
        ClusterProfile=ClusterProfile,
        load_profiles=lambda: dict(store),
        save_profiles=lambda profiles: store.clear() or store.update(profiles),
    )


def _api(entries=(), profiles=None):
    instance = Api(
        ledger_mod=_Ledger(entries),
        profiles_mod=_profiles(profiles if profiles is not None else {}))
    instance._project_role_map = lambda: {}
    return instance


class _AdvisoryIndex:
    def __init__(self):
        ids = ["target", "candidate-c", "candidate-a", "candidate-b"]
        self.records = [types.SimpleNamespace(job_id=item) for item in ids]
        self.truncated = True
        self.total_entries = 999
        self.limit = 4

    def advisory(self, targets):
        records = [row for row in self.records if row.job_id not in targets]
        matches = [{
            "source_job_id": row.job_id,
            "project_relation": "unknown",
            "verification": {"status": "incomplete", "reusable": False,
                             "issues": [r"C:\private\source\OUTCAR"]},
            "saved_estimate": {"status": "unknown", "core_hours": None},
            "fingerprint": {"status": "incomplete", "fields": []},
            "directory": r"C:\private\source",
            "token": "secret-token",
        } for row in records]
        return {
            "schema": reuse.ADVISORY_SCHEMA,
            "ok": True,
            "advisory_only": True,
            "automatic_reuse": False,
            "equivalence_claim": False,
            "authorizes_submission": False,
            "requires_user_confirmation": True,
            "targets": [{
                "target_job_id": targets[0],
                "fingerprint": {"status": "incomplete", "fields": []},
                "exact_matches": matches,
                "near_matches": [],
                "source_statuses": {"incomplete": [
                    row["source_job_id"] for row in matches]},
                "requires_explicit_choice": False,
                "default_action": "recalculate",
            }],
            "index": {
                "schema": reuse.INDEX_SCHEMA,
                "capacity": self.limit,
                "indexed": len(self.records),
                "observed": self.total_entries,
                "truncated": self.truncated,
                "rebuildable": True,
                "authoritative": False,
            },
        }


def test_advisory_enforces_target_candidate_and_page_limits():
    api = _api()

    too_many_targets = api.jobs_reuse_advisory(
        [f"job-{index}" for index in range(33)])
    bad_page = api.jobs_reuse_advisory(["target"], page_size=129)
    bad_candidates = api.jobs_reuse_advisory(["target"], candidate_limit=129)

    assert too_many_targets["ok"] is False
    assert "between 1 and 32" in too_many_targets["error"]
    assert bad_page["ok"] is False and "between 1 and 128" in bad_page["error"]
    assert bad_candidates["ok"] is False and "at most 128" in bad_candidates["error"]
    assert all(result["authorizes_submission"] is False for result in (
        too_many_targets, bad_page, bad_candidates))


def test_advisory_paginates_with_stable_order_and_redacts_private_values(monkeypatch):
    api = _api()
    index = _AdvisoryIndex()
    monkeypatch.setattr(
        api, "_calculation_reuse_index",
        lambda **_kwargs: index)

    result = api.jobs_reuse_advisory(
        ["target"], cursor=1, page_size=1, candidate_limit=4,
        sort_order="job_id_asc")

    assert result["ok"] is True and result["truncated"] is True
    assert result["pagination"] == {
        "cursor": 1,
        "page_size": 1,
        "returned_candidates": 1,
        "bounded_candidates": 3,
        "next_cursor": 2,
        "has_more": True,
        "sort_order": "job_id_asc",
    }
    matches = result["targets"][0]["exact_matches"]
    assert [item["source_job_id"] for item in matches] == ["candidate-b"]
    assert matches[0]["project_relation"] == "unknown"
    assert "directory" not in matches[0] and "token" not in matches[0]
    assert "private" not in str(result).lower()
    assert result["index"]["authoritative"] is False
    assert result["absence_authoritative"] is False
    assert result["authorizes_submission"] is False


def test_profile_bound_advisory_binds_server_side_before_index(monkeypatch, tmp_path):
    job_dir = tmp_path / "target"
    manifest = {"job_uuid": "target"}
    profile = ClusterProfile(
        name="hpc", vasp_version="6.4.3",
        vasp_build_identity="vasp_std-linux-x86_64-gcc12-openmpi4",
        vasp_build_evidence_sha256="a" * 64)
    api = _api([(str(job_dir), manifest)], {"hpc": profile})
    calls = []
    monkeypatch.setattr(
        api, "_bind_reuse_execution_environment",
        lambda dirs, selected: calls.append((list(dirs), selected.name)))
    monkeypatch.setattr(
        api, "_calculation_reuse_index", lambda **_kwargs: _AdvisoryIndex())

    result = api.jobs_reuse_advisory(["target"], profile_name="hpc")

    assert result["ok"] is True
    assert calls == [([str(job_dir)], "hpc")]


def test_submission_guard_uses_full_authoritative_lookup_not_bounded_index(
        monkeypatch, tmp_path):
    target = tmp_path / "target"
    source = tmp_path / "source"
    entries = [
        (str(target), {"job_uuid": "target"}),
        (str(source), {"job_uuid": "source"}),
    ]
    api = _api(entries)
    calls = []

    def lookup(given_entries, target_ids, *, job_id, project_id=None):
        calls.append((len(list(given_entries)), list(target_ids), project_id is not None))
        assert job_id(str(target), entries[0][1]) == "target"
        return {
            "schema": "vcstudio.authoritative-reuse-lookup/v1",
            "authoritative": True,
            "complete": True,
            "authorizes_submission": False,
            "targets": [{
                "target_job_id": "target",
                "fingerprint": {"schema": reuse.FINGERPRINT_SCHEMA,
                                "status": "complete", "digest": "a" * 64},
                "exact_matches": [{
                    "source_job_id": "source", "project_relation": "unknown",
                    "verification": {"reusable": True, "status": "verified"},
                }],
                "requires_explicit_choice": True,
            }],
        }

    monkeypatch.setattr(reuse, "authoritative_reuse_lookup", lookup, raising=False)
    monkeypatch.setattr(reuse, "has_current_force_recalculation",
                        lambda _manifest, _fingerprint: False)
    monkeypatch.setattr(
        api, "_calculation_reuse_index",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("bounded advisory index must not authorize submission")))

    blocked = api._reuse_submission_guard([str(target)])

    assert calls == [(2, ["target"], True)]
    assert blocked["code"] == "reuse_decision_required"
    assert blocked["blocked_job_ids"] == ["target"]
    assert blocked["advisory"]["authoritative"] is True
    assert str(tmp_path) not in str(blocked)


def test_submission_guard_fails_closed_when_authoritative_scan_is_incomplete(
        monkeypatch, tmp_path):
    target = tmp_path / "target"
    api = _api([(str(target), {"job_uuid": "target"})])
    monkeypatch.setattr(
        reuse, "authoritative_reuse_lookup",
        lambda *_args, **_kwargs: {
            "authoritative": True, "complete": False,
            "authorizes_submission": False, "targets": [],
        }, raising=False)

    with pytest.raises(RuntimeError, match="incomplete"):
        api._reuse_submission_guard([str(target)])


def test_submit_sanitizes_environment_binding_failure(monkeypatch, tmp_path):
    target = tmp_path / "private" / "target"
    profile = ClusterProfile(name="hpc")
    api = _api([(str(target), {"job_uuid": "target"})], {"hpc": profile})
    monkeypatch.setattr(
        api, "_bind_reuse_execution_environment",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError(f"invalid authority at {target}")))

    result = api.submit_jobs([str(target)], "hpc", None)

    assert result["ok"] is False
    assert str(tmp_path) not in result["error"]
    assert "<local-path>" in result["error"]


def test_profile_environment_identity_is_atomic_validated_and_path_free():
    store = {}
    api = _api(profiles=store)
    evidence = "ABCDEF0123456789" * 4

    saved = api.save_profile({
        "name": "hpc",
        "vasp_version": "6.4.3",
        "vasp_build_identity": "vasp_std-linux-x86_64-gcc12-openmpi4",
        "vasp_build_evidence_sha256": evidence,
    })
    partial = api.save_profile({
        "name": "partial", "vasp_version": "6.4.3",
        "vasp_build_identity": "", "vasp_build_evidence_sha256": "",
    })
    locator = api.save_profile({
        "name": "locator", "vasp_version": "6.4.3",
        "vasp_build_identity": r"C:\private\vasp_std",
        "vasp_build_evidence_sha256": "a" * 64,
    })

    assert saved == {"ok": True, "error": None}
    assert store["hpc"].vasp_build_evidence_sha256 == evidence.lower()
    assert api.list_profiles()["profiles"][0]["vasp_build_identity"] == (
        "vasp_std-linux-x86_64-gcc12-openmpi4")
    assert partial["ok"] is False and "同时填写" in partial["error"]
    assert locator["ok"] is False and "无路径" in locator["error"]
    assert "partial" not in store and "locator" not in store


def test_browser_only_requests_server_evidence_and_shows_unknown_relation():
    script = (ASSETS / "jobs.js").read_text(encoding="utf-8")
    cluster = (ASSETS / "cluster.js").read_text(encoding="utf-8")
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    query = script[
        script.index("async function inspectSelectedReuse"):
        script.index("async function referenceExistingResult")]
    renderer = script[
        script.index("function reuseMatchHtml"):
        script.index("function renderReuseAdvisory")]

    assert "profileName" in query
    assert "VCS.call('jobs_reuse_advisory', ids)" in query
    assert "'job_id_asc', profileName" in query
    assert "project_relation" in renderer and "project_unknown" in renderer
    assert "match.cross_project" not in renderer
    assert "verification.reusable === true && relation !== 'unknown'" in renderer
    for forbidden in (
            "POSCAR", "INCAR", "KPOINTS", "POTCAR", "FileReader",
            "crypto.subtle", "parseFloat", "Math."):
        assert forbidden not in query
    for identifier in ("cl-vaspversion", "cl-vaspbuild", "cl-vaspevidence"):
        assert identifier in html and identifier in cluster
    assert "浏览器只保存这组服务端声明" in html
