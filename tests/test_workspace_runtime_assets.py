"""Executable Node regressions for the production workspace/settings IIFEs."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"
NODE = shutil.which("node")


_NODE_PRELUDE = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const trace = {
  events: [], history: [], raf: [], focus: [], calls: [], storageSets: [],
  logs: [], toasts: [], appliedScenarios: [],
};

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const wanted = force === undefined ? !this.values.has(name) : !!force;
    if (wanted) this.values.add(name); else this.values.delete(name);
    return wanted;
  }
}

class FakeElement {
  constructor(id = '', properties = {}) {
    this.id = id;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.type = '';
    this.tagName = 'DIV';
    this.options = [];
    this.dataset = {};
    this.style = { removeProperty() {} };
    this.classList = new FakeClassList();
    this.attributes = new Map();
    this.listeners = new Map();
    this.children = [];
    this.textContent = '';
    this.innerHTML = '';
    this.isConnected = true;
    Object.assign(this, properties);
  }
  addEventListener(name, callback) {
    const items = this.listeners.get(name) || [];
    items.push(callback); this.listeners.set(name, items);
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    for (const callback of this.listeners.get(event.type) || []) callback(event);
    return true;
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  insertBefore(child) { this.children.unshift(child); return child; }
  remove() { this.isConnected = false; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() { trace.focus.push(this.id || 'anonymous'); document.activeElement = this; }
  click() {
    for (const callback of this.listeners.get('click') || []) {
      callback({ type: 'click', target: this, preventDefault() {} });
    }
  }
}

const elements = new Map();
function element(id, properties = {}) {
  const value = new FakeElement(id, properties);
  elements.set(id, value);
  return value;
}

const documentListeners = new Map();
const heading = new FakeElement('page-heading');
const pageSection = new FakeElement('page-section', { dataset: { page: 'report-workbench' } });
pageSection.querySelector = selector => selector === 'h1,.pagebar h1' ? heading : null;
pageSection.querySelectorAll = () => [];

global.document = {
  readyState: 'loading',
  title: '',
  activeElement: null,
  body: new FakeElement('body'),
  documentElement: new FakeElement('html'),
  getElementById(id) { return elements.get(id) || null; },
  createElement(tag) { return new FakeElement('', { tagName: String(tag).toUpperCase() }); },
  querySelector(selector) {
    if (selector.startsWith('main section[data-page')) return pageSection;
    return null;
  },
  querySelectorAll() { return []; },
  addEventListener(name, callback) {
    const items = documentListeners.get(name) || [];
    items.push(callback); documentListeners.set(name, items);
  },
  dispatchEvent(event) {
    trace.events.push({ type: event.type, detail: event.detail });
    for (const callback of documentListeners.get(event.type) || []) callback(event);
    return true;
  },
};

class FakeEvent {
  constructor(type, init = {}) { this.type = type; Object.assign(this, init); }
  preventDefault() { this.defaultPrevented = true; }
}
class FakeCustomEvent extends FakeEvent {
  constructor(type, init = {}) { super(type, init); this.detail = init.detail; }
}
global.Event = FakeEvent;
global.CustomEvent = FakeCustomEvent;
global.MutationObserver = class { observe() {} disconnect() {} };

const storage = new Map();
global.localStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) {
    storage.set(String(key), String(value));
    trace.storageSets.push({ key: String(key), value: String(value) });
  },
  removeItem(key) { storage.delete(String(key)); },
};

global.history = {
  state: null,
  pushState(state, _title, url) {
    this.state = state; trace.history.push({ method: 'push', state, url });
    window.location.hash = url;
  },
  replaceState(state, _title, url) {
    this.state = state; trace.history.push({ method: 'replace', state, url });
    window.location.hash = url;
  },
  go(delta) { trace.history.push({ method: 'go', delta }); },
  back() { trace.history.push({ method: 'back' }); },
};

global.window = global;
window.window = window;
window.document = document;
window.location = { hash: '#/home' };
window.__VCS_TEST__ = true;
window.addEventListener = () => {};
window.matchMedia = () => ({ matches: false });
window.scrollX = 0;
window.scrollY = 0;
window.scrollTo = () => {};
window.getComputedStyle = () => ({ display: 'block', visibility: 'visible' });
global.requestAnimationFrame = callback => { trace.raf.push(callback); return trace.raf.length; };

function interpolate(fallback, params) {
  return String(fallback || '').replace(/\{([^{}]+)\}/g,
    (match, name) => Object.prototype.hasOwnProperty.call(params || {}, name)
      ? String(params[name]) : match);
}

window.VCS = {
  ready: Promise.resolve(),
  i18n: { lang: 'en' },
  pipeline: { events: [], runtime: {} },
  scenario: {},
  esc(value) { return String(value == null ? '' : value); },
  t(_key, params, fallback) { return interpolate(fallback, params); },
  call: async method => { trace.calls.push(method); return {}; },
  confirm: async () => true,
  toast(message) { trace.toasts.push(String(message)); },
  log(message) { trace.logs.push(String(message)); },
  canActivatePage: () => true,
  sceneVisible: () => true,
  activatePage: () => true,
  focusNavigationTarget: async options => {
    trace.focus.push('explicit:' + String(options.focusSelector || options.focusJobDir || ''));
    return true;
  },
  applyScenario(scenario) { trace.appliedScenarios.push(scenario.key); },
};

function loadAsset(path) {
  vm.runInThisContext(fs.readFileSync(path, 'utf8'), { filename: path });
}

function deferred() {
  let resolve; let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function waitFor(predicate, message) {
  for (let index = 0; index < 50; index += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  throw new Error(message || 'condition was not reached');
}
"""


