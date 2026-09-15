"""Static UI contracts for the Phase 3 project-center lifecycle flows."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "vcstudio" / "gui_web" / "assets"
NODE = shutil.which("node")


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def test_no_project_center_is_neutral_by_default_and_has_four_explicit_entries():
    html = _source("index.html")
    css = _source("app.css")
    assert 'class="page project-neutral" data-page="project" id="page-project"' in html
    assert 'id="project-empty-hub"' in html
    assert "未选择项目" in html and "No project selected" in json.loads(
        (ROOT / "vcstudio" / "shared" / "locales" / "en.json").read_text(
            encoding="utf-8"
        )
    ).values()
    for control in (
        "pj-hub-import", "pj-hub-structure", "pj-hub-recent", "pj-hub-adopt",
    ):
        assert f'id="{control}"' in html
    assert (
        "#page-project.project-neutral > :not(.pagebar):not(#project-empty-hub)"
        in css
    )


def test_existing_folder_adoption_defaults_to_remint_and_requires_preflight():
    html = _source("index.html")
    js = _source("project.js")
    remint = html.index('<option value="remint" selected')
    preserve = html.index('<option value="preserve"')
    assert remint < preserve
    assert 'id="pj-adopt-apply" disabled' in html
    assert "VCS.call('proj_lifecycle_select', 'adopt_source')" in js
    assert re.search(
        r"VCS\.call\('proj_lifecycle_preflight', 'adopt', null,\s*"
        r"State\.adoptSelectionToken, null, mode\)", js,
    )
    assert js.index("VCS.call('proj_lifecycle_preflight', 'adopt'") < js.index(
        "VCS.call('proj_lifecycle_apply', token)"
    )
    assert "State.adoptOperationToken = result && result.ready" in js


def test_clone_and_move_use_canonical_id_opaque_selection_and_confirmed_plan_only():
    html = _source("index.html")
    js = _source("project.js")
    for control in (
        "pj-lifecycle-target-name", "pj-clone-preflight", "pj-move-preflight",
        "pj-lifecycle-apply", "pj-lifecycle-status",
    ):
        assert f'id="{control}"' in html
    assert "VCS.call('proj_lifecycle_select', `${action}_destination`)" in js
    assert re.search(
        r"VCS\.call\('proj_lifecycle_preflight', action,\s*"
        r"identity, selectionToken, targetName, null\)", js,
    )
    assert "State.lifecycleOperationToken = result && result.ready" in js
    assert "if (!State.lifecycleOperationToken || State.lifecycleBusy) return;" in js
    assert "result.project.project_id" in js and "await selectById(" in js


@pytest.mark.skipif(NODE is None, reason="Node.js is required for executable asset tests")
def test_lifecycle_picker_and_preflight_are_last_wins_across_project_switches(tmp_path):
    harness = tmp_path / "project-lifecycle-race.js"
    harness.write_text(
        r"""
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function fakeElement(id, value = '') {
  return {
    id, value, hidden: false, disabled: false, textContent: '', innerHTML: '',
    dataset: {}, listeners: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
}
const elements = Object.create(null);
for (const id of [
  'page-project', 'project-empty-hub', 'pj-lifecycle-card', 'pj-hub-recent',
  'pj-lifecycle-identity', 'pj-lifecycle-target-name', 'pj-lifecycle-apply',
  'pj-lifecycle-status', 'pj-select',
]) elements[id] = fakeElement(id);
const document = {
  readyState: 'loading',
  getElementById(id) { return elements[id] || null; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, dispatchEvent() {},
};
const published = [];
const calls = [];
let selectResponses = [];
let preflightResponses = new Map();
const VCS = {
  i18n: { lang: 'en' },
  t: (_key, params, fallback) => String(fallback || '').replace(/{([A-Za-z0-9_]+)}/g,
    (match, name) => Object.prototype.hasOwnProperty.call(params || {}, name)
      ? String(params[name]) : match),
  call: async (method, ...args) => {
    calls.push([method, ...args]);
    if (method === 'proj_lifecycle_select') return selectResponses.shift();
    if (method === 'proj_lifecycle_preflight') {
      const identity = args[1];
      const response = preflightResponses.get(identity);
      return typeof response === 'function' ? response() : response;
    }
    throw new Error(`unexpected bridge call ${method}`);
  },
  operations: { publish(event) { published.push(event); } },
  esc: value => String(value == null ? '' : value),
};
const window = { __VCS_TEST__: true };
Object.assign(globalThis, { document, window, VCS });
vm.runInThisContext(fs.readFileSync(process.env.PROJECT_SOURCE, 'utf8'), {
  filename: process.env.PROJECT_SOURCE,
});
const hooks = window.Project.__test;
const state = hooks.State;
const projectA = { project_id: 'project-a', name: 'A' };
const projectB = { project_id: 'project-b', name: 'B' };
state.projects = [projectA, projectB];

function switchTo(project) {
  state.currentProjectId = project.project_id;
  elements['pj-select'].value = project.project_id;
  hooks.updateProjectHub();
}
async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}

(async () => {
  // A response that returns after B's completed preflight must not overwrite B.
  const latePreflightA = deferred();
  selectResponses = [
    Promise.resolve({ ok: true, selection_token: 'selection-a' }),
    Promise.resolve({ ok: true, selection_token: 'selection-b' }),
  ];
  preflightResponses = new Map([
    ['project-a', () => latePreflightA.promise],
    ['project-b', { ok: true, ready: true, operation_token: 'operation-b', jobs: { entries: [] } }],
  ]);
  switchTo(projectA);
  const pendingA = hooks.preflightProjectLifecycle('clone');
  await settle();
  assert.ok(calls.some(call => call[0] === 'proj_lifecycle_preflight' && call[2] === 'project-a'));
  switchTo(projectB);
  const completedB = hooks.preflightProjectLifecycle('move');
  await completedB;
  assert.equal(state.lifecycleSelectionToken, 'selection-b');
  assert.equal(state.lifecycleOperationToken, 'operation-b');
  assert.equal(state.lifecycleAction, 'move');
  assert.equal(elements['pj-lifecycle-apply'].disabled, false);
  latePreflightA.resolve({
    ok: true, ready: true, operation_token: 'operation-a', jobs: { entries: [] },
  });
  await pendingA;
  assert.equal(state.lifecycleSelectionToken, 'selection-b');
  assert.equal(state.lifecycleOperationToken, 'operation-b');
  assert.equal(elements['pj-lifecycle-apply'].disabled, false);

  // The same last-wins rule applies while the first server picker is still open.
  const latePickerA = deferred();
  selectResponses = [
    latePickerA.promise,
    Promise.resolve({ ok: true, selection_token: 'selection-b-2' }),
  ];
  preflightResponses = new Map([
    ['project-a', { ok: true, ready: true, operation_token: 'operation-a-2', jobs: { entries: [] } }],
    ['project-b', { ok: true, ready: true, operation_token: 'operation-b-2', jobs: { entries: [] } }],
  ]);
  switchTo(projectA);
  const pickerA = hooks.preflightProjectLifecycle('clone');
  switchTo(projectB);
  const pickerB = hooks.preflightProjectLifecycle('clone');
  await pickerB;
  const aPreflightsBefore = calls.filter(
    call => call[0] === 'proj_lifecycle_preflight' && call[2] === 'project-a'
  ).length;
  latePickerA.resolve({ ok: true, selection_token: 'late-selection-a' });
  await pickerA;
  const aPreflightsAfter = calls.filter(
    call => call[0] === 'proj_lifecycle_preflight' && call[2] === 'project-a'
  ).length;
  assert.equal(aPreflightsAfter, aPreflightsBefore, 'stale picker must not start preflight');
  assert.equal(state.lifecycleSelectionToken, 'selection-b-2');
  assert.equal(state.lifecycleOperationToken, 'operation-b-2');
  assert.equal(elements['pj-lifecycle-apply'].disabled, false);
  process.stdout.write('ok\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(harness)],
        cwd=ASSETS,
        env=dict(os.environ, PROJECT_SOURCE=str(ASSETS / "project.js")),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"


def test_lifecycle_flows_publish_to_the_shared_operation_queue():
    js = _source("project.js")
    publisher = re.search(
        r"function publishLifecycleOperation\(operation, status, error = ''\) \{"
        r"(.*?)\n  \}", js, re.S,
    ).group(1)
    assert "VCS.operations.publish" in publisher
    assert "route: 'project-overview'" in publisher
    assert "kind: `project-${operation.kind}`" in publisher
    for status in ("confirming", "running", "succeeded", "failed"):
        assert f"'{status}'" in js
    assert "State.adoptQueueOperation" in js
    assert "State.lifecycleQueueOperation" in js


def test_preflight_renders_path_free_jobs_ledger_entries():
    js = _source("project.js")
    assert "result.jobs && Array.isArray(result.jobs.entries)" in js
    assert "item.target_identity || item.source_identity" in js
    assert "runtime.project.lifecycle.ready_jobs" in js


def test_project_modes_are_not_auto_opened_by_the_last_registry_row():
    js = _source("project.js")
    reload_body = re.search(
        r"async function performProjectReload\(generation, preferredReference\) \{"
        r"(.*?)\n  \}", js, re.S,
    ).group(1)
    assert "(No project selected)" in reload_body
    candidates = re.search(r"const candidates = \[(.*?)\];", reload_body, re.S).group(1)
    assert "requestedHit" in candidates and "workspaceId" in candidates and "active" in candidates
    assert "saved" not in candidates
    assert "State.projects[State.projects.length - 1]" not in candidates
    assert "setExplicitWorkflow('import')" in js
    assert "setExplicitWorkflow('structure')" in js


def test_lifecycle_static_locale_keys_exist_in_both_bundles():
    en = json.loads(
        (ROOT / "vcstudio" / "shared" / "locales" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    zh = json.loads(
        (ROOT / "vcstudio" / "shared" / "locales" / "zh.json").read_text(
            encoding="utf-8"
        )
    )
    for key in (
        "project.hub.title",
        "project.hub.import",
        "project.hub.structure",
        "project.hub.recent",
        "project.hub.adopt",
        "project.adopt.remint",
        "project.adopt.preserve",
        "project.lifecycle.clone",
        "project.lifecycle.move",
        "runtime.project.lifecycle.ready_summary",
        "runtime.project.lifecycle.ready_jobs",
        "runtime.project.operation.cancelled",
        "runtime.project.performprojectreload.no_selection",
    ):
        assert key in en and key in zh
        assert en[key] and zh[key]
