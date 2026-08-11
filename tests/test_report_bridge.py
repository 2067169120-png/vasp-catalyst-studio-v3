"""Legacy Tk report bridge tests without constructing a Tk root."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vcstudio.gui import report_bridge
from vcstudio.gui import jobs_tab
from vcstudio.gui import project_tab


@pytest.fixture(autouse=True)
def _reset_bridge_singleton(monkeypatch):
    monkeypatch.setattr(report_bridge, "_API_SINGLETON", None)


class _FakeApi:
    def __init__(self, calls, *, scientific_status="diagnostic", raise_error=None,
                 write_file=True):
        self.calls = calls
        self.scientific_status = scientific_status
        self.raise_error = raise_error
        self.write_file = write_file

    def _proj_report_bundle_for_path(
            self, project_path, out_dir, *, formats, final, stem,
            requested_kind=None):
        call = {
            "project_path": project_path,
            "out_dir": out_dir,
            "formats": formats,
            "final": final,
            "stem": stem,
        }
        if requested_kind is not None:
            call["requested_kind"] = requested_kind
        self.calls.append(call)
        if self.raise_error:
            raise self.raise_error
        files = {}
        for fmt in formats:
            output = Path(out_dir) / f"{stem}.{fmt}"
            if self.write_file:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(f"{self.scientific_status} {fmt}", encoding="utf-8")
            files[fmt] = str(output)
        science = requested_kind or self.scientific_status
        return {
            "ok": True,
            "artifact_status": "complete",
            "scientific_status": science,
            "gate_reason": "参考态尚未满足最终门禁",
            "files": files,
            "error": None,
        }

    def _proj_report_status_for_path(self, project_path):
        self.calls.append({"status_path": project_path})
        return {
            "schema": "vcstudio.report-status/v1",
            "ok": True,
            "artifact_status": "ready",
            "artifact_current": True,
            "scientific_status": self.scientific_status,
            "scientific_stale": False,
            "files": {"html": "C:/reports/current.html"},
            "error": None,
        }


def test_importing_bridge_does_not_import_heavy_web_api():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import vcstudio.gui.report_bridge; "
                "print('vcstudio.gui_web.api' in sys.modules)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "False"


def test_bridge_is_lazy_singleton_and_calls_canonical_api(tmp_path, monkeypatch):
    calls = []
    factories = []

    def factory():
        factories.append("created")
        return _FakeApi(calls)

    monkeypatch.setattr(report_bridge, "_api_factory", factory)
    assert factories == []

    first = report_bridge.generate_project_report_bundle(
        tmp_path / "project.yaml",
        tmp_path / "reports",
        formats=("html",),
        final=True,
        stem="催化剂_完整报告",
    )
    second = report_bridge.generate_project_report_bundle(
        tmp_path / "project.yaml",
        tmp_path / "reports-2",
        formats=("html",),
        final=True,
        stem="second",
    )

    assert factories == ["created"]
    assert len(calls) == 2
    assert calls[0] == {
        "project_path": str(tmp_path / "project.yaml"),
        "out_dir": str((tmp_path / "reports").resolve()),
        "formats": ("html",),
        "final": True,
        "stem": "催化剂_完整报告",
    }
    assert first["ok"] is True and first["artifact_ok"] is True
    assert first["artifact_status"] == "complete"
    assert first["scientific_status"] == "diagnostic"
    assert first["gate_reason"] == "参考态尚未满足最终门禁"
    assert Path(first["primary_file"]).is_file()
    assert second["artifact_ok"] is True


@pytest.mark.parametrize(
    ("project_path", "out_dir", "formats", "stem", "message"),
    (
        ("", "reports", ("html",), "report", "project.yaml"),
        ("project.yaml", "", ("html",), "report", "输出目录"),
        ("project.yaml", "reports", (), "report", "至少选择"),
        ("project.yaml", "reports", ("txt",), "report", "不支持的报告格式"),
        ("project.yaml", "reports", ("html",), "../escape", "stem 非法"),
        ("project.yaml", "reports", ("html",), r"C:\escape", "stem 非法"),
        ("project.yaml", "reports", ("html",), "trailing.", "stem 非法"),
    ),
)
def test_bridge_rejects_invalid_paths_formats_and_stems_without_loading_api(
        tmp_path, monkeypatch, project_path, out_dir, formats, stem, message):
    loaded = []
    monkeypatch.setattr(report_bridge, "_api_factory", lambda: loaded.append(True))
    target = "" if not out_dir else tmp_path / out_dir

    result = report_bridge.generate_project_report_bundle(
        project_path, target, formats=formats, stem=stem)

    assert result == {
        "ok": False,
        "artifact_ok": False,
        "artifact_status": "failed",
        "scientific_status": None,
        "scientific_qualification": None,
        "desired_report_kind": None,
        "publication_gate_status": "unknown",
        "gate_reason": "",
        "files": {},
        "primary_file": None,
        "marker": None,
        "error": result["error"],
    }
    assert message in result["error"]
    assert loaded == []


def test_bridge_rejects_file_as_output_directory(tmp_path, monkeypatch):
    destination = tmp_path / "not-a-directory"
    destination.write_text("occupied", encoding="utf-8")
    loaded = []
    monkeypatch.setattr(report_bridge, "_api_factory", lambda: loaded.append(True))

    result = report_bridge.generate_project_report_bundle(
        "project.yaml", destination, stem="report")

    assert result["ok"] is False and result["artifact_status"] == "failed"
    assert "不是目录" in result["error"]
    assert loaded == []


def test_bridge_normalizes_api_exception_and_missing_artifact(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        report_bridge,
        "_api_factory",
        lambda: _FakeApi(calls, raise_error=RuntimeError("renderer unavailable")),
    )
    failed = report_bridge.generate_project_report_bundle(
        "project.yaml", tmp_path / "failed", stem="report")
    assert failed["ok"] is False and failed["artifact_ok"] is False
    assert failed["scientific_status"] is None
    assert "renderer unavailable" in failed["error"]

    monkeypatch.setattr(report_bridge, "_API_SINGLETON", None)
    monkeypatch.setattr(
        report_bridge,
        "_api_factory",
        lambda: _FakeApi(calls, scientific_status="final", write_file=False),
    )
    missing = report_bridge.generate_project_report_bundle(
        "project.yaml", tmp_path / "missing", stem="report")
    assert missing["ok"] is False and missing["artifact_ok"] is False
    assert missing["artifact_status"] == "complete"
    assert missing["scientific_status"] == "final"
    assert "不可读" in missing["error"]


def test_bridge_forwards_explicit_draft_without_upgrading_it(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(report_bridge, "_api_factory", lambda: _FakeApi(calls))

    result = report_bridge.generate_project_report_bundle(
        "project.yaml",
        tmp_path / "draft",
        formats=("html",),
        final=True,
        requested_kind="draft",
        stem="draft-report",
    )

    assert result["ok"] is True
    assert result["artifact_ok"] is True
    assert result["scientific_status"] == "draft"
    assert calls == [{
        "project_path": "project.yaml",
        "out_dir": str((tmp_path / "draft").resolve()),
        "formats": ("html",),
        "final": True,
        "stem": "draft-report",
        "requested_kind": "draft",
    }]


def test_bridge_rejects_unknown_requested_kind_without_loading_api(
        tmp_path, monkeypatch):
    loaded = []
    monkeypatch.setattr(report_bridge, "_api_factory", lambda: loaded.append(True))

    result = report_bridge.generate_project_report_bundle(
        "project.yaml", tmp_path / "reports", requested_kind="reviewed")

    assert result["ok"] is False
    assert "requested_kind" in result["error"]
    assert loaded == []


def test_status_text_keeps_artifact_and_science_axes_separate():
    text = report_bridge.status_text({
        "artifact_status": "complete",
        "scientific_status": "diagnostic",
    })
    assert text == "报告产物状态：完整生成；科学状态：诊断"


def test_project_report_status_uses_canonical_api_not_file_mtime(monkeypatch):
    calls = []
    monkeypatch.setattr(
        report_bridge, "_api_factory", lambda: _FakeApi(calls, scientific_status="final"))

    result = report_bridge.project_report_status("C:/project/project.yaml")

    assert result["ok"] is True
    assert result["artifact_current"] is True
    assert result["scientific_status"] == "final"
    assert calls == [{"status_path": "C:/project/project.yaml"}]


def test_project_tab_never_opens_an_unsuccessful_artifact(tmp_path, monkeypatch):
    report = tmp_path / "diagnostic.html"
    report.write_text("diagnostic", encoding="utf-8")
    payload = {
        "ok": False,
        "artifact_ok": False,
        "artifact_status": "generated_unrecorded",
        "scientific_status": "diagnostic",
        "gate_reason": "输入在生成期间变化",
        "primary_file": str(report),
        "error": "产物未登记",
    }
    messages = []
    opened = []
    dummy = SimpleNamespace(
        log=SimpleNamespace(write=messages.append),
        after=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(project_tab.runner, "poll", lambda _queue: ("ok", payload))
    monkeypatch.setattr(project_tab.sys, "platform", "win32")
    monkeypatch.setattr(
        project_tab.os, "startfile", lambda path: opened.append(path), raising=False)

    project_tab.ProjectTab._poll_report(dummy, object())

    assert opened == []
    assert any("报告产物状态：已生成但未登记；科学状态：诊断" in msg
               for msg in messages)
    assert any("报告产物不可用" in msg for msg in messages)


def test_project_tab_opens_successful_diagnostic_but_does_not_call_it_final(
        tmp_path, monkeypatch):
    report = tmp_path / "diagnostic.html"
    report.write_text("diagnostic", encoding="utf-8")
    payload = {
        "ok": True,
        "artifact_ok": True,
        "artifact_status": "complete",
        "scientific_status": "diagnostic",
        "gate_reason": "缺少完整自由能路径",
        "primary_file": str(report),
        "error": None,
    }
    messages = []
    opened = []
    dummy = SimpleNamespace(
        log=SimpleNamespace(write=messages.append),
        after=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(project_tab.runner, "poll", lambda _queue: ("ok", payload))
    monkeypatch.setattr(project_tab.sys, "platform", "win32")
    monkeypatch.setattr(
        project_tab.os, "startfile", lambda path: opened.append(path), raising=False)

    project_tab.ProjectTab._poll_report(dummy, object())

    assert opened == [str(report)]
    assert any("科学状态：诊断" in msg for msg in messages)
    assert any(msg.startswith("⚠ 报告产物已生成") for msg in messages)
    assert not any("最终报告已生成" in msg for msg in messages)


def test_jobs_tab_auto_report_logs_both_statuses_and_gate(monkeypatch):
    payload = {
        "ok": True,
        "artifact_ok": True,
        "artifact_status": "complete",
        "scientific_status": "diagnostic",
        "gate_reason": "方法证据尚未完成",
        "primary_file": "C:/reports/demo.html",
        "error": None,
    }
    messages = []
    dummy = SimpleNamespace(
        log=SimpleNamespace(write=messages.append),
        after=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(jobs_tab.runner, "poll", lambda _queue: ("ok", payload))

    jobs_tab.JobsTab._poll_report(dummy, object(), "demo")

    assert any("报告产物状态：完整生成；科学状态：诊断" in msg
               for msg in messages)
    assert any("科学门禁:方法证据尚未完成" in msg for msg in messages)
    assert any(msg.startswith("⚠ 项目「demo」报告产物已生成") for msg in messages)


def test_legacy_project_entry_points_delegate_to_bridge_only():
    root = Path(__file__).resolve().parents[1]
    project_source = (root / "vcstudio/gui/project_tab.py").read_text(encoding="utf-8")
    jobs_source = (root / "vcstudio/gui/jobs_tab.py").read_text(encoding="utf-8")

    assert "report_bridge.generate_project_report_bundle" in project_source
    assert "report_bridge.generate_project_report_bundle" in jobs_source
    assert "report_bridge.project_report_status" in jobs_source
    assert "report_full.generate_project_report" not in project_source
    assert "report_full.generate_project_report" not in jobs_source
    assert "os.path.getmtime" not in jobs_source[
        jobs_source.index("def _maybe_auto_reports"):jobs_source.index("def _poll_report")
    ]
    assert "report_full._member_dirs" in project_source
    assert "report_full._member_dirs" in jobs_source
    for source in (project_source, jobs_source):
        assert "formats=('html',)" in source
        assert "final=True" in source
        assert "stem=stem" in source
    assert "from vcstudio.gui_web.api import Api" not in project_source
    assert "from vcstudio.gui_web.api import Api" not in jobs_source
