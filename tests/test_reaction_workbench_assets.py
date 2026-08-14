from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_workspace_runtime_assets import _run_node


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "vcstudio" / "gui_web" / "assets"
LOCALES = ROOT / "vcstudio" / "shared" / "locales"


def _source(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def test_reaction_controls_and_accessible_server_owned_views_are_present():
    html = _source("index.html")
    css = _source("analysis-workbench.css")
    script = _source("analysis-workbench.js")

    for control_id in (
        "aw-condition-fields", "aw-temperature", "aw-pressure", "aw-ph",
        "aw-potential", "aw-coverage",
    ):
        assert f'id="{control_id}"' in html
    for title_id in (
        "aw-reaction-map-title", "aw-thermo-ledger-title",
        "aw-condition-revision-title", "aw-parameter-sensitivity-title",
    ):
        assert title_id in script
    assert "role', 'region'" in script
    assert "aria-labelledby" in script
    assert ".aw-reaction-section" in css
    assert ".aw-condition-fields" in css
    assert re.search(r"@media\s*\(max-width:\s*520px\)", css)


def test_browser_sends_only_condition_parameters_and_renders_server_values():
    script = _source("analysis-workbench.js")
    request = script.split("function requestFromControls()", 1)[1].split(
        "function renderRegistry()", 1)[0]
    renderer = script.split("function renderReactionWorkbench", 1)[1].split(
        "function renderFreeEnergyView", 1)[0]

    for key in (
        "temperature_k", "pressure_pa", "ph", "electrode_potential_v", "coverage",
    ):
        assert key in request
    for forbidden in (
        "electronic_energy_e0_eV", "zpe_eV", "delta_h_thermal_eV",
        "minus_t_delta_s_eV", "final_delta_g_eV", "method_sha256",
        "evidence_sha256", "structure_sha256",
    ):
        assert forbidden not in request
    for server_display in (
        "reaction_delta_g_display", "activation_delta_g_display",
        "final_delta_g_display", "base_delta_g_display",
        "condition_delta_g_display", "derived_delta_g_display",
    ):
        assert server_display in renderer
    for forbidden_calculation in (
        "Math.log", ".reduce(", "slope_eV_per_unit", "coefficient_eV",
        "final_delta_g_eV +", "derived_delta_g_eV =",
    ):
        assert forbidden_calculation not in renderer


def test_reaction_i18n_keys_exist_in_both_locales_and_match():
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    keys = {key for key in zh if key.startswith("analysis.reaction.")}
    assert keys
    assert keys == {key for key in en if key.startswith("analysis.reaction.")}
    script = _source("analysis-workbench.js")
    authored = set(re.findall(r"tr\('(analysis\.reaction\.[^']+)'", script))
    assert authored <= keys
    assert all(zh[key].strip() and en[key].strip() for key in keys)


def test_executable_fake_dom_renders_all_four_reaction_views_without_recalculation():
    _run_node(
        r"""
FakeElement.prototype.prepend = function (...children) { this.children.unshift(...children); };
const box = element('reaction-box');
loadAsset(process.argv[1]);
const seam = window.__VCS_ANALYSIS_TEST__;
assert.ok(seam && seam.renderReactionWorkbench, 'reaction renderer seam missing');
const hashes = 'a'.repeat(64);
seam.renderReactionWorkbench({
  schema: 'vcstudio.reaction-workbench-view/v1',
  graph: {
    nodes: [{ node_id: 'state-r', entity_type: 'adsorbate_state', label: 'R*',
      structure_sha256: hashes, method_sha256: hashes, evidence_sha256: hashes,
      artifact_status: 'available' }],
    edges: [{ edge_id: 'step-1', artifact_status: 'available', missing: [],
      display: { reactants: 'R*', products: 'P*', transition_state: 'TS*' },
      thermochemistry: { reaction_delta_g_display: '-0.5000',
        activation_delta_g_display: '1.0000' } }],
    missing_edges: [],
  },
  ledger: { rows: [{ entity_id: 'state-r', entity_type: 'adsorbate_state', label: 'R*',
    terms: [
      { key: 'electronic_energy_e0_eV', display: '-10.0000' },
      { key: 'zpe_eV', display: '0.1000' },
      { key: 'delta_h_thermal_eV', display: '0.0500' },
      { key: 'minus_t_delta_s_eV', display: '-0.0300' },
    ], standard_state: { display: '1-bar 100000 Pa' },
    temperature_display: '300.00 K', pressure_display: '100000 Pa',
    models_display: 'vibration=harmonic', final_delta_g_display: '-9.8700',
    artifact_status: 'available',
    low_frequency: { original_frequencies_display: '18, 42 cm-1',
      rule: 'quasi_harmonic', reason: 'audit', sensitivity: [
        { parameter: 'cutoff', value: 50, unit: 'cm-1', delta_g_eV: 0.01,
          evidence_sha256: hashes }] },
    frequency_qualification: { display: 'unavailable', reason: 'minimum' } }] },
  condition_revision: { revision_id: 'derived-abc', revision_sha256: hashes,
    applicability_display: { temperature_k: '250 to 500' },
    source_evidence_mutated: false, rows: [{ label: 'R*',
      base_delta_g_display: '-9.8700', condition_delta_g_display: '-0.0200',
      derived_delta_g_display: '-9.8900', status: 'available', missing: [] }] },
}, box);
function flatten(node) {
  return [String(node.textContent || ''), ...(node.children || []).flatMap(flatten)].join(' ');
}
const rendered = flatten(box);
for (const expected of ['Reaction Map', 'Thermochemistry Ledger',
  'Condition Explorer derived revision', 'Parameter sensitivity', '-9.8900', hashes]) {
  assert.ok(rendered.includes(expected), `missing rendered value: ${expected}`);
}
assert.ok(!rendered.includes('NaN'));
assert.ok(!rendered.includes('undefined'));
""",
        str(ASSETS / "analysis-workbench.js"),
    )
