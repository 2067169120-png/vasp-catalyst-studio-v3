from __future__ import annotations

from collections import Counter
import json
import re
from pathlib import Path

from tests.test_workspace_runtime_assets import _run_node


ASSETS = Path(__file__).parents[1] / "vcstudio" / "gui_web" / "assets"
LOCALES = Path(__file__).parents[1] / "vcstudio" / "shared" / "locales"
RESEARCH_EXPLORER_KEYS = {
    "research.elements",
    "research.sort",
    "research.task",
    "research.state",
    "research.project",
    "research.projects",
    "research.denominator",
    "research.server_finalized",
    "research.histogram",
    "research.scatter_title",
    "research.live_provenance",
    "research.load_view",
    "research.delete_view",
    *{
        f"research.option.{name}"
        for name in (
            "project", "job", "formula", "facet", "adsorbate", "task",
            "state", "method", "evidence", "energy_eV", "barrier_eV",
            "asc", "desc",
        )
    },
}


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _locale_pairs(name: str) -> list[tuple[str, str]]:
    return json.loads(
        (LOCALES / f"{name}.json").read_text(encoding="utf-8"),
        object_pairs_hook=lambda pairs: pairs,
    )


def test_research_explorer_is_mounted_on_home_and_project_without_a_new_route():
    html = _source("index.html")
    workspace = _source("workspace.js")
    script = _source("research-explorer.js")

    assert html.count('href="research-explorer.css"') == 1
    assert html.count('src="research-explorer.js"') == 1
    assert html.count('data-research-explorer-host="home"') == 1
    assert html.count('data-research-explorer-host="project"') == 1
    assert html.index('src="project.js"') < html.index(
        'src="research-explorer.js"') < html.index('src="analysis-workbench.js"')
    route_block = workspace.split("const ROUTES = Object.freeze({", 1)[1].split(
        "const AREA_LABELS", 1)[0]
    assert len(re.findall(r"^\s{4}(?:'[^']+'|home):\s*\{", route_block, re.M)) == 34
    assert "research-explorer" not in route_block
    assert "VCS.navigate" not in script
    assert "const ROUTES" not in script


def test_assets_expose_bilingual_keyboard_narrow_and_server_finalized_contracts():
    html = _source("index.html")
    css = _source("research-explorer.css")
    script = _source("research-explorer.js")

    assert 'aria-labelledby="rex-home-title"' in html
    assert 'aria-labelledby="rex-project-title"' in html
    assert 'tabindex="0" role="region"' in script
    assert 'role="search"' in script
    assert 'aria-live="polite"' in script
    assert re.search(r"@media\s*\(max-width:\s*760px\)", css)
    assert re.search(r"@media\s*\(max-width:\s*480px\)", css)
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert "server-finalized DTO" in script
    assert "A rebuildable read-only index" in script
    assert "Data and logical provenance are layered" in script
    assert "frozen report graph remains separate" in script


def test_research_explorer_locale_contract_is_explicit_synced_and_translated():
    script = _source("research-explorer.js")
    pairs = {name: _locale_pairs(name) for name in ("en", "zh")}
    locales = {name: dict(values) for name, values in pairs.items()}

    assert len(RESEARCH_EXPLORER_KEYS) == 26
    assert RESEARCH_EXPLORER_KEYS <= set(locales["en"]) == set(locales["zh"])
    assert not [
        key for name in ("en", "zh")
        for key, count in Counter(key for key, _ in pairs[name]).items()
        if count > 1
    ]
    assert all(locales[name][key].strip()
               for name in ("en", "zh") for key in RESEARCH_EXPLORER_KEYS)
    assert not [
        key for key in RESEARCH_EXPLORER_KEYS
        if re.search(r"[\u3400-\u9fff]", locales["en"][key])
    ]
    placeholder = re.compile(r"\{([A-Za-z0-9_]+)\}")
    assert not [
        key for key in RESEARCH_EXPLORER_KEYS
        if set(placeholder.findall(locales["en"][key])) !=
        set(placeholder.findall(locales["zh"][key]))
    ]
    assert "research.option.${value}" not in script
    for key in sorted(key for key in RESEARCH_EXPLORER_KEYS
                      if key.startswith("research.option.")):
        assert f"t('{key}'" in script


