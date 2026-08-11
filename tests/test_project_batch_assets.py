"""多催化剂选择、对比出图与批次报告的前端静态合同。"""
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"


def _source(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def _function(source, name, next_name):
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def test_multi_project_selection_is_persistent_state_not_a_dom_snapshot():
    js = _source("project.js")
    compare = _function(js, "makeCompareFigures", "loadPresets")

    assert "COMPARE_PROJECTS_KEY = 'vcs.adsorption.compare_projects'" in js
    assert "comparePaths: new Set()" in js
    assert "localStorage.getItem(COMPARE_PROJECTS_KEY)" in js
    assert "localStorage.setItem(COMPARE_PROJECTS_KEY" in js
    assert "function selectedComparePaths()" in js
    assert "function legacyComparisonPreview(paths)" in js
    assert "if (listSucceeded) reconcileCompareSelection()" in js
    assert "else restoreCompareSelection()" in js
    assert "const paths = selectedComparePaths()" in compare
    assert "querySelectorAll('input:checked')" not in compare


def test_comparison_toolbar_preview_counts_and_ladder_are_visible():
    html = _source("index.html")
    js = _source("project.js")
    css = _source("app.css")

    for control in (
        "fig-select-all",
        "fig-select-comparable",
        "fig-select-clear",
        "fig-selection-summary",
        "fig-compare-status",
        "fig-compare-ladder",
        "pj-batch-report",
        "pj-batch-files",
    ):
        assert f'id="{control}"' in html
    for label in ("已选", "有效", "阻断", "可叠加台阶"):
        assert label in html
        assert label in js
    assert "VCS.call('proj_compare_preview', paths, preset || null)" in js
    assert "kinds.push('ladder')" in js
    assert ".pj-selection-summary" in css
    assert ".pj-compare-status" in css


def test_report_bundle_and_batch_contracts_emit_clickable_files():
    html = _source("index.html")
    js = _source("project.js")

    assert "在报告工作台预览与生成" in html
    assert 'id="pj-report-formats"' in html
    assert "'proj_report_bundle', proj.path, dr.path, selectedFormats, true" in js
    assert (
        "'proj_batch_report', paths, dr.path, preset || null,\n"
        "        ['html', 'docx', 'pdf'], true, true"
    ) in js
    assert "function collectReportFiles(value)" in js
    assert "function renderReportFiles(containerId, result, heading)" in js
    assert 'State.reportDiagnostic ? tr(' in js
    assert 'runtime.project.syncreportformatcontrols.text_a73e7ab0fe' in js
    assert 'runtime.project.syncreportformatcontrols.text_a92bdc8948' in js
    assert '在工作台配置诊断报告' in js and '打开报告工作台' in js
    assert 'data-report-open="' in js
    assert "VCS.call('open_dir', button.dataset.reportOpen)" in js


def test_legacy_report_buttons_only_enter_the_unified_workbench():
    js = _source("project.js")
    init = js[js.index("function init()") :]

    assert "async function openReportWorkbench(mode)" in js
    assert "wire('pj-report', () => openReportWorkbench('report'))" in init
    assert "wire('pj-batch-report', () => openReportWorkbench('comparison'))" in init
    assert "wire('pj-draft', () => openReportWorkbench('draftpack'))" in init
    assert "wire('pj-report', report)" not in init
    assert "wire('pj-batch-report', batchReport)" not in init
    assert "wire('pj-draft', draftReady)" not in init


def test_single_report_keeps_legacy_html_fallback():
    js = _source("project.js")
    legacy = _function(js, "legacyBatchReports", "report")

    assert "function bridgeMethodUnavailable(result)" in js
    assert "async function legacyBatchReports(paths, outDir)" in js
    assert "VCS.call('proj_report', proj.path, save, true)" in js
    assert "VCS.call('proj_report', path, save, true)" in legacy
    assert "individual.push(Object.assign({}, result, {" in legacy
    preserved = legacy[legacy.index("individual.push(Object.assign({}, result, {"):
                       legacy.index("}));", legacy.index("individual.push"))]
    assert "kind: 'final'" not in preserved
    assert "scientific_status:" not in preserved
    assert "gate_reason:" not in preserved
    assert "report_reason:" not in preserved
    assert "当前后端仅支持 HTML，已使用兼容模式生成" in js


def test_candidate_evaluation_is_visible_and_refreshes_with_project():
    html = _source("index.html")
    js = _source("project.js")
    css = _source("app.css")
    refresh = _function(js, "refreshCandidateEvaluation", "updateProjectSummary")

    assert 'id="pj-candidate-evaluation"' in html
    assert "'proj_evaluate_candidate', wanted" in refresh
    assert "bridgeMethodUnavailable(result)" in refresh
    assert "hideCandidateEvaluation()" in refresh
    assert "VCS.log" not in refresh
    for label in ("建议继续", "先补证据", "降低优先级", "阻止判断"):
        assert label in js
    assert "decision.claim_ceiling || evidence.claim_ceiling" in js
    assert "profile.short_chain_risk" in js
    assert "typeof item === 'object').slice(0, 3)" in js
    assert "refreshCandidateEvaluation(path)" in js
    assert "applyProjectSelection(State.projects.find(p => p.path === want) || null)" in js
    assert "await requestProjectSelection(hit, previous)" in js
    assert "refreshCandidateEvaluation(proj.path)" in js
    assert ".pj-candidate-card.advance" in css
    assert ".pj-candidate-card.blocked" in css
