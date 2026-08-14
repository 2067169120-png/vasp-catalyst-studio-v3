from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"
NODE = shutil.which("node")


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def test_generate_page_contains_accessible_four_step_recipe_wizard_and_narrow_layout():
    html = _source("index.html")
    css = _source("app.css")
    for element_id in (
        "method-recipe-card", "mr-system", "mr-task", "mr-policy", "mr-suggest",
        "mr-dispersion", "mr-spin", "mr-u-mode", "mr-dipole", "mr-preview",
        "mr-risk-list", "mr-diff", "mr-ack", "mr-confirm",
    ):
        assert f'id="{element_id}"' in html
    assert html.count('class="mr-step"') == 4
    assert "<fieldset>" in html and "<legend>" in html
    assert 'for="mr-ack"' in html
    assert "@media(max-width:560px)" in css
    assert "#page-generate .mr-grid{grid-template-columns:minmax(0,1fr)}" in css


def test_browser_recipe_section_delegates_science_and_never_calls_submission():
    script = _source("generate.js")
    section = script.split("// ── Method Recipe / INCAR 向导", 1)[1].split(
        "// ── SAC 批量建模", 1)[0]
    for backend_call in (
        "method_recipe_catalog", "method_recipe_suggest", "method_recipe_preview",
        "method_recipe_confirm",
    ):
        assert backend_call in section
    for forbidden in (
        "recommend_kpoints", "max_enmax", "parse_incar", "effective_u_by_element",
        "submit_jobs", "submit_batch", "continue_jobs", "localStorage",
    ):
        assert forbidden not in section
    assert "preview.token" in section and "preview.preview_sha256" in section
    assert "confirmed: true" in section


@pytest.mark.skipif(NODE is None, reason="node is required for executable asset tests")
def test_production_generate_js_recipe_seam_passes_raw_draft_and_opaque_confirmation(tmp_path):
    harness = tmp_path / "method-recipe-runtime.cjs"
    harness.write_text(
        r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase(); this.id = id; this.value = ''; this.checked = false;
    this.disabled = false; this.children = []; this.dataset = {}; this.className = '';
    this.style = {}; this.type = ''; this.textContent = ''; this.listeners = {};
  }
  appendChild(child) { this.children.push(child); return child; }
  append(...items) { items.forEach(item => this.appendChild(item)); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  get options() { return this.children; }
}

const ids = {};
function add(id, tag = 'input', value = '') {
  const element = new Element(tag, id); element.value = value; ids[id] = element; return element;
}
[
  ['mr-system','select','slab'], ['mr-task','select','relax'], ['mr-policy','select',''],
  ['mr-xc','select','PBE'], ['mr-dispersion','select','none'],
  ['mr-precision','select','accurate'], ['mr-ediff','input','0.00001'],
  ['mr-ediffg','input','0.02'], ['mr-spin','select','nonspin'], ['mr-magmom','input',''],
  ['mr-u-mode','select','off'], ['mr-u-json','textarea',''], ['mr-dipole','select','off'],
  ['mr-solvent','select','off'], ['mr-dielectric','input','78.4'],
  ['mr-encut-mode','select','enmax_multiplier'], ['mr-encut-multiplier','input','1.3'],
  ['mr-encut-value','input',''], ['mr-kpoints-mode','select','recommended'],
  ['mr-kpoints-grid','input',''], ['gen-poscar','input','C:/private/POSCAR'],
  ['gen-incar','input','C:/private/INCAR'], ['gen-out','input','C:/private/job'],
  ['gen-lib','input','C:/private/potentials'], ['mr-preview-status','div',''],
  ['mr-risk-list','div',''], ['mr-diff','div',''], ['mr-ack','input',''],
  ['mr-confirm','button',''], ['mr-dry-run','input',''],
].forEach(args => add(...args));
ids['mr-ack'].type = 'checkbox'; ids['mr-dry-run'].type = 'checkbox';

function walk(root, predicate, out = []) {
  (root.children || []).forEach(child => { if (predicate(child)) out.push(child); walk(child, predicate, out); });
  return out;
}
global.document = {
  readyState: 'loading',
  getElementById: id => ids[id] || null,
  createElement: tag => new Element(tag),
  addEventListener: () => {},
  querySelectorAll: selector => selector === '#mr-diff .mr-resolution'
    ? walk(ids['mr-diff'], el => String(el.className).split(/\s+/).includes('mr-resolution')) : [],
};
global.window = global;

const calls = [];
global.VCS = {
  i18n: { lang: 'en' },
  t: (_key, _params, fallback) => fallback,
  call: async (name, payload) => {
    calls.push([name, payload]);
    if (name === 'method_recipe_preview') return {
      ok: true, token: 'opaque-preview-token', preview_sha256: 'a'.repeat(64),
      target_id: 'b'.repeat(64), conflicts: ['ENCUT'],
      recipe: { dimensions: { encut: { risk: 'high', reason: { zh: '风险', en: 'Risk' } } } },
      diff: [{ key: 'ENCUT', status: 'conflict', existing: 450, proposed: 520,
        requires_resolution: true, risk: 'high', reason: { zh: '原因', en: 'Reason' } }],
    };
    if (name === 'method_recipe_confirm') return {
      ok: true, job_id: 'job-opaque', recipe_semantic_sha256: 'c'.repeat(64), warnings: [],
    };
    throw new Error(`unexpected call ${name}`);
  },
  log: () => {}, toast: () => {}, confirm: async () => true,
};
global.Jobs = { reload: () => {} };

vm.runInThisContext(fs.readFileSync(process.env.GENERATE_SOURCE, 'utf8'), {
  filename: process.env.GENERATE_SOURCE,
});

(async () => {
  const draft = window.Generate.methodRecipe.collectDraft();
  assert.equal(draft.encut_value, null);
  assert.equal(draft.kpoints_grid, null);
  assert.equal(draft.encut_multiplier, 1.3);

  await window.Generate.methodRecipe.preview();
  const previewCall = calls.find(item => item[0] === 'method_recipe_preview');
  assert.equal(previewCall[1].draft.encut_value, null);
  assert.equal(previewCall[1].draft.kpoints_grid, null);
  assert.equal(previewCall[1].poscar_path, 'C:/private/POSCAR');

  const resolution = document.querySelectorAll('#mr-diff .mr-resolution')[0];
  assert.ok(resolution); resolution.value = 'existing'; ids['mr-ack'].checked = true;
  await window.Generate.methodRecipe.confirm();
  const confirmCall = calls.find(item => item[0] === 'method_recipe_confirm');
  assert.equal(confirmCall[1].token, 'opaque-preview-token');
  assert.equal(confirmCall[1].preview_sha256, 'a'.repeat(64));
  assert.equal(confirmCall[1].target_id, 'b'.repeat(64));
  assert.deepEqual(confirmCall[1].resolutions, { ENCUT: 'existing' });
  assert.equal(confirmCall[1].confirmed, true);
  assert.ok(!calls.some(item => /submit|continue/.test(item[0])));
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["GENERATE_SOURCE"] = str(ASSETS / "generate.js")
    subprocess.run([NODE, str(harness)], check=True, env=env, timeout=30)
