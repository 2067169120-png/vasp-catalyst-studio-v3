from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ASSETS = Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' / 'assets'


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding='utf-8')


def _run_node(source: str, *args: str) -> subprocess.CompletedProcess:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is not installed')
    result = subprocess.run(
        [node, '-e', source, *args], text=True, encoding='utf-8',
        capture_output=True, check=False,
    )
    if result.returncode:
        pytest.fail(f'Node fake-DOM failed:\n{result.stderr}\n{result.stdout}')
    return result


def test_player_assets_are_loaded_offline_before_jobs_wires_row_action():
    html = _source('index.html')
    assert '<link rel="stylesheet" href="trajectory-player.css">' in html
    assert html.index('<script src="structure.js"></script>') < html.index(
        '<script src="trajectory-player.js"></script>') < html.index(
        '<script src="jobs.js"></script>')
    assert 'http://' not in _source('trajectory-player.js')
    assert 'https://' not in _source('trajectory-player.js')


def test_player_uses_only_opaque_tokens_and_server_projected_scientific_fields():
    player = _source('trajectory-player.js')
    jobs = _source('jobs.js')
    assert "VCS.call('trajectory_open', String(jobId || ''))" in player
    assert "VCS.call('trajectory_steps', overview.session_token" in player
    assert "VCS.call('trajectory_frame', token)" in player
    assert 'data-frame-token' in player
    assert "VCS.showTrajectory(jobId" in jobs
    row_handler = jobs[jobs.index("if (e.target.closest('.trajectory'))"):
                       jobs.index("if (e.target.closest('.meth'))")]
    assert 'tr.dataset.dir' not in row_handler
    assert 'tr.dataset.jobId' in row_handler
    for forbidden in ('parseFloat(point.energy', 'barrier_f', 'energy_drift_total_ev ='):
        assert forbidden not in player


def test_repair_confirmation_publishes_to_shared_operation_queue_and_reuses_key():
    jobs = _source('jobs.js')
    block = jobs[jobs.index('async function confirmTrajectoryRepair('):
                 jobs.index('// ── 集群队列 + 认领')]
    assert "withExclusiveOperation('trajectory-repair'" in block
    assert "updateOperation(op, 'running')" in block
    assert "VCS.call(\n        'trajectory_confirm_repair'" in block
    assert "name, pw, trust, op.id, jobId" in block
    assert 'INCAR 逐字冻结' in block
    assert 'unknown 保持暂停' in block


def test_player_has_bilingual_a11y_and_reflow_contracts():
    player = _source('trajectory-player.js')
    css = _source('trajectory-player.css')
    for marker in (
        'role="status" aria-live="polite"', 'role="img"',
        'role="region" tabindex="0"', 'aria-label=',
        "'Trajectory and diagnostics — {title}'",
        "'Transparent repair review (read-only by default)'",
    ):
        assert marker in player
    assert '@media (max-width:959px)' in css
    assert '@media (max-width:600px)' in css
    assert '@media (prefers-reduced-motion:reduce)' in css
    assert '@media (forced-colors:active)' in css
    assert '.tp-table-region' in css and 'overflow:auto' in css


def test_fake_dom_renders_step_buttons_and_read_only_repair_by_default():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.className = ''; this.innerHTML = ''; this.textContent = '';
    this.dataset = {}; this.listeners = {}; this.disabled = false;
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector(selector) {
    if (selector === '.tp-confirm-repair' && this.innerHTML.includes('tp-confirm-repair')) {
      if (!this.confirmButton) this.confirmButton = new FakeElement('button');
      return this.confirmButton;
    }
    return null;
  }
  querySelectorAll() { return []; }
}

global.window = { __VCS_TEST__: true };
global.document = { createElement: tag => new FakeElement(tag) };
global.VCS = {
  i18n: { lang: 'en' },
  t: (_key, params, fallback) => String(fallback).replace(/\{([A-Za-z0-9_]+)\}/g,
    (match, name) => Object.prototype.hasOwnProperty.call(params || {}, name)
      ? String(params[name]) : match),
  esc: value => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;'),
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), { filename: process.argv[1] });
const seam = window.__VCS_TRAJECTORY_TEST__;
assert.ok(seam);

const page = { rows: [{ step: 7, frame_token: 'frame-safe', energy_ev: -10.2,
  fmax_ev_a: 0.03, temperature_k: 310, minimum_distance_a: 1.8, anomaly: 'warn' }] };
const html = seam.rowsHtml(page);
assert.ok(html.includes('data-frame-token="frame-safe"'));
assert.ok(html.includes('Step 7'));
assert.ok(html.includes('Review'));

let confirmed = 0;
const box = new FakeElement();
seam.renderRepair(box, {
  ok: true, failure_class: 'UNKNOWN', detected_evidence: 'uncovered',
  suggested_change: 'pause', estimated_cost: { class: 'unknown' },
  scientific_impact: 'unknown', method_diff: [], execution_allowed: false,
}, () => { confirmed += 1; });
assert.ok(box.innerHTML.includes('Default decision: pause'));
assert.ok(box.innerHTML.includes('remains read-only'));
assert.strictEqual(box.querySelector('.tp-confirm-repair'), null);
assert.strictEqual(confirmed, 0);

const allowed = new FakeElement();
seam.renderRepair(allowed, {
  ok: true, failure_class: 'NONCONVERGED', detected_evidence: 'bounded',
  suggested_change: 'continue', estimated_cost: { class: 'one_bounded_restart' },
  scientific_impact: 'unchanged', method_diff: [], execution_allowed: true,
}, () => { confirmed += 1; });
assert.ok(allowed.confirmButton && allowed.confirmButton.listeners.click);
allowed.confirmButton.listeners.click();
assert.strictEqual(confirmed, 1);
"""
    _run_node(script, str(ASSETS / 'trajectory-player.js'))


def test_page_and_frame_share_one_intent_generation_gate():
    player = _source('trajectory-player.js')
    assert 'const intentGate = createIntentGate();' in player
    assert 'const intent = intentGate.begin();' in player
    assert 'if (closed || !intentGate.valid(intent)) return;' in player
    assert 'await showFrame(response.rows[0].frame_token, intent);' in player
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
global.window = { __VCS_TEST__: true };
global.document = { createElement: () => ({}) };
global.VCS = { i18n: { lang: 'en' }, t: (_k, _p, fallback) => fallback,
  esc: value => String(value == null ? '' : value) };
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const gate = window.__VCS_TRAJECTORY_TEST__.createIntentGate();
const oldPage = gate.begin();
assert.strictEqual(gate.valid(oldPage), true);
const newerFrame = gate.begin();
assert.strictEqual(gate.valid(oldPage), false);
assert.strictEqual(gate.valid(newerFrame), true);
gate.invalidate();
assert.strictEqual(gate.valid(newerFrame), false);
"""
    _run_node(script, str(ASSETS / 'trajectory-player.js'))


def test_node_syntax_and_index_references_have_no_missing_local_asset():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is not installed')
    for name in ('trajectory-player.js', 'jobs.js'):
        subprocess.run([node, '--check', str(ASSETS / name)], check=True,
                       text=True, encoding='utf-8', capture_output=True)
    html = _source('index.html')
    refs = re.findall(r'(?:src|href)="([^"]*trajectory-player\.(?:js|css))"', html)
    assert refs == ['trajectory-player.css', 'trajectory-player.js']
    assert all((ASSETS / ref).is_file() for ref in refs)
