from __future__ import annotations

import re
import json
from pathlib import Path

from tests.test_workspace_runtime_assets import _run_node


ASSETS = Path(__file__).parents[1] / "vcstudio" / "gui_web" / "assets"
LOCALES = ASSETS.parents[1] / "shared" / "locales"


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def test_analysis_workbench_assets_and_semantic_page_are_loaded_once():
    html = _source("index.html")

    assert html.count('href="analysis-workbench.css"') == 1
    assert html.count('src="analysis-workbench.js"') == 1
    assert html.count('data-page="analysis-workbench"') >= 2
    assert html.count('id="page-analysis-workbench"') == 1
    assert html.index('src="analysis-workbench.js"') < html.index(
        'src="report-workbench.js"')
    assert re.search(
        r'id="nav-analyze"[^>]+data-page="analysis-workbench"'
        r'[^>]+data-scene="pages\.analysis-workbench"',
        html,
    )


def test_workbench_uses_native_labels_groups_status_and_locally_scrollable_table():
    html = _source("index.html")
    css = _source("analysis-workbench.css")

    for control_id in (
        "aw-data-mode", "aw-deadband", "aw-precision", "aw-missing-policy",
        "aw-projects", "aw-baseline", "aw-sort-key", "aw-sort-direction",
    ):
        assert f'for="{control_id}"' in html
        assert f'id="{control_id}"' in html
    assert 'id="aw-sensitivity" role="group"' in html
    assert 'id="aw-alert" role="alert"' in html
    assert 'id="aw-operation" aria-live="polite"' in html
    assert 'id="aw-table-scroll" tabindex="0"' in html
    assert '<main class="aw-panel aw-main"' not in html
    assert 'class="aw-panel aw-main" role="region"' in html
    assert ".aw-table-scroll" in css and "overflow: auto" in css
    assert re.search(r"@media\s*\(max-width:\s*959px\)", css)
    assert re.search(r"@media\s*\(max-width:\s*520px\)", css)
    assert "grid-template-columns: minmax(0, 1fr)" in css


def test_analysis_routes_share_one_physical_workbench_and_keep_opaque_ids():
    workspace = _source("workspace.js")
    routes = workspace.split("const ROUTES = Object.freeze({", 1)[1].split(
        "const AREA_LABELS", 1)[0]
    expected = {
        "analyze-energy": ("adsorption-energy", "project"),
        "analyze-thermo": ("free-energy-path", "project"),
        "analyze-kinetics": ("kinetic-dashboard", "project"),
        "analyze-electronic": ("electronic-structure", "wavefunction"),
        "analyze-charge": ("charge-wavefunction", "wavefunction"),
        "analyze-comparison": ("multi-project-comparison", "project"),
        "analyze-custom": ("task-results", "project"),
    }
    for route_id, (analysis_id, scene_page) in expected.items():
        block = routes.split(f"'{route_id}':", 1)[1].split("},", 1)[0]
        assert "page: 'analysis-workbench'" in block
        assert f"analysisId: '{analysis_id}'" in block
        assert f"scenePage: '{scene_page}'" in block
        assert "focus: '#aw-title'" in block
    assert "const analysisId = route.def.analysisId || route.def.analysis || '';" in workspace
    assert "route.def.page !== 'analysis-workbench'" in workspace
    assert "return !def.scenePage || VCS.canActivatePage(def.scenePage);" in workspace
    properties = routes.split("'analyze-properties':", 1)[1].split("},", 1)[0]
    assert "analysisId: 'property-calculators'" in properties
    assert "scenePage: 'project'" in properties
    assert "'analyze-properties': 'property-calculators'" in _source(
        "analysis-workbench.js")


