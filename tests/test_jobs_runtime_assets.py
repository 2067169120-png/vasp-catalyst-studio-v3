"""Executable Node regressions for the Jobs page state machine.

The production asset remains a classic browser script.  A deliberately tiny fake DOM is enough to
exercise its bridge/state transitions without adding a Node package manager or a second UI stack.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for executable asset tests")
def test_jobs_last_good_selection_tray_and_operation_mutex_are_executable(tmp_path):
    harness = tmp_path / "jobs-runtime-test.js"
    harness.write_text(
        r"""
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fakeElement(id = '') {
  return {
    id, hidden: false, disabled: false, value: '', innerHTML: '', textContent: '',
    className: '', options: [], children: [], attributes: Object.create(null),
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] || null; },
    removeAttribute(name) { delete this.attributes[name]; },
    appendChild(child) { this.children.push(child); this.options.push(child); return child; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, focus() {},
  };
}

const elements = Object.create(null);
function install(id, value = {}) {
  elements[id] = Object.assign(fakeElement(id), value);
  return elements[id];
}
const document = {
  readyState: 'loading', body: fakeElement('body'),
  querySelector(selector) { return selector.startsWith('#') ? (elements[selector.slice(1)] || null) : null; },
  querySelectorAll() { return []; },
  createElement(tag) { return fakeElement(tag); },
  createTextNode(value) { return { textContent: String(value) }; },
  addEventListener() {}, dispatchEvent() {},
};
const window = { __VCS_TEST__: true, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) };
const logs = []; const toasts = [];
let jobResponses = [];
const VCS = {
  i18n: { lang: 'en' }, workspace: { state: { project_id: '' } },
  call: async name => name === 'list_profiles' ? { profiles: [] } : jobResponses.shift(),
  t: (_key, params, fallback) => String(fallback || '').replace(/\{([A-Za-z0-9_]+)\}/g,
    (match, name) => Object.prototype.hasOwnProperty.call(params || {}, name) ? String(params[name]) : match),
  esc: value => String(value == null ? '' : value),
  pill: state => `<span>${String(state)}</span>`, elementBadge: () => '',
  log: (...args) => logs.push(args), toast: (...args) => toasts.push(args),
  operations: { records: [], publish(record) { this.records.push(Object.assign({}, record)); } },
};
Object.assign(globalThis, {
  document, window, VCS, localStorage: { getItem: () => null, setItem() {} },
  CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
  Event: class Event { constructor(type) { this.type = type; } },
  setInterval: () => 1, clearInterval() {},
});
vm.runInThisContext(fs.readFileSync(process.env.JOBS_SOURCE, 'utf8'), { filename: process.env.JOBS_SOURCE });
const hooks = window.Jobs.__test;
assert.ok(hooks, 'test seam was not exposed');

(async () => {
  // A successful load followed by a backend error keeps the authoritative last-good rows.
  jobResponses = [{ jobs: [{ dir: '/a', name: 'A', state: 'DONE' }], stale: ['/old'] }];
  hooks.State.reloadGeneration = 1;
  await hooks.performReload(1);
  assert.equal(hooks.State.ledgerStatus, 'ready');
  assert.deepEqual(hooks.State.rows.map(row => row.dir), ['/a']);
  jobResponses = [{ error: 'disk temporarily unavailable', jobs: [], stale: [] }];
  hooks.State.reloadGeneration = 2;
  await hooks.performReload(2);
  assert.equal(hooks.State.ledgerStatus, 'stale');
  assert.deepEqual(hooks.State.rows.map(row => row.dir), ['/a']);
  assert.equal(hooks.State.stale[0], '/old');

  hooks.State.rows = []; hooks.State.stale = []; hooks.State.lastLoadedAt = '';
  jobResponses = [{ error: 'first load failed' }]; hooks.State.reloadGeneration = 3;
  await hooks.performReload(3);
  assert.equal(hooks.State.ledgerStatus, 'unavailable');

  // The Selection Tray enumerates the complete selection and calls out filter-hidden targets.
  install('jobs-selection-tray'); install('jobs-selection-list');
  install('jobs-selection-summary'); install('jobs-selection-review');
  install('jf-cluster', { value: 'c1' }); install('jf-status', { value: '' });
  install('jf-sort', { value: 'time' });
  hooks.State.rows = [
    { dir: '/a', name: 'A', state: 'CREATED', cluster: 'c1', project: 'P' },
    { dir: '/b', name: 'B', state: 'DONE', cluster: 'c2', project: 'P' },
  ];
  hooks.State.selected = new Set(['/a', '/b']);
  hooks.renderSelectionTray();
  assert.match(elements['jobs-selection-summary'].textContent, /2 selected; 1 hidden/);
  assert.match(elements['jobs-selection-list'].innerHTML, />A</);
  assert.match(elements['jobs-selection-list'].innerHTML, />B</);
  const targets = hooks.operationTargetText(['/a', '/b']);
  assert.match(targets, /1\. A \| Project: P \| Status: CREATED \| Cluster: c1/);
  assert.match(targets, /2\. B \| Project: P \| Status: DONE \| Cluster: c2/);

  // The mutex is acquired before the async confirmation/work callback can settle.
  install('jobs-operation-queue'); install('jobs-operation-active'); install('jobs-operation-history');
  ['jb-submit', 'jb-fetch', 'jb-continue', 'jb-cancelchecked', 'jb-remove',
    'jb-tray-submit', 'jb-tray-fetch', 'jb-tray-continue', 'jb-tray-cancel', 'jb-tray-remove']
    .forEach(id => install(id));
  let release; const gate = new Promise(resolve => { release = resolve; }); let workCalls = 0;
  const first = hooks.withExclusiveOperation('submit', 'Submit', ['/a'], 'c1', async () => {
    workCalls += 1; await gate; return { status: 'succeeded', value: { ok: true } };
  });
  await Promise.resolve();
  assert.equal(elements['jb-submit'].disabled, true);
  const duplicate = await hooks.withExclusiveOperation('submit', 'Submit', ['/a'], 'c1', async () => {
    workCalls += 1; return { status: 'succeeded' };
  });
  assert.equal(duplicate, null); assert.equal(workCalls, 1);
  release(); await first;
  assert.equal(hooks.State.activeOperation, null);
  assert.equal(hooks.State.operationHistory[0].status, 'succeeded');
  assert.equal(elements['jb-submit'].disabled, false);
  assert.deepEqual(VCS.operations.records.map(record => record.status), ['confirming', 'succeeded']);

  // Programmatic selection expands the same canonical key used by table grouping.
  hooks.State.selected = new Set(); hooks.State.expanded = new Set();
  jobResponses = [{ jobs: [{ dir: '/created', name: 'C', state: 'CREATED', cluster: '',
    project: 'P', project_id: 'project-alpha' }], stale: [] }];
  await window.Jobs.selectCreatedProject('project-alpha');
  assert.equal(hooks.State.expanded.has('p:id:project-alpha'), true);
  process.stdout.write('ok\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
        encoding="utf-8",
    )
    env = dict(os.environ, JOBS_SOURCE=str(ASSETS / "jobs.js"))
    result = subprocess.run(
        [NODE, str(harness)], cwd=ASSETS, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"