def _run_node(body: str, *assets: str) -> None:
    if NODE is None:
        pytest.skip("Node.js is required for executable frontend regressions")
    script = (
        _NODE_PRELUDE
        + "\n(async () => {\n"
        + body
        + "\n})().catch(error => { console.error(error); process.exitCode = 1; });\n"
    )
    command = [NODE, "-e", script, *assets]
    completed = subprocess.run(
        command,
        cwd=ASSETS.parents[2],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Node runtime regression failed\nstdout:\n{completed.stdout}"
        f"\nstderr:\n{completed.stderr}"
    )


def test_project_callback_false_does_not_commit_shell_history_or_event():
    _run_node(
        r"""
loadAsset(process.argv[1]);
const seam = window.__VCS_WORKSPACE_TEST__;
assert.ok(seam, 'guarded workspace seam was not exposed');
seam.configure({
  projectRows: [
    { id: 'project-a', name: 'A', path: 'A/project.yaml' },
    { id: 'project-b', name: 'B', path: 'B/project.yaml' },
  ],
  projectId: 'project-a',
});
const before = seam.snapshot();
let callbackCalls = 0;
const switched = await seam.requestProjectSwitch('project-b', async hit => {
  callbackCalls += 1;
  assert.strictEqual(hit.id, 'project-b');
  return false;
});
const after = seam.snapshot();
assert.strictEqual(switched, false);
assert.strictEqual(callbackCalls, 1);
assert.strictEqual(before.state.project_id, 'project-a');
assert.strictEqual(after.state.project_id, 'project-a');
assert.strictEqual(after.selected_project.id, 'project-a');
assert.strictEqual(trace.history.length, 0, 'history must not commit');
assert.strictEqual(trace.storageSets.length, 0, 'shell state must not persist');
assert.strictEqual(trace.events.filter(item => item.type === 'vcs:workspace-project').length, 0,
  'workspace project event must not publish');
""",
        str(ASSETS / "workspace.js"),
    )


