"""Report workbench preset registry and untrusted DTO tests."""
from __future__ import annotations

import json

import pytest

from vcstudio.project.report_contracts import DEFAULT_OUTLINE, ReportSpec
from vcstudio.project.report_presets import (
    CATALOG_SCHEMA,
    PRESET_IDS,
    ReportRequestError,
    builtin_report_presets,
    get_report_preset,
    normalize_report_request,
    report_workbench_catalog,
)


PROJECT_ID = "project-123e4567-e89b-12d3-a456-426614174000"


def _request(**overrides):
    value = {
        "preset_id": "scientific-review",
        "scope": {"kind": "project", "project_ids": [PROJECT_ID]},
    }
    value.update(overrides)
    return value


def _html_only_capabilities():
    return {
        "formats": {
            "html": {"available": True, "reason": ""},
            "docx": {"available": False, "reason": "python-docx unavailable"},
            "pdf": {"available": False, "reason": "PDF fonts unavailable"},
        },
        "accessibility": {
            "html": {
                "status": "conditional",
                "reason": "semantic HTML; manual review required",
                "semantic_structure": True,
                "manual_review_required": True,
            },
            "docx": {
                "status": "conditional",
                "reason": "Word structure is independent of renderer availability",
                "semantic_structure": True,
                "manual_review_required": True,
            },
            "pdf": {
                "status": "partial",
                "reason": "visual/searchable but untagged; pdf_ua=false",
                "visual": True,
                "searchable": True,
                "semantic_structure": False,
                "tagged": False,
                "pdf_ua": False,
                "manual_review_required": True,
            },
        },
    }


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


def test_builtin_registry_has_exactly_five_versioned_stable_presets():
    presets = builtin_report_presets()

    assert [item["id"] for item in presets] == list(PRESET_IDS) == [
        "quick-decision-brief",
        "scientific-review",
        "manuscript-materials",
        "supporting-information",
        "diagnostic-repair",
    ]
    assert len(presets) == len({item["id"] for item in presets}) == 5
    assert all(item["version"] == "1" for item in presets)
    assert all(item["outline"] for item in presets)
    assert all(set(item["outline"]) <= set(DEFAULT_OUTLINE) for item in presets)
    assert all(len(item["outline"]) == len(set(item["outline"])) for item in presets)
    forbidden = {
        "snapshot",
        "validation",
        "revision",
        "final_allowed",
        "qualification",
        "scientific_qualification",
        "claims",
        "extensions",
        "template_ref",
        "policy_refs",
    }
    assert forbidden.isdisjoint(set(_all_keys(presets)))
    json.dumps(presets, ensure_ascii=False, allow_nan=False)


def test_registry_and_lookup_return_detached_copies():
    first = builtin_report_presets()
    first[0]["outline"].append("methods")
    first[1]["scope"]["stable_only"] = True
    looked_up = get_report_preset("scientific-review")
    looked_up["options"]["precision"] = 2

    fresh = builtin_report_presets()
    assert fresh[0]["outline"].count("methods") == 0
    assert fresh[1]["scope"]["stable_only"] is False
    assert get_report_preset("scientific-review")["options"]["precision"] == 4


def test_unknown_preset_is_rejected():
    with pytest.raises(ReportRequestError, match="unknown report preset"):
        get_report_preset("not-a-preset")