def test_browser_submits_only_filters_sort_axes_cursor_and_never_scientific_rows():
    script = _source("research-explorer.js")
    request = re.search(
        r"function requestFromHost\(host\) \{(.*?)\n  \}", script, re.S
    ).group(1)

    for field in (
            "elements", "formula", "facet", "adsorbate", "task_types", "states",
            "evidence_levels", "energy_min_eV", "energy_max_eV",
            "barrier_min_eV", "barrier_max_eV", "method_compatible",
            "sort_key", "sort_direction", "axis_x", "axis_y"):
        assert field in request
    for forbidden in (
            "project_path", "job_path", "method_matrix", "scientific_status",
            "energy_quantity", "results", "validation_status", "data_fingerprint"):
        assert forbidden not in request
    assert "research_explorer_query" in script
    assert "research_explorer_rebuild" in script
    assert "research_explorer_provenance" in script
    assert "research_view_save" in script
    assert "authority.authority_id" in script
    assert "authority.revision" in script
    assert "delta_e_eV -" not in script
    assert "100 - point" not in script
    assert ".reduce(" not in script


def test_executable_fake_dom_renders_server_dtos_without_paths_or_recalculation():
    _run_node(
        r"""
const host = element('research-host', { dataset: { researchExplorerHost: 'home' } });
document.querySelectorAll = selector =>
  selector === '[data-research-explorer-host]' ? [host] : [];
loadAsset(process.argv[1]);
const seam = window.__VCS_RESEARCH_EXPLORER_TEST__;
assert.ok(seam, 'guarded research explorer seam was not exposed');
const absolute = 'C:\\private\\project\\project.yaml';
const result = {
  ok: true, status: 'ready', error: null,
  freshness: { status: 'ready', age_seconds: 0.25, indexed_projects: 2,
    indexed_jobs: 3, registry_total: 2, registry_state: 'ready' },
  method_compatibility: { enabled: true, status: 'compatible',
    selected_method_fingerprint: 'method-safe', input_rows: 3,
    compatible_rows: 2, excluded_rows: 1 },
  energy_compatibility: { status: 'compatible',
    selected_energy_contract_id: 'energy-single-gas-safe', numeric_rows: 2,
    unverified_contract_rows: 0 },
  table: { sample_count: 2, visible_count: 1, next_cursor: 'opaque-cursor', rows: [{
    project_id: 'project-safe', project_name: 'Catalyst A', job_id: 'job-safe',
    source_id: 'source-safe', formula: 'Pt4S', facet: '111', adsorbate: 'Li2S8',
    task_type: 'static', state: 'DONE', method_fingerprint: 'method-safe',
    method_fingerprint_schema: 'vcstudio.method-fingerprint/vasp/v1',
    energy_quantity: 'adsorption_energy', energy_contract_status: 'verified',
    energy_reference_mode: 'single', energy_contract_id: 'energy-single-gas-safe',
    evidence_level: 'verified', provenance_status: 'observed',
    display: { energy_eV: '-2.2', barrier_eV: '0.45' },
  }] },
  histogram: { status: 'ready', metric: 'energy_eV', unit: 'eV',
    sample_count: 2, missing_count: 0, bins: [{ low_display: '-2.5',
      high_display: '-2.0', count: 2, percent_of_peak: 100 }] },
  scatter: { status: 'ready', sample_count: 1, missing_count: 1,
    points: [{ project_id: 'project-safe', job_id: 'job-safe', source_id: 'source-safe',
      label: 'Catalyst A', x_display: '-2.2', y_display: '0.45',
      x_percent: 50, top_percent: 50 }] },
  periodic_table: { status: 'ready', element_count: 2, missing_element_rows: 0,
    cells: [{ element: 'Pt', period: 6, group: 10, sample_count: 2 }] },
};
seam.configure({ result });
seam.renderAll();
assert.match(host.innerHTML, /Catalyst A/);
assert.match(host.innerHTML, /Pt4S/);
assert.match(host.innerHTML, /-2\.2/);
assert.match(host.innerHTML, /0\.45/);
assert.match(host.innerHTML, /method-safe/);
assert.match(host.innerHTML, /energy-single-gas-safe/);
assert.match(host.innerHTML, /adsorption_energy · verified/);
assert.match(host.innerHTML, /server-finalized DTO/);
assert.match(host.innerHTML, /Registry state/);
assert.match(host.innerHTML, /id="rex-home-title"/);
assert.match(host.innerHTML, /width:100%/);
assert.match(host.innerHTML, /left:50%;top:50%/);
assert.ok(!host.innerHTML.includes(absolute));
assert.ok(!host.innerHTML.includes('undefined'));
""",
        str(ASSETS / "research-explorer.js"),
    )