def test_operation_queue_persists_redacted_failures_and_exposes_safe_retry_route():
    _run_node(
        r"""
storage.set('vcs.workspace.operations.v1', JSON.stringify({
  schema: 'vcstudio.operation-queue/v1',
  records: [{
    id: 'old-job', kind: 'submit', label: 'Old cached failure', status: 'failed',
    count: 1, route: 'run-jobs', error: 'Safe summary',
    nested_backend_payload: {
      password: 'must-not-survive', path: '/scratch/private/old-job',
    },
  }],
}));
loadAsset(process.argv[1]);
assert.ok(VCS.operations && typeof VCS.operations.publish === 'function');
const rewrittenCache = storage.get('vcs.workspace.operations.v1');
assert.ok(!rewrittenCache.includes('must-not-survive'));
assert.ok(!rewrittenCache.includes('/scratch/private'));
assert.strictEqual(VCS.operations.publish({
  id: 'continue-job-1', kind: 'continue', label: 'Continue one job',
  status: 'failed', count: 1, route: 'run-jobs',
  error: [
    'Submission failed but can be retried.',
    'C:\\Users\\alice\\Private\\job.yaml',
    '/scratch/team/private/job.yaml',
    'password=hunter2',
    'https://alice:swordfish@example.test/api',
    'AKIA1234567890ABCDEF',
    'aws_secret_access_key=abcdEFGH0123456789/secret',
    'github_pat_1234567890secret',
    'client_secret=hunter2-client',
    'Authorization: Basic dXNlcjpwYXNzd29yZA==',
    'failed,/scratch/private/punctuated-job.yaml',
  ].join(' '),
}), true);
const records = VCS.operations.snapshot();
assert.strictEqual(records.length, 2);
const current = records.find(item => item.id === 'continue-job-1');
assert.strictEqual(current.status, 'failed');
assert.strictEqual(current.route, 'run-jobs');
assert.ok(current.error.includes('Submission failed but can be retried.'));
assert.ok(current.error.includes('[redacted-path]'));
assert.ok(current.error.includes('[redacted]'));
const rawStored = storage.get('vcs.workspace.operations.v1');
for (const forbidden of [
  'alice', 'swordfish', 'hunter2', 'github_pat_', 'AKIA1234567890ABCDEF',
  'abcdEFGH0123456789', 'dXNlcjpwYXNzd29yZA', '/scratch/', 'C:\\Users\\',
]) assert.ok(!rawStored.includes(forbidden), forbidden + ' leaked to localStorage');
const stored = JSON.parse(rawStored);
assert.strictEqual(stored.schema, 'vcstudio.operation-queue/v1');
assert.strictEqual(stored.records.length, 2);
assert.strictEqual(stored.records.find(item => item.id === 'continue-job-1').error, current.error);
assert.ok(await VCS.operations.retry('continue-job-1'));
assert.strictEqual(trace.history.at(-1).url, '#/jobs');
""",
        str(ASSETS / "workspace.js"),
    )


def test_workspace_adopts_explicit_authority_reset_and_resumes_cas_saves():
    _run_node(
        r"""
const oldAuthority = 'a'.repeat(32);
const newAuthority = 'b'.repeat(32);
storage.set('vcs.workspace.context.v1', JSON.stringify({
  schema: 'vcstudio.workspace-context/v1', route: '#/home', project_id: '',
  analysis_id: '', selected_job_id: '', panels: {}, filters: {}, sort: {}, scroll: {},
  draft_refs: {
    import_flow: { project_id: null, route: '#/home', updated_at_ms: 10,
      dirty: true, status: 'unverified_draft', size: 12 },
  },
  server_authority_id: oldAuthority, server_revision: 5, updated_at_ms: 20,
}));
const completeSnapshot = {
  ok: true, schema: 'vcstudio.workspace-context/v1', authority_id: newAuthority,
  state_revision: 0,
  selection: { route_hash: '#/home', project_id: null, analysis_id: null, job_id: null },
  restore: { panels: {}, filters: {}, sort: {}, scroll: {}, draft_refs: {} },
  projects: [],
};
let updateCalls = 0;
VCS.call = async (method, ...args) => {
  trace.calls.push({ method, args });
  if (method === 'workspace_preferences_update') {
    updateCalls += 1;
    if (updateCalls === 1) {
      return { ok: false, conflict: true, authority_id: newAuthority,
        state_revision: 0, preferences: {} };
    }
    return { ok: true, conflict: false, authority_id: newAuthority,
      state_revision: 1, preferences: args[0].set };
  }
  if (method === 'workspace_context') return completeSnapshot;
  return {};
};
loadAsset(process.argv[1]);
const seam = window.__VCS_WORKSPACE_TEST__;
let snapshot = seam.snapshot();
assert.strictEqual(snapshot.authority_id, oldAuthority);
assert.strictEqual(snapshot.revision, 5);

await seam.saveRemoteState();
assert.strictEqual(seam.snapshot().remote_save_blocked, true);
await seam.refreshWorkspaceContext();
snapshot = seam.snapshot();
assert.strictEqual(snapshot.authority_id, newAuthority);
assert.strictEqual(snapshot.revision, 0);
assert.strictEqual(snapshot.remote_save_blocked, false);
assert.ok(snapshot.state.draft_refs.import_flow, 'local draft reference must survive reset adoption');

await seam.saveRemoteState();
const updates = trace.calls.filter(item => item.method === 'workspace_preferences_update');
assert.strictEqual(updates.length, 2);
assert.deepStrictEqual(updates.map(item => item.args.slice(1)), [
  [5, oldAuthority], [0, newAuthority],
]);
snapshot = seam.snapshot();
assert.strictEqual(snapshot.authority_id, newAuthority);
assert.strictEqual(snapshot.revision, 1);
assert.strictEqual(snapshot.remote_save_blocked, false);
""",
        str(ASSETS / "workspace.js"),
    )


