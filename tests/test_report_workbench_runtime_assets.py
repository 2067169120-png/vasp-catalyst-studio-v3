"""Executable fake-DOM contract tests for the production report workbench IIFE."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ASSET = (
    Path(__file__).resolve().parents[1]
    / "vcstudio"
    / "gui_web"
    / "assets"
    / "report-workbench.js"
)
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required for executable asset tests")
def test_report_workbench_iife_sends_only_opaque_project_and_destination_tokens():
    script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = process.argv[1];
const projectId = 'project-' + '1'.repeat(32);
const privatePath = 'C:\\private\\project.yaml';
const calls = [];

class ClassList {
  add() {} remove() {} toggle() {} contains() { return false; }
}
class Element {
  constructor(id = '', tag = 'div') {
    this.id = id; this.tagName = tag.toUpperCase(); this.value = '';
    this.checked = false; this.disabled = false; this.hidden = false;
    this.dataset = {}; this.classList = new ClassList(); this.children = [];
    this.options = []; this.attributes = new Map(); this.textContent = '';
    this.innerHTML = ''; this.style = { removeProperty() {} };
  }
  addEventListener() {}
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) || null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...items) { items.forEach(item => this.appendChild(item)); }
  appendChild(item) {
    this.children.push(item);
    if (item && item.tagName === 'OPTION') this.options.push(item);
    return item;
  }
  querySelector(selector) { return new Element(String(selector || 'nested')); }
  querySelectorAll() { return []; }
  closest(selector) { return new Element(String(selector || 'closest')); }
  focus() {}
  scrollIntoView() {}
}
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};
global.document = {
  readyState: 'loading',
  getElementById: element,
  createElement: tag => new Element('', tag),
  createDocumentFragment: () => new Element('', 'fragment'),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
};
global.window = global;
window.window = window; window.document = document; window.__VCS_TEST__ = true;
window.addEventListener = () => {};
global.CustomEvent = class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } };

const spec = {
  preset_id: 'diagnostic-repair', requested_kind: 'diagnostic', audience: 'internal',
  locale: 'zh-CN', formats: ['html'], theme_id: 'diagnostic-a4',
  scope: { kind: 'project', project_ids: [projectId], job_ids: [], species: [],
    configuration_ids: [], stable_only: false, include_failed: true },
  outline: ['executive_summary'], options: { bilingual_mode: 'none', precision: 4 },
};
const catalog = {
  presets: [{ id: 'diagnostic-repair' }],
  formats: { html: { available: true, reason: '' } },
  locales: [{ id: 'zh-CN', available: true, reason: '' }],
  themes: [{ id: 'diagnostic-a4', available: true, reason: '' }],
  bilingual_modes: [{ id: 'none', available: true, reason: '' }],
  precision: { available: true, minimum: 1, maximum: 12, reason: '' },
};
const hash = 'a'.repeat(64);
const token = {
  schema: 'vcstudio.report-preview-token/v1', preview_id: 'preview-opaque',
  project_id: projectId, spec_sha256: hash, snapshot_sha256: hash,
  validation_sha256: hash, report_model_sha256: hash, base_revision: 0,
  base_manifest_sha256: null,
};
window.Project = { current: () => ({ project_id: projectId, name: 'Opaque project' }) };
window.VCS = {
  ready: Promise.resolve(), i18n: { lang: 'en' },
  workspace: {
    state: { project_id: projectId }, current: {},
    drafts: { load: () => null, save() {}, remove() {} },
  },
  unsaved: { clear() {}, mark() {} }, operations: { publish() {} },
  esc: value => String(value == null ? '' : value),
  t: (_key, _params, fallback) => String(fallback || ''),
  toast() {}, log() {}, confirm: async () => true,
  call: async (method, ...args) => {
    calls.push([method, ...args]);
    if (method === 'report_workbench_bootstrap') return {
      schema: 'vcstudio.report-workbench-bootstrap/v1', ok: true,
      project_id: projectId, project: { id: projectId, name: 'Opaque project' },
      catalog, report_spec: spec, status: {}, error: null,
    };
    if (method === 'proj_report_capabilities') return {
      ok: true, formats: { html: { available: true, reason: '' } },
    };
    if (method === 'report_workbench_history') return {
      schema: 'vcstudio.report-history/v1', ok: true, project_id: projectId,
      generation: 0, revisions: [], error: null,
    };
    if (method === 'report_workbench_preview') return {
      schema: 'vcstudio.report-preview/v1', ok: true, project_id: projectId,
      preview_id: token.preview_id, preview_token: token, report_spec: spec,
      html: '<!doctype html><title>preview</title>', report_model_sha256: hash,
      validation: { schema: 'vcstudio.validation-result/v1' },
      format_status: { html: { available: true, state: 'ready', reason: '' } },
      error: null,
    };
    if (method === 'report_workbench_pick_destination') return {
      schema: 'vcstudio.report-workbench-destination/v1', ok: true,
      cancelled: false, destination_token: 'report-workbench.destination-token',
      display_name: 'Reports', error: null,
    };
    if (method === 'report_workbench_publish') return {
      schema: 'vcstudio.report-publish/v1', ok: true, project_id: projectId,
      preview_id: token.preview_id, artifact_status: 'complete', files: {},
      revision: { sequence: 1, revision_id: 'report-r0001' }, error: null,
    };
    throw new Error('unexpected bridge method: ' + method);
  },
};

vm.runInThisContext(fs.readFileSync(source, 'utf8'), { filename: source });
assert.ok(window.__VCS_REPORT_WORKBENCH_TEST__);

(async () => {
  const seam = window.__VCS_REPORT_WORKBENCH_TEST__;
  assert.strictEqual(await seam.loadBootstrap('diagnostic-repair'), true);
  assert.strictEqual(await seam.pickOutputDirectory(projectId),
    'report-workbench.destination-token');
  assert.strictEqual(await seam.publishBoundPreview(), true);

  const workbench = calls.filter(item => item[0].startsWith('report_workbench_'));
  const methods = workbench.map(item => item[0]);
  for (const required of ['report_workbench_bootstrap', 'report_workbench_history',
    'report_workbench_preview', 'report_workbench_pick_destination',
    'report_workbench_publish']) assert.ok(methods.includes(required), required);
  workbench.forEach(item => assert.strictEqual(item[1], projectId));
  const publish = workbench.find(item => item[0] === 'report_workbench_publish');
  assert.strictEqual(publish[2], 'report-workbench.destination-token');
  assert.ok(!JSON.stringify(workbench).includes(privatePath));
  assert.ok(!JSON.stringify(workbench).includes('outputDir'));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        [NODE, "-e", script, str(ASSET)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(NODE is None, reason="node is required for executable asset tests")
def test_report_workbench_test_seam_is_guarded():
    source = str(ASSET).replace("\\", "\\\\")
    script = rf"""
global.window = global; window.__VCS_TEST__ = false;
global.document = {{ readyState: 'loading', addEventListener() {{}}, getElementById() {{ return null; }},
  querySelectorAll() {{ return []; }} }};
window.addEventListener = () => {{}};
window.VCS = {{ ready: Promise.resolve(), workspace: {{}}, t: (_k, _p, f) => f }};
require('{source}');
if (window.__VCS_REPORT_WORKBENCH_TEST__ !== undefined) process.exitCode = 1;
"""
    completed = subprocess.run(
        [NODE, "-e", script], check=False, capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
