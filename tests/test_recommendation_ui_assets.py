from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"
LOCALES = ASSETS.parents[1] / "shared" / "locales"
NODE = shutil.which("node")


def _source(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def test_lab_policy_card_has_dirty_scope_latest_intent_and_no_job_action():
    html = _source("index.html")
    script = _source("settings.js")
    section = script.split("// ── 实验室策略", 1)[1].split(
        "// ── 载入 / 回填", 1)[0]

    for element_id in (
        "set-lab-policy-card", "set-lab-policy", "set-lab-applicability",
        "set-lab-preview", "set-lab-confirm",
        "set-lab-preview-out",
    ):
        assert f'id="{element_id}"' in html
    assert "'settings-lab-policy': [" in script
    assert "labPolicyIntentGeneration" in section
    assert "generation !== labPolicyIntentGeneration" in section
    assert "lab_policy_preview" in section and "lab_policy_confirm" in section
    assert "State.labPolicyRevision" in section
    assert ", State.labPolicyRevision, true" in section
    assert "request.actor" not in section
    for forbidden in ("submit_jobs", "jobs_submit", "submit_batch", "continue_jobs"):
        assert forbidden not in section


def test_resource_forecast_ui_uses_server_display_and_preserves_submit_gate():
    html = _source("index.html")
    script = _source("jobs.js")
    renderer = script.split("function renderResourceForecast()", 1)[1].split(
        "async function estimateSelectedResources", 1)[0]
    estimator = script.split("async function estimateSelectedResources()", 1)[1].split(
        "function renderSelectionTray", 1)[0]

    assert 'id="jb-tray-forecast"' in html
    assert 'id="jobs-resource-forecast"' in html
    assert "jobs_resource_forecast" in estimator
    assert "rows.map(stableJobId)" in estimator
    for forbidden in ("row.dir", "natoms:", "nkpts:", "cores:", "submit_jobs"):
        assert forbidden not in estimator
    for field in (
        "display.estimate_core_hours", "display.range_core_hours",
        "item.matching_successful_samples", "item.matching_observed_samples",
        "item.failure_rate", "display.remaining_budget_core_hours",
    ):
        assert field in renderer
    for forbidden in ("toFixed(", "Math.", "parseFloat(", "parseInt("):
        assert forbidden not in renderer
    # The original submit control and handler remain present and separate.
    assert 'id="jb-tray-submit"' in html
    assert "wire('jb-tray-submit', doSubmit)" in script


def test_recommendation_locale_keys_are_complete_and_equal():
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    assert set(en) == set(zh)
    keys = {key for key in en if key.startswith(("settings.lab.", "jobs.forecast."))}
    assert keys
    neutral = {"settings.lab.walltime_placeholder"}
    assert all(en[key] and zh[key] and (key in neutral or en[key] != zh[key]) for key in keys)


@pytest.mark.skipif(NODE is None, reason="Node.js is required for executable asset tests")
def test_resource_forecast_fake_dom_ready_unavailable_and_cannot_act(tmp_path):
    harness = tmp_path / "forecast-ui.js"
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
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    replaceChildren(...items) { this.children = items; this.innerHTML = ''; this.textContent = ''; },
    appendChild(child) { this.children.push(child); this.options.push(child); return child; },
  };
}
const elements = Object.create(null);
function install(id, values = {}) {
  elements[id] = Object.assign(fakeElement(id), values); return elements[id];
}
[
  'jobs-selection-tray', 'jobs-selection-list', 'jobs-selection-summary',
  'jobs-selection-review', 'jobs-resource-forecast', 'jobs-resource-forecast-out',
  'jb-tray-forecast',
].forEach(id => install(id));
install('jf-cluster', { value: '' }); install('jf-status', { value: '' });
install('jf-sort', { value: 'time' });
const document = {
  readyState: 'loading', body: fakeElement('body'),
  querySelector(selector) { return selector.startsWith('#') ? elements[selector.slice(1)] || null : null; },
  querySelectorAll() { return []; }, createElement: tag => fakeElement(tag),
  addEventListener() {}, dispatchEvent() {},
};
const calls = []; let response;
const VCS = {
  i18n: { lang: 'en' }, workspace: { state: {} },
  call: async (method, ...args) => { calls.push([method, ...args]); return response; },
  t: (_key, params, fallback) => String(fallback || '').replace(/\{([A-Za-z0-9_]+)\}/g,
    (match, name) => Object.prototype.hasOwnProperty.call(params || {}, name) ? String(params[name]) : match),
  esc: value => String(value == null ? '' : value), pill: value => `<span>${value}</span>`,
  elementBadge: () => '', log() {}, toast() {},
};
const window = { __VCS_TEST__: true };
Object.assign(globalThis, { document, window, VCS, localStorage: { getItem: () => null, setItem() {} },
  CustomEvent: class {}, Event: class {}, setInterval: () => 1, clearInterval() {} });
vm.runInThisContext(fs.readFileSync(process.env.JOBS_SOURCE, 'utf8'));
const hooks = window.Jobs.__test;
hooks.State.rows = [{ id: 'opaque-1', dir: '/private/not-sent', name: 'A', state: 'CREATED' }];
hooks.State.selected = new Set(['/private/not-sent']);
response = {
  ok: true, status: 'ready', history_basis_sha256: 'a'.repeat(64),
  denominators: { selected_jobs: 1, forecastable_jobs: 1, usable_history_entries: 3 },
  unknown_jobs: [], forecast: { risk_flags: [], authorizes_submission: false },
  display: {
    estimate_core_hours: '12.345', range_core_hours: '6.000–24.000',
    confidence: 'medium', remaining_budget_core_hours: '100.000', budget_status: 'within_range',
    jobs: [{ job_id: 'opaque-1', estimate_core_hours: '12.345',
      range_core_hours: '6.000–24.000', matching_successful_samples: '3',
      matching_observed_samples: '4', confidence: 'medium', failure_rate: '25.0%' }],
  },
};
(async () => {
  hooks.renderSelectionTray();
  assert.equal(elements['jb-tray-forecast'].disabled, false);
  assert.equal(await hooks.estimateSelectedResources(), true);
  assert.deepEqual(calls[0], ['jobs_resource_forecast', ['opaque-1']]);
  assert.match(elements['jobs-resource-forecast-out'].innerHTML, /12\.345/);
  assert.match(elements['jobs-resource-forecast-out'].innerHTML, /25\.0%/);
  assert.ok(!calls.some(call => /submit/i.test(call[0])), 'forecast must not submit');

  hooks.State.selected.clear(); hooks.renderSelectionTray();
  assert.equal(elements['jb-tray-forecast'].disabled, true);
  assert.equal(elements['jobs-resource-forecast'].hidden, true);

  hooks.State.selected = new Set(['/private/not-sent']);
  response = { ok: true, status: 'unavailable', display: null,
    error: null, unknown_jobs: [{ job_id: 'opaque-1', missing: ['cores'] }] };
  hooks.renderSelectionTray();
  assert.equal(await hooks.estimateSelectedResources(), false);
  assert.equal(hooks.State.resourceForecastStatus, 'unavailable');
  assert.equal(elements['jb-tray-forecast'].disabled, false,
    'unavailable forecast is retryable but cannot perform any job action');
  assert.ok(!calls.some(call => /submit/i.test(call[0])));
  process.stdout.write('ok\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(harness)], cwd=ASSETS,
        env={**os.environ, "JOBS_SOURCE": str(ASSETS / "jobs.js")},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"