def test_workspace_rejects_lower_revision_from_same_authority():
    _run_node(
        r"""
const authority = 'c'.repeat(32);
storage.set('vcs.workspace.context.v1', JSON.stringify({
  schema: 'vcstudio.workspace-context/v1', route: '#/home', project_id: 'local-project',
  analysis_id: '', selected_job_id: '', panels: {}, filters: {}, sort: {}, scroll: {},
  draft_refs: {}, server_authority_id: authority, server_revision: 5, updated_at_ms: 20,
}));
VCS.call = async (method, ...args) => {
  trace.calls.push({ method, args });
  if (method === 'workspace_context') return {
    ok: true, schema: 'vcstudio.workspace-context/v1', authority_id: authority,
    state_revision: 4,
    selection: { route_hash: '#/home', project_id: 'stale-project' },
    restore: { panels: {}, filters: {}, sort: {}, scroll: {}, draft_refs: {} },
    projects: [],
  };
  if (method === 'workspace_preferences_update') return {
    ok: true, authority_id: authority, state_revision: 6, preferences: args[0].set,
  };
  return {};
};
loadAsset(process.argv[1]);
const seam = window.__VCS_WORKSPACE_TEST__;
await seam.refreshWorkspaceContext();
let snapshot = seam.snapshot();
assert.strictEqual(snapshot.authority_id, authority);
assert.strictEqual(snapshot.revision, 5);
assert.strictEqual(snapshot.state.project_id, 'local-project');

await seam.saveRemoteState();
const update = trace.calls.find(item => item.method === 'workspace_preferences_update');
assert.deepStrictEqual(update.args.slice(1), [5, authority]);
snapshot = seam.snapshot();
assert.strictEqual(snapshot.revision, 6);
assert.strictEqual(snapshot.state.project_id, 'local-project');
""",
        str(ASSETS / "workspace.js"),
    )


def test_workspace_project_switch_uses_only_opaque_id_and_purges_legacy_locator_storage():
    _run_node(
        r"""
storage.set('vcs.adsorption.current_project', 'C:\\secret\\project.yaml');
storage.set('vcs.adsorption.compare_projects', JSON.stringify(['C:\\secret\\project.yaml']));
const selected = [];
window.Project = { selectById: async id => { selected.push(id); return true; } };
loadAsset(process.argv[1]);
const seam = window.__VCS_WORKSPACE_TEST__;
assert.ok(seam);
assert.strictEqual(storage.has('vcs.adsorption.current_project'), false);
assert.strictEqual(storage.has('vcs.adsorption.compare_projects'), false);
seam.configure({
  projectRows: [
    { id: 'project-a', project_id: 'project-a', name: 'A', n_members: 2, n_done: 1 },
    { id: 'project-b', project_id: 'project-b', name: 'B', n_members: 3, n_done: 3 },
  ],
  projectId: 'project-a',
});
const switched = await seam.requestProjectSwitch('project-b',
  hit => window.Project.selectById(hit.id));
assert.strictEqual(switched, true);
assert.deepStrictEqual(selected, ['project-b']);
const event = trace.events.filter(item => item.type === 'vcs:workspace-project').at(-1);
assert.ok(event);
assert.strictEqual(event.detail.project_id, 'project-b');
assert.deepStrictEqual(Object.keys(event.detail).sort(), ['counts', 'name', 'project_id']);
assert.ok(!JSON.stringify(event.detail).includes('secret'));
assert.strictEqual(seam.snapshot().state.project_id, 'project-b');
""",
        str(ASSETS / "workspace.js"),
    )


