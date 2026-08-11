from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

from vcstudio.gui_web.api import Api
from vcstudio.project import paper_report
from vcstudio.project.report_service import ReportService, _PreviewRecord


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05"
    b"\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _Charts:
    @staticmethod
    def _write(path, formats):
        stem = Path(path).with_suffix("")
        outputs = []
        for fmt in formats:
            target = stem.with_suffix(f".{fmt}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_PNG if fmt == "png" else b"%PDF-1.4\n%%EOF\n")
            outputs.append(str(target))
        return outputs

    def heatmap_matrix(self, data, path, **kwargs):
        return self._write(path, kwargs.get("formats") or ("png",))

    def adsorption_bar(self, data, path, **kwargs):
        return self._write(path, kwargs.get("formats") or ("png",))

    def energy_matrix_table(self, data, path, **kwargs):
        return self._write(path, kwargs.get("formats") or ("png",))


def _batch_api(tmp_path):
    paths = []
    stores = {}
    summaries = {}
    manifests = {}
    for index, (name, letter) in enumerate((("Catalyst A", "a"), ("Catalyst B", "b"))):
        root = tmp_path / letter
        root.mkdir()
        project_path = str(root / "project.yaml")
        Path(project_path).write_text("schema: vcstudio.project/v1\n", encoding="utf-8")
        clean = str(root / "clean")
        reference = str(root / "reference")
        configs = [str(root / f"{letter}_Li2S8"), str(root / f"{letter}_Li2S6")]
        for directory in (clean, reference, *configs):
            Path(directory).mkdir()
        project_uuid = (letter * 32)
        project = {
            "name": name,
            "project_uuid": project_uuid,
            "root": str(root),
            "members": {
                "clean_slab": clean,
                "gas_ref": reference,
                "configs": configs,
            },
            "comparison_method_fingerprint": "same-method",
        }
        stores[project_path] = project
        summaries[project_uuid] = {
            "slab": ("DONE", -100.0),
            "ref": ("DONE", -10.0),
            "has_ref": True,
            "reference_mode": "single",
            "comparison_method_fingerprint": "same-method",
            "method_consistency": {
                "status": "verified", "issues": [], "warnings": [],
            },
            "rows": [
                {
                    "name": Path(configs[0]).name,
                    "species": "Li2S8", "state": "DONE",
                    "delta_e": -4.0 + index * 0.2,
                    "reference_valid": True,
                    "method_check": {"status": "verified"}, "note": "",
                },
                {
                    "name": Path(configs[1]).name,
                    "species": "Li2S6", "state": "DONE",
                    "delta_e": -3.0 + index * 0.1,
                    "reference_valid": True,
                    "method_check": {"status": "verified"}, "note": "",
                },
            ],
        }
        for member_index, directory in enumerate((clean, reference, *configs)):
            manifests[directory] = {
                "job_uuid": f"job-{letter}-{member_index}",
                "state": "DONE",
                "attempts": [{"n": 1, "attempt_token": f"{letter}-{member_index}-a1"}],
                "results": {
                    "energy_e0_eV": -100.0 - member_index,
                    "fetched_sha256": {"OUTCAR": letter * 64},
                },
            }
        paths.append(project_path)

    def load_project(path):
        value = stores.get(str(path))
        return copy.deepcopy(value) if value is not None else None

    def save_project(root, value):
        matches = [path for path, project in stores.items()
                   if Path(project["root"]).resolve() == Path(root).resolve()]
        assert len(matches) == 1
        stores[matches[0]] = copy.deepcopy(value)

    adsorption = SimpleNamespace(
        list_projects=lambda: list(paths),
        load_project=load_project,
        save_project=save_project,
        delta_e_rows=lambda project: copy.deepcopy(
            summaries[str(project["project_uuid"])]),
    )
    manifest = SimpleNamespace(
        load_manifest=lambda path: copy.deepcopy(manifests.get(str(path))))
    state = {"inside_host": False, "renders": 0}

    def guarded_render(*args, **kwargs):
        assert state["inside_host"] is True
        state["renders"] += 1
        return paper_report.render_report_bundle(*args, **kwargs)

    paper = SimpleNamespace(
        report_capabilities=paper_report.report_capabilities,
        report_content_sha256=paper_report.report_content_sha256,
        render_report_html_preview=paper_report.render_report_html_preview,
        render_report_bundle=guarded_render,
    )
    api = Api(
        adsorption_mod=adsorption,
        manifest_mod=manifest,
        native_charts_mod=_Charts(),
        paper_report_mod=paper,
    )
    api._proj_fed = lambda *_args, **_kwargs: (None, "not available")
    original_host_render = api._report_workbench_render_build

    def guarded_host(*args, **kwargs):
        state["inside_host"] = True
        try:
            return original_host_render(*args, **kwargs)
        finally:
            state["inside_host"] = False

    api._report_workbench_render_build = guarded_host
    return api, paths, stores, summaries, state


def _project_ids(api, paths, stores):
    return [api._workspace_project_id(path, stores[path]) for path in paths]


def test_comparison_report_uses_revision_history_marker_and_stale_replay(tmp_path):
    api, paths, stores, summaries, state = _batch_api(tmp_path)
    project_ids = _project_ids(api, paths, stores)
    out_dir = tmp_path / "reports"

    first = api.proj_batch_report(
        project_ids, str(out_dir), formats=["html"], include_individual=False,
        requested_kind="final")
    second = api.proj_batch_report(
        project_ids, str(out_dir), formats=["html"], include_individual=False,
        requested_kind="final")

    assert first["ok"] is True and second["ok"] is True
    assert first["kind"] == second["kind"] == "diagnostic"
    assert first["revision"]["sequence"] == 1
    assert second["revision"]["sequence"] == 2
    assert second["marker"]["scope_kind"] == "comparison"
    assert second["marker"]["comparison_project_ids"] == [
        api._workspace_project_id(path, stores[path]) for path in paths]
    assert state["renders"] == 2
    history = api.report_workbench_history(
        api._workspace_project_id(paths[0], stores[paths[0]]))
    assert history["ok"] is True
    assert [item["sequence"] for item in history["revisions"]] == [2, 1]
    assert all(item["current"] is True for item in history["revisions"])
    status = api.proj_report_status(project_ids[0])
    assert status["artifact_status"] == "ready"
    assert status["artifact_current"] is True
    assert status["revision"]["sequence"] == 2

    summaries[stores[paths[1]]["project_uuid"]]["rows"][0]["delta_e"] -= 0.5
    stale = api.proj_report_status(project_ids[0])
    assert stale["artifact_status"] == "stale"
    assert stale["artifact_current"] is False


def test_comparison_preview_uses_shared_bounded_store_and_cleans_temp_roots(tmp_path):
    api, paths, stores, _summaries, _state = _batch_api(tmp_path)
    project_ids = _project_ids(api, paths, stores)
    service = ReportService(
        api, preview_limit=1, temp_root=tmp_path / "bounded-previews"
    )
    api._report_service_instance = service
    sentinel_root = tmp_path / "bounded-previews" / "sentinel"
    sentinel_root.mkdir(parents=True)
    anchor_id = api._workspace_project_id(paths[0], stores[paths[0]])
    sentinel = _PreviewRecord(
        preview_id="sentinel-preview",
        operation_id="sentinel-operation",
        project_id=anchor_id,
        project_path=paths[0],
        project_root=str(Path(paths[0]).parent),
        report_id="sentinel-report",
        created_at_utc="2026-01-01T00:00:00+00:00",
        created_at=0.0,
        expires_at=service._clock() + service._ttl,
        base_revision=0,
        base_manifest_sha256=None,
        build={},
        token={},
        temp_root=str(sentinel_root),
    )
    service._register_preview(sentinel)
    registered = []
    original_register = service._register_preview

    def observe_register(record):
        original_register(record)
        registered.append({
            "kind": record.build.get("build_kind"),
            "count": len(service._previews),
            "sentinel_exists": sentinel_root.exists(),
            "temp_root": Path(record.temp_root),
        })

    service._register_preview = observe_register

    result = api.proj_batch_report(
        project_ids,
        str(tmp_path / "reports"),
        formats=["html"],
        include_individual=False,
        requested_kind="diagnostic",
    )

    assert result["ok"] is True
    assert registered and registered[0]["kind"] == "comparison"
    assert registered[0]["count"] == 1
    assert registered[0]["sentinel_exists"] is False
    assert not registered[0]["temp_root"].exists()
    assert service._previews == {}


def test_batch_individual_reports_are_revisioned_and_main_marker_wins(tmp_path):
    api, paths, stores, _summaries, state = _batch_api(tmp_path)
    project_ids = _project_ids(api, paths, stores)
    api._report_bundle_unrecorded = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("batch reports must not use the unrecorded seam"))

    result = api.proj_batch_report(
        project_ids, str(tmp_path / "reports"), formats=["html"],
        include_individual=True, requested_kind="diagnostic")

    assert result["ok"] is True
    assert result["revision"]["sequence"] == 1
    assert len(result["files"]["individual"]) == 2
    assert all(item["ok"] is True for item in result["files"]["individual"])
    assert all(item["revision"]["sequence"] == 1
               for item in result["files"]["individual"])
    assert stores[paths[0]]["autopilot_report"]["scope_kind"] == "comparison"
    assert stores[paths[1]]["autopilot_report"].get("scope_kind") != "comparison"
    assert state["renders"] == 3
    anchor_history = api.report_workbench_history(
        api._workspace_project_id(paths[0], stores[paths[0]]))
    second_history = api.report_workbench_history(
        api._workspace_project_id(paths[1], stores[paths[1]]))
    assert len(anchor_history["revisions"]) == 2
    assert len(second_history["revisions"]) == 1


