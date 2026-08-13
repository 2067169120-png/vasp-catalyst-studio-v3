"""Versioned report-workbench presets and its untrusted request boundary.

The browser may choose presentation and scope preferences, but it must never be
able to submit a snapshot, validation decision, scientific qualification, or
revision.  This module is therefore the sole server-side registry for the Phase
C built-ins and the strict DTO normalizer that constructs :class:`ReportSpec`.

Presets are content defaults only.  Scientific state is resolved later by the
report service from a freshly built snapshot and the applicable validation gate.
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from vcstudio.project.report_contracts import DEFAULT_OUTLINE, REPORT_KINDS, ReportSpec


CATALOG_SCHEMA = "vcstudio.report-workbench-catalog/v1"

PRESET_IDS = (
    "quick-decision-brief",
    "scientific-review",
    "manuscript-materials",
    "supporting-information",
    "diagnostic-repair",
)
SUPPORTED_LOCALES = ("zh-CN", "en-US")
SUPPORTED_FORMATS = ("html", "docx", "pdf")
_ACCESSIBILITY_STATUSES = frozenset({
    "conditional", "partial", "unsupported", "unknown",
})
_ACCESSIBILITY_BOOLEAN_FIELDS = (
    "visual",
    "searchable",
    "semantic_structure",
    "document_language",
    "metadata",
    "image_alt",
    "tagged",
    "pdf_ua",
    "manual_review_required",
)
SUPPORTED_THEMES = (
    "compact-brief",
    "academic-a4",
    "diagnostic-a4",
)
SUPPORTED_AUDIENCES = (
    "project-lead",
    "researcher",
    "author",
    "reviewer",
)

_REQUEST_KEYS = frozenset({
    "preset_id",
    "requested_kind",
    "audience",
    "locale",
    "formats",
    "scope",
    "outline",
    "theme_id",
    "options",
})
_SCOPE_KEYS = frozenset({
    "kind",
    "project_ids",
    "job_ids",
    "species",
    "configuration_ids",
    "stable_only",
    "include_failed",
})
_OPTION_KEYS = frozenset({
    "bilingual_mode",
    "precision",
    "include_thermochemistry",
    "version_policy",
})
# Phase C publishes one complete locale at a time.  Mixed-language bodies are a
# Phase E internationalisation deliverable and must not be accepted as if they
# already changed the rendered report.
_BILINGUAL_MODES = ("none",)
# Replacing an existing report is deliberately not an MVP client option.  The
# report service may add an administrative replacement workflow later, but the
# public workbench defaults to immutable revisions.
_VERSION_POLICIES = ("new_revision",)

_MAX_STRING_LENGTH = 256
_MAX_SEQUENCE_LENGTH = 256
_MAX_MAPPING_LENGTH = 64
_MAX_NESTING_DEPTH = 8
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_URL_RE = re.compile(r"(?i)\b(?:https?|s3)://[^\s]+")
_DRIVE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:(?:[\\/]|$)")
_UNC_PATH_RE = re.compile(
    r"(?<![:A-Za-z0-9])(?:\\\\|//)[^\\/\s]+[\\/][^\s]+"
)
_POSIX_PATH_RE = re.compile(r"(?<![#/A-Za-z0-9_])/(?!/)[^\s]+")
_TILDE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])~[\\/]")
_FILE_URI_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])file:(?:/{0,3}|\\)")
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+\S+"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|https?://[^\s/:]+:[^\s/@]+@"
    r")"
)
_SENSITIVE_KEY_WORDS = frozenset({
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "credentials",
    "auth",
    "authorization",
    "cookie",
    "cookies",
})
_SENSITIVE_KEY_NAMES = frozenset({
    "key",
    "apikey",
    "api_key",
    "accesskey",
    "access_key",
    "privatekey",
    "private_key",
    "secretkey",
    "secret_key",
    "clientsecret",
    "client_secret",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "sshkey",
    "ssh_key",
    "signingkey",
    "signing_key",
    "encryptionkey",
    "encryption_key",
})

_SECTION_LABELS = {
    "executive_summary": ("执行摘要", "Executive summary"),
    "key_findings": ("核心结论", "Key findings"),
    "candidate_evaluations": ("候选评价", "Candidate evaluation"),
    "adsorption_table": ("吸附能结果", "Adsorption-energy results"),
    "comparison_table": ("多项目比较", "Cross-project comparison"),
    "figures": ("图表", "Figures"),
    "methods": ("计算方法", "Methods"),
    "limitations": ("局限性与未验证项", "Limitations"),
    "recommendations": ("后续建议", "Recommendations"),
}
_THEME_LABELS = {
    "compact-brief": ("紧凑简报", "Compact brief"),
    "academic-a4": ("学术 A4", "Academic A4"),
    "diagnostic-a4": ("诊断 A4", "Diagnostic A4"),
}
_FORMAT_LABELS = {
    "html": ("HTML", "HTML"),
    "docx": ("Word", "Word"),
    "pdf": ("PDF", "PDF"),
}

# These categories are server-owned.  A client may reorder or omit optional
# sections, but it cannot attach a ``final`` badge to a methods-only shell.  The
# decision brief is intentionally stricter than a general research report: a
# finding must be accompanied by a figure or table which will actually be bound
# into the rendered outline.
_DECISION_BRIEF_PRESETS = frozenset({"quick-decision-brief"})
_FINAL_RESEARCH_REPORT_PRESETS = frozenset({
    "scientific-review",
    "manuscript-materials",
    "supporting-information",
    "diagnostic-repair",
})
_SUMMARY_OR_CONCLUSION_SECTIONS = frozenset({
    "executive_summary", "key_findings",
})
_RESULT_OR_EVIDENCE_SECTIONS = frozenset({
    "candidate_evaluations", "adsorption_table", "comparison_table", "figures",
})
_BOUND_FIGURE_OR_TABLE_SECTIONS = frozenset({
    "candidate_evaluations", "adsorption_table", "comparison_table", "figures",
})


class ReportRequestError(ValueError):
    """An untrusted report-workbench request violates its public contract."""


_PRESETS = (
    {
        "id": "quick-decision-brief",
        "version": "1",
        "label_zh": "快速决策简报",
        "label_en": "Quick decision brief",
        "description_zh": "面向项目负责人和组会的短篇关键结论、风险与下一步。",
        "audience": "project-lead",
        "requested_kind": "final",
        "locale": "zh-CN",
        "formats": ["html", "pdf"],
        "outline": [
            "executive_summary",
            "key_findings",
            "figures",
            "limitations",
            "recommendations",
        ],
        "theme_id": "compact-brief",
        "scope": {"stable_only": True, "include_failed": False},
        "options": {
            "bilingual_mode": "none",
            "precision": 3,
            "include_thermochemistry": True,
            "version_policy": "new_revision",
        },
    },
    {
        "id": "scientific-review",
        "version": "1",
        "label_zh": "科学审阅报告",
        "label_en": "Scientific review",
        "description_zh": "面向计算研究者的全构型、方法、门禁与溯源报告。",
        "audience": "researcher",
        "requested_kind": "final",
        "locale": "zh-CN",
        "formats": ["html", "docx", "pdf"],
        "outline": list(DEFAULT_OUTLINE),
        "theme_id": "academic-a4",
        "scope": {"stable_only": False, "include_failed": True},
        "options": {
            "bilingual_mode": "none",
            "precision": 4,
            "include_thermochemistry": True,
            "version_policy": "new_revision",
        },
    },
    {
        "id": "manuscript-materials",
        "version": "1",
        "label_zh": "论文正文材料",
        "label_en": "Manuscript materials",
        "description_zh": "供作者继续编辑的图、三线表、方法和局限性材料。",
        "audience": "author",
        "requested_kind": "draft",
        "locale": "zh-CN",
        "formats": ["html", "docx"],
        "outline": [
            "executive_summary",
            "key_findings",
            "adsorption_table",
            "comparison_table",
            "figures",
            "methods",
            "limitations",
        ],
        "theme_id": "academic-a4",
        "scope": {"stable_only": False, "include_failed": False},
        "options": {
            "bilingual_mode": "none",
            "precision": 4,
            "include_thermochemistry": True,
            "version_policy": "new_revision",
        },
    },
    {
        "id": "supporting-information",
        "version": "1",
        "label_zh": "Supporting Information",
        "label_en": "Supporting information",
        "description_zh": "面向审稿和复现的完整数值、参数、哈希与输入身份。",
        "audience": "reviewer",
        "requested_kind": "final",
        "locale": "en-US",
        "formats": ["html", "docx", "pdf"],
        "outline": [
            "executive_summary",
            "candidate_evaluations",
            "adsorption_table",
            "comparison_table",
            "figures",
            "methods",
            "limitations",
        ],
        "theme_id": "academic-a4",
        "scope": {"stable_only": False, "include_failed": True},
        "options": {
            "bilingual_mode": "none",
            "precision": 6,
            "include_thermochemistry": True,
            "version_policy": "new_revision",
        },
    },
    {
        "id": "diagnostic-repair",
        "version": "1",
        "label_zh": "诊断与修复报告",
        "label_en": "Diagnostic and repair report",
        "description_zh": "数据不完整时列出阻断项、受影响结论和修复动作。",
        "audience": "researcher",
        "requested_kind": "diagnostic",
        "locale": "zh-CN",
        "formats": ["html"],
        "outline": [
            "executive_summary",
            "key_findings",
            "methods",
            "limitations",
            "recommendations",
        ],
        "theme_id": "diagnostic-a4",
        "scope": {"stable_only": False, "include_failed": True},
        "options": {
            "bilingual_mode": "none",
            "precision": 4,
            "include_thermochemistry": False,
            "version_policy": "new_revision",
        },
    },
)
_PRESET_BY_ID = {item["id"]: item for item in _PRESETS}


def builtin_report_presets() -> list[dict[str, Any]]:
    """Return detached, JSON-safe built-ins in their stable display order."""

    return copy.deepcopy(list(_PRESETS))


def get_report_preset(preset_id: str) -> dict[str, Any]:
    """Return one detached built-in preset or reject an unknown identifier."""

    identifier = _validate_identifier(preset_id, field="preset_id")
    try:
        return copy.deepcopy(_PRESET_BY_ID[identifier])
    except KeyError as exc:
        raise ReportRequestError(f"unknown report preset: {identifier!r}") from exc


def report_workbench_catalog(capabilities: Any = None) -> dict[str, Any]:
    """Return the stable preset/section/locale/format/theme capability catalog."""

    format_records = _normalize_capabilities(capabilities)
    return {
        "schema": CATALOG_SCHEMA,
        "presets": builtin_report_presets(),
        "sections": [
            {
                "id": section,
                "label_zh": _SECTION_LABELS[section][0],
                "label_en": _SECTION_LABELS[section][1],
            }
            for section in DEFAULT_OUTLINE
        ],
        "locales": [
            {
                "id": "zh-CN", "label_zh": "简体中文", "label_en": "Chinese",
                "available": True, "reason": "",
            },
            {
                "id": "en-US", "label_zh": "英语", "label_en": "English",
                "available": True, "reason": "",
            },
        ],
        "formats": {
            fmt: {
                "id": fmt,
                "label_zh": _FORMAT_LABELS[fmt][0],
                "label_en": _FORMAT_LABELS[fmt][1],
                **format_records[fmt],
            }
            for fmt in SUPPORTED_FORMATS
        },
        "themes": [
            {
                "id": theme,
                "label_zh": _THEME_LABELS[theme][0],
                "label_en": _THEME_LABELS[theme][1],
                "available": True,
                "reason": "",
            }
            for theme in SUPPORTED_THEMES
        ],
        "bilingual_modes": [
            {
                "id": "none",
                "label_zh": "不启用",
                "label_en": "Single language",
                "available": True,
                "reason": "",
            },
            {
                "id": "zh-en",
                "label_zh": "中文为主 / 英文辅助",
                "label_en": "Chinese with English",
                "available": False,
                "reason": "双语正文将在完整国际化阶段提供",
            },
            {
                "id": "en-zh",
                "label_zh": "英文为主 / 中文辅助",
                "label_en": "English with Chinese",
                "available": False,
                "reason": "Bilingual bodies are not available in the Phase C renderer",
            },
        ],
        "precision": {
            "minimum": 2,
            "maximum": 8,
            "available": True,
            "reason": "",
        },
        "audiences": list(SUPPORTED_AUDIENCES),
    }


def normalize_report_request(
    value: Mapping[str, Any],
    *,
    project_id: str,
    capabilities: Any = None,
) -> ReportSpec:
    """Validate an untrusted workbench DTO and return a server-owned ReportSpec.

    The caller supplies the already resolved opaque project identifier.  A client
    cannot switch projects by placing another identifier (or a filesystem path)
    in ``scope.project_ids``.
    """

    if not isinstance(value, Mapping):
        raise ReportRequestError("report request must be a mapping")
    _reject_unsafe_tree(value, field="request")
    data = dict(value)
    _reject_unknown_fields(data, _REQUEST_KEYS, field="request")

    resolved_project_id = _validate_identifier(project_id, field="project_id")
    preset = get_report_preset(data.get("preset_id", "scientific-review"))

    requested_kind = _enum_value(
        data.get("requested_kind", preset["requested_kind"]),
        field="requested_kind",
        allowed=REPORT_KINDS,
    )
    audience = _enum_value(
        data.get("audience", preset["audience"]),
        field="audience",
        allowed=SUPPORTED_AUDIENCES,
    )
    locale = _enum_value(
        data.get("locale", preset["locale"]),
        field="locale",
        allowed=SUPPORTED_LOCALES,
    )
    formats = _normalize_formats(
        data.get("formats", preset["formats"]), capabilities=capabilities)
    outline = _normalize_outline(data.get("outline", preset["outline"]))
    enforce_report_outline_policy(preset["id"], requested_kind, outline)
    theme_id = _enum_value(
        data.get("theme_id", preset["theme_id"]),
        field="theme_id",
        allowed=SUPPORTED_THEMES,
    )
    scope = _normalize_scope(
        data.get("scope"), project_id=resolved_project_id,
        defaults=preset["scope"])
    options = _normalize_options(data.get("options"), defaults=preset["options"])

    # Do not pass template_ref, policy_refs, extensions, created_at, validation,
    # qualification, or revision.  Those are exclusively service-owned.
    return ReportSpec(
        preset_id=preset["id"],
        requested_kind=requested_kind,
        audience=audience,
        locale=locale,
        formats=formats,
        scope=scope,
        outline=outline,
        theme_id=theme_id,
        options=options,
    )


def _normalize_scope(value: Any, *, project_id: str, defaults: Mapping[str, Any]) -> dict:
    if value is None:
        data: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        data = dict(value)
    else:
        raise ReportRequestError("scope must be a mapping")
    _reject_unknown_fields(data, _SCOPE_KEYS, field="scope")
    kind = _enum_value(data.get("kind", "project"), field="scope.kind", allowed=("project",))
    project_ids = _identifier_sequence(
        data.get("project_ids", [project_id]), field="scope.project_ids",
        project_identifiers=True, allow_empty=False)
    if project_ids != [project_id]:
        raise ReportRequestError(
            "scope.project_ids must contain exactly the resolved project_id")
    return {
        "kind": kind,
        "project_ids": project_ids,
        "job_ids": _identifier_sequence(
            data.get("job_ids", []), field="scope.job_ids"),
        "species": _identifier_sequence(
            data.get("species", []), field="scope.species", general_tokens=True),
        "configuration_ids": _identifier_sequence(
            data.get("configuration_ids", []), field="scope.configuration_ids",
            general_tokens=True),
        "stable_only": _bool_value(
            data.get("stable_only", defaults["stable_only"]),
            field="scope.stable_only"),
        "include_failed": _bool_value(
            data.get("include_failed", defaults["include_failed"]),
            field="scope.include_failed"),
    }


def _normalize_options(value: Any, *, defaults: Mapping[str, Any]) -> dict:
    if value is None:
        data: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        data = dict(value)
    else:
        raise ReportRequestError("options must be a mapping")
    _reject_unknown_fields(data, _OPTION_KEYS, field="options")
    precision = data.get("precision", defaults["precision"])
    if isinstance(precision, bool) or not isinstance(precision, int):
        raise ReportRequestError("options.precision must be an integer")
    if not 2 <= precision <= 8:
        raise ReportRequestError("options.precision must be between 2 and 8")
    return {
        "bilingual_mode": _enum_value(
            data.get("bilingual_mode", defaults["bilingual_mode"]),
            field="options.bilingual_mode", allowed=_BILINGUAL_MODES),
        "precision": precision,
        "include_thermochemistry": _bool_value(
            data.get("include_thermochemistry", defaults["include_thermochemistry"]),
            field="options.include_thermochemistry"),
        "version_policy": _enum_value(
            data.get("version_policy", defaults["version_policy"]),
            field="options.version_policy", allowed=_VERSION_POLICIES),
    }


def _normalize_formats(value: Any, *, capabilities: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ReportRequestError("formats must be a non-empty sequence")
    if not value:
        raise ReportRequestError("formats must not be empty")
    normalized = []
    for index, raw in enumerate(value):
        fmt = _enum_value(
            raw, field=f"formats[{index}]", allowed=SUPPORTED_FORMATS)
        if fmt in normalized:
            raise ReportRequestError("formats must not contain duplicates")
        normalized.append(fmt)
    if capabilities is not None:
        records = _normalize_capabilities(capabilities)
        for fmt in normalized:
            if records[fmt]["available"] is not True:
                detail = records[fmt]["reason"] or "capability is unavailable"
                raise ReportRequestError(f"format {fmt!r} is unavailable: {detail}")
    return tuple(normalized)


def _normalize_outline(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ReportRequestError("outline must be a non-empty sequence")
    if not value:
        raise ReportRequestError("outline must not be empty")
    allowed = frozenset(DEFAULT_OUTLINE)
    result = []
    for index, raw in enumerate(value):
        section = _plain_string(raw, field=f"outline[{index}]", max_length=64)
        if section not in allowed:
            raise ReportRequestError(f"unsupported outline section: {section!r}")
        if section in result:
            raise ReportRequestError("outline must not contain duplicates")
        result.append(section)
    return tuple(result)


def enforce_report_outline_policy(
    preset_id: str,
    report_kind: str,
    outline: Sequence[str],
) -> tuple[str, ...]:
    """Enforce the server-owned section contract required for a final report."""

    identifier = str(preset_id or "").strip()
    kind = str(report_kind or "").strip().lower()
    normalized = tuple(str(item) for item in outline)
    if kind != "final":
        return normalized
    selected = frozenset(normalized)
    if identifier in _DECISION_BRIEF_PRESETS:
        missing = []
        if "key_findings" not in selected:
            missing.append("key_findings")
        if _BOUND_FIGURE_OR_TABLE_SECTIONS.isdisjoint(selected):
            missing.append("a bound figure/table section")
        if "limitations" not in selected:
            missing.append("limitations")
        if missing:
            raise ReportRequestError(
                "final decision brief outline must include key_findings, at least "
                "one bound figure/table section, and limitations; missing: "
                + ", ".join(missing)
            )
        return normalized
    if identifier not in _FINAL_RESEARCH_REPORT_PRESETS:
        raise ReportRequestError(
            f"final outline policy is unavailable for preset {identifier!r}"
        )
    missing = []
    if _SUMMARY_OR_CONCLUSION_SECTIONS.isdisjoint(selected):
        missing.append("a summary/conclusion section")
    if _RESULT_OR_EVIDENCE_SECTIONS.isdisjoint(selected):
        missing.append("a scientific result/evidence section")
    if "methods" not in selected:
        missing.append("methods")
    if "limitations" not in selected:
        missing.append("limitations")
    if missing:
        raise ReportRequestError(
            "final report outline for a research report must include a "
            "summary/conclusion, at least one scientific result/evidence section, "
            "methods, and limitations; missing: " + ", ".join(missing)
        )
    return normalized


def enforce_report_content_policy(
    preset_id: str,
    report_kind: str,
    content: Mapping[str, Any],
) -> None:
    """Reject a visually empty final even when its outline names every section.

    ``content`` is the normalized renderer model.  Consequently a truthy table
    already has rows and every figure has a concrete source; checking only
    selected outline members guarantees the evidence will be visible rather
    than merely present in an unrendered model field.
    """

    identifier = str(preset_id or "").strip()
    kind = str(report_kind or "").strip().lower()
    if kind != "final":
        return
    outline = tuple(str(item) for item in content.get("outline") or ())
    enforce_report_outline_policy(identifier, kind, outline)
    selected = frozenset(outline)

    def has_content(section: str) -> bool:
        return section in selected and bool(content.get(section))

    if identifier in _DECISION_BRIEF_PRESETS:
        missing = []
        if not has_content("key_findings"):
            missing.append("key_findings content")
        if not any(has_content(section) for section in _BOUND_FIGURE_OR_TABLE_SECTIONS):
            missing.append("a rendered figure/table")
        if not has_content("limitations"):
            missing.append("limitations content")
        if missing:
            raise ReportRequestError(
                "final decision brief content is incomplete; missing: "
                + ", ".join(missing)
            )
        return

    missing = []
    if not any(has_content(section) for section in _SUMMARY_OR_CONCLUSION_SECTIONS):
        missing.append("summary/conclusion content")
    if not any(has_content(section) for section in _RESULT_OR_EVIDENCE_SECTIONS):
        missing.append("scientific result/evidence content")
    if not has_content("methods"):
        missing.append("methods content")
    if not has_content("limitations"):
        missing.append("limitations content")
    if missing:
        raise ReportRequestError(
            "final research report content is incomplete; missing: "
            + ", ".join(missing)
        )


def _safe_capability_reason(value: Any, fallback: str = "") -> str:
    reason = value if isinstance(value, str) else fallback
    if len(reason) > _MAX_STRING_LENGTH or _looks_like_path(reason) \
            or _looks_like_credential(reason):
        return fallback
    return reason


def _normalize_accessibility_capability(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        value = {}
    raw_status = value.get("status")
    status = raw_status if raw_status in _ACCESSIBILITY_STATUSES else "unknown"
    fallback = "accessibility capability not declared"
    reason = _safe_capability_reason(value.get("reason"), fallback)
    reason_zh = _safe_capability_reason(value.get("reason_zh"), reason)
    reason_en = _safe_capability_reason(value.get("reason_en"), reason)
    record: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "reason_zh": reason_zh,
        "reason_en": reason_en,
    }
    for field in _ACCESSIBILITY_BOOLEAN_FIELDS:
        raw = value.get(field)
        record[field] = raw if isinstance(raw, bool) else None
    return record


def _normalize_capabilities(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {
            fmt: {
                "available": True,
                "reason": "",
                "accessibility": _normalize_accessibility_capability(None),
            }
            for fmt in SUPPORTED_FORMATS
        }
    if not isinstance(value, Mapping):
        raise ReportRequestError("format capabilities must be a mapping")
    source = value.get("formats", value)
    if not isinstance(source, Mapping):
        raise ReportRequestError("format capabilities.formats must be a mapping")
    accessibility_source = value.get("accessibility")
    if not isinstance(accessibility_source, Mapping):
        accessibility_source = {}
    result = {}
    for fmt in SUPPORTED_FORMATS:
        raw = source.get(fmt)
        nested_accessibility = (
            raw.get("accessibility")
            if isinstance(raw, Mapping) and isinstance(raw.get("accessibility"), Mapping)
            else accessibility_source.get(fmt)
        )
        if isinstance(raw, bool):
            available = raw
            reason = ""
        elif isinstance(raw, Mapping):
            available = raw.get("available") is True
            raw_reason = raw.get("reason", "")
            reason = raw_reason if isinstance(raw_reason, str) else ""
        elif raw is None:
            available = False
            reason = "capability not declared"
        else:
            available = False
            reason = "invalid capability record"
        reason = _safe_capability_reason(reason, "format capability unavailable")
        result[fmt] = {
            "available": bool(available),
            "reason": reason,
            "accessibility": _normalize_accessibility_capability(nested_accessibility),
        }
    return result


def _identifier_sequence(
    value: Any,
    *,
    field: str,
    project_identifiers: bool = False,
    general_tokens: bool = False,
    allow_empty: bool = True,
) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ReportRequestError(f"{field} must be a sequence")
    if len(value) > _MAX_SEQUENCE_LENGTH:
        raise ReportRequestError(f"{field} has too many entries")
    result = []
    for index, raw in enumerate(value):
        item_field = f"{field}[{index}]"
        if general_tokens:
            item = _validate_token(raw, field=item_field)
        else:
            item = _validate_identifier(raw, field=item_field)
        if project_identifiers and not _SAFE_ID_RE.fullmatch(item):
            raise ReportRequestError(f"{item_field} must be an opaque project identifier")
        if item in result:
            raise ReportRequestError(f"{field} must not contain duplicates")
        result.append(item)
    if not allow_empty and not result:
        raise ReportRequestError(f"{field} must not be empty")
    return result


def _validate_identifier(value: Any, *, field: str) -> str:
    text = _plain_string(value, field=field, max_length=128)
    if not _SAFE_ID_RE.fullmatch(text):
        raise ReportRequestError(f"{field} must be an opaque identifier")
    return text


def _validate_token(value: Any, *, field: str) -> str:
    text = _plain_string(value, field=field, max_length=128)
    if any(character in text for character in ("/", "\\", "?", "#", "&", "=")):
        raise ReportRequestError(f"{field} must be an opaque token")
    if text in {".", ".."} or ".." in text.replace("\\", "/").split("/"):
        raise ReportRequestError(f"{field} must be an opaque token")
    return text


def _plain_string(value: Any, *, field: str, max_length: int = _MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str):
        raise ReportRequestError(f"{field} must be a string")
    if value != value.strip() or not value:
        raise ReportRequestError(f"{field} must be a non-empty trimmed string")
    if len(value) > max_length:
        raise ReportRequestError(f"{field} exceeds {max_length} characters")
    if _CONTROL_RE.search(value):
        raise ReportRequestError(f"{field} contains control characters")
    if _looks_like_path(value):
        raise ReportRequestError(f"{field} must not contain a filesystem path")
    if _looks_like_credential(value):
        raise ReportRequestError(f"{field} must not contain a credential")
    return value


def _enum_value(value: Any, *, field: str, allowed: Sequence[str]) -> str:
    text = _plain_string(value, field=field, max_length=64)
    if text not in allowed:
        raise ReportRequestError(
            f"unsupported {field}: {text!r}; expected one of {tuple(allowed)!r}")
    return text


def _bool_value(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ReportRequestError(f"{field} must be a bool")
    return value


def _reject_unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], *, field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ReportRequestError(f"unknown {field} fields: {', '.join(unknown)}")


def _reject_unsafe_tree(value: Any, *, field: str, depth: int = 0) -> None:
    if depth > _MAX_NESTING_DEPTH:
        raise ReportRequestError(f"{field} exceeds the maximum nesting depth")
    if isinstance(value, Mapping):
        if len(value) > _MAX_MAPPING_LENGTH:
            raise ReportRequestError(f"{field} has too many fields")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReportRequestError(f"{field} keys must be strings")
            _plain_string(key, field=f"{field} key", max_length=64)
            if _is_sensitive_key(key):
                raise ReportRequestError(f"{field} contains a sensitive credential key")
            _reject_unsafe_tree(item, field=f"{field}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_SEQUENCE_LENGTH:
            raise ReportRequestError(f"{field} has too many entries")
        for index, item in enumerate(value):
            _reject_unsafe_tree(item, field=f"{field}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        _plain_string(value, field=field)
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReportRequestError(f"{field} must be finite")
        return
    raise ReportRequestError(f"{field} must contain only JSON values")


def _looks_like_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    inspected = _URL_RE.sub("", text)
    if (_FILE_URI_RE.search(inspected) or _DRIVE_PATH_RE.search(inspected)
            or _UNC_PATH_RE.search(inspected) or _TILDE_PATH_RE.search(inspected)
            or _POSIX_PATH_RE.search(inspected)):
        return True
    normalized = inspected.replace("\\", "/")
    return any(part == ".." for part in normalized.split("/"))


def _looks_like_credential(value: str) -> bool:
    return bool(_CREDENTIAL_VALUE_RE.search(str(value or "")))


def _is_sensitive_key(value: str) -> bool:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
    if normalized in _SENSITIVE_KEY_NAMES:
        return True
    if set(normalized.split("_")) & _SENSITIVE_KEY_WORDS:
        return True
    compact = normalized.replace("_", "")
    return any(compact.endswith(word) for word in (
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "auth",
    ))


__all__ = [
    "CATALOG_SCHEMA",
    "PRESET_IDS",
    "SUPPORTED_AUDIENCES",
    "SUPPORTED_FORMATS",
    "SUPPORTED_LOCALES",
    "SUPPORTED_THEMES",
    "ReportRequestError",
    "builtin_report_presets",
    "enforce_report_content_policy",
    "enforce_report_outline_policy",
    "get_report_preset",
    "normalize_report_request",
    "report_workbench_catalog",
]