def test_project_registry_comparison_and_figure_bridges_reject_locator_identity_at_runtime():
    _run_node(
        r"""
element('pj-select', { tagName: 'SELECT' });
element('fig-bar', { checked: true });
element('fig-table', { checked: false });
element('fig-ladder', { checked: false });
element('fig-heatmap', { checked: true });
element('fig-scaling', { checked: false });
element('fig-volcano', { checked: false });
element('fig-compare-ladder', { checked: false });
element('pj-preset', { tagName: 'SELECT', value: '' });
element('pj-figs');
const absolute = 'C:\\private\\alpha\\project.yaml';
storage.set('vcs.adsorption.current_project', absolute);
storage.set('vcs.adsorption.compare_projects', JSON.stringify([absolute]));
const bridgeCalls = [];
VCS.call = async (method, ...args) => {
  bridgeCalls.push({ method, args });
  if (method === 'proj_list') return { ok: true, projects: [
    { project_id: 'project-alpha', name: 'Alpha', counts: { members: 2, done: 2 }, path: absolute },
    { project_id: 'project-beta', name: 'Beta', counts: { members: 3, done: 3 }, path: 'D:\\private\\beta\\project.yaml' },
  ] };
  if (method === 'pipeline_status') return { projects: [
    { project_id: 'project-alpha', stage: 'analysis', counts: { members: 2, done: 2 }, path: absolute },
    { project_id: 'project-beta', stage: 'analysis', counts: { members: 3, done: 3 } },
  ] };
  if (method === 'proj_compare_preview') return { ok: true, projects: [
    { project_id: 'project-alpha', name: 'Alpha', status: 'ready', path: absolute },
    { project_id: 'project-beta', name: 'Beta', status: 'ready', path: 'D:\\private\\beta\\project.yaml' },
  ], selected_count: 2, ready_count: 2 };
  if (method === 'proj_evaluate_candidate') return { error: 'method not found' };
  if (method === 'proj_figures' || method === 'proj_compare_figures') return { ok: true, files: [] };
  return {};
};
loadAsset(process.argv[1]);
assert.ok(window.Project && window.Project.__test);
assert.strictEqual(await window.Project.selectById('project-alpha'), true);
assert.strictEqual(storage.has('vcs.adsorption.current_project'), false);
assert.strictEqual(storage.has('vcs.adsorption.compare_projects'), false);
assert.deepStrictEqual(Object.keys(window.Project.current()).sort(), ['counts', 'name', 'project_id']);
assert.ok(window.Project.list().every(project => !Object.hasOwn(project, 'path')));
assert.ok(window.Project.__test.State.projects.every(project => !Object.hasOwn(project, 'path')));
const event = trace.events.filter(item => item.type === 'vcs:project-context').at(-1);
assert.deepStrictEqual(Object.keys(event.detail).sort(), ['counts', 'name', 'project_id']);

window.Project.__test.setCompareSelection(['project-alpha', absolute, 'project-beta']);
await window.Project.__test.refreshComparePreview();
assert.deepStrictEqual(Array.from(window.Project.__test.State.compareProjectIds),
  ['project-alpha', 'project-beta']);
assert.ok(window.Project.__test.State.comparePreview.projects.every(project =>
  !Object.hasOwn(project, 'path')));
await window.Project.__test.makeFigures();
await window.Project.__test.makeCompareFigures();
const compare = bridgeCalls.find(call => call.method === 'proj_compare_preview');
const figure = bridgeCalls.find(call => call.method === 'proj_figures');
const multi = bridgeCalls.find(call => call.method === 'proj_compare_figures');
assert.deepStrictEqual(compare.args[0], ['project-alpha', 'project-beta']);
assert.strictEqual(figure.args[0], 'project-alpha');
assert.deepStrictEqual(multi.args[0], ['project-alpha', 'project-beta']);
assert.ok(!JSON.stringify([compare, figure, multi, event]).includes(absolute));
""",
        str(ASSETS / "project.js"),
    )