def test_catalog_is_json_safe_ordered_and_reflects_format_capabilities():
    catalog = report_workbench_catalog(_html_only_capabilities())

    assert catalog["schema"] == CATALOG_SCHEMA
    assert [item["id"] for item in catalog["presets"]] == list(PRESET_IDS)
    assert [item["id"] for item in catalog["sections"]] == list(DEFAULT_OUTLINE)
    assert [item["id"] for item in catalog["locales"]] == ["zh-CN", "en-US"]
    assert list(catalog["formats"]) == ["html", "docx", "pdf"]
    assert catalog["formats"]["html"]["available"] is True
    assert catalog["formats"]["docx"]["id"] == "docx"
    assert catalog["formats"]["docx"]["label_zh"] == "Word"
    assert catalog["formats"]["docx"]["label_en"] == "Word"
    assert catalog["formats"]["docx"]["available"] is False
    assert catalog["formats"]["docx"]["reason"] == "python-docx unavailable"
    assert catalog["formats"]["docx"]["accessibility"]["status"] == "conditional"
    assert catalog["formats"]["docx"]["accessibility"]["semantic_structure"] is True
    pdf_accessibility = catalog["formats"]["pdf"]["accessibility"]
    assert pdf_accessibility["status"] == "partial"
    assert pdf_accessibility["visual"] is True
    assert pdf_accessibility["searchable"] is True
    assert pdf_accessibility["semantic_structure"] is False
    assert pdf_accessibility["tagged"] is False
    assert pdf_accessibility["pdf_ua"] is False
    assert [item["id"] for item in catalog["themes"]] == [
        "compact-brief", "academic-a4", "diagnostic-a4"]
    assert all(item["available"] is True for item in catalog["locales"])
    assert all(item["available"] is True for item in catalog["themes"])
    assert catalog["precision"] == {
        "minimum": 2, "maximum": 8, "available": True, "reason": ""}
    bilingual = {item["id"]: item for item in catalog["bilingual_modes"]}
    assert bilingual["none"]["available"] is True
    assert bilingual["zh-en"]["available"] is False
    assert bilingual["en-zh"]["available"] is False
    json.dumps(catalog, ensure_ascii=False, allow_nan=False)


def test_catalog_detaches_presets_and_redacts_unsafe_capability_reason():
    catalog = report_workbench_catalog({
        "html": True,
        "docx": {
            "available": False,
            "reason": r"missing C:\private\font.ttf",
            "accessibility": {
                "status": "partial",
                "reason": r"checked at C:\private\report.docx",
            },
        },
        "pdf": False,
    })
    catalog["presets"][0]["formats"].append("docx")

    assert report_workbench_catalog()["presets"][0]["formats"] == ["html", "pdf"]
    assert catalog["formats"]["docx"]["reason"] == "format capability unavailable"
    assert catalog["formats"]["docx"]["accessibility"]["reason"] == (
        "accessibility capability not declared"
    )


def test_accessibility_capability_is_fail_closed_and_independent_of_rendering():
    catalog = report_workbench_catalog({
        "formats": {
            "html": {"available": True},
            "docx": {"available": False},
            "pdf": {"available": True},
        },
        "accessibility": {
            "docx": {
                "status": "conditional",
                "semantic_structure": True,
            },
            "pdf": {
                "status": "partial",
                "visual": True,
                "searchable": True,
                "semantic_structure": False,
                "tagged": False,
                "pdf_ua": False,
            },
        },
    })

    assert catalog["formats"]["html"]["available"] is True
    assert catalog["formats"]["html"]["accessibility"]["status"] == "unknown"
    assert catalog["formats"]["docx"]["available"] is False
    assert catalog["formats"]["docx"]["accessibility"]["status"] == "conditional"
    assert catalog["formats"]["pdf"]["available"] is True
    assert catalog["formats"]["pdf"]["accessibility"]["status"] == "partial"
    assert catalog["formats"]["pdf"]["accessibility"]["pdf_ua"] is False


def test_minimal_request_uses_scientific_review_defaults_and_server_project():
    spec = normalize_report_request({}, project_id=PROJECT_ID)

    assert isinstance(spec, ReportSpec)
    assert spec.preset_id == "scientific-review"
    assert spec.requested_kind == "final"
    assert spec.audience == "researcher"
    assert spec.locale == "zh-CN"
    assert spec.formats == ("html", "docx", "pdf")
    assert spec.outline == DEFAULT_OUTLINE
    assert spec.theme_id == "academic-a4"
    assert spec.to_dict()["scope"] == {
        "kind": "project",
        "project_ids": [PROJECT_ID],
        "job_ids": [],
        "species": [],
        "configuration_ids": [],
        "stable_only": False,
        "include_failed": True,
    }
    assert spec.to_dict()["options"] == {
        "bilingual_mode": "none",
        "precision": 4,
        "include_thermochemistry": True,
        "version_policy": "new_revision",
    }
    assert spec.template_ref is None
    assert spec.policy_refs == ()
    assert not spec.extensions