def test_browser_sends_only_analysis_spec_and_never_computes_scientific_values():
    script = _source("analysis-workbench.js")
    request = re.search(
        r"function requestFromControls\(\) \{(.*?)\n  \}", script, re.S
    ).group(1)

    for key in (
        "analysis_id", "project_id", "comparison_project_ids", "data_mode",
        "near_degenerate_eV", "precision", "baseline_project_id",
        "missing_policy", "sort", "sensitivity_deadbands_eV", "view_id",
    ):
        assert key in request
    for forbidden in (
        "project_path", "source_job", "delta_e", "energy_e0", "method_matrix",
        "scientific_status", "data_fingerprint", "results",
    ):
        assert forbidden not in request
    assert "analysis_workbench_preview" in script
    assert "analysis_workbench_bootstrap" in script
    assert "projectPath" not in script
    assert "Scientific rows, rankings, gates and sensitivity sets are server-owned" in script
    assert "delta_e_eV -" not in script
    assert "lowest_energy_eV =" not in script
    assert "if (parameters.includes('sort'))" in request
    assert "request.sort = {" in request
    assert "if (parameters.includes('near_degenerate_eV'))" in request
    assert "if (parameters.includes('missing_policy'))" in request
    assert "if (parameters.includes('precision'))" in request
    assert "record && record.data_modes) && record.data_modes.length > 1" in request


def test_capability_cards_disable_every_non_available_state_and_offer_one_action():
    script = _source("analysis-workbench.js")
    css = _source("analysis-workbench.css")
    registry = re.search(
        r"function renderRegistry\(\) \{(.*?)\n  \}\n\n  function renderTemplates",
        script, re.S,
    ).group(1)

    for status in (
            "available", "missing_prerequisite", "mode_mismatch",
            "not_implemented", "unavailable"):
        assert f"{status}: 'analysis.capability." in script
        assert f'data-status="{status}"' in css
    assert "record.activatable === true" in registry
    assert "capabilityStatus === 'available'" in registry
    assert "button.disabled = State.busy || !activatable" in registry
    assert "action.className = 'aw-next-action'" in registry
    assert "CAPABILITY_ACTION_KEYS[capabilityStatus]" in registry
    assert ".aw-registry button:disabled" in css


def test_unavailable_analysis_is_disabled_in_the_executable_fake_dom():
    """A server-declared unavailable capability is never an ordinary action."""
    _run_node(
        r"""
const registry = element('aw-registry', { tagName: 'UL' });
loadAsset(process.argv[1]);
const seam = window.__VCS_ANALYSIS_TEST__;
assert.ok(seam, 'guarded analysis seam was not exposed');
seam.configure({
  analysisId: 'adsorption-energy',
  preferences: { favorites: [] },
  catalog: { analyses: [
    { id: 'adsorption-energy', label_en: 'Adsorption', description_en: 'Ready',
      capability_status: 'available', activatable: true },
    { id: 'elf-analysis', label_en: 'ELF', description_en: 'Missing ELFCAR',
      capability_status: 'unavailable', activatable: false,
      next_action: 'Generate ELFCAR first' },
  ] },
});
seam.renderRegistry();
assert.strictEqual(registry.children.length, 2);
const available = registry.children[0].children[0];
const unavailable = registry.children[1].children[0];
assert.strictEqual(available.disabled, false);
assert.strictEqual(available.dataset.capabilityStatus, 'available');
assert.strictEqual(unavailable.disabled, true);
assert.strictEqual(unavailable.dataset.capabilityStatus, 'unavailable');
assert.strictEqual(unavailable.children[1].dataset.status, 'unavailable');
assert.match(unavailable.children[3].textContent, /Generate ELFCAR first/);
assert.strictEqual(registry.getAttribute('aria-busy'), 'false');
""",
        str(ASSETS / "analysis-workbench.js"),
    )


def test_analysis_capability_and_server_result_i18n_keys_are_synced():
    script = _source("analysis-workbench.js")
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    keys = {
        *re.findall(r"'(analysis\.capability\.[^']+)'", script),
        *re.findall(r"tr\('(analysis\.results\.[^']+)'", script),
    }

    assert keys
    assert not (keys - set(en))
    assert not (keys - set(zh))
    assert set(en) == set(zh)
    assert all(en[key] != zh[key] for key in keys)


