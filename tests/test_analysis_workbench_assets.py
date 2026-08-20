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


def test_six_analysis_routes_share_one_physical_workbench_and_keep_opaque_ids():
    workspace = _source("workspace.js")
    routes = workspace.split("const ROUTES = Object.freeze({", 1)[1].split(
        "const AREA_LABELS", 1)[0]
    expected = {
        "analyze-energy": ("adsorption-energy", "project"),
        "analyze-thermo": ("free-energy-path", "project"),
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


def test_neb_convergence_and_aimd_have_dedicated_server_only_renderers():
    script = _source("analysis-workbench.js")
    css = _source("analysis-workbench.css")
    dispatch = re.search(
        r"function renderView\(\) \{(.*?)\n  \}\n\n  function renderAll",
        script, re.S,
    ).group(1)
    for analysis_id, renderer in (
        ("neb-path", "renderNebView"),
        ("convergence-scan", "renderConvergenceView"),
        ("aimd-diagnostics", "renderAimdView"),
    ):
        assert f"State.analysisId === '{analysis_id}'" in dispatch
        assert f"{renderer}(view, box)" in dispatch
        body = re.search(
            rf"function {renderer}\(view, box\) \{{(.*?)\n  \}}\n\n",
            script, re.S,
        ).group(1)
        for forbidden in ("toFixed(", "Math.", "parseFloat(", ".reduce(", "ECharts"):
            assert forbidden not in body
    aimd = re.search(
        r"function renderAimdView\(view, box\) \{(.*?)\n  \}\n\n",
        script, re.S,
    ).group(1)
    assert "createElement('button')" not in aimd
    assert "createElement('input')" not in aimd
    assert "quantityText(sample.time)" in aimd
    assert "quantityText(sample.total_energy)" in aimd
    assert "quantityText(sample.temperature)" in aimd
    assert "appendSourceDetails" in script
    for selector in (
        ".aw-specialized-card", ".aw-diagnostic-strip", ".aw-source-details",
        ".aw-specialized-evidence", ".aw-specialized-metrics",
    ):
        assert selector in css


def test_specialized_renderers_preserve_server_display_strings_in_fake_dom():
    _run_node(
        r"""
loadAsset(process.argv[1]);
const seam = window.__VCS_ANALYSIS_TEST__;
assert.ok(seam, 'guarded analysis seam was not exposed');
function q(display, unit = '', value = 1) {
  return { display, unit, value, denominator: 'server denominator',
    source_id: 'source-a', parser_module: 'parser.module', parser_version: '9.1',
    file_hashes: [{ name: 'OSZICAR', sha256: 'a'.repeat(64) }] };
}
function source(id) {
  return { source_id: id, files: [{ name: 'OSZICAR', sha256: 'b'.repeat(64) }] };
}
function parser() { return { module: 'parser.module', version: '9.1' }; }

const nebBox = new FakeElement('neb-box');
seam.renderNebView({ paths: [{
  status: 'available', source: source('neb-source'), parser: parser(),
  scientific_boundary: 'NEB supplied-path diagnostic only',
  path_quality: { status: 'diagnostic', issues: ['manual TS review'], warnings: [] },
  barriers: { forward: q('SERVER-FWD', 'eV'), reverse: q('SERVER-REV', 'eV') },
  points: [{ image_label: '01', reaction_coordinate: q('SERVER-RC'),
    relative_energy: q('SERVER-DE'), max_force: q('SERVER-FMAX'),
    electronic_convergence: 'server-electronic', ionic_convergence: 'server-ionic' }],
}] }, nebBox);
assert.strictEqual(nebBox.children.length, 1);
const nebCard = nebBox.children[0];
assert.strictEqual(nebCard.children[1].textContent, 'NEB supplied-path diagnostic only');
const nebRow = nebCard.children[3].children[1].children[0];
assert.deepStrictEqual(nebRow.children.map(cell => cell.textContent),
  ['01', 'SERVER-RC', 'SERVER-DE', 'SERVER-FMAX', 'server-electronic', 'server-ionic']);
const nebDetails = nebCard.children[6];
assert.strictEqual(nebDetails.tagName, 'DETAILS');
assert.match(nebDetails.children[1].textContent, /parser\.module @ 9\.1/);

const convergenceBox = new FakeElement('convergence-box');
seam.renderConvergenceView({ series: [{
  kind: 'encut', series_id: 'series-1', status: 'available', parser: parser(),
  scientific_boundary: 'Threshold-defined platform only', issues: ['missing point noted'],
  platform: { status: 'available', threshold_mev_per_atom: 1,
    recommendation: q('SERVER-REC', 'eV') },
  sensitivity: [{ threshold: q('SERVER-THR', 'meV/atom'),
    recommended_parameter: q('SERVER-SENS', 'eV') }],
  points: [{ label: '450 eV', source: source('conv-source'),
    parameter: q('SERVER-X'), absolute_energy: q('SERVER-E', 'eV', -10),
    atom_count: q('SERVER-NATOMS'),
    energy_per_atom: q('SERVER-EPA'), delta_per_atom: q('SERVER-DELTA'),
    platform_member: true, anomalies: ['server anomaly'] }],
}] }, convergenceBox);
const convergenceCard = convergenceBox.children[0];
const convergenceRow = convergenceCard.children[3].children[1].children[0];
assert.deepStrictEqual(convergenceRow.children.map(cell => cell.textContent),
  ['450 eV', 'SERVER-X', 'SERVER-E', 'SERVER-NATOMS', 'SERVER-EPA', 'SERVER-DELTA',
    'yes', 'server anomaly']);
const sensitivityRow = convergenceCard.children[4].children[1].children[0];
assert.strictEqual(sensitivityRow.children[0].textContent, 'SERVER-THR');
assert.strictEqual(sensitivityRow.children[1].textContent, 'SERVER-SENS');

const aimdBox = new FakeElement('aimd-box');
seam.renderAimdView({ trajectories: [{
  status: 'available', source: source('aimd-source'), parser: parser(),
  scientific_boundary: 'Short AIMD is diagnostic only', issues: [], warnings: ['short'],
  metrics: { drift: Object.assign(q('SERVER-DRIFT', 'eV'), { label: 'Drift' }) },
  segments: [{ segment_index: 0, start_step: q('SERVER-START'),
    end_step: q('SERVER-END'), sample_count: q('SERVER-COUNT'),
    duration: q('SERVER-DURATION') }],
  samples: [{ sample_index: 0, step: q('SERVER-STEP'), segment_index: 0,
    time: q('SERVER-TIME'),
    total_energy: q('SERVER-ENERGY'), temperature: q('SERVER-TEMP') }],
}] }, aimdBox);
const aimdCard = aimdBox.children[0];
const metricRow = aimdCard.children[2].children[1].children[0];
assert.strictEqual(metricRow.children[1].textContent, 'SERVER-DRIFT');
const segmentRow = aimdCard.children[3].children[1].children[0];
assert.deepStrictEqual(segmentRow.children.map(cell => cell.textContent),
  ['0', 'SERVER-START', 'SERVER-END', 'SERVER-COUNT', 'SERVER-DURATION']);
const sampleRow = aimdCard.children[4].children[1].children[0];
assert.deepStrictEqual(sampleRow.children.map(cell => cell.textContent),
  ['0', 'SERVER-STEP', '0', 'SERVER-TIME', 'SERVER-ENERGY', 'SERVER-TEMP']);
assert.strictEqual(aimdCard.children[1].textContent, 'Short AIMD is diagnostic only');
""",
        str(ASSETS / "analysis-workbench.js"),
    )


def test_registry_directly_loads_capabilities_that_share_a_semantic_route():
    script = _source("analysis-workbench.js")
    wire = re.search(
        r"function wire\(\) \{(.*?)\n  \}\n\n  if \(window\.__VCS_TEST__",
        script, re.S,
    ).group(1)

    assert "ROUTE_ANALYSIS[record.route] === record.id" in wire
    assert "else loadBootstrap(record.id)" in wire


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
        r"  function renderSensitivity",
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