@pytest.mark.parametrize(
    ("preset_id", "kind", "audience", "locale", "formats", "stable_only"),
    [
        ("quick-decision-brief", "final", "project-lead", "zh-CN", ("html", "pdf"), True),
        ("scientific-review", "final", "researcher", "zh-CN",
         ("html", "docx", "pdf"), False),
        ("manuscript-materials", "draft", "author", "zh-CN", ("html", "docx"), False),
        ("supporting-information", "final", "reviewer", "en-US",
         ("html", "docx", "pdf"), False),
        ("diagnostic-repair", "diagnostic", "researcher", "zh-CN", ("html",), False),
    ],
)
def test_each_preset_supplies_only_content_defaults(
        preset_id, kind, audience, locale, formats, stable_only):
    spec = normalize_report_request({"preset_id": preset_id}, project_id=PROJECT_ID)

    assert spec.requested_kind == kind
    assert spec.audience == audience
    assert spec.locale == locale
    assert spec.formats == formats
    assert spec.scope["stable_only"] is stable_only
    assert spec.template_ref is None and spec.policy_refs == ()
    assert spec.extensions == {}


def test_valid_overrides_preserve_outline_and_scope_token_order():
    request = _request(
        requested_kind="draft",
        audience="author",
        locale="en-US",
        formats=["pdf", "html"],
        outline=["methods", "figures", "limitations"],
        theme_id="compact-brief",
        scope={
            "kind": "project",
            "project_ids": [PROJECT_ID],
            "job_ids": ["job-b", "job-a"],
            "species": ["Li₂S₈", "S8"],
            "configuration_ids": ["top@N4(1)", "bridge-2"],
            "stable_only": True,
            "include_failed": False,
        },
        options={
            "bilingual_mode": "none",
            "precision": 7,
            "include_thermochemistry": False,
            "version_policy": "new_revision",
        },
    )

    spec = normalize_report_request(request, project_id=PROJECT_ID)

    # ReportSpec canonicalizes format order, while outline and scoped token order
    # remain semantically significant.
    assert spec.formats == ("html", "pdf")
    assert spec.outline == ("methods", "figures", "limitations")
    assert list(spec.scope["job_ids"]) == ["job-b", "job-a"]
    assert list(spec.scope["species"]) == ["Li₂S₈", "S8"]
    assert list(spec.scope["configuration_ids"]) == ["top@N4(1)", "bridge-2"]
    assert spec.options["precision"] == 7
    assert spec.options["bilingual_mode"] == "none"


def test_input_mutation_cannot_drift_normalized_spec():
    request = _request(
        requested_kind="draft",
        outline=["executive_summary", "limitations"],
        scope={"project_ids": [PROJECT_ID], "job_ids": ["job-1"]},
        options={"precision": 5},
    )
    spec = normalize_report_request(request, project_id=PROJECT_ID)

    request["outline"].append("methods")
    request["scope"]["job_ids"].append("job-2")
    request["options"]["precision"] = 2

    assert spec.outline == ("executive_summary", "limitations")
    assert list(spec.scope["job_ids"]) == ["job-1"]
    assert spec.options["precision"] == 5


@pytest.mark.parametrize("field", [
    "snapshot",
    "report_snapshot",
    "validation",
    "validation_result",
    "revision",
    "final_allowed",
    "qualification",
    "scientific_qualification",
    "claims",
    "extensions",
    "template_ref",
    "policy_refs",
    "created_at_utc",
])
def test_client_cannot_inject_server_owned_top_level_fields(field):
    request = _request()
    request[field] = False if field == "final_allowed" else {}

    with pytest.raises(ReportRequestError, match="unknown request fields"):
        normalize_report_request(request, project_id=PROJECT_ID)