def test_kinetic_dashboard_i18n_keys_are_exactly_synced():
    html = _source("index.html")
    script = _source("analysis-workbench.js")
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    referenced = set(re.findall(r"analysis\.kinetic\.[A-Za-z0-9_]+", html + script))
    en_keys = {key for key in en if key.startswith("analysis.kinetic.")}
    zh_keys = {key for key in zh if key.startswith("analysis.kinetic.")}

    assert referenced
    assert referenced == en_keys == zh_keys
    assert set(en) == set(zh)
    assert all(en[key] and zh[key] for key in referenced)
    assert sum(en[key] != zh[key] for key in referenced) >= len(referenced) - 2


def test_generic_analysis_renderer_only_consumes_server_display_values():
    script = _source("analysis-workbench.js")
    renderer = re.search(
        r"function renderServerResults\(view, box\) \{(.*?)\n  \}\n\n",
        script, re.S,
    ).group(1)

    assert "value.display" in renderer
    assert "source.source_id" in renderer
    assert "row.source_ids" in renderer
    for forbidden in ("toFixed(", "Math.", ".reduce(", "value.value -", "parseFloat("):
        assert forbidden not in renderer
    assert "serverResult.analysis_kind === 'elf_distribution_summary'" in renderer
    assert "analysis.elf.boundary" in renderer


def test_next_calculation_card_is_read_only_and_has_no_execution_control():
    html = _source("index.html")
    script = _source("analysis-workbench.js")
    backend = (ASSETS.parent / "api.py").read_text(encoding="utf-8")
    renderer = re.search(
        r"function renderNextCalculation\(view\) \{(.*?)\n  \}\n\n",
        script, re.S,
    ).group(1)
    draft_bridge = backend.split(
        "def analysis_workbench_next_intent", 1)[1].split(
            "def analysis_workbench_bootstrap", 1)[0]

    assert 'id="aw-next-calculation"' in html
    assert 'data-i18n="analysis.next.title"' in html
    assert 'data-i18n="analysis.next.readOnly"' in html
    assert 'data-i18n="analysis.next.not_loaded"' in html
    assert "governed.recommendations" in renderer
    assert "recommendation.evidence_refs" in renderer
    assert "recommendation.reason" in renderer
    assert "createElement('button')" not in renderer
    assert "VCS.call" not in renderer
    for forbidden in (
            "submit_jobs", "continue_jobs", "jobs_cancel_batch",
            "surface_energy_calc", "formation_binding_calc"):
        assert forbidden not in draft_bridge


def test_next_calculation_i18n_keys_exist_and_static_fallbacks_match_zh():
    html = _source("index.html")
    script = _source("analysis-workbench.js")
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    keys = set(re.findall(r"analysis\.next\.[A-Za-z_]+", html + script))

    assert {"analysis.next.title", "analysis.next.readOnly",
            "analysis.next.not_loaded"} <= keys
    assert not (keys - set(en))
    assert not (keys - set(zh))
    assert set(en) == set(zh)
    for key in ("analysis.next.title", "analysis.next.readOnly",
                "analysis.next.not_loaded"):
        match = re.search(
            rf'data-i18n="{re.escape(key)}"[^>]*>([^<]+)<', html)
        assert match and match.group(1).strip() == zh[key]


def test_sort_controls_follow_the_registry_capability_and_never_replay_placeholder_sort():
    script = _source("analysis-workbench.js")
    request = re.search(
        r"function requestFromControls\(\) \{(.*?)\n  \}", script, re.S
    ).group(1)
    controls = re.search(
        r"function renderControls\(\) \{(.*?)\n  \}", script, re.S
    ).group(1)

    assert "const parameters = Array.isArray(record && record.parameters)" in request
    assert "if (parameters.includes('sort'))" in request
    assert "request.sort = {" in request
    assert "const supportsSort = parameters.includes('sort')" in controls
    assert "supportsSort && Array.isArray(record && record.sort_keys)" in controls
    assert "!supportsSort || State.busy" in controls


