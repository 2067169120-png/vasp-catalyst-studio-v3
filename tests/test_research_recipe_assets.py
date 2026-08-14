from __future__ import annotations

import json
from pathlib import Path

from tests.test_workspace_runtime_assets import _run_node


ASSETS = Path(__file__).parents[1] / "vcstudio" / "gui_web" / "assets"
LOCALES = ASSETS.parents[1] / "shared" / "locales"


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def test_prepare_templates_hosts_accessible_readonly_recipe_gallery():
    html = _source("index.html")
    workspace = _source("workspace.js")
    css = _source("research-recipes.css")

    assert html.count('href="research-recipes.css"') == 1
    assert html.count('src="research-recipes.js"') == 1
    assert html.count('id="research-recipes-card"') == 1
    assert 'aria-labelledby="rr-title"' in html
    assert 'id="rr-form"' in html
    assert 'id="rr-status" role="status" aria-live="polite"' in html
    assert 'id="rr-node-list" aria-label="DAG nodes"' in html
    assert 'id="rr-preview-hash" tabindex="0"' in html
    assert "focus: '#research-recipes-card'" in workspace
    assert "@media(max-width:760px)" in css
    assert "@media(max-width:480px)" in css
    assert ".rr-recipe:focus-visible" in css


def test_recipe_browser_has_no_instantiation_or_remote_operation_path():
    script = _source("research-recipes.js")
    assert "research_recipe_catalog" in script
    assert "research_recipe_preview" in script
    for forbidden in (
        "campaign_instantiate", "submit_jobs", "continue_jobs", "operation_token",
        "select_dir", "project_path", "job_dir", "remote_host", "password",
    ):
        assert forbidden not in script
    assert "const OPAQUE_ID" in script
    assert "authorizes_execution" not in script
    assert "if (State.busy && options.force !== true) return false" in script
    assert "State.selectionGeneration !== generation" in script
    assert "if (previewPane) previewPane.hidden = true" in script


def test_recipe_i18n_keys_are_complete_and_bilingual():
    script = _source("research-recipes.js")
    html = _source("index.html")
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    keys = {
        *(__import__("re").findall(r"tr\('(research_recipes\.[^']+)'", script)),
        *(__import__("re").findall(r'data-i18n(?:-aria-label)?="(research_recipes\.[^"]+)"',
                                  html)),
    }
    assert keys
    assert not (keys - set(en))
    assert not (keys - set(zh))
    assert set(en) == set(zh)
    assert all(en[key] != zh[key] for key in keys if key != "research_recipes.hash")