@pytest.mark.parametrize("payload", [
    {"unknown": "safe"},
    {"scope": {"project_ids": [PROJECT_ID], "extra": "safe"}},
    {"options": {"layout": "wide"}},
])
def test_unknown_top_level_and_nested_fields_are_rejected(payload):
    with pytest.raises(ReportRequestError, match="unknown"):
        normalize_report_request(payload, project_id=PROJECT_ID)


@pytest.mark.parametrize("locale", ["zh", "en", "fr-FR", "ZH-CN", ""])
def test_locale_is_a_strict_two_value_enum(locale):
    with pytest.raises(ReportRequestError):
        normalize_report_request(_request(locale=locale), project_id=PROJECT_ID)


@pytest.mark.parametrize("field,value", [
    ("requested_kind", "publication"),
    ("audience", "administrator"),
    ("theme_id", "user-theme"),
])
def test_other_public_enums_are_strict(field, value):
    with pytest.raises(ReportRequestError, match="unsupported"):
        normalize_report_request(_request(**{field: value}), project_id=PROJECT_ID)


@pytest.mark.parametrize("outline", [
    [],
    "methods",
    ["methods", "methods"],
    ["methods", "raw_manifest"],
])
def test_outline_is_nonempty_unique_and_renderer_bounded(outline):
    with pytest.raises(ReportRequestError, match="outline|section"):
        normalize_report_request(_request(outline=outline), project_id=PROJECT_ID)


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_methods_only_outline_cannot_claim_final_for_any_server_preset(preset_id):
    with pytest.raises(ReportRequestError, match="final .* outline"):
        normalize_report_request(
            _request(
                preset_id=preset_id,
                requested_kind="final",
                outline=["methods"],
            ),
            project_id=PROJECT_ID,
        )


@pytest.mark.parametrize(
    ("outline", "missing"),
    [
        (["adsorption_table", "methods", "limitations"], "summary/conclusion"),
        (["executive_summary", "methods", "limitations"], "result/evidence"),
        (["executive_summary", "adsorption_table", "limitations"], "methods"),
        (["executive_summary", "adsorption_table", "methods"], "limitations"),
    ],
)
def test_final_research_report_outline_enforces_every_minimum_category(
    outline, missing,
):
    with pytest.raises(ReportRequestError, match=missing):
        normalize_report_request(
            _request(
                preset_id="scientific-review",
                requested_kind="final",
                outline=outline,
            ),
            project_id=PROJECT_ID,
        )


@pytest.mark.parametrize(
    ("outline", "missing"),
    [
        (["figures", "limitations"], "key_findings"),
        (["key_findings", "limitations"], "figure/table"),
        (["key_findings", "figures"], "limitations"),
    ],
)
def test_final_decision_brief_outline_requires_findings_evidence_and_limitations(
    outline, missing,
):
    with pytest.raises(ReportRequestError, match=missing):
        normalize_report_request(
            _request(
                preset_id="quick-decision-brief",
                requested_kind="final",
                outline=outline,
            ),
            project_id=PROJECT_ID,
        )


def test_canonical_minimum_outlines_are_accepted_for_both_final_families():
    research = normalize_report_request(
        _request(
            requested_kind="final",
            outline=[
                "executive_summary", "adsorption_table", "methods", "limitations",
            ],
        ),
        project_id=PROJECT_ID,
    )
    brief = normalize_report_request(
        _request(
            preset_id="quick-decision-brief",
            requested_kind="final",
            outline=["key_findings", "figures", "limitations"],
        ),
        project_id=PROJECT_ID,
    )

    assert research.requested_kind == brief.requested_kind == "final"


@pytest.mark.parametrize("kind", ("draft", "diagnostic"))
def test_methods_only_outline_remains_available_for_nonfinal_work(kind):
    spec = normalize_report_request(
        _request(requested_kind=kind, outline=["methods"]),
        project_id=PROJECT_ID,
    )

    assert spec.requested_kind == kind
    assert spec.outline == ("methods",)


@pytest.mark.parametrize("formats", [
    [],
    "html",
    ["html", "html"],
    ["HTML"],
    ["markdown"],
])
def test_formats_are_nonempty_unique_and_strict(formats):
    with pytest.raises(ReportRequestError, match="formats|unsupported"):
        normalize_report_request(_request(formats=formats), project_id=PROJECT_ID)


