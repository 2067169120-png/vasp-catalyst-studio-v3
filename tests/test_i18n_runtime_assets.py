"""Runtime i18n contracts for static, dynamic, and accessibility-tree text."""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "vcstudio" / "gui_web" / "assets"
LOCALES = ROOT / "vcstudio" / "shared" / "locales"


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _locale(name: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{name}.json").read_text(encoding="utf-8"))


def test_language_bundle_keeps_explicit_target_and_exact_zh_source():
    api = _source("app.js")
    backend = (ROOT / "vcstudio" / "gui_web" / "api.py").read_text(
        encoding="utf-8"
    )

    assert "export_bundle_for_js" in backend
    assert "'source': source" in backend
    assert "VCS.i18n = { dict: {}, source: {}" in api
    assert "VCS.applyI18n(r.dict, r.source)" in api
    assert "VCS.t = function (key, params, fallback)" in api
    assert "document.dispatchEvent(new CustomEvent('vcs:language'" in api


def test_runtime_translates_accessible_attributes_and_new_dynamic_nodes():
    api = _source("app.js")

    for marker, attribute in (
        ("data-i18n-ph", "placeholder"),
        ("data-i18n-title", "title"),
        ("data-i18n-aria-label", "aria-label"),
        ("data-i18n-aria-description", "aria-description"),
        ("data-i18n-alt", "alt"),
    ):
        assert f"'{marker}': '{attribute}'" in api
    assert "new MutationObserver" in api
    assert "record.addedNodes.forEach" in api
    assert "translateLegacyTextNode" in api
    assert "translateLegacyAttributes" in api
    assert "textKey && element.childElementCount === 0" in api
    assert "I18N_RAW_SELECTOR" in api
    assert "[data-i18n-raw]" in api


def test_all_accordion_summaries_are_keyed_and_refresh_on_language_change():
    html = _source("index.html")
    ui = _source("ui.js")
    zh, en = _locale("zh"), _locale("en")
    tags = re.findall(
        r'<[^>]+\bdata-acc="[^"]+"[^>]+\bdata-sum="[^"]+"[^>]*>',
        html,
        flags=re.DOTALL,
    )
    assert tags
    assert len(tags) == html.count('data-sum="')
    keys = []
    for tag in tags:
        match = re.search(r'\bdata-i18n-sum="([^"]+)"', tag)
        assert match, tag
        keys.append(match.group(1))
    assert len(keys) == len(set(keys))
    assert not (set(keys) - set(zh))
    assert not (set(keys) - set(en))
    assert not [key for key in keys if re.search(r"[\u3400-\u9fff]", en[key])]
    assert "function localizedAccordionSummary(el)" in ui
    assert "function refreshAccordionSummaries(root)" in ui
    assert "document.addEventListener('vcs:language'" in ui
    assert "refreshAccordionSummaries(document)" in ui


def test_every_static_log_header_has_an_explicit_translation_key():
    html = _source("index.html")
    zh, en = _locale("zh"), _locale("en")
    expected = {
        "structure-log": "structure.log",
        "generate-log": "generate.log",
        "project-log": "project.log",
        "wavefn-log": "wavefn.log",
        "figures-log": "figures.log",
        "ai-log": "ai.log",
        "jobs-log": "jobs.log",
        "cluster-log": "cluster.log",
        "settings-log": "settings.log",
    }
    for element_id, key in expected.items():
        pattern = (
            rf'<div class="log" id="{re.escape(element_id)}"[^>]*>'
            rf'\s*<div class="head" data-i18n="{re.escape(key)}">'
        )
        assert re.search(pattern, html), (element_id, key)
        assert key in zh and key in en
        assert en[key].strip() and not re.search(r"[\u3400-\u9fff]", en[key])


def test_every_explicit_runtime_translation_key_exists_in_both_locales():
    zh, en = _locale("zh"), _locale("en")
    used: dict[str, set[str]] = {}
    for path in sorted(ASSETS.glob("*.js")):
        name = path.name
        source = path.read_text(encoding="utf-8")
        for pattern in (
            r"VCS\.t\(\s*['\"]([^'\"]+)['\"]",
            r"\btr\(\s*['\"]([^'\"]+)['\"]",
        ):
            for key in re.findall(pattern, source):
                used.setdefault(key, set()).add(name)

    missing_zh = sorted(set(used) - set(zh))
    missing_en = sorted(set(used) - set(en))
    assert not missing_zh, {key: sorted(used[key]) for key in missing_zh}
    assert not missing_en, {key: sorted(used[key]) for key in missing_en}


def test_workbench_interface_language_is_not_report_output_locale():
    report = _source("report-workbench.js")
    labels = report[
        report.index("function uiIsEnglish(") :
        report.index("function normalizeHistory(")
    ]
    accessibility = report[
        report.index("function accessibilitySummary(") :
        report.index("function explicitCapabilities(")
    ]

    assert "VCS.i18n && VCS.i18n.lang === 'en'" in labels
    assert "State.spec && State.spec.locale" not in labels
    assert "State.spec && State.spec.locale" not in accessibility
    assert "document.addEventListener('vcs:language'" in report
    assert "document.addEventListener('vcs:language'" in _source(
        "analysis-workbench.js"
    )


def test_ai_language_switch_only_redraws_cached_dynamic_views():
    source = _source("ai.js")
    start = source.index("document.addEventListener('vcs:language'")
    end = source.index("\n    });", start)
    listener = source[start:end]

    for redraw in (
        "renderChat()",
        "if (State.spec) renderSpec()",
        "if (State.planResult) renderPlan(State.planResult)",
        "if (State.tables) renderTables()",
    ):
        assert redraw in listener
    for mutation in (
        "VCS.call(",
        "extract()",
        "plan()",
        "instantiate()",
        "genVariantMatrix()",
        "genManuscript()",
    ):
        assert mutation not in listener


def test_css_generated_status_text_has_an_english_variant():
    css = _source("app.css")
    assert 'tbody tr.stale td:first-child .name::after{content:"过期"' in css
    assert (
        'html[lang^="en"] tbody tr.stale td:first-child .name::after'
        '{content:"Stale"}'
    ) in css
    assert '.lis-step.current .lis-step-title::after{content:"当前步骤"' in css
    assert (
        'html[lang^="en"] .lis-step.current .lis-step-title::after'
        '{content:"Current step"}'
    ) in css
    assert (
        'html[lang^="en"] .lis-step.ready:not(.current) .lis-step-title::after'
        '{content:"Completed · Click to edit"}'
    ) in css


def test_figure_gallery_consumes_bilingual_server_metadata():
    source = _source('figures.js')
    assert "item[field + '_en']" in source
    assert "localized(p, 'name')" in source
    assert "localized(p, 'description')" in source
    assert "localized(preset, 'required_data')" in source
    assert "localized(rp, 'description')" in source
    assert "document.addEventListener('vcs:language'" in source


def test_mode_chip_uses_localized_scenario_name_and_refreshes_on_language_change():
    source = _source("app.js")
    block = source.split("function refreshModeChip()", 1)[1].split(
        "let scenarioNavigationGeneration", 1
    )[0]

    assert "VCS.i18n.lang === 'en' && sc.name_en" in block
    assert "settings.scenario.label" in block
    assert "legacy.dynamic.app.0010" in block
    assert "document.addEventListener('vcs:language', refreshModeChip)" in block