def test_ai_and_figure_selectors_submit_only_project_ids_at_runtime():
    _run_node(
        r"""
const aiCompare = element('ai-cmp-project', { tagName: 'SELECT', value: '' });
const aiManuscript = element('ai-ms-project', { tagName: 'SELECT', value: '' });
element('ai-ms-format', { tagName: 'SELECT', value: 'markdown' });
element('ai-compare-out');
element('ai-ms-out');
const figureProject = element('fig-project', { tagName: 'SELECT', value: '' });
element('fig-drawer-body');
element('fig-drawer-gen');
element('fig-drawer', { hidden: false });
const absolute = 'C:\\private\\alpha\\project.yaml';
const bridgeCalls = [];
VCS.call = async (method, ...args) => {
  bridgeCalls.push({ method, args });
  if (method === 'proj_list') return { projects: [
    { project_id: 'project-alpha', name: 'Alpha', counts: { members: 2 }, path: absolute },
  ] };
  if (method === 'ai_compare') return { ok: true, n: 0, pairs: [], worst: [], summary: '' };
  if (method === 'ai_manuscript') return { ok: true, stats: {}, sections: [], path: '/artifact.md' };
  if (method === 'render_figure_preset') return { ok: true, files: [], skipped: [] };
  return {};
};
loadAsset(process.argv[1]);
loadAsset(process.argv[2]);
await window.AIAssistant.__test.loadProjects();
await window.Figures.__test.loadProjects();
assert.ok(aiCompare.innerHTML.includes('value="project-alpha"'));
assert.ok(!aiCompare.innerHTML.includes(absolute));
assert.strictEqual(figureProject.children[0].value, 'project-alpha');
assert.ok(window.Figures.__test.State.projects.every(project => !Object.hasOwn(project, 'path')));

aiCompare.value = 'project-alpha';
aiManuscript.value = 'project-alpha';
window.AIAssistant.__test.State.tables = [];
// A non-empty, server-owned table payload is required before comparison.
window.AIAssistant.__test.State.tables = [{ table_id: 'literature-1' }];
await window.AIAssistant.__test.compareLit();
await window.AIAssistant.__test.genManuscript();
figureProject.value = 'project-alpha';
window.Figures.__test.State.current = { key: 'adsorption_bar', name: 'Bar' };
await window.Figures.__test.generate();
const compared = bridgeCalls.find(call => call.method === 'ai_compare');
const manuscript = bridgeCalls.find(call => call.method === 'ai_manuscript');
const figure = bridgeCalls.find(call => call.method === 'render_figure_preset');
assert.strictEqual(compared.args[0], 'project-alpha');
assert.strictEqual(manuscript.args[0], 'project-alpha');
assert.strictEqual(figure.args[1], 'project-alpha');
assert.ok(!JSON.stringify([compared, manuscript, figure]).includes(absolute));
""",
        str(ASSETS / "ai.js"),
        str(ASSETS / "figures.js"),
    )


def test_settings_reverse_async_responses_keep_latest_intent_authoritative():
    _run_node(
        r"""
const scenario = element('set-scenario', { tagName: 'SELECT', value: 'A' });
const pending = { A: deferred(), B: deferred() };
const authority = (key, revision, intent = null) => ({
  revision, intent_id: intent,
  scenario: { key, defaults: {} }, engine: 'vasp', calculation: 'relax',
  capability: {}, allowed: ['relax'],
  configured: { scenario: true, engine: true, calculation: true },
});
VCS.call = async (method, ...args) => {
  trace.calls.push({ method, args });
  if (method === 'settings_context_get') {
    return { ok: true, revision: 0, context: authority('full', 0) };
  }
  if (method === 'settings_context_update') return pending[args[0].scenario].promise;
  return {};
};
loadAsset(process.argv[1]);
const seam = window.__VCS_SETTINGS_TEST__;
assert.ok(seam, 'guarded settings seam was not exposed');

scenario.value = 'A';
const requestA = seam.onScenarioChange();
await waitFor(() => trace.calls.some(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'A'));
scenario.value = 'B';
const requestB = seam.onScenarioChange();
await waitFor(() => trace.calls.some(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B'));

pending.B.resolve({ ok: true, revision: 1, context: authority('B', 1, 'B:1') });
assert.strictEqual(await requestB, true);
pending.A.resolve({
  ok: false, conflict: true, revision: 1, context: authority('B', 1, 'B:1'),
});
assert.strictEqual(await requestA, false, 'superseded A response must be rejected');

const snapshot = seam.snapshot();
assert.strictEqual(snapshot.state.scenarioKey, 'B');
assert.strictEqual(snapshot.state.contextRevision, 1);
assert.deepStrictEqual(trace.appliedScenarios, ['B']);
assert.strictEqual(snapshot.state.workspaceBusy, false);
""",
        str(ASSETS / "settings.js"),
    )


