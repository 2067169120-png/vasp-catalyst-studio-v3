"""Legacy Tk bridge to the canonical project report bundle.

Importing this module is intentionally cheap.  The Web API façade is imported
and instantiated only inside :func:`get_api`, which is first reached from a
background ``gui.runner`` worker.  Both legacy Tk tabs therefore share one API
instance without paying the report dependency cost during Tk startup.
"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Sequence
from typing import Any


_ALLOWED_FORMATS = ("html", "docx", "pdf")
_SUCCESS_ARTIFACT_STATES = frozenset(("complete", "ready"))
_UNSAFE_STEM = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_API_LOCK = threading.Lock()
_API_SINGLETON = None


def _default_api_factory():
    # Deliberately local: importing report_bridge from a Tk tab must not import
    # gui_web.api or its optional report stack on the UI thread.
    from vcstudio.gui_web.api import Api

    return Api()


_api_factory = _default_api_factory


def get_api():
    """Return the process-local lazily constructed Web API façade."""
    global _API_SINGLETON
    if _API_SINGLETON is None:
        with _API_LOCK:
            if _API_SINGLETON is None:
                _API_SINGLETON = _api_factory()
    return _API_SINGLETON


def _failure(error: Any, *, gate_reason: str = "") -> dict:
    return {
        "ok": False,
        "artifact_ok": False,
        "artifact_status": "failed",
        "scientific_status": None,
        "scientific_qualification": None,
        "desired_report_kind": None,
        "publication_gate_status": "unknown",
        "gate_reason": str(gate_reason or ""),
        "files": {},
        "primary_file": None,
        "marker": None,
        "error": str(error or "报告生成失败"),
    }


def _formats(value: Sequence[str] | str | None) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if value is None:
        value = ("html",)
    result = []
    for item in value:
        fmt = str(item or "").strip().lower()
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError(
                f"不支持的报告格式 {item!r}；可选值为 {', '.join(_ALLOWED_FORMATS)}"
            )
        if fmt not in result:
            result.append(fmt)
    if not result:
        raise ValueError("至少选择一种报告格式")
    return tuple(result)


def _stem(value: Any) -> str:
    stem = str(value or "").strip()
    if (
        not stem
        or stem in {".", ".."}
        or stem.endswith((".", " "))
        or _UNSAFE_STEM.search(stem)
    ):
        raise ValueError("报告文件名 stem 非法：只能提供不含目录和 Windows 保留字符的文件名")
    return stem


def _files(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, path in value.items():
        if path in (None, ""):
            continue
        try:
            result[str(key)] = os.fspath(path)
        except TypeError:
            continue
    return result


def status_text(result: Any) -> str:
    """Return a compact user-facing status without conflating both axes."""
    value = result if isinstance(result, dict) else {}
    artifact = str(value.get("artifact_status") or "failed").strip().lower()
    science = str(value.get("scientific_status") or "pending").strip().lower()
    artifact_label = {
        "complete": "完整生成",
        "ready": "已登记",
        "generated_unrecorded": "已生成但未登记",
        "failed": "失败",
    }.get(artifact, artifact or "未知")
    science_label = {
        "final": "最终",
        "diagnostic": "诊断",
        "blocked": "阻断",
        "pending": "待判定",
        "draft": "草稿",
    }.get(science, science or "待判定")
    return f"报告产物状态：{artifact_label}；科学状态：{science_label}"


def project_report_status(project_path) -> dict:
    """Read canonical report freshness for a legacy Tk caller."""
    try:
        project = os.fspath(project_path).strip()
    except (TypeError, AttributeError):
        project = ""
    if not project:
        return {
            "ok": False,
            "artifact_status": "missing",
            "artifact_current": False,
            "scientific_status": None,
            "scientific_qualification": None,
            "desired_report_kind": None,
            "publication_gate_status": "unknown",
            "scientific_stale": False,
            "error": "未指定 project.yaml 路径",
        }
    try:
        raw = get_api()._proj_report_status_for_path(project)
    except Exception as exc:  # noqa: BLE001 - keep Tk status reads structured
        return {
            "ok": False,
            "artifact_status": "missing",
            "artifact_current": False,
            "scientific_status": None,
            "scientific_qualification": None,
            "desired_report_kind": None,
            "publication_gate_status": "unknown",
            "scientific_stale": False,
            "error": str(exc),
        }
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "artifact_status": "missing",
            "artifact_current": False,
            "scientific_status": None,
            "scientific_qualification": None,
            "desired_report_kind": None,
            "publication_gate_status": "unknown",
            "scientific_stale": False,
            "error": "报告状态 API 返回了无效结果",
        }
    return dict(raw)


def generate_project_report_bundle(
    project_path,
    out_dir,
    *,
    formats: Sequence[str] | str | None = ("html",),
    final: bool = True,
    requested_kind: str | None = None,
    stem: str = "report",
) -> dict:
    """Generate a canonical report bundle and return a stable Tk-facing result.

    The bridge never upgrades artifact success into scientific ``final``.  It
    preserves the API's scientific status and gate reason, while separately
    calculating whether every requested artifact still exists and is safe for
    the Tk caller to open.
    """
    try:
        project = os.fspath(project_path).strip()
    except (TypeError, AttributeError):
        return _failure("未指定有效的 project.yaml 路径")
    if not project:
        return _failure("未指定 project.yaml 路径")

    try:
        target_raw = os.fspath(out_dir).strip()
    except (TypeError, AttributeError):
        return _failure("未指定有效的报告输出目录")
    if not target_raw:
        return _failure("未指定报告输出目录")

    try:
        wanted = _formats(formats)
        safe_stem = _stem(stem)
    except (TypeError, ValueError) as exc:
        return _failure(exc)
    normalized_kind = str(requested_kind or "").strip().lower()
    if normalized_kind and normalized_kind not in {"diagnostic", "draft", "final"}:
        return _failure(
            "requested_kind 非法：只能是 diagnostic、draft 或 final"
        )

    target = os.path.abspath(os.path.normpath(os.path.expanduser(target_raw)))
    if os.path.exists(target) and not os.path.isdir(target):
        return _failure(f"报告输出路径不是目录：{target}")

    try:
        kwargs = {
            "formats": wanted,
            "final": bool(final),
            "stem": safe_stem,
        }
        if normalized_kind:
            kwargs["requested_kind"] = normalized_kind
        raw = get_api()._proj_report_bundle_for_path(project, target, **kwargs)
    except Exception as exc:  # noqa: BLE001 - Tk needs a structured failure
        return _failure(exc)
    if not isinstance(raw, dict):
        return _failure("报告 API 返回了无效结果")

    result = dict(raw)
    files = _files(result.get("files"))
    api_ok = result.get("ok") is not False and not result.get("error")
    artifact_status = str(result.get("artifact_status") or "").strip().lower()
    if not artifact_status:
        artifact_status = "complete" if api_ok and files else "failed"
    scientific_status = str(
        result.get("scientific_status")
        or result.get("report_status")
        or result.get("kind")
        or "pending"
    ).strip().lower()
    gate_reason = str(result.get("gate_reason") or result.get("report_reason") or "")
    primary = next((files.get(fmt) for fmt in wanted if files.get(fmt)), None)
    requested_exist = all(
        files.get(fmt) and os.path.isfile(files[fmt]) for fmt in wanted
    )
    artifact_ok = bool(
        api_ok
        and artifact_status in _SUCCESS_ARTIFACT_STATES
        and requested_exist
    )

    result.update({
        "ok": bool(api_ok and artifact_ok),
        "artifact_ok": artifact_ok,
        "artifact_status": artifact_status,
        "scientific_status": scientific_status or "pending",
        "gate_reason": gate_reason,
        "files": files,
        "primary_file": primary,
        "error": (None if artifact_ok else
                  str(result.get("error") or "报告产物未完整生成或已不可读")),
    })
    return result


__all__ = [
    "generate_project_report_bundle",
    "get_api",
    "project_report_status",
    "status_text",
]
