"""Structure Source Hub and generic surface-wizard asset contracts.

These tests keep the browser bridge path-free and exercise the production ``editor.js``
IIFE with a small fake DOM.  Geometry generation remains a dry-run until two explicit
user confirmations have completed; candidate creation never implies submission or a
scientific-status promotion.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "vcstudio" / "gui_web" / "assets"
LOCALES = ROOT / "vcstudio" / "shared" / "locales"
EDITOR = ASSETS / "editor.js"
NODE = shutil.which("node")


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _hub_html() -> str:
    html = _source("index.html")
    start = html.index('<div class="card acc source-hub"')
    end = html.index("<!-- 分子建模分区", start)
    return html[start:end]


def _locale(name: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{name}.json").read_text(encoding="utf-8"))


def test_source_hub_uses_only_provider_agnostic_opaque_bridge_contracts():
    js = _source("editor.js")
    for call in (
        "VCS.call('structure_source_capabilities')",
        "VCS.call('structure_source_select_local')",
        "VCS.call('structure_source_search', providerId, queryText)",
        "VCS.call('structure_source_preview', token)",
        "VCS.call('structure_source_confirm', preview.token)",
        "VCS.call('surface_dry_run', SourceHubState.sourceTokens.bulk",
        "VCS.call('structure_output_select')",
        "VCS.call('surface_create_candidates', SourceHubState.operationToken",
    ):
        assert call in js

    # No provider-specific HTTP/auth/cache implementation belongs in this UI.
    hub_js = js[js.index("const SourceHubState = {") : js.index("// ── 初始化 ──")]
    for forbidden in (
        "fetch(", "XMLHttpRequest", "api_key", "apiKey", "Authorization",
        "materialsproject", "optimade", "catalysis-hub", "localStorage",
    ):
        assert forbidden not in hub_js


def test_source_hub_has_native_controls_and_no_path_or_secret_entry():
    hub = _hub_html()
    required_ids = {
        "src-bulk-local", "src-bulk-confirm", "src-ads-local", "src-ads-confirm",
        "source-provider", "source-query", "source-search", "surface-dry-run",
        "surface-output-select", "surface-candidate-ack", "surface-create-candidates",
    }
    assert required_ids <= set(re.findall(r'\bid="([^"]+)"', hub))
    assert 'role="status"' in hub and 'aria-live="polite"' in hub
    assert 'type="password"' not in hub

    input_tags = re.findall(r"<input\b[^>]*>", hub)
    for tag in input_tags:
        identifier = re.search(r'\bid="([^"]+)"', tag)
        if identifier:
            lowered = identifier.group(1).lower()
            assert "path" not in lowered
            assert "key" not in lowered
            assert "secret" not in lowered
        assert "api key" not in tag.lower()
        assert "api-key" not in tag.lower()

    # Local structures and destinations are selected through real buttons, never text paths.
    assert '<button type="button" class="btn" id="src-bulk-local"' in hub
    assert '<button type="button" class="btn" id="surface-output-select"' in hub


def test_source_hub_exposes_complete_generic_slab_and_site_controls():
    hub = _hub_html()
    for control in (
        "surface-h", "surface-k", "surface-l", "surface-termination",
        "surface-layers", "surface-vacuum", "surface-fixed", "surface-dedup",
        "surface-binding-atom", "surface-orientation", "surface-rotations", "surface-coverage",
        "surface-sides", "surface-min-distance",
    ):
        assert f'id="{control}"' in hub
    for family in ("ontop", "bridge", "hollow", "other"):
        assert f'value="{family}"' in hub
    assert 'value="both"' in hub
    assert "几何位点，不等于活性位点" in hub
    assert "fail-closed" in hub


def test_source_hub_is_responsive_and_i18n_keys_are_symmetric():
    css = _source("app.css")
    for selector in (
        ".source-hub-grid", ".source-gateway-controls", ".source-view-grid",
        ".surface-fields", ".surface-confirm",
    ):
        assert selector in css
    assert "@media(max-width:620px)" in css

    zh, en = _locale("zh"), _locale("en")
    zh_keys = {key for key in zh if key.startswith("structure.hub.")}
    en_keys = {key for key in en if key.startswith("structure.hub.")}
    assert zh_keys == en_keys
    assert zh_keys
    used = set(re.findall(
        r'(?:data-i18n(?:-[a-z-]+)?=["\']|i18n\(["\'])'
        r'(structure\.hub\.[A-Za-z0-9_.]+)',
        _hub_html() + _source("editor.js"),
    ))
    assert not (used - zh_keys)
    assert not (used - en_keys)
    assert zh["structure.hub.site.boundary"].startswith("几何位点，不等于活性位点")
    assert en["structure.hub.site.boundary"].startswith(
        "Geometric sites are not active sites"
    )
    assert zh["structure.hub.surface.status"].endswith("candidate")
    assert en["structure.hub.create.done"].endswith(
        "scientific status remains candidate."
    )


@pytest.mark.skipif(NODE is None, reason="node is required for executable asset tests")
def test_fake_dom_requires_source_confirmation_then_dry_run_review_before_create():
    script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = process.argv[1];
const calls = [];

class ClassList {
  add() {} remove() {} contains() { return false; }
  toggle(_name, force) { return !!force; }
}
class Element {
  constructor(id = '', tag = 'div') {
    this.id = id; this.tagName = String(tag).toUpperCase(); this.value = '';
    this.checked = false; this.disabled = false; this.hidden = false;
    this.dataset = {}; this.classList = new ClassList(); this.children = [];
    this.attributes = new Map(); this.textContent = ''; this.innerHTML = '';
    this.style = {}; this.focused = false;
  }
  addEventListener() {}
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  appendChild(item) { this.children.push(item); return item; }
  focus() { this.focused = true; }
  closest() { return null; }
}
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};
const siteInputs = ['ontop', 'bridge', 'hollow', 'other'].map(value => {
  const input = new Element('', 'input'); input.value = value; input.checked = true; return input;
});

global.document = {
  readyState: 'loading',
  getElementById: element,
  createElement: tag => new Element('', tag),
  createElementNS: (_ns, tag) => new Element('', tag),
  querySelectorAll: selector => selector === 'input[name="surface-site-family"]:checked'
    ? siteInputs.filter(input => input.checked) : [],
  addEventListener() {},
};
global.window = global;
window.window = window; window.document = document; window.__VCS_TEST__ = true;
window.addEventListener = () => {}; window.removeEventListener = () => {};

const defaults = {
  'surface-h': '1', 'surface-k': '1', 'surface-l': '1',
  'surface-termination': 'term-opaque', 'surface-layers': '5',
  'surface-vacuum': '18', 'surface-fixed': '2',
  'surface-binding-atom': '0', 'surface-orientation': 'normal',
  'surface-rotations': '0,90,180,270',
  'surface-coverage': '0.25', 'surface-sides': 'both',
  'surface-min-distance': '1.3',
};
Object.entries(defaults).forEach(([id, value]) => { element(id).value = value; });
element('surface-dedup').checked = true;
element('surface-candidate-ack').checked = false;

window.VCS = {
  esc: value => String(value == null ? '' : value)
    .replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
  t: (_key, params, fallback) => String(fallback || '').replace(/\{(\w+)\}/g,
    (_m, key) => params && params[key] != null ? String(params[key]) : `{${key}}`),
  toast() {}, log() {},
  call: async (method, ...args) => {
    calls.push([method, ...args]);
    if (method === 'structure_source_preview') return {
      ok: true,
      preview: {
        token: args[0], formula: args[0].includes('ads') ? 'CO' : 'Pt', natoms: 2,
        structure_hash: 'a'.repeat(64),
        view: { xyz: '2\nfixture\nPt 0 0 0\nPt 1 1 1' },
        top_view: { points: [{ element: 'Pt', x: 0, y: 0 }, { element: 'Pt', x: 1, y: 1 }] },
        provenance: { provider: 'fixture', database_id: 'fixture-1', license: 'CC0',
          raw_structure_hash: 'a'.repeat(64) },
      },
    };
    if (method === 'structure_source_confirm') return {
      ok: true, confirmed: true, source_token: args[0] + '.confirmed',
    };
    if (method === 'surface_dry_run') return {
      ok: true, operation_token: 'operation.opaque',
      dry_run: {
        scientific_status: 'candidate', warnings: [],
        slabs: [{ formula: 'Pt', miller: [1, 1, 1], termination: 'term-opaque',
          layers: 5, structure_hash: 'b'.repeat(64), site_counts: { ontop: 1 },
          equivalence_groups: 1, minimum_distance: 1.8, rejections: [] }],
      },
    };
    if (method === 'structure_output_select') return {
      ok: true, output_token: 'destination.opaque', selection: { label: 'Candidates' },
    };
    if (method === 'surface_create_candidates') return {
      ok: true, jobs: [{ job_id: 'candidate.opaque', scientific_status: 'candidate' }],
    };
    if (method === 'structure_source_capabilities') return { ok: true, providers: [] };
    if (method === 'molecule_list') return { molecules: [] };
    throw new Error('unexpected bridge method: ' + method);
  },
};

vm.runInThisContext(fs.readFileSync(source, 'utf8'), { filename: source });
const seam = window.__VCS_STRUCTURE_SOURCE_HUB_TEST__;
assert.ok(seam);

(async () => {
  element('surface-termination').value = 'auto';
  const automaticTermination = seam.buildSurfaceRequests().slabRequest;
  assert.strictEqual(Object.hasOwn(automaticTermination, 'termination'), false);
  assert.ok(!JSON.stringify(automaticTermination).includes('auto'));
  element('surface-termination').value = 'term-opaque';

  assert.strictEqual(await seam.previewSource('bulk', 'bulk.preview.opaque'), true);
  assert.strictEqual(await seam.runSurfaceDryRun(), false);
  assert.strictEqual(calls.some(call => call[0] === 'surface_dry_run'), false);

  assert.strictEqual(await seam.confirmSource('bulk'), true);
  assert.strictEqual(seam.state.sourceTokens.bulk, 'bulk.preview.opaque.confirmed');
  assert.strictEqual(await seam.previewSource('ads', 'ads.preview.opaque'), true);
  assert.strictEqual(await seam.confirmSource('ads'), true);
  assert.strictEqual(seam.state.sourceTokens.ads, 'ads.preview.opaque.confirmed');

  assert.strictEqual(await seam.runSurfaceDryRun(), true);
  const dry = calls.find(call => call[0] === 'surface_dry_run');
  assert.ok(dry);
  assert.strictEqual(dry[1], 'bulk.preview.opaque.confirmed');
  assert.deepStrictEqual(dry[2], {
    miller: [1, 1, 1], termination: 'term-opaque', layers: 5,
    vacuum: 18, fixed_layers: 2, surface_sides: 'both',
  });
  assert.deepStrictEqual(dry[3], {
    site_kinds: ['ontop', 'bridge', 'hollow', 'other'], binding_atom: 0,
    orientation: 'normal', rotations: [0, 90, 180, 270],
    coverage: 0.25, sides: 'both', min_distance: 1.3,
    adsorbate_source_token: 'ads.preview.opaque.confirmed',
  });

  assert.strictEqual(element('surface-create-candidates').disabled, true);
  assert.strictEqual(await seam.selectSurfaceOutput(), true);
  seam.syncCandidateCreateButton();
  assert.strictEqual(element('surface-create-candidates').disabled, true);
  element('surface-candidate-ack').checked = true;
  seam.syncCandidateCreateButton();
  assert.strictEqual(element('surface-create-candidates').disabled, false);
  assert.strictEqual(await seam.createSurfaceCandidates(), true);

  const create = calls.find(call => call[0] === 'surface_create_candidates');
  assert.deepStrictEqual(create, [
    'surface_create_candidates', 'operation.opaque', 'destination.opaque',
  ]);
  const serialized = JSON.stringify(calls);
  assert.ok(!serialized.includes('C:\\private'));
  assert.ok(!serialized.toLowerCase().includes('api_key'));
  assert.ok(!serialized.toLowerCase().includes('accepted'));
  assert.ok(!serialized.toLowerCase().includes('verified'));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        [NODE, "-e", script, str(EDITOR)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(NODE is None, reason="node is required for executable asset tests")
def test_source_hub_test_seam_is_guarded():
    source = str(EDITOR).replace("\\", "\\\\")
    script = rf"""
global.window = global; window.__VCS_TEST__ = false;
global.document = {{ readyState: 'loading', addEventListener() {{}},
  getElementById() {{ return null; }}, querySelectorAll() {{ return []; }} }};
window.addEventListener = () => {{}};
window.VCS = {{ t: (_key, _params, fallback) => fallback }};
require('{source}');
if (window.__VCS_STRUCTURE_SOURCE_HUB_TEST__ !== undefined) process.exitCode = 1;
"""
    completed = subprocess.run(
        [NODE, "-e", script], check=False, capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