def test_free_energy_renderer_projects_only_server_owned_rows_and_conclusions():
    script = _source("analysis-workbench.js")
    css = _source("analysis-workbench.css")
    renderer = re.search(
        r"function renderFreeEnergyView\(view, box\) \{(.*?)\n  \}\n\n"
        r"  function appendKineticTable",
        script,
        re.S,
    ).group(1)

    assert "State.analysisId === 'free-energy-path'" in script
    assert "renderFreeEnergyView(view, box)" in script
    assert "Array.isArray(view.rows) ? view.rows : []" in renderer
    for server_field in (
        "row.step_index", "row.label", "row.sub_label", "row.G_display",
        "pds.index", "pds.from_label", "pds.to_label", "view.u_l_display",
        "view.mu_li_display", "thermo.status", "thermo.temperature_display",
        "thermo.correction_fingerprint", "view.method_status", "method.status",
        "method.errors", "method.warnings", "view.missing", "view.blocking",
        "view.reference", "view.reaction_path_id",
    ):
        assert server_field in renderer

    # Rendering may format labels and units, but it must not reconstruct the
    # ladder, energy differences, limiting potential, or PDS in JavaScript.
    for forbidden in (
        ".sort(", ".reduce(", "Math.", "row.G -", "steps[pds", "pds_index",
        "u_l =", "mu_li =",
    ):
        assert forbidden not in renderer
    assert "textContent" in renderer
    assert "innerHTML = `<" not in renderer
    assert ".aw-free-energy-summary" in css
    assert ".aw-free-energy-evidence-grid" in css


def test_kinetic_renderer_only_projects_server_display_values_and_units():
    script = _source("analysis-workbench.js")
    css = _source("analysis-workbench.css")
    renderer = re.search(
        r"function appendKineticTable\(.*?\) \{(.*?)\n  \}\n\n"
        r"  function renderKineticDashboard\(view, box\) \{(.*?)\n  \}\n\n"
        r"  function renderSensitivity",
        script,
        re.S,
    ).group(0)

    for server_field in (
        "point.condition_display", "record.display", "view.units",
        "point.tof", "point.coverage", "point.selectivity", "point.drc",
        "point.dsc", "point.reaction_order",
        "point.apparent_activation_energy", "point.free_energy_diagram",
        "convergence.status", "convergence.residual_display",
        "convergence.iterations", "convergence.solver", "view.audit",
        "view.adapter", "view.limitations", "view.reason_codes",
    ):
        assert server_field in renderer
    for forbidden in (
        "Number(", "parseFloat(", "parseInt(", "toFixed(", "Math.",
        ".reduce(", ".sort(", "record.value", "point.conditions",
        "convergence.residual ", "fetch(", "VCS.call", "new Function", "eval(",
    ):
        assert forbidden not in renderer
    assert "innerHTML = `<" not in renderer
    assert ".aw-kinetic-grid" in css