def test_executable_request_contract_does_not_send_server_result_fields():
    _run_node(
        r"""
document.querySelectorAll = () => [];
loadAsset(process.argv[1]);
const seam = window.__VCS_RESEARCH_EXPLORER_TEST__;
const controls = {
  elements: { value: 'Pt, S' }, formula: { value: 'Pt4S' }, facet: { value: '111' },
  adsorbate: { value: 'Li2S8' }, task_types: { value: 'static, neb' },
  states: { value: 'done' }, evidence_levels: { value: 'verified' },
  energy_min_eV: { value: '-3.0' }, energy_max_eV: { value: '' },
  barrier_min_eV: { value: '' }, barrier_max_eV: { value: '1.0' },
  method_compatible: { checked: true }, sort_key: { value: 'energy_eV' },
  sort_direction: { value: 'asc' }, axis_x: { value: 'energy_eV' },
  axis_y: { value: 'barrier_eV' },
};
const fakeHost = {
  querySelector(selector) {
    const name = selector.match(/data-rex-field="([^"]+)"/)[1];
    return controls[name];
  },
};
const request = seam.requestFromHost(fakeHost);
assert.deepStrictEqual(request, {
  schema: 'vcstudio.research-query/v1',
  filters: { method_compatible: true, elements: ['Pt', 'S'],
    task_types: ['static', 'neb'], evidence_levels: ['verified'], states: ['DONE'],
    formula: 'Pt4S', facet: '111', adsorbate: 'Li2S8',
    energy_min_eV: -3, barrier_max_eV: 1 },
  sort: { key: 'energy_eV', direction: 'asc' },
  axes: { x: 'energy_eV', y: 'barrier_eV' }, limit: 50,
});
const encoded = JSON.stringify(request);
for (const forbidden of ['energy_eV":-2.2', 'scientific_status', 'project_path',
  'method_matrix', 'validation_status', 'results']) assert.ok(!encoded.includes(forbidden));
""",
        str(ASSETS / "research-explorer.js"),
    )


def test_executable_live_provenance_keeps_data_logical_and_frozen_graph_separate():
    _run_node(
        r"""
document.querySelectorAll = () => [];
loadAsset(process.argv[1]);
const seam = window.__VCS_RESEARCH_EXPLORER_TEST__;
const graph = {
  ok: true, status: 'partial', graph_kind: 'live_derived',
  report_frozen_graph_included: false,
  nodes: [
    { type: 'input', layer: 'data', origin_status: 'observed', label: 'source-safe' },
    { type: 'analysis', layer: 'logical', origin_status: 'inferred', label: 'energy' },
    { type: 'report', layer: 'logical', origin_status: 'missing', label: 'no revision' },
  ],
  denominator: { nodes: 3, edges: 2, missing: 1 },
};
const html = seam.provenanceMarkup(graph);
assert.match(html, /data-layer="data"/);
assert.match(html, /data-layer="logical"/);
assert.match(html, /observed · data/);
assert.match(html, /inferred · logical/);
assert.match(html, /frozen report graph remains separate/);
assert.ok(!html.includes('report_evidence_graph') ||
  html.includes('frozen report graph remains separate'));
""",
        str(ASSETS / "research-explorer.js"),
    )
