"""Executable contracts for the Dashboard laboratory portfolio summary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "vcstudio" / "gui_web" / "assets"
LOCALES = ROOT / "vcstudio" / "shared" / "locales"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for executable asset tests")
def test_dashboard_portfolio_facts_states_and_routes_are_executable(tmp_path):
    harness = tmp_path / "dashboard-portfolio.js"
    harness.write_text(
        r"""
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fakeElement(id = '') {
  return {
    id, hidden: false, innerHTML: '', textContent: '', dataset: {}, listeners: {},
    addEventListener(type, handler) { this.listeners[type] = handler; },
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
}
const elements = Object.create(null);
elements['db-pipeline'] = fakeElement('db-pipeline');
const document = {
  readyState: 'loading',
  getElementById(id) { return elements[id] || null; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, dispatchEvent() {},
};
const routeCalls = [];
let releaseCalls = null;
const bridgeResponses = {
  list_jobs: { jobs: [], stale: [], error: null },
  proj_list: { projects: [], error: null },
  list_profiles: { profiles: [], error: null },
  campaign_list: { available: false, campaigns: [] },
  pipeline_runtime_status: { state: {}, error: null },
};
const VCS = {
  workspace: {
    navigateRoute(route, options) {
      routeCalls.push([route, options]);
      return Promise.resolve({ ok: true });
    },
  },
  call: async method => {
    if (releaseCalls) await releaseCalls;
    if (method === 'pipeline_status') return globalThis.pipelineResponse;
    return bridgeResponses[method] || {};
  },
  navigate: async (page, options) => { routeCalls.push([page, options]); return { ok: true }; },
  t: (_key, params, fallback) => String(fallback || '').replace(/{([A-Za-z0-9_]+)}/g,
    (match, name) => Object.prototype.hasOwnProperty.call(params || {}, name)
      ? String(params[name]) : match),
  esc: value => String(value == null ? '' : value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  elementBadge: () => '',
};
const window = { __VCS_TEST__: true };
Object.assign(globalThis, { document, window, VCS });
vm.runInThisContext(fs.readFileSync(process.env.DASHBOARD_SOURCE, 'utf8'), {
  filename: process.env.DASHBOARD_SOURCE,
});
const hooks = window.Dashboard.__test;
assert.ok(hooks, 'Dashboard test seam must be exposed only in test mode');

globalThis.portfolioRows = [
  {
    stage: 'monitor', needs_human: true, publication_gate_status: 'blocked',
    scientific_status: 'diagnostic', scientific_qualification: 'diagnostic',
    report_reason: 'text says eligible final and human reviewed, but is not authority',
  },
  {
    stage: 'analysis', needs_human: false, publication_gate_status: 'eligible',
    scientific_status: 'final', scientific_qualification: 'human_scientific_reviewed',
    artifact_status: 'ready', artifact_current: true, scientific_stale: false,
  },
  {
    stage: 'report_done', needs_human: false, publication_gate_status: 'blocked',
    scientific_status: null, scientific_qualification: 'human_scientific_reviewed',
  },
  {
    stage: 'unknown', needs_human: false, publication_gate_status: 'unknown',
    report_status: 'final', report_reason: 'final',
  },
  {
    stage: 'report_done', needs_human: false, publication_gate_status: 'eligible',
    scientific_status: 'final', scientific_qualification: 'human_scientific_reviewed',
    artifact_status: 'stale', artifact_current: false, scientific_stale: true,
  },
  {
    stage: 'report_done', needs_human: false, publication_gate_status: 'blocked',
    scientific_status: 'final', scientific_qualification: 'human_scientific_reviewed',
    artifact_status: 'ready', artifact_current: true, scientific_stale: false,
  },
  {
    stage: 'report_done', needs_human: false, publication_gate_status: 'eligible',
    scientific_status: 'final', scientific_qualification: 'human_scientific_reviewed',
    artifact_status: 'ready', artifact_current: true, scientific_stale: true,
  },
  {
    stage: 'report_done', needs_human: false, publication_gate_status: 'eligible',
    scientific_status: 'final', scientific_qualification: 'human_scientific_reviewed',
    artifact_status: 'ready', artifact_current: false, scientific_stale: false,
  },
];
globalThis.pipelineResponse = {
  ok: true, status: 'ready', registered_total: 8, successful_count: 8,
  failed_count: 0, failures: [], projects: globalThis.portfolioRows, error: null,
};

(async () => {
  const sourceBefore = JSON.stringify(globalThis.portfolioRows);
  const summary = hooks.summarizeProjectPortfolio(globalThis.portfolioRows);
  assert.deepEqual(summary, {
    projects: 8,
    active_pipeline: 2,
    needs_human: 1,
    publication_gate_blocked: 3,
    eligible_final: 4,
    human_scientific_reviewed_final: 1,
  });
  assert.equal(JSON.stringify(globalThis.portfolioRows), sourceBefore, 'summarizer must be pure');
  assert.ok(Object.values(summary).slice(1).reduce((a, b) => a + b, 0) > summary.projects,
    'overlapping categories must not be normalized into a completion rate');

  hooks.renderPipeline(globalThis.portfolioRows, null, 'ready', globalThis.pipelineResponse);
  let html = elements['db-pipeline'].innerHTML;
  assert.match(html, /data-state="ready"/);
  assert.equal((html.match(/data-portfolio-count=/g) || []).length, 5);
  assert.equal((html.match(/<small> \/ 8<\/small>/g) || []).length, 5,
    'every metric must show the same registered-project denominator');
  assert.match(html,
    /data-portfolio-count="human_scientific_reviewed_final">1<small>/,
    'stale, blocked, or non-current raw final rows must not be counted');
  assert.match(html, /类别可重叠，不是完成率/);

  const degraded = {
    ok: true, status: 'degraded', registered_total: 4, successful_count: 2,
    failed_count: 2,
    failures: [
      { project_ref: 'registered-project-3', code: 'project_status_unavailable' },
      { project_ref: 'registered-project-4', code: 'project_status_unavailable' },
    ],
    projects: globalThis.portfolioRows.slice(0, 2),
    error: '2 of 4 registered project statuses could not be read; counts cover 2 readable projects only.',
  };
  hooks.renderPipeline(degraded.projects, degraded.error, degraded.status, degraded);
  html = elements['db-pipeline'].innerHTML;
  assert.match(html, /data-state="degraded"/);
  assert.match(html, /2 of 4 registered project statuses could not be read/);
  assert.equal((html.match(/<small> \/ 4<\/small>/g) || []).length, 5,
    'degraded cards must retain the registered denominator');
  assert.equal((html.match(/data-portfolio-count=/g) || []).length, 5,
    'readable facts remain available in an explicitly degraded view');

  hooks.renderPipeline([], null, 'loading');
  assert.match(elements['db-pipeline'].innerHTML, /data-state="loading"/);
  hooks.renderPipeline([], null);
  assert.match(elements['db-pipeline'].innerHTML, /data-state="empty"/);
  assert.doesNotMatch(elements['db-pipeline'].innerHTML, /data-portfolio-count=/);
  hooks.renderPipeline([], 'pipeline backend unavailable', 'unavailable', {
    status: 'unavailable', registered_total: 2, successful_count: 0,
    failed_count: 2, failures: [],
  });
  html = elements['db-pipeline'].innerHTML;
  assert.match(html, /data-state="unavailable"/);
  assert.match(html, /pipeline backend unavailable/);
  assert.doesNotMatch(html, /data-portfolio-count=|>0<|0 \/ /,
    'an error must not be rendered as zero projects or zero counts');

  await hooks.navigatePortfolio('project-workflow');
  await hooks.navigatePortfolio('run-jobs', 'need');
  await hooks.navigatePortfolio('publish-report');
  assert.deepEqual(routeCalls.map(call => call[0]),
    ['project-workflow', 'run-jobs', 'publish-report']);
  assert.deepEqual(routeCalls[1][1].query, { status: 'need' });
  assert.ok(routeCalls.every(call => call[1].source === 'dashboard-portfolio'));

  let release;
  releaseCalls = new Promise(resolve => { release = resolve; });
  globalThis.pipelineResponse = degraded;
  const pending = window.Dashboard.refresh();
  assert.match(elements['db-pipeline'].innerHTML, /data-state="loading"/,
    'refresh must publish loading before bridge calls settle');
  release();
  await pending;
  assert.match(elements['db-pipeline'].innerHTML, /data-state="degraded"/);
  assert.equal((elements['db-pipeline'].innerHTML.match(/<small> \/ 4<\/small>/g) || []).length, 5,
    'refresh must forward the server denominator and degraded state');
  process.stdout.write('ok\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
        encoding="utf-8",
    )
    env = dict(os.environ, DASHBOARD_SOURCE=str(ASSETS / "dashboard.js"))
    result = subprocess.run(
        [NODE, str(harness)],
        cwd=ASSETS,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"


def test_dashboard_portfolio_locale_keys_are_complete_and_parallel():
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((LOCALES / "zh.json").read_text(encoding="utf-8"))
    assert set(en) == set(zh)
    expected = {
        "dashboard.portfolio.title",
        "dashboard.portfolio.loading",
        "dashboard.portfolio.empty",
        "dashboard.portfolio.unavailable",
        "dashboard.portfolio.denominator",
        "dashboard.portfolio.overlap",
        "dashboard.portfolio.active",
        "dashboard.portfolio.needs_human",
        "dashboard.portfolio.blocked",
        "dashboard.portfolio.eligible",
        "dashboard.portfolio.human_final",
        "dashboard.portfolio.open_pipeline",
        "dashboard.portfolio.open_jobs",
        "dashboard.portfolio.open_publish",
    }
    assert expected <= set(en)
    assert all(en[key] and zh[key] and en[key] != zh[key] for key in expected)