def test_kinetic_renderer_uses_server_text_without_bridge_calls_or_raw_math():
    _run_node(
        r"""
const box = element('kinetic-results');
const sensitivityBox = element('aw-sensitivity-results');
loadAsset(process.argv[1]);
const seam = window.__VCS_ANALYSIS_TEST__;
assert.ok(seam, 'guarded analysis seam was not exposed');
seam.configure({ analysisId: 'kinetic-dashboard' });
const view = {
  scientific_status: 'diagnostic', input_audit_status: 'passed',
  solver_status: 'available', result_status: 'diagnostic',
  units: {
    tof: 'SERVER_TOF_UNIT', coverage: 'SERVER_COVERAGE_UNIT',
    selectivity: 'SERVER_SELECTIVITY_UNIT', drc: 'SERVER_DRC_UNIT',
    dsc: 'SERVER_DSC_UNIT', reaction_order: 'SERVER_ORDER_UNIT',
    apparent_activation_energy: 'SERVER_EA_UNIT',
    free_energy: 'SERVER_FREE_ENERGY_UNIT', residual: 'SERVER_RESIDUAL_UNIT',
  },
  audit: { input_sha256: 'SERVER_INPUT_HASH', issues: [] },
  adapter: { version: 'SERVER_ADAPTER_VERSION' },
  reason_codes: [],
  points: [{
    condition_display: 'SERVER_CONDITION',
    conditions: { temperature: 11111, pressure: 22222, potential: 33333 },
    tof: [{ species_id: '<img onerror=boom>', value: 10101, display: 'SERVER_TOF' }],
    coverage: [{ species_id: 'A*', site_type: 'top', value: 20202,
      display: 'SERVER_COVERAGE' }],
    selectivity: [{ species_id: 'P', value: 30303, display: 'SERVER_SELECTIVITY' }],
    drc: [{ step_id: 's1', value: 40404, display: 'SERVER_DRC' }],
    dsc: [{ step_id: 's1', value: 50505, display: 'SERVER_DSC' }],
    reaction_order: [{ species_id: 'A_g', value: 60606, display: 'SERVER_ORDER' }],
    apparent_activation_energy: [{ species_id: 'P', value: 70707,
      display: 'SERVER_EA' }],
    free_energy_diagram: [
      { state_id: 'reactants', value: 80808, display: 'SERVER_G0' },
      { state_id: 'ts', value: 90909, display: 'SERVER_G1' },
    ],
    convergence: { status: 'converged', residual: 0.123456789,
      residual_display: 'SERVER_RESIDUAL', iterations: 42, solver: 'SERVER_SOLVER' },
  }],
  kinetic_sensitivity: { status: 'passed', analyses: [{ kind: 'energy_uncertainty',
    max_relative_change: 99999, max_relative_change_display: 'SERVER_SENSITIVITY' }],
    warnings: [] },
  limitations: { mean_field: true, steady_state: true, uniform_sites: true,
    lateral_interactions: 'none', mechanism_completeness: 'asserted_complete',
    mechanism_completeness_is_asserted_not_proven: true, browser_solves: false,
    diagnostic_only: true, may_enter_accepted_or_final: false },
};
seam.renderKineticDashboard(view, box);
seam.renderSensitivity(view);
function renderedText(node) {
  return [String(node.textContent || ''),
    ...(node.children || []).map(renderedText)].join('|');
}
const text = renderedText(box) + '|' + renderedText(sensitivityBox);
for (const token of [
  'SERVER_CONDITION', 'SERVER_TOF', 'SERVER_COVERAGE', 'SERVER_SELECTIVITY',
  'SERVER_DRC', 'SERVER_DSC', 'SERVER_ORDER', 'SERVER_EA', 'SERVER_G0',
  'SERVER_G1', 'SERVER_RESIDUAL', 'SERVER_SOLVER', 'SERVER_SENSITIVITY',
  'SERVER_TOF_UNIT', 'SERVER_COVERAGE_UNIT', 'SERVER_FREE_ENERGY_UNIT',
  'diagnostic', 'accepted/final', 'asserted_complete', '<img onerror=boom>',
]) assert.ok(text.includes(token), token + ' missing');
for (const raw of [10101, 20202, 30303, 40404, 50505, 60606, 70707, 80808,
  90909, 99999, 0.123456789, 11111, 22222, 33333]) {
  assert.ok(!text.includes(String(raw)), 'raw numeric value leaked: ' + raw);
}
assert.ok(text.indexOf('SERVER_G0') < text.indexOf('SERVER_G1'));
assert.strictEqual(trace.calls.length, 0);
""",
        str(ASSETS / "analysis-workbench.js"),
    )


