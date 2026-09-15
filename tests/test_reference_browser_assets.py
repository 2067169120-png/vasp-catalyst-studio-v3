from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from vcstudio.project.external_references import (
    PUBLIC_ERROR_CODES as BACKEND_PUBLIC_ERROR_CODES,
)


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"
LOCALES = Path(__file__).resolve().parents[1] / "vcstudio" / "shared" / "locales"
PUBLIC_ERROR_CODES = {
    "candidate_import_busy", "candidate_import_failed", "confirmation_required",
    "credential_echo_detected", "credential_not_supported", "credential_rejected",
    "credential_required", "expired_token", "gateway_unavailable", "identity_mismatch",
    "invalid_credential", "invalid_item", "invalid_project_identity", "invalid_query",
    "invalid_token", "keyring_unavailable", "network_busy",
    "network_confirmation_required", "network_disabled", "network_failure",
    "network_session_changed", "partial_data", "project_unavailable", "provider_error",
    "provider_unavailable", "rate_limited", "request_timeout", "response_too_large",
    "result_too_large", "schema_drift", "structure_changed", "structure_unavailable",
    "token_replayed", "unknown_provider",
}


def _source(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def test_reference_browser_assets_and_semantic_page_are_wired():
    html = _source("index.html")

    assert '<link rel="stylesheet" href="reference-browser.css">' in html
    assert '<script src="reference-browser.js"></script>' in html
    assert re.search(
        r'<section class="page reference-browser-page"[^>]*'
        r'data-page="reference-browser"[^>]*\s+id="page-reference-browser"',
        html,
    )
    assert 'data-route="analyze-references"' in html
    assert 'href="#/projects/current/analysis/references"' in html


def test_reference_route_is_project_bound_and_has_no_query_extension_surface():
    workspace = _source("workspace.js")
    route = workspace.split("'analyze-references':", 1)[1].split(
        "'publish-figures':", 1)[0]

    assert "page: 'reference-browser'" in route
    assert "`/projects/${projectToken(c)}/analysis/references`" in route
    assert "focus: '#rb-title'" in route
    assert "references: 'analyze-references'" in workspace
    assert "analysis\\/(adsorption|thermo|kinetics|electronic|charge|comparison|custom|properties|references)" in workspace
    query_keys = workspace.split("const ROUTE_QUERY_KEYS", 1)[1].split(
        "function tr", 1)[0]
    assert "analyze-references" not in query_keys


def test_reference_search_form_is_native_keyboard_accessible_and_bounded():
    html = _source("index.html")
    required = (
        'id="rb-title" tabindex="-1"',
        'id="rb-alert" role="alert"',
        'id="rb-search-form"',
        'label for="rb-provider"',
        'id="rb-provider" required',
        'label for="rb-api-key"',
        'id="rb-api-key" type="password" autocomplete="off"',
        'id="rb-page" type="number" min="1" max="4"',
        'id="rb-results" class="rb-results" role="region" tabindex="0"',
        'id="rb-comparison" class="rb-comparison-body" tabindex="0"',
        'id="rb-operation" class="rb-operation" aria-live="polite"',
    )
    for fragment in required:
        assert fragment in html
    for index in range(1, 6):
        assert f'label for="rb-filter-{index}"' in html
        assert f'id="rb-filter-{index}" type="text" maxlength="96"' in html
    assert '<button class="btn primary" id="rb-search" type="submit"' in html
    assert '<button class="btn" id="rb-compare" type="button" disabled' in html


def test_browser_search_sends_only_provider_and_fixed_filter_object():
    script = _source("reference-browser.js")
    search = script.split("async function search(event)", 1)[1].split(
        "async function compareSelected", 1)[0]
    filters = script.split("const FILTERS = Object.freeze({", 1)[1].split(
        "function plain", 1)[0]

    assert "VCS.call('external_reference_search', providerId, filters)" in search
    assert "buildFilters()" in search
    for forbidden in (
            "endpoint", "graphql", "response_fields", "headers",
            "timeout", "cache_path", "out_dir"):
        assert forbidden not in search.lower()
    for provider in ("materials_project", "optimade", "catalysis_hub"):
        assert f"{provider}: [" in filters
    for field in (
            "formula", "chemsys", "elements", "material_ids",
            "nelements_min", "nelements_max", "reactants", "products",
            "chemical_composition", "surface", "facet"):
        assert f"key: '{field}'" in filters
    assert "fetch(" not in script
    assert "XMLHttpRequest" not in script
    assert "WebSocket" not in script
    assert "localStorage" not in script and "sessionStorage" not in script


def test_api_key_never_enters_ui_state_and_is_cleared_after_bridge_call():
    script = _source("reference-browser.js")
    state = script.split("const State = {", 1)[1].split("};", 1)[0]
    save_key = script.split("async function saveKey()", 1)[1].split(
        "async function deleteKey", 1)[0]

    assert "key" not in state.lower()
    assert "external_reference_store_api_key" in save_key
    assert "input.value = ''" in save_key
    assert "State." not in save_key.split("const key", 1)[1].split(
        "external_reference_store_api_key", 1)[0]
    assert "rb-key-label" in script
    assert "reference.key.provider_label" in script


def test_import_and_compare_keep_external_evidence_outside_claim_authority():
    script = _source("reference-browser.js")
    html = _source("index.html")
    imported = script.split("async function importCandidate", 1)[1].split(
        "async function toggleNetwork", 1)[0]
    compared = script.split("async function compareSelected", 1)[1].split(
        "async function importCandidate", 1)[0]

    assert "window.confirm" in imported
    assert "external_reference_import_preview" in imported
    assert imported.index("external_reference_import_preview") < imported.index("window.confirm")
    assert "generation !== State.generation" in imported
    assert "projectId !== State.projectId" in imported
    assert "content_sha256" in imported
    assert "preview.preview_token" in imported
    assert "{ confirmed: true, scope: 'candidate_provenance' }" in imported
    assert "result.status !== 'candidate_only'" in imported
    assert "external_reference_import" in imported
    assert "external_reference_compare" in compared
    assert "'aggregate':" not in compared and ".aggregate" not in compared
    assert "ValidationResult" in html and "final claims" in html
    for forbidden in (
            "report_workbench_publish", "proj_report_bundle", "pipeline_tick",
            "submit_jobs", "continue_jobs"):
        assert forbidden not in script


def test_external_rows_use_dom_text_and_https_link_projection():
    script = _source("reference-browser.js")

    assert ".innerHTML" not in script
    assert "textContent" in script
    assert "createElement" in script
    assert "replaceChildren" in script
    assert "url.protocol === 'https:'" in script
    assert "!url.username && !url.password" in script
    assert "target = '_blank'" in script and "rel = 'noreferrer'" in script


def test_reference_browser_has_narrow_screen_and_focus_contracts():
    css = _source("reference-browser.css")

    assert ".reference-browser-page{min-width:0;max-width:100%" in css
    assert ".rb-layout{display:grid" in css
    assert ".rb-results{" in css and "overflow:auto" in css
    assert ".rb-comparison-body{" in css and "overflow:auto" in css
    assert ":focus-visible" in css
    assert "@media(max-width:1050px)" in css
    assert "@media(max-width:720px)" in css
    assert ".rb-search-form{grid-template-columns:1fr}" in css
    assert ".rb-compare-grid{grid-template-columns:1fr}" in css
    assert "@media(prefers-reduced-motion:reduce)" in css


def test_reference_i18n_keys_exist_in_both_locales_and_static_fallbacks_match_zh():
    html = _source("index.html")
    script = _source("reference-browser.js")
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    keys = {key for key in re.findall(
        r"(?:workspace\.route\.analyze_references|reference\.[A-Za-z0-9_.]+)",
        html + script) if not key.endswith(".")}

    assert keys
    assert not (keys - set(en))
    assert not (keys - set(zh))
    assert set(en) == set(zh)
    for key in (
            "reference.title", "reference.project.waiting",
            "reference.network.disabled", "reference.network.enable",
            "reference.policy.title", "reference.policy.body",
            "reference.providers.title", "reference.providers.help",
            "reference.search.title", "reference.search.help",
            "reference.results.title", "reference.results.empty",
            "reference.comparison.title", "reference.comparison.help",
            "reference.comparison.empty", "reference.operation.ready"):
        match = re.search(
            rf'data-i18n="{re.escape(key)}"[^>]*>([^<]+)<', html)
        assert match and match.group(1).strip() == zh[key]


def test_reference_public_error_codes_have_exact_bilingual_locale_closure():
    script = _source("reference-browser.js")
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    block = script.split("const PUBLIC_ERROR_CODES = new Set([", 1)[1].split("]);", 1)[0]
    authored = set(re.findall(r"'([a-z][a-z0-9_]+)'", block))

    assert authored == PUBLIC_ERROR_CODES == set(BACKEND_PUBLIC_ERROR_CODES)
    assert not ({f"reference.error.{code}" for code in authored} - set(en))
    assert not ({f"reference.error.{code}" for code in authored} - set(zh))


def test_unknown_backend_error_message_is_never_rendered_by_browser():
    runner = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
global.window = {
  VCS: { t: (key, _params, fallback) => key.startsWith('reference.error.') ? `LOC:${key}` : fallback },
  __VCS_TEST__: true,
};
global.document = { readyState: 'loading', addEventListener() {}, getElementById() { return null; } };
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const hook = window.__VCS_REFERENCE_TEST__;
const unknown = hook.errorMessage(
  { error: { code: 'future_backend_error', message: 'ENGLISH_SENTINEL' } },
  '本地化操作失败');
const known = hook.errorMessage(
  { error: { code: 'invalid_query', message: 'SECOND_SENTINEL' } },
  '本地化操作失败');
assert.strictEqual(unknown, '本地化操作失败');
assert.strictEqual(known, 'LOC:reference.error.invalid_query');
assert.ok(!unknown.includes('SENTINEL') && !known.includes('SENTINEL'));
"""
    result = subprocess.run(
        ["node", "-e", runner, str(ASSETS / "reference-browser.js")],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_reference_filter_mapping_executes_under_node_without_extension_fields():
    runner = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const values = {};
global.window = { VCS: { t: (_key, _params, fallback) => fallback }, __VCS_TEST__: true };
global.document = {
  readyState: 'loading',
  addEventListener() {},
  getElementById(id) { return values[id] || null; },
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const hook = window.__VCS_REFERENCE_TEST__;
function input(id, value) { values[id] = { value: String(value) }; }
input('rb-page', 1); input('rb-limit', 10);
for (let i = 1; i <= 5; i += 1) input(`rb-filter-${i}`, '');
hook.configure('materials_project');
input('rb-filter-1', 'Si'); input('rb-filter-2', 'Si-O');
input('rb-filter-3', 'Si,O'); input('rb-filter-4', 'mp-149,mp-13');
assert.deepStrictEqual(hook.buildFilters(), {
  page: 1, limit: 10, formula: 'Si', chemsys: 'Si-O',
  elements: ['Si', 'O'], material_ids: ['mp-149', 'mp-13'],
});
for (let i = 1; i <= 5; i += 1) input(`rb-filter-${i}`, '');
hook.configure('catalysis_hub'); input('rb-filter-1', 'COstar'); input('rb-filter-5', '111');
assert.deepStrictEqual(hook.buildFilters(), {
  page: 1, limit: 10, reactants: 'COstar', facet: '111',
});
"""
    result = subprocess.run(
        ["node", "-e", runner, str(ASSETS / "reference-browser.js")],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