def test_fake_dom_builds_gallery_and_submits_only_opaque_evidence():
    _run_node(
        r"""
[
  'research-recipes-card', 'rr-list', 'rr-selection', 'rr-form', 'rr-parameters',
  'rr-evidence', 'rr-preview-button', 'rr-status', 'rr-preview', 'rr-preview-meta',
  'rr-node-list', 'rr-missing-list', 'rr-limits-list', 'rr-reference-list',
  'rr-source-list', 'rr-preview-hash',
].forEach(id => element(id));

const recipe = {
  recipe_id: 'neb_path', recipe_version: '1.0.0',
  label_zh: 'NEB 路径', label_en: 'NEB pathway',
  summary_zh: '只读依赖图', summary_en: 'Read-only dependency graph',
  parameters: [{
    parameter_id: 'intermediate_images', value_kind: 'integer', default: 5,
    label_zh: '中间图像数', label_en: 'Intermediate images', unit: null,
    minimum: 1, maximum: 32,
  }, {
    parameter_id: 'climbing_image', value_kind: 'boolean', default: true,
    label_zh: '爬山图像', label_en: 'Climbing image', unit: null,
    minimum: null, maximum: null,
  }],
  inputs: [{
    input_id: 'initial_state', evidence_type: 'structure_record',
    label_zh: '始态', label_en: 'Initial state', required: true,
  }, {
    input_id: 'atom_mapping', evidence_type: 'imported_record',
    label_zh: '原子映射', label_en: 'Atom mapping', required: true,
  }],
};
const secondRecipe = Object.assign({}, recipe, {
  recipe_id: 'adsorption_energy',
  label_zh: '吸附能', label_en: 'Adsorption energy',
});
const preview = {
  recipe_id: 'neb_path', recipe_version: '1.0.0', status: 'preview_ready',
  nodes: [{ node_id: 'validate_endpoints', task_kind: 'evidence_validation',
    depends_on: [], status: 'ready', missing_prerequisites: [], blocked_by: [],
    parameters: {}, parameter_sources: {}, outputs: ['validated_endpoint_pair'] }],
  missing_prerequisites: [], parameter_overrides: [{
    parameter_id: 'intermediate_images', value: 7, source: 'user_override', unit: null,
  }],
  scientific_limits: [{ code: 'path_not_proof', text_zh: '路径不是机理证明',
    text_en: 'A path is not mechanism proof' }],
  official_reference_urls: ['https://www.vasp.at/wiki/index.php/Nudged_elastic_bands'],
  preview_semantic_sha256: 'a'.repeat(64),
};
const bridgeCalls = [];
VCS.call = async (method, ...args) => {
  bridgeCalls.push({ method, args });
  if (method === 'research_recipe_catalog') return {
    ok: true, recipes: [recipe, secondRecipe], read_only: true, authorizes_execution: false,
  };
  if (method === 'research_recipe_preview') return { ok: true, preview };
  return {};
};
loadAsset(process.argv[1]);
await waitFor(() => bridgeCalls.some(call => call.method === 'research_recipe_catalog'));
const seam = window.__VCS_RESEARCH_RECIPES_TEST__;
assert.ok(seam, 'guarded research recipe seam was not exposed');
await waitFor(() => seam.State.parameterControls.length === 2);
await waitFor(() => seam.State.busy === false);
assert.strictEqual(elements.get('rr-list').children.length, 2);
assert.strictEqual(elements.get('rr-list').children[0].getAttribute('aria-pressed'), 'true');
assert.strictEqual(seam.State.parameterControls[1].input.tagName, 'SELECT');

const parameter = seam.State.parameterControls[0];
parameter.checkbox.checked = true;
parameter.input.disabled = false;
parameter.input.value = '7';
const evidence = seam.State.evidenceControls[0];
evidence.input.value = 'evidence-initial-001';
evidence.origin.value = 'imported';
const mapping = seam.State.evidenceControls[1];
assert.strictEqual(mapping.origin.value, 'imported');
mapping.input.value = 'evidence-mapping-001';
assert.strictEqual(await seam.requestPreview({ preventDefault() {} }), true);

const call = bridgeCalls.find(item => item.method === 'research_recipe_preview');
assert.deepStrictEqual(call.args, ['neb_path', '1.0.0', {
  overrides: { intermediate_images: 7 },
  evidence: { initial_state: {
    ref_type: 'structure_record', opaque_id: 'evidence-initial-001',
    origin: 'imported', revision_id: null,
  }, atom_mapping: {
    ref_type: 'imported_record', opaque_id: 'evidence-mapping-001',
    origin: 'imported', revision_id: null,
  } },
}]);
assert.strictEqual(elements.get('rr-preview').hidden, false);
assert.strictEqual(elements.get('rr-node-list').children.length, 1);
assert.ok(elements.get('rr-preview-hash').textContent.includes('a'.repeat(64)));
assert.ok(!JSON.stringify(call).includes('project_path'));
assert.ok(!JSON.stringify(call).includes('C:\\'));

const count = bridgeCalls.filter(item => item.method === 'research_recipe_preview').length;
evidence.input.value = 'C:\\private\\job.yaml';
assert.strictEqual(await seam.requestPreview({ preventDefault() {} }), false);
assert.strictEqual(bridgeCalls.filter(item =>
  item.method === 'research_recipe_preview').length, count);
assert.ok(!JSON.stringify(bridgeCalls).includes('C:\\private'));

elements.get('rr-list').children[1].click();
assert.strictEqual(elements.get('rr-preview').hidden, true,
  'changing recipe selection must clear the old DAG');
assert.ok(trace.focus.includes('rr-recipe-adsorption_energy'),
  'the replacement recipe button must recover keyboard focus');
""",
        str(ASSETS / "research-recipes.js"),
    )


def test_runtime_test_seam_is_guarded():
    _run_node(
        r"""
element('rr-list');
window.__VCS_TEST__ = false;
loadAsset(process.argv[1]);
assert.strictEqual(window.__VCS_RESEARCH_RECIPES_TEST__, undefined);
""",
        str(ASSETS / "research-recipes.js"),
    )