def test_kinetic_adapter_controls_require_preview_and_explicit_confirmation():
    html = _source("index.html")
    script = _source("analysis-workbench.js")

    assert 'id="aw-kinetics-actions"' in html
    assert 'id="aw-kinetics-confirm-export"' in html and "disabled" in html
    assert 'type="file" accept="application/json,.json"' in html
    assert "window.confirm(" in script
    assert "kinetics_export_preview', projectId" in script
    assert "'kinetics_export_confirm', projectId, previewSha" in script
    assert "kinetics_audit_export_preview', projectId" in script
    assert "kinetics_audit_export_confirm', projectId, sha, true" in script
    assert "kinetics_result_import', projectId, resultObject" in script
    assert "'kinetics_result_select', projectId" in script
    assert "20 * 1024 * 1024" in script
    assert "JSON.parse(await file.text())" in script
    for forbidden in (
        "webkitdirectory", "result_path", "project_path", "project_root",
        "command:", "argv:", "subprocess", "child_process", "eval(",
    ):
        assert forbidden not in html + script


def test_fake_dom_audit_only_preview_never_enables_or_reports_model_publish():
    _run_node(
        r"""
const confirm = element('aw-kinetics-confirm-export', { disabled: true });
element('aw-kinetics-preview-export');
const status = element('aw-kinetics-adapter-status');
VCS.workspace = {
  state: { project_id: 'project-a' },
  projects: [{ project_id: 'project-a', name: 'A' }],
};
VCS.call = async method => {
  trace.calls.push(method);
  return {
    ok: true, schema: 'vcstudio.catmap-export-preview/v3',
    project_id: 'project-a', export_kind: 'model', export_ready: false,
    preview_sha256: null, model_published: false,
  };
};
loadAsset(process.argv[1]);
const seam = window.__VCS_ANALYSIS_TEST__;
seam.configure({ analysisId: 'kinetic-dashboard', projectId: 'project-a' });
const outcome = await seam.previewKineticsExport();
assert.strictEqual(outcome, false);
assert.strictEqual(seam.snapshot().kinetics_preview_sha256, '');
assert.strictEqual(confirm.disabled, true);
assert.ok(!String(status.textContent).includes('模型已发布'));
assert.deepStrictEqual(trace.calls, ['kinetics_export_preview']);
""",
        str(ASSETS / "analysis-workbench.js"),
    )


def test_async_results_are_project_bound_and_last_request_wins():
    script = _source("analysis-workbench.js")

    assert "const generation = ++State.previewGeneration" in script
    assert "const intent = ++State.intentGeneration" in script
    assert "State.previewGeneration += 1" in script
    assert "analysisId !== State.analysisId" in script
    assert "function beginBusy()" in script and "function endBusy(token)" in script
    assert "generation !== State.previewGeneration" in script
    assert "const generation = ++State.bootstrapGeneration" in script
    assert "generation !== State.bootstrapGeneration" in script
    assert "sameProject(projectId)" in script
    assert "safeId(result.project_id) !== projectId" in script
    assert "vcs:workspace-project" in script
    assert "result.default_spec || result.spec" in script
    assert "controls.disabled = State.busy || !State.spec" in script


def test_project_multiselect_rebuilds_only_the_baseline_without_resetting_selection():
    script = _source("analysis-workbench.js")
    change = re.search(
        r"const form = \$\('aw-spec-form'\); if \(form\) "
        r"form\.addEventListener\('change', event => \{(.*?)\n    \}\);",
        script,
        re.S,
    ).group(1)

    assert "selectedComparisonProjectIds()" in change
    assert "renderBaselineOptions(" in change
    assert "renderProjects()" not in change
    assert "if (!selected.includes(State.projectId)) selected.unshift(State.projectId)" in script


def test_view_templates_and_favorites_use_revision_cas_and_do_not_persist_project_scope():
    script = _source("analysis-workbench.js")

    assert "analysis_preferences_update" in script
    assert "analysis_preferences_favorite" in script
    assert "State.preferenceRevision" in script
    assert "result.conflict === true" in script
    assert "favorites.has(right.id)" in script
    assert "button.dataset.favorite" in script
    assert "aria-describedby" in script
    assert "option.disabled = true" in script
    assert "spec: requestFromControls()" in script
    assert "values: requestFromControls()" not in script
    assert "comparison_project_ids" not in re.search(
        r"async function saveView\(\) \{(.*?)\n  \}", script, re.S
    ).group(1)