def test_unavailable_or_undeclared_requested_format_is_rejected():
    with pytest.raises(ReportRequestError, match="docx.*unavailable"):
        normalize_report_request(
            _request(formats=["html", "docx"]), project_id=PROJECT_ID,
            capabilities=_html_only_capabilities())

    with pytest.raises(ReportRequestError, match="pdf.*unavailable"):
        normalize_report_request(
            _request(formats=["pdf"]), project_id=PROJECT_ID,
            capabilities={"formats": {"html": True, "docx": True}})


def test_available_capability_shape_accepts_requested_subset():
    spec = normalize_report_request(
        _request(formats=["html"]), project_id=PROJECT_ID,
        capabilities=_html_only_capabilities())
    assert spec.formats == ("html",)


@pytest.mark.parametrize("scope", [
    "project",
    {"kind": "comparison", "project_ids": [PROJECT_ID]},
    {"project_ids": PROJECT_ID},
    {"project_ids": []},
    {"project_ids": [PROJECT_ID, "project-other"]},
    {"project_ids": ["project-other"]},
    {"project_ids": [PROJECT_ID], "job_ids": ["job-1", "job-1"]},
    {"project_ids": [PROJECT_ID], "stable_only": 1},
    {"project_ids": [PROJECT_ID], "include_failed": "yes"},
])
def test_project_scope_is_exactly_bound_to_server_resolved_project(scope):
    with pytest.raises(ReportRequestError):
        normalize_report_request(_request(scope=scope), project_id=PROJECT_ID)


@pytest.mark.parametrize("project_id", [
    r"C:\work\project",
    "C:",
    "/mnt/project",
    r"\\server\share\project",
    "file:///tmp/project",
    "../project",
    "project/child",
])
def test_server_project_id_must_also_be_an_opaque_non_path_identifier(project_id):
    with pytest.raises(ReportRequestError, match="path|identifier"):
        normalize_report_request({}, project_id=project_id)


@pytest.mark.parametrize("options", [
    "defaults",
    {"precision": True},
    {"precision": 4.0},
    {"precision": 1},
    {"precision": 9},
    {"bilingual_mode": "bilingual"},
    {"bilingual_mode": "zh-en"},
    {"bilingual_mode": "en-zh"},
    {"include_thermochemistry": 1},
    {"version_policy": "replace_current"},
])
def test_options_use_bounded_types_enums_and_numeric_range(options):
    with pytest.raises(ReportRequestError):
        normalize_report_request(_request(options=options), project_id=PROJECT_ID)


@pytest.mark.parametrize("payload", [
    {"audience": r"reviewer C:\work\project"},
    {"theme_id": "file:///tmp/theme.json"},
    {"scope": {"project_ids": [PROJECT_ID], "job_ids": ["/mnt/jobs/1"]}},
    {"scope": {"project_ids": [PROJECT_ID],
               "configuration_ids": [r"\\server\share\config"]}},
    {"scope": {"project_ids": [PROJECT_ID], "species": ["../secret"]}},
    {"scope": {"project_ids": [PROJECT_ID], "api_key": "safe-looking-value"}},
    {"options": {"accessToken": "safe-looking-value"}},
    {"scope": {"project_ids": [PROJECT_ID],
               "species": ["ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"]}},
    {"options": {"bilingual_mode": "Bearer secret-session-token"}},
])
def test_paths_file_uris_unc_and_credentials_are_recursively_rejected(payload):
    with pytest.raises(ReportRequestError, match="path|credential|sensitive"):
        normalize_report_request(payload, project_id=PROJECT_ID)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_request_tree_rejects_non_json_or_nonfinite_values(value):
    with pytest.raises(ReportRequestError):
        normalize_report_request({"options": {"precision": value}}, project_id=PROJECT_ID)


def test_capabilities_must_be_a_mapping_when_provided():
    with pytest.raises(ReportRequestError, match="capabilities"):
        normalize_report_request(
            _request(formats=["html"]), project_id=PROJECT_ID,
            capabilities=["html"])