def test_comparison_exception_preserves_successful_individual_revisions(tmp_path):
    api, paths, stores, _summaries, state = _batch_api(tmp_path)
    project_ids = _project_ids(api, paths, stores)

    def fail_comparison(*_args, **_kwargs):
        raise RuntimeError("comparison publisher unavailable")

    api._comparison_report_publish_via_service = fail_comparison
    result = api.proj_batch_report(
        project_ids, str(tmp_path / "reports"), formats=["html"],
        include_individual=True, requested_kind="diagnostic")

    assert result["ok"] is False
    assert result["artifact_status"] == "partial_success"
    assert result["requested_kind"] == "diagnostic"
    assert result["files"]["comparison"] == {}
    assert len(result["files"]["individual"]) == 2
    assert all(item["ok"] is True for item in result["files"]["individual"])
    assert all(item["revision"]["sequence"] == 1
               for item in result["files"]["individual"])
    assert all(Path(item["files"]["html"]).is_file()
               for item in result["files"]["individual"])
    assert "comparison publisher unavailable" in result["error"]
    assert state["renders"] == 2
    assert all(stores[path]["autopilot_report"].get("scope_kind") != "comparison"
               for path in paths)


def test_comparison_failure_without_success_is_failed_and_has_no_fake_file(
        tmp_path):
    api, paths, stores, _summaries, state = _batch_api(tmp_path)
    project_ids = _project_ids(api, paths, stores)

    def reject_comparison(selected, _out_dir, *, preset_key, wanted, requested):
        build = api._comparison_report_build(
            selected, preset_key, wanted, requested, tmp_path / "comparison-build")
        return ({
            "ok": False,
            "artifact_status": "partial_success",
            "error": "comparison publish rejected",
            "files": {"html": "must-not-leak.html"},
        }, build)

    api._comparison_report_publish_via_service = reject_comparison
    result = api.proj_batch_report(
        project_ids, str(tmp_path / "reports"), formats=["html"],
        include_individual=False, requested_kind="diagnostic")

    assert result["ok"] is False
    assert result["artifact_status"] == "failed"
    assert result["files"] == {"comparison": {}, "individual": []}
    assert result["error"] == "comparison publish rejected"
    assert state["renders"] == 0
