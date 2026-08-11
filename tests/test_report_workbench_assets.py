"""Phase C report-workbench DOM, routing, safety and adapter contracts."""
import json
from pathlib import Path
import re


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"
LOCALES = ASSETS.parents[1] / "shared" / "locales"


def _source(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def test_dedicated_workbench_page_and_assets_are_loaded_in_dependency_order():
    html = _source("index.html")

    assert '<link rel="stylesheet" href="report-workbench.css">' in html
    assert re.search(
        r'<section class="page report-workbench-page"[^>]*'
        r'data-page="report-workbench"[^>]*id="page-report-workbench"',
        html,
    )
    assert html.index('<script src="project.js"></script>') < html.index(
        '<script src="report-workbench.js"></script>'
    ) < html.index('<script src="dashboard.js"></script>')
    assert 'data-page="report-workbench" data-scene="pages.report-workbench"' in html


def test_workbench_has_seven_accessible_steps_and_required_landmarks():
    html = _source("index.html")
    steps = re.findall(r'data-rw-step="([a-z]+)"', html)

    assert steps == [
        "scope", "audience", "gates", "outline", "content", "language", "export"
    ]
    for step in steps:
        assert f'id="rw-step-button-{step}"' in html
        assert f'aria-controls="rw-step-{step}"' in html
        assert f'id="rw-step-{step}" data-rw-panel="{step}"' in html
        assert f'aria-labelledby="rw-step-button-{step}"' in html
    assert 'id="rw-spec-form" novalidate' in html
    assert 'id="rw-preview-frame" title="报告 HTML 预览" sandbox=""' in html
    assert 'id="rw-evidence" aria-labelledby="rw-evidence-heading"' in html
    assert 'id="rw-format-status" aria-live="polite"' in html
    assert 'id="rw-history-list"' in html


def test_presets_and_editable_spec_come_from_bootstrap_not_copied_defaults():
    js = _source("report-workbench.js")

    assert "VCS.call('report_workbench_bootstrap', projectPath, presetId || null)" in js
    assert "const presets = catalogRecord(catalog.presets" in js
    assert "bootstrap.report_spec || bootstrap.spec" in js
    for copied_label in (
        "快速决策简报", "科学审阅报告", "论文正文材料", "诊断与修复报告"
    ):
        assert copied_label not in js
    assert "REQUEST_KEYS" in js
    for forbidden in (
        "public_snapshot", "validation", "scientific_qualification", "base_revision"
    ):
        request_function = js[js.index("function requestFromSpec()") :
                              js.index("function specFingerprint()")]
        assert forbidden not in request_function


def test_preview_is_sandboxed_last_wins_and_project_bound():
    js = _source("report-workbench.js")

    assert "VCS.call('report_workbench_preview', projectPath, request)" in js
    assert "const generation = ++State.previewGeneration" in js
    assert "generation !== State.previewGeneration" in js
    assert "!sameProject(projectId, projectPath)" in js
    assert "fingerprint !== specFingerprint()" in js
    assert "safeId(result.project_id) !== projectId" in js
    assert "function hasCompletePreviewToken(preview)" in js
    assert "throw new Error('预览缺少完整、可核对的发布绑定 token')" in js
    assert "frame.setAttribute('sandbox', '')" in js
    assert "frame.srcdoc = preview.html" in js
    assert "allow-scripts" not in js
    assert "allow-same-origin" not in js


def test_publish_uses_only_preview_id_and_expected_bindings():
    js = _source("report-workbench.js")
    publish = js[js.index("async function publishBoundPreview()") :
                 js.index("function handleFormChange(")]

    assert "State.dirty || !State.preview || !State.preview.preview_id" in publish
    assert "'report_workbench_publish', projectPath, outputDir, previewId, expected" in publish
    assert "requestFromSpec()" not in publish
    assert "public_snapshot" not in publish
    assert "validation" not in publish
    expected = js[js.index("function expectedBindings(") :
                  js.index("async function pickOutputDirectory(")]
    for binding in (
        "preview_id", "project_id", "spec_sha256", "snapshot_sha256",
        "validation_sha256", "report_model_sha256", "base_revision",
        "base_manifest_sha256",
    ):
        assert binding in expected
    assert "preview.preview_token" in expected


def test_capabilities_fail_closed_and_each_format_has_independent_status():
    html = _source("index.html")
    js = _source("report-workbench.js")

    assert "record.available === true" in js
    assert "reason: record ? String(record.reason || '') : '服务端未明确确认此格式可用。'" in js
    assert "selectedFormatsAreAvailable()" in js
    for fmt in ("html", "docx", "pdf", "model_json", "validation_json", "manifest"):
        assert f'data-format="{fmt}"' in html
    assert "State.spec.formats.forEach(format => formatState(format, '生成中', 'busy'))" in js
    assert "State.spec.formats.forEach(format => formatState(format, '生成失败', 'bad'))" in js


def test_render_availability_and_accessibility_are_independent_and_visible():
    js = _source("report-workbench.js")
    capabilities = js[js.index("function normalizeAccessibility(") :
                      js.index("function selectedFormatsAreAvailable(")]
    formats = js[js.index("function renderFormats(") :
                 js.index("function syncSimpleControls(")]

    assert "capabilityResult && capabilityResult.accessibility" in capabilities
    assert "plain(catalog.formats)" in capabilities
    assert "plain(source[format]).accessibility" in capabilities
    assert "available: record ? record.available === true : false" in capabilities
    assert "accessibility: normalizeAccessibility" in capabilities
    for field in (
        "visual", "searchable", "semantic_structure", "document_language",
        "metadata", "image_alt", "tagged", "pdf_ua", "manual_review_required",
    ):
        assert f"'{field}'" in capabilities
    assert "report.accessibility.summary" in capabilities
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    assert en["report.accessibility.summary"].startswith("Accessibility:")
    assert zh["report.accessibility.summary"].startswith("可访问性：")
    assert "data-accessibility-status" in formats
    assert "accessibilitySummary(accessibility)" in formats
    assert "生成可用" in formats and "Rendering available" in formats
    assert "input.disabled = capability.available !== true" in formats


def test_interface_language_is_independent_from_report_output_locale():
    js = _source("report-workbench.js")
    labels = js[js.index("function uiIsEnglish("):
                js.index("function normalizeHistory(")]
    accessibility = js[js.index("function accessibilitySummary("):
                       js.index("function explicitCapabilities(")]

    assert "VCS.i18n && VCS.i18n.lang === 'en'" in labels
    assert "State.spec && State.spec.locale" not in labels
    assert "State.spec && State.spec.locale" not in accessibility
    assert "document.addEventListener('vcs:language'" in js


def test_report_routes_share_the_new_page_and_strict_query_contract():
    workspace = _source("workspace.js")
    route_block = workspace.split("const ROUTES = Object.freeze({", 1)[1].split(
        "const AREA_LABELS", 1
    )[0]

    for route in (
        "publish-report", "publish-si", "publish-draftpack",
        "publish-versions", "publish-export",
    ):
        block = route_block.split(f"'{route}':", 1)[1].split("},", 1)[0]
        assert "page: 'report-workbench'" in block
    assert "const REPORT_QUERY_KEYS = new Set(['project', 'spec', 'revision'])" in workspace
    assert "if (name === 'spec') return safeToken(value, PROJECT_TOKEN)" in workspace
    assert "return /^[1-9][0-9]{0,8}$/.test(out) ? out : ''" in workspace


def test_legacy_buttons_only_navigate_and_preserve_compatibility_functions():
    project = _source("project.js")
    init = project[project.index("function init()") :]

    assert "async function openReportWorkbench(mode)" in project
    assert "window.ReportWorkbench.open(intent)" in project
    assert "wire('pj-report', () => openReportWorkbench('report'))" in init
    assert "wire('pj-batch-report', () => openReportWorkbench('comparison'))" in init
    assert "wire('pj-draft', () => openReportWorkbench('draftpack'))" in init
    assert "async function report()" in project
    assert "async function batchReport()" in project
    assert "async function draftReady()" in project


def test_compact_layout_contains_only_local_horizontal_scroll_regions():
    css = _source("report-workbench.css")

    assert ".rw-layout{display:grid" in css
    assert "grid-template-columns:minmax(236px,270px) minmax(420px,1fr) minmax(260px,320px)" in css
    assert "@media(max-width:959px)" in css
    assert ".rw-layout{grid-template-columns:minmax(0,1fr)}" in css
    assert ".rw-preview-scroll" in css and "overflow:auto" in css
    assert ".rw-steps{max-width:100%;overflow-x:auto" in css
    assert "min-width:0" in css


def test_dirty_spec_autosaves_and_disables_publish_until_new_preview():
    js = _source("report-workbench.js")
    dirty = js[js.index("function markSpecDirty(") : js.index("function applyPendingIntent(")]
    actions = js[js.index("function renderActions(") : js.index("function persistDraft(")]

    assert "State.dirty = true" in dirty
    assert "persistDraft()" in dirty
    assert "previewCurrentSpec({ automatic: true })" in dirty
    assert "!State.dirty" in actions
    assert "publishButton.disabled = !bound" in actions
    assert "State.dirty = false" in js


def test_history_has_explicit_empty_state_and_revision_list_renderer():
    html = _source("index.html")
    js = _source("report-workbench.js")

    assert "VCS.call('report_workbench_history', projectPath)" in js
    assert "当前项目还没有已登记的报告 revision。" in js
    assert "State.history.forEach" in js
    assert 'id="rw-compare-revision"' in html
    assert "当前 MVP 只提供历史列表，尚未开放差异 API" in js


def test_publish_freezes_editors_and_only_clears_matching_spec_draft():
    js = _source("report-workbench.js")
    actions = js[js.index("function renderActions(") : js.index("function persistDraft(")]
    publish = js[js.index("async function publishBoundPreview(") :
                 js.index("function handleFormChange(")]

    assert "form.querySelectorAll('input, select, textarea')" in actions
    assert "if (State.publishBusy) control.disabled = true" in actions
    assert "const publishFingerprint = specFingerprint()" in publish
    assert "const publishDraftKey = draftKey()" in publish
    assert "const publishMode = State.mode" in publish
    assert "publishFingerprint === specFingerprint()" in publish
    assert "publishDraftKey === draftKey()" in publish
    assert "State.mode === publishMode" in publish
    assert "if (specUnchanged && publishDraftKey" in publish
    assert "else if (!specUnchanged) persistDraft()" in publish


def test_output_directory_is_bound_to_project_and_cleared_on_switch():
    js = _source("report-workbench.js")
    picker = js[js.index("async function pickOutputDirectory(") :
                js.index("function collectPublishedFiles(")]
    bootstrap = js[js.index("async function loadBootstrap(") :
                   js.index("async function selectPreset(")]
    publish = js[js.index("async function publishBoundPreview(") :
                 js.index("function handleFormChange(")]

    assert "outputProjectId: ''" in js
    assert "sameProject(expectedProjectId, expectedProjectPath)" in picker
    assert "State.outputProjectId = expectedProjectId" in picker
    assert "const projectChanged = projectId !== State.projectId" in bootstrap
    assert "State.outputDir = ''; State.outputProjectId = ''" in bootstrap
    assert "State.outputProjectId === projectId && State.outputDir" in publish
    assert "State.outputProjectId === State.projectId ? State.outputDir" in js


def test_same_project_route_queries_are_last_wins_and_rerender_history():
    js = _source("report-workbench.js")
    enter = js[js.index("async function enterWorkbench(") : js.index("function open(")]

    assert "const routeGeneration = ++State.routeGeneration" in enter
    assert enter.count("routeGeneration !== State.routeGeneration") >= 2
    assert "State.bootstrapBusy && id === State.projectId" not in enter
    assert "requestedSpec !== State.loadedSpecQuery" in enter
    assert "requestedRevision" in enter
    assert "renderHistory();" in enter


def test_drafts_are_project_and_mode_scoped_with_explicit_discard_seam():
    html = _source("index.html")
    js = _source("report-workbench.js")
    draft = js[js.index("function draftKey(") : js.index("function setOperation(")]
    recover = js[js.index("function recoveredDraft(") : js.index("function markSpecDirty(")]
    discard = js[js.index("async function discardDraft(") :
                 js.index("function markSpecDirty(")]

    assert "projectId = State.projectId, mode = State.mode" in draft
    assert "report-spec-${suffix}-${modeSuffix}" in draft
    assert "value.mode === State.mode" in recover
    assert "VCS.workspace.drafts.remove(key)" in discard
    assert 'id="rw-discard-draft"' in html
    assert "vcs:report-workbench-discard-draft" in js
    assert "discardDraft," in js


def test_history_unavailable_is_not_rendered_as_an_empty_history():
    js = _source("report-workbench.js")
    history = js[js.index("function renderHistory(") : js.index("function renderActions(")]
    fetch = js[js.index("async function fetchHistory(") :
               js.index("async function loadBootstrap(")]

    assert "State.historyStatus === 'unavailable'" in history
    assert "不能据此判断当前项目没有 revision" in history
    assert "else if (!State.history.length)" in history
    assert "status: 'unavailable'" in fetch
    assert "status: 'ready'" in fetch
    assert "catch (error)" in fetch


def test_revision_highlight_is_unambiguous_and_bound_to_report_or_spec():
    js = _source("report-workbench.js")
    history = js[js.index("function renderHistory(") : js.index("function renderActions(")]

    assert "boundReportId" in history
    assert "[row.report_id, row.spec_id, row.spec_sha256, row.preset_id]" in history
    assert "highlightCandidates.length === 1" in history
    assert "revisionIdentity === highlightedRevisionId" in history
    assert "data-report-id" in history
    assert "data-spec-id" in history


def test_format_updates_preserve_checkbox_nodes_and_keyboard_focus():
    js = _source("report-workbench.js")
    formats = js[js.index("function renderFormats(") :
                 js.index("function syncSimpleControls(")]

    assert "let input = list.querySelector" in formats
    assert "if (!label)" in formats
    assert "input.checked = selected.has(format)" in formats
    assert "fieldset.innerHTML" not in formats
    assert "list.innerHTML" not in formats


def test_language_theme_bilingual_and_precision_follow_catalog_capabilities():
    html = _source("index.html")
    js = _source("report-workbench.js")
    capability = js[js.index("function capabilityAvailable(") :
                    js.index("function renderPresets(")]

    assert "record.available === true" in capability
    assert "option.disabled = !capabilityAvailable(record)" in capability
    assert "catalog.bilingual_modes" in capability
    assert "catalog.precision" in capability
    assert "specCapabilitiesSatisfied" in capability
    for name in ("locale", "theme", "bilingual", "precision"):
        assert f'id="rw-{name}-capability"' in html
    assert '<option value="zh-en">' not in html
    assert "!specCapabilitiesSatisfied()" in js