def test_settings_conflict_adopts_authority_until_user_creates_new_intent():
    _run_node(
        r"""
const scenario = element('set-scenario', { tagName: 'SELECT', value: 'A' });
const first = { A: deferred(), B: deferred() };
const authority = (key, revision, intent = null) => ({
  revision, intent_id: intent,
  scenario: { key, defaults: {} }, engine: 'vasp', calculation: 'relax',
  capability: {}, allowed: ['relax'],
  configured: { scenario: true, engine: true, calculation: true },
});
let bAttempts = 0;
VCS.call = async (method, ...args) => {
  trace.calls.push({ method, args });
  if (method === 'settings_context_get') {
    return { ok: true, revision: 0, context: authority('full', 0) };
  }
  if (method === 'settings_context_update') {
    const key = args[0].scenario;
    if (key === 'A') return first.A.promise;
    bAttempts += 1;
    if (bAttempts === 1) return first.B.promise;
    return { ok: true, revision: 2, context: authority('B', 2, args[2]) };
  }
  return {};
};
loadAsset(process.argv[1]);
const seam = window.__VCS_SETTINGS_TEST__;

scenario.value = 'A';
const requestA = seam.onScenarioChange();
await waitFor(() => trace.calls.some(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'A'));
scenario.value = 'B';
const requestB = seam.onScenarioChange();
await waitFor(() => trace.calls.some(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B'));

first.A.resolve({ ok: true, revision: 1, context: authority('A', 1, 'A:1') });
assert.strictEqual(await requestA, false);
const firstBCall = trace.calls.find(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B');
first.B.resolve({
  ok: false, conflict: true, revision: 1, context: authority('A', 1, 'A:1'),
});
assert.strictEqual(await requestB, false);

let bCalls = trace.calls.filter(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B');
assert.strictEqual(bCalls.length, 1, 'a conflicted patch must not be replayed automatically');
assert.strictEqual(firstBCall.args[1], 0);
let snapshot = seam.snapshot();
assert.strictEqual(snapshot.state.scenarioKey, 'A');
assert.strictEqual(snapshot.state.contextRevision, 1);
assert.deepStrictEqual(trace.appliedScenarios, ['A']);

// A second user change is a new intent and may commit against revision 1.
scenario.value = 'B';
assert.strictEqual(await seam.onScenarioChange(), true);
bCalls = trace.calls.filter(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B');
assert.deepStrictEqual(bCalls.map(item => item.args[1]), [0, 1]);
assert.notStrictEqual(bCalls[0].args[2], bCalls[1].args[2]);
snapshot = seam.snapshot();
assert.strictEqual(snapshot.state.scenarioKey, 'B');
assert.strictEqual(snapshot.state.contextRevision, 2);
assert.deepStrictEqual(trace.appliedScenarios, ['A', 'B']);
""",
        str(ASSETS / "settings.js"),
    )


def test_app_context_conflict_adopts_authority_until_new_user_intent():
    _run_node(
        r"""
loadAsset(process.argv[1]);
const pendingA = deferred();
const pendingB = deferred();
const context = (key, revision, intent) => ({
  revision, intent_id: intent,
  scenario: {
    key, name: key, pages: ['report-workbench'], cards: {}, defaults: {},
    figure_preset_order: [], reaction_presets: [], engines: ['vasp'], task_keys: ['relax'],
  },
  engine: key === 'A' ? 'cp2k' : 'vasp',
  calculation: key === 'A' ? 'static' : 'relax',
  capability: {}, allowed: ['relax', 'static'],
  configured: { scenario: true, engine: true, calculation: true },
});
const observed = [];
['vcs:scenario', 'vcs:engine', 'vcs:calculation'].forEach(name => {
  document.addEventListener(name, () => observed.push([
    VCS.scenario && VCS.scenario.key, VCS.activeEngine, VCS.activeCalculation,
  ]));
});
let bAttempt = 0;
VCS.call = async (method, ...args) => {
  trace.calls.push({ method, args });
  if (method === 'settings_context_get') {
    return { ok: true, revision: 0, context: context('full', 0, null) };
  }
  if (method === 'settings_context_update') {
    if (args[0].scenario === 'A') return pendingA.promise;
    bAttempt += 1;
    if (bAttempt === 1) return pendingB.promise;
    return { ok: true, revision: 2, context: context('B', 2, args[2]) };
  }
  return {};
};

const requestA = VCS.updateWorkspaceContext({ scenario: 'A' });
await waitFor(() => trace.calls.some(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'A'));
const requestB = VCS.updateWorkspaceContext({ scenario: 'B' });
await waitFor(() => trace.calls.some(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B'));
pendingA.resolve({ ok: true, revision: 1, context: context('A', 1, 'A:1') });
assert.strictEqual((await requestA).superseded, true);
pendingB.resolve({
  ok: false, conflict: true, revision: 1, context: context('A', 1, 'A:1'),
});
const conflict = await requestB;
assert.strictEqual(conflict.ok, false);
assert.strictEqual(conflict.conflict, true);

let bCalls = trace.calls.filter(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B');
assert.strictEqual(bCalls.length, 1, 'a conflicted patch must not be replayed automatically');
assert.strictEqual(VCS.workspaceContext.revision, 1);
assert.strictEqual(VCS.workspaceContext.context.scenario.key, 'A');
assert.strictEqual(VCS.scenario.key, 'A');

const committed = await VCS.updateWorkspaceContext({ scenario: 'B' });
assert.strictEqual(committed.ok, true);
bCalls = trace.calls.filter(item =>
  item.method === 'settings_context_update' && item.args[0].scenario === 'B');
assert.deepStrictEqual(bCalls.map(item => item.args[1]), [0, 1]);
assert.notStrictEqual(bCalls[0].args[2], bCalls[1].args[2]);
assert.strictEqual(VCS.workspaceContext.revision, 2);
assert.strictEqual(VCS.workspaceContext.context.scenario.key, 'B');
assert.strictEqual(VCS.scenario.key, 'B');
const eventCount = observed.length;
await VCS.loadWorkspaceContext();
assert.strictEqual(observed.length, eventCount, 'stale/equal loads must not redispatch context events');
assert.ok(observed.length >= 9);
assert.ok(observed.every(item =>
  (item[0] === 'full' && item[1] === 'vasp' && item[2] === 'relax') ||
  (item[0] === 'A' && item[1] === 'cp2k' && item[2] === 'static') ||
  (item[0] === 'B' && item[1] === 'vasp' && item[2] === 'relax')),
  JSON.stringify(observed));
""",
        str(ASSETS / "app.js"),
    )


def test_confirmed_dirty_guard_calls_component_restore_and_retains_failed_scope():
    _run_node(
        r"""
const prompt = element('set-prompt', { tagName: 'TEXTAREA', value: 'committed draft' });
loadAsset(process.argv[1]);
loadAsset(process.argv[2]);
const workspace = window.__VCS_WORKSPACE_TEST__;
const settings = window.__VCS_SETTINGS_TEST__;
assert.ok(workspace && settings);

settings.captureDraftScope('settings-prompt');
prompt.value = 'unsaved edit';
settings.updateDraftScope('settings-prompt');
assert.strictEqual(VCS.unsaved.isDirty(), true);
VCS.confirm = async () => true;
assert.strictEqual(await workspace.guardUnsaved('route change'), true);
assert.strictEqual(prompt.value, 'committed draft', 'component discard must restore its baseline');
assert.strictEqual(VCS.unsaved.isDirty(), false);
assert.deepStrictEqual(settings.snapshot().dirty_scopes, []);

let failedDiscardCalls = 0;
VCS.unsaved.mark('broken-component', 'Broken component', {
  discard: async () => { failedDiscardCalls += 1; return false; },
});
assert.strictEqual(await workspace.guardUnsaved('route change'), false);
assert.strictEqual(failedDiscardCalls, 1);
assert.strictEqual(VCS.unsaved.isDirty(), true, 'failed discard must retain dirty state');
assert.deepStrictEqual(VCS.unsaved.scopes(), ['broken-component']);
""",
        str(ASSETS / "workspace.js"),
        str(ASSETS / "settings.js"),
    )


def test_explicit_route_focus_waits_for_next_frame_and_heading_never_steals_it():
    _run_node(
        r"""
loadAsset(process.argv[1]);
const seam = window.__VCS_WORKSPACE_TEST__;
seam.configure({
  projectRows: [{ id: 'project-a', name: 'A', path: 'A/project.yaml' }],
  projectId: 'project-a',
});
const route = seam.parseRoute('#/publish/versions?project=project-a');
assert.ok(route && route.def.focus === '#rw-history-heading');
const applying = seam.applyRoute(route, { source: 'runtime-test', persist: false });
await waitFor(() => trace.raf.length === 1, 'route never reached animation-frame boundary');
assert.deepStrictEqual(trace.focus, [], 'no focus is allowed before the next frame');

trace.raf.shift()();
const result = await applying;
assert.strictEqual(result.ok, true);
assert.deepStrictEqual(trace.focus, ['explicit:#rw-history-heading']);
assert.ok(!trace.focus.includes('page-heading'), 'heading focus must not steal explicit focus');
""",
        str(ASSETS / "workspace.js"),
    )


def test_runtime_test_seams_are_absent_without_explicit_guard():
    _run_node(
        r"""
window.__VCS_TEST__ = false;
loadAsset(process.argv[1]);
loadAsset(process.argv[2]);
assert.strictEqual(window.__VCS_WORKSPACE_TEST__, undefined);
assert.strictEqual(window.__VCS_SETTINGS_TEST__, undefined);
""",
        str(ASSETS / "workspace.js"),
        str(ASSETS / "settings.js"),
    )
