"""Executable regressions for the Catalysis/Kinetics authoring browser state machines."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_workspace_runtime_assets import ASSETS, _run_node


ROOT = Path(__file__).resolve().parents[1]
INDEX = ASSETS / "index.html"
SCRIPT = ASSETS / "analysis-workbench.js"


_DOM = r"""
const originalCreate = document.createElement;
document.createElement = tag => {
  const node = originalCreate(tag);
  delete node.id;
  let identifier = '';
  Object.defineProperty(node, 'id', {
    configurable: true,
    get() { return identifier; },
    set(value) {
      if (identifier) elements.delete(identifier);
      identifier = String(value || '');
      if (identifier) elements.set(identifier, node);
    },
  });
  return node;
};

function authoringElement(id, properties = {}) { return element(id, properties); }
[
  'aw-authoring-epoch', 'aw-network-authoring-status', 'aw-network-active',
  'aw-network-authority', 'aw-network-generation', 'aw-network-closure',
  'aw-model-authoring-status', 'aw-model-save-status', 'aw-model-solver-status',
].forEach(id => authoringElement(id));
[
  'aw-network-gaps', 'aw-model-issues', 'aw-model-rate-policy', 'aw-model-assumptions',
  'aw-model-reservoirs', 'aw-model-targets', 'aw-model-steps', 'aw-model-sites',
  'aw-model-saddles',
].forEach(id => authoringElement(id, { tagName: id.includes('gaps') || id.includes('issues') ? 'UL' : 'DIV' }));
authoringElement('aw-network-authoring', { tagName: 'ARTICLE' });
authoringElement('aw-model-authoring', { tagName: 'ARTICLE' });
authoringElement('aw-network-authoring-form', { tagName: 'FORM' });
authoringElement('aw-model-authoring-form', { tagName: 'FORM' });
authoringElement('aw-network-authoring-fields', { tagName: 'FIELDSET' });
authoringElement('aw-model-identity-fields', { tagName: 'FIELDSET' });
authoringElement('aw-model-rate-fields', { tagName: 'FIELDSET' });
authoringElement('aw-model-assumption-fields', { tagName: 'FIELDSET' });
authoringElement('aw-network-candidate', { tagName: 'SELECT', options: [] });
authoringElement('aw-network-ack', { tagName: 'INPUT', type: 'checkbox' });
authoringElement('aw-model-ack', { tagName: 'INPUT', type: 'checkbox' });
authoringElement('aw-model-spec-id', { tagName: 'INPUT' });
authoringElement('aw-model-mode', { tagName: 'SELECT', options: [] });
['aw-network-preview', 'aw-network-confirm', 'aw-model-preview', 'aw-model-confirm']
  .forEach(id => authoringElement(id, { tagName: 'BUTTON' }));
['aw-network-operation', 'aw-model-operation']
  .forEach(id => authoringElement(id, { tagName: 'P', className: 'aw-authoring-operation' }));
authoringElement('page-analysis-workbench', { hidden: false });

let currentProjectId = 'project-a';
const projectRows = [
  { project_id: 'project-a', name: 'Project A' },
  { project_id: 'project-b', name: 'Project B' },
];
window.Project = { current: () => projectRows.find(item => item.project_id === currentProjectId) };
VCS.workspace = { state: { project_id: currentProjectId }, projects: projectRows };
"""


_FIXTURES = r"""
const sha = char => String(char || 'a').repeat(64);
const authority = char => String(char || 'a').repeat(32);

function emptyBinding() {
  return {
    schema: 'vcstudio.catalysis-binding-authority-snapshot/v1',
    authority_id: null, revision: 0, current_hash: null, binding: null,
    snapshot_sha256: sha('a'),
  };
}
function networkHead(networkId, char = 'b') {
  return {
    schema: 'vcstudio.catalysis-network-head-cas/v1',
    domain_authority_id: authority('d'), domain_generation: 7,
    domain_snapshot_sha256: sha('d'), network_id: networkId,
    network_status: 'available', network_revision_id: 'rev-1',
    network_semantic_sha256: sha(char), snapshot_sha256: sha('c'),
  };
}
function gapSummary(total = 0) {
  return { total, returned: 0, omitted: total, by_code: total ? { missing_member: total } : {} };
}
function candidate(networkId, char = 'b', available = true) {
  return {
    schema: 'vcstudio.catalysis-authoring-network-candidate/v1',
    network_head: networkHead(networkId, char),
    projection_status: available ? 'available' : 'unavailable',
    gap_summary: gapSummary(available ? 0 : 1), closure_sha256: sha('e'),
  };
}
function choiceCatalog() {
  return {
    schema: 'vcstudio.kinetics-authoring-choice-catalog/v1',
    rate_law_policy: {
      activity: ['ideal'], reversibility: ['explicit_reverse'],
      detailed_balance: ['enforced'], prefactor: ['explicit_per_step'],
      electrochemical: ['none'], reactor: ['mean_field_steady_state'],
    },
    assumptions: {
      mean_field: [true], steady_state: [true], site_uniformity: ['uniform'],
      lateral_interactions: ['neglected', 'parameterized'],
      mechanism_completeness: ['claimed_complete', 'partial', 'unknown'],
    },
    feed_reservoir_units: ['bar', 'mol/L', 'dimensionless'],
    site_total_units: ['sites', 'dimensionless'],
    site_total_bases: ['surface_unit_cell', 'normalized_site_population'],
    prefactor_units: ['s^-1', 'bar^-1 s^-1', 'mol^-1 L s^-1'],
  };
}
function publicSource(status = 'available') {
  return {
    status,
    required_feed_reservoir_ids: ['gas-co'],
    allowed_target_product_ids: ['gas-co2'],
    required_step_ids: ['step-1'], required_site_type_ids: ['top'],
    saddle_candidates_by_step: { 'step-1': ['saddle-1'] },
    evidence_catalog: [{ reference_id: 'ref-1', kind: 'literature' }],
    solver_ready: false, solver_readiness_reasons: ['conditions_missing'],
  };
}
function selectedBinding(networkId = 'network-a', { stale = false } = {}) {
  const semantic = stale ? sha('9') : networkHead(networkId,
    networkId === 'network-a' ? 'b' : 'c').network_semantic_sha256;
  const record = {
    schema: 'vcstudio.catalysis-active-network-binding/v1',
    authority_id: authority('a'), revision: 1, parent_revision: null,
    expected_current_hash: null, domain_authority_id: authority('d'),
    domain_generation: 7, domain_snapshot_sha256: sha('d'),
    network_id: networkId, network_revision_id: 'rev-1',
    network_semantic_sha256: semantic, intent_id: 'existing-selection',
    confirmed: true, job_source_of_truth: 'job.yaml',
    authorizes_execution: false, binding_sha256: sha('a'),
  };
  return {
    schema: 'vcstudio.catalysis-binding-authority-snapshot/v1',
    authority_id: authority('a'), revision: 1, current_hash: sha('a'),
    binding: record, snapshot_sha256: sha('b'),
  };
}
function bootstrapEnvelope({ networkStatus = 'available', kineticsStatus = 'available',
  bootstrapStatus = null } = {}) {
  const networks = networkStatus === 'available'
    ? [candidate('network-a', 'b'), candidate('network-b', 'c')] : [];
  const status = bootstrapStatus || (networkStatus === 'available' ? kineticsStatus : networkStatus);
  const conflict = status === 'conflict';
  const binding = networkStatus === 'available' ?
    selectedBinding('network-a', { stale: conflict }) : emptyBinding();
  const shared = networkStatus === 'available' ? {
    schema: 'vcstudio.catalysis-authoring-shared-authority/v1',
    domain_authority_id: authority('d'), domain_generation: 7,
    domain_snapshot_sha256: sha('d'), network_id: binding.binding.network_id,
    network_revision_id: binding.binding.network_revision_id,
    network_semantic_sha256: binding.binding.network_semantic_sha256,
  } : null;
  const reason = status === 'available' ? null : conflict ?
    'stale_active_network_binding' : networkStatus !== 'available' ?
      'network_missing' : 'source_missing';
  return {
    ok: true, schema: 'vcstudio.catalysis-authoring-bootstrap-api/v1',
    project_id: 'project-a', bootstrap_status: status, reason,
    shared_authority: shared,
    active_network: {
      schema: 'vcstudio.catalysis-authoring-bootstrap/v1', project_id: 'project-a',
      status: networkStatus, reason: networkStatus === 'available' ? null : 'network_missing',
      domain_cas: networkStatus === 'available' ? {
        authority_id: authority('d'), generation: 7, snapshot_sha256: sha('d'),
      } : null,
      binding_cas: binding, candidates: networks,
      active_network_id: binding.binding && binding.binding.network_id,
      active_status: binding.binding ? (conflict ? 'stale' : 'available') : 'none',
      snapshot_sha256: sha('f'),
    },
    kinetics_authoring: {
      status: kineticsStatus,
      reason: kineticsStatus === 'available' ? null : conflict ?
        'stale_active_network_binding' : 'source_missing',
      source: kineticsStatus === 'available' ? publicSource() : null,
      model_store: { status: kineticsStatus === 'unavailable' ? 'unavailable' : 'available' },
      selector: { status: 'none', migration_state: 'none', selected: null },
      choice_catalog: choiceCatalog(),
    },
    error_code: null, error_field: null, error: null,
  };
}
function networkPreviewEnvelope(request, status = 'ready') {
  const ready = status === 'ready';
  return {
    ok: true, schema: 'vcstudio.catalysis-active-network-preview-api/v1',
    project_id: 'project-a',
    preview: {
      schema: 'vcstudio.catalysis-active-network-preview/v1', project_id: 'project-a',
      network_id: request.network_id, intent_id: request.intent_id,
      request_sha256: sha('a'), status, reason: ready ? null : 'head_changed',
      action: ready ? (request.expected_binding.revision === 0 ? 'create' : 'advance') : null,
      binding_cas: request.expected_binding,
      network_cas: request.expected_network_head,
      projection_status: 'available', gaps: [], gap_summary: gapSummary(),
      closure_sha256: sha('e'), issued_at_ms: 1000, expires_at_ms: 61000,
      preview_sha256: sha('b'), can_confirm: ready,
      confirmed: false, authorizes_execution: false,
    },
    error_code: null, error_field: null, error: null,
  };
}
function committedBinding(request) {
  const base = request.expected_binding;
  const revision = base.revision + 1;
  const digest = sha(revision === 1 ? 'a' : '9');
  const record = {
    schema: 'vcstudio.catalysis-active-network-binding/v1',
    authority_id: base.authority_id || authority('a'), revision,
    parent_revision: base.revision || null,
    expected_current_hash: base.current_hash,
    domain_authority_id: authority('d'),
    domain_generation: 7, domain_snapshot_sha256: sha('d'),
    network_id: request.network_id, network_revision_id: 'rev-1',
    network_semantic_sha256: request.expected_network_head.network_semantic_sha256,
    intent_id: request.intent_id, confirmed: true, job_source_of_truth: 'job.yaml',
    authorizes_execution: false, binding_sha256: digest,
  };
  return {
    schema: 'vcstudio.catalysis-binding-authority-snapshot/v1',
    authority_id: record.authority_id, revision, current_hash: digest,
    binding: record, snapshot_sha256: sha('8'),
  };
}
function networkConfirmEnvelope(request, action = 'advanced') {
  const conflict = action === 'conflict' ? {
    schema: 'vcstudio.catalysis-active-network-conflict/v1', reason: 'binding_advanced',
    latest_binding_cas: committedBinding(request), latest_network_cas: request.expected_network_head,
    retry_automatically: false,
  } : null;
  return {
    ok: true, schema: 'vcstudio.catalysis-active-network-confirm-api/v1',
    project_id: 'project-a',
    result: {
      schema: 'vcstudio.catalysis-active-network-confirm-result/v1', action,
      binding_cas: action === 'conflict' ? null : committedBinding(request),
      network_cas: action === 'conflict' ? null : request.expected_network_head,
      conflict, reason: action === 'conflict' ? 'binding_advanced' : null,
    },
    error_code: null, error_field: null, error: null,
  };
}
function sourceCAS() {
  return {
    schema: 'vcstudio.kinetics-authoring-source-cas/v1', project_id: 'project-a',
    domain_authority_id: authority('d'), domain_generation: 7,
    domain_snapshot_sha256: sha('d'), network_id: 'network-a', network_revision: 'rev-1',
    network_semantic_sha256: sha('b'), source_projection_sha256: sha('c'),
    snapshot_sha256: sha('d'),
  };
}
function storeCAS() {
  return {
    schema: 'vcstudio.kinetics-model-spec-store-snapshot/v1', authority_id: authority('e'),
    generation: 0, heads: [], chain_sha256: sha('a'), store_sha256: sha('b'),
    anchor_chain_sha256: sha('c'), snapshot_sha256: sha('d'),
  };
}
function selectorCAS() {
  return {
    schema: 'vcstudio.kinetics-active-spec-selector-snapshot/v2', authority_id: null,
    revision: 0, current_hash: null, selection: null, migration_state: 'none',
    legacy_selection_sha256: null, snapshot_sha256: sha('e'),
  };
}
function modelPreviewEnvelope(intentId, { canConfirm = true } = {}) {
  return {
    ok: true, schema: 'vcstudio.kinetics-model-spec-preview-api/v1', project_id: 'project-a',
    preview: {
      schema: 'vcstudio.kinetics-authoring-preview/v1', intent_id: intentId,
      draft_sha256: sha('a'), preview_sha256: sha('b'),
      source_cas: canConfirm ? sourceCAS() : null,
      store_cas: canConfirm ? storeCAS() : null,
      selector_cas: canConfirm ? selectorCAS() : null,
      target_spec: canConfirm ? {
        project_id: 'project-a', spec_id: 'model-a', revision: 'rev-1', spec_sha256: sha('c'),
      } : null,
      issues: canConfirm ? [] : [{ code: 'missing_choice', path: '$draft.steps[0]', message: 'choice missing' }],
      can_confirm: canConfirm, solver_ready: false,
      solver_readiness_reasons: canConfirm ? ['conditions_missing'] : [],
      authorizes_execution: false,
    },
    error_code: null, error_field: null, error: null,
  };
}
function modelConfirmEnvelope(action = 'committed', request = null) {
  const conflict = action === 'conflict' ? {
    schema: 'vcstudio.kinetics-authoring-conflict/v1', reason: 'store_advanced',
    latest_source_cas: sourceCAS(), latest_store_cas: storeCAS(),
    latest_selector_cas: selectorCAS(), retry_automatically: false,
  } : null;
  const confirmation = request && request.confirmation;
  const spec = action === 'committed' ? {
    schema: 'vcstudio.kinetics-model-spec/v1', project_id: 'project-a', spec_id: 'model-a',
    revision: 'rev-1', parent_revision: null, expected_current_hash: null,
    source_binding: {}, rate_law_policy: {}, assumptions: {}, feed_reservoirs: [],
    target_products: [], steps: [], site_population_totals: [], saddle_selector: null,
  } : null;
  const selector = action === 'committed' ? {
    schema: 'vcstudio.kinetics-active-spec-selection/v2', authority_id: authority('a'),
    revision: 1, parent_revision: null, expected_current_hash: null,
    project_id: 'project-a', spec_id: 'model-a', spec_revision: 'rev-1',
    spec_sha256: sha('c'), intent_id: confirmation.intent_id, transaction_id: 'txn-1',
    confirmed: true, selection_sha256: sha('f'),
  } : null;
  const receipt = action === 'committed' ? {
    schema: 'vcstudio.kinetics-authoring-commit-receipt/v1', outcome: 'committed',
    transaction_id: 'txn-1', intent_id: confirmation.intent_id,
    draft_sha256: confirmation.draft_sha256, preview_sha256: confirmation.preview_sha256,
    request_sha256: sha('a'), source_snapshot_sha256: confirmation.source_snapshot_sha256,
    base_store_snapshot_sha256: confirmation.store_snapshot_sha256,
    base_selector_snapshot_sha256: confirmation.selector_snapshot_sha256,
    project_id: 'project-a', spec_id: 'model-a', spec_revision: 'rev-1',
    spec_sha256: sha('c'), selector_sha256: sha('f'), receipt_sha256: sha('a'),
  } : null;
  return {
    ok: true, schema: 'vcstudio.kinetics-model-spec-confirm-api/v1', project_id: 'project-a',
    result: {
      schema: 'vcstudio.kinetics-authoring-confirm-result/v1', action,
      spec, selector, receipt,
      conflict, issues: [],
    },
    error_code: null, error_field: null, error: null,
  };
}
function chooseMulti(id) {
  const control = elements.get(id); assert.ok(control && control.options.length, id);
  control.options[0].selected = true;
}
function fillModel(specId = 'model-a') {
  elements.get('aw-model-spec-id').value = specId;
  elements.get('aw-model-mode').value = 'create';
  const singles = {
    'aw-model-rate-activity': 'ideal',
    'aw-model-rate-reversibility': 'explicit_reverse',
    'aw-model-rate-detailed_balance': 'enforced',
    'aw-model-rate-prefactor': 'explicit_per_step',
    'aw-model-rate-electrochemical': 'none',
    'aw-model-rate-reactor': 'mean_field_steady_state',
    'aw-model-assumption-mean_field': 'true',
    'aw-model-assumption-steady_state': 'true',
    'aw-model-assumption-site_uniformity': 'uniform',
    'aw-model-assumption-lateral_interactions': 'neglected',
    'aw-model-assumption-mechanism_completeness': 'claimed_complete',
    'aw-feed-0-activity': '1', 'aw-feed-0-unit': 'bar', 'aw-feed-0-source': 'ref-1',
    'aw-step-0-forward-value': '1e13', 'aw-step-0-forward-unit': 's^-1',
    'aw-step-0-forward-source': 'ref-1', 'aw-step-0-reverse-value': '1e13',
    'aw-step-0-reverse-unit': 's^-1', 'aw-step-0-reverse-source': 'ref-1',
    'aw-step-0-bep-used': 'false', 'aw-step-0-scaling-used': 'false',
    'aw-step-0-uncertainty': '0.1', 'aw-site-0-value': '1',
    'aw-site-0-unit': 'sites', 'aw-site-0-basis': 'surface_unit_cell',
    'aw-model-saddle-mode': 'omit',
  };
  Object.entries(singles).forEach(([id, value]) => {
    assert.ok(elements.get(id), id); elements.get(id).value = value;
  });
  elements.get('aw-target-0').checked = true;
  chooseMulti('aw-model-assumption-evidence');
  chooseMulti('aw-step-0-evidence');
  chooseMulti('aw-site-0-evidence');
}
function acknowledge(kind) {
  const ack = elements.get(`aw-${kind}-ack`); ack.checked = true;
  elements.get(`aw-${kind}-authoring-form`).dispatchEvent({ type: 'change', target: ack });
}
function allRenderedText() {
  return [...elements.values()].map(item => String(item.textContent || '') + ' ' +
    String(item.placeholder || '')).join('\n');
}
"""


def _run_authoring(body: str) -> None:
    _run_node(_DOM + _FIXTURES + body, str(SCRIPT))


def test_authoring_markup_locales_and_accessibility_are_complete() -> None:
    html = INDEX.read_text(encoding="utf-8")
    en = json.loads((ROOT / "vcstudio/shared/locales/en.json").read_text(encoding="utf-8"))
    zh = json.loads((ROOT / "vcstudio/shared/locales/zh.json").read_text(encoding="utf-8"))

    assert 'id="aw-network-authoring-form"' in html
    assert 'id="aw-model-authoring-form"' in html
    assert html.count("<fieldset") >= 2 and html.count("<legend") >= 2
    assert 'id="aw-network-operation" role="status"' in html
    assert 'id="aw-model-operation" role="status"' in html
    assert html.count('aria-live="polite"') >= 4
    assert 'id="aw-network-authoring"' in html and 'aria-busy="false"' in html
    assert 'id="aw-model-authoring"' in html
    assert 'data-i18n-ph="analysis.authoring.model.spec_id_placeholder"' in html
    assert {key for key in en if key.startswith("analysis.authoring.")} == {
        key for key in zh if key.startswith("analysis.authoring.")
    }
    assert en["analysis.authoring.model.spec_id_placeholder"]
    assert zh["analysis.authoring.model.spec_id_placeholder"]


def test_network_epoch_discards_late_work_and_conflict_requires_explicit_retry() -> None:
    _run_authoring(
        r"""
loadAsset(process.argv[1]);
document.dispatchEvent(new Event('DOMContentLoaded'));
const seam = window.__VCS_ANALYSIS_TEST__; assert.ok(seam);
const firstPreview = deferred(); const lateConfirm = deferred();
let previewRequests = []; let confirmRequests = []; let previewMode = 'deferred';
VCS.call = async (method, projectId, payload) => {
  trace.calls.push({ method, projectId, payload });
  if (method === 'catalysis_authoring_bootstrap') return bootstrapEnvelope();
  if (method === 'catalysis_active_network_preview') {
    previewRequests.push(payload);
    if (previewMode === 'deferred') return firstPreview.promise;
    return networkPreviewEnvelope(payload);
  }
  if (method === 'catalysis_active_network_confirm') {
    confirmRequests.push(payload);
    if (confirmRequests.length === 1) return lateConfirm.promise;
    if (confirmRequests.length === 2) return networkConfirmEnvelope(payload.request, 'conflict');
    return networkConfirmEnvelope(payload.request, 'advanced');
  }
  throw new Error('unexpected method ' + method);
};
seam.resetAuthoring('project-a', 'kinetic-dashboard');
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
const candidateSelect = elements.get('aw-network-candidate');
candidateSelect.value = 'network-a';
const pendingPreview = seam.previewActiveNetwork();
await waitFor(() => previewRequests.length === 1, 'preview did not start');
assert.strictEqual(elements.get('aw-network-authoring').getAttribute('aria-busy'), 'true');
candidateSelect.value = 'network-b';
elements.get('aw-network-authoring-form').dispatchEvent({ type: 'change', target: candidateSelect });
firstPreview.resolve(networkPreviewEnvelope(previewRequests[0]));
assert.strictEqual(await pendingPreview, false, 'late preview must be ignored');
assert.strictEqual(seam.snapshot().authoring.network.has_preview, false);

previewMode = 'immediate';
assert.strictEqual(await seam.previewActiveNetwork(), true);
assert.ok(!/[a-f0-9]{64}/.test(allRenderedText()),
  'network receipts and closure hashes must remain out of the DOM');
acknowledge('network');
const pendingConfirm = seam.confirmActiveNetwork();
await waitFor(() => confirmRequests.length === 1, 'confirm did not start');
assert.strictEqual(elements.get('aw-network-authoring').getAttribute('aria-busy'), 'true');
assert.strictEqual(elements.get('aw-network-authoring-fields').disabled, true);
candidateSelect.value = 'network-a';
elements.get('aw-network-authoring-form').dispatchEvent({ type: 'change', target: candidateSelect });
lateConfirm.resolve(networkConfirmEnvelope(confirmRequests[0].request, 'advanced'));
assert.strictEqual(await pendingConfirm, false, 'confirm owned by changed form must be ignored');
assert.strictEqual(seam.snapshot().authoring.network.last_action, null);

candidateSelect.value = 'network-b';
assert.strictEqual(await seam.previewActiveNetwork(), true);
acknowledge('network');
assert.strictEqual(await seam.confirmActiveNetwork(), false, 'conflict must stop');
let snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.network.conflict, true);
assert.strictEqual(snapshot.authoring.network.has_preview, false);
assert.strictEqual(candidateSelect.value, 'network-b', 'conflict must preserve the form');
assert.strictEqual(confirmRequests.length, 2, 'conflict must not retry automatically');

assert.strictEqual(await seam.previewActiveNetwork(), true, 'user can explicitly preview again');
assert.strictEqual(previewRequests[3].expected_binding.revision, 2,
  'an explicit retry must use the adopted authoritative binding CAS');
acknowledge('network');
assert.strictEqual(await seam.confirmActiveNetwork(), true);
snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.network.last_action, 'advanced');
assert.strictEqual(confirmRequests.length, 3);
assert.ok(trace.focus.includes('aw-network-operation'), 'status changes should be focusable');

const projectLate = deferred(); previewMode = 'project-late';
VCS.call = async (method, projectId, payload) => {
  if (method === 'catalysis_active_network_preview') {
    previewRequests.push(payload); return projectLate.promise;
  }
  throw new Error('unexpected method ' + method);
};
candidateSelect.value = 'network-a';
const oldProjectPreview = seam.previewActiveNetwork();
await waitFor(() => previewRequests.length === 5, 'project-owned preview did not start');
currentProjectId = 'project-b'; VCS.workspace.state.project_id = 'project-b';
seam.resetAuthoring('project-b', 'kinetic-dashboard');
projectLate.resolve(networkPreviewEnvelope(previewRequests[4]));
assert.strictEqual(await oldProjectPreview, false);
assert.strictEqual(seam.snapshot().authoring.project_id, 'project-b');
assert.strictEqual(seam.snapshot().authoring.network.has_preview, false);
const epochBeforeRoute = seam.snapshot().authoring.epoch;
document.dispatchEvent(new CustomEvent('vcs:route', { detail: { page: 'home' } }));
assert.ok(seam.snapshot().authoring.epoch > epochBeforeRoute);
assert.strictEqual(seam.snapshot().authoring.project_id, '',
  'leaving the analysis route must clear all authoring ownership');
assert.deepStrictEqual(trace.storageSets, [], 'network authoring must not persist drafts or receipts');
"""
    )


def test_model_receipt_ownership_busy_conflict_and_explicit_second_submission() -> None:
    _run_authoring(
        r"""
loadAsset(process.argv[1]);
document.dispatchEvent(new Event('DOMContentLoaded'));
const seam = window.__VCS_ANALYSIS_TEST__; assert.ok(seam);
const firstPreview = deferred(); const conflictConfirm = deferred();
let previewCalls = []; let confirmCalls = []; let previewCount = 0;
VCS.call = async (method, projectId, payload) => {
  trace.calls.push({ method, projectId, payload });
  if (method === 'catalysis_authoring_bootstrap') return bootstrapEnvelope();
  if (method === 'kinetics_model_spec_preview') {
    previewCalls.push(payload); previewCount += 1;
    return previewCount === 1 ? firstPreview.promise : modelPreviewEnvelope(payload.intent_id);
  }
  if (method === 'kinetics_model_spec_confirm') {
    confirmCalls.push(payload);
    return confirmCalls.length === 1 ? conflictConfirm.promise : modelConfirmEnvelope('committed', payload);
  }
  throw new Error('unexpected method ' + method);
};
seam.resetAuthoring('project-a', 'kinetic-dashboard');
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
fillModel('model-a');
const pendingPreview = seam.previewModelSpec();
await waitFor(() => previewCalls.length === 1, 'model preview did not start');
assert.strictEqual(elements.get('aw-model-authoring').getAttribute('aria-busy'), 'true');
elements.get('aw-model-spec-id').value = 'model-b';
elements.get('aw-model-authoring-form').dispatchEvent({
  type: 'input', target: elements.get('aw-model-spec-id'),
});
firstPreview.resolve(modelPreviewEnvelope(previewCalls[0].intent_id));
assert.strictEqual(await pendingPreview, false, 'late model preview must be ignored');
assert.strictEqual(seam.snapshot().authoring.model.has_preview, false);

fillModel('model-a');
assert.strictEqual(await seam.previewModelSpec(), true);
assert.strictEqual(elements.get('aw-model-save-status').dataset.status, 'ready');
assert.strictEqual(elements.get('aw-model-solver-status').dataset.status, 'blocked',
  'save eligibility and solver readiness must remain separate');
acknowledge('model');
const pendingConfirm = seam.confirmModelSpec();
await waitFor(() => confirmCalls.length === 1, 'model confirm did not start');
assert.strictEqual(elements.get('aw-model-authoring').getAttribute('aria-busy'), 'true');
assert.strictEqual(elements.get('aw-model-spec-id').disabled, true);
assert.strictEqual(elements.get('aw-model-confirm').disabled, true);
conflictConfirm.resolve(modelConfirmEnvelope('conflict'));
assert.strictEqual(await pendingConfirm, false);
let snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.model.conflict, true);
assert.strictEqual(snapshot.authoring.model.has_preview, false);
assert.strictEqual(elements.get('aw-model-spec-id').value, 'model-a');
assert.strictEqual(confirmCalls.length, 1, 'conflict must not auto retry');

assert.strictEqual(await seam.previewModelSpec(), true);
acknowledge('model');
assert.strictEqual(await seam.confirmModelSpec(), true);
snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.model.last_action, 'committed');
assert.strictEqual(confirmCalls.length, 2, 'second submission must be explicit');
assert.ok(trace.focus.includes('aw-model-operation'));
assert.ok(!/[a-f0-9]{64}/.test(allRenderedText()),
  'model CAS, receipt, and evidence hashes must remain out of the DOM');
assert.deepStrictEqual(trace.storageSets, [], 'model authoring must not persist drafts or receipts');
"""
    )


def test_preview_and_confirm_receipts_cannot_cross_the_frozen_authority() -> None:
    _run_authoring(
        r"""
loadAsset(process.argv[1]);
document.dispatchEvent(new Event('DOMContentLoaded'));
const seam = window.__VCS_ANALYSIS_TEST__; assert.ok(seam);
let phase = 'network-preview-cross'; let confirmCalls = 0;
VCS.call = async (method, projectId, payload) => {
  if (method === 'catalysis_authoring_bootstrap') return bootstrapEnvelope();
  if (method === 'catalysis_active_network_preview') {
    const response = networkPreviewEnvelope(payload);
    if (phase === 'network-preview-cross') {
      response.preview.network_cas.domain_generation = 8;
      response.preview.network_cas.domain_snapshot_sha256 = sha('8');
    }
    return response;
  }
  if (method === 'catalysis_active_network_confirm') {
    confirmCalls += 1;
    const response = networkConfirmEnvelope(payload.request, 'advanced');
    response.result.network_cas.domain_generation = 8;
    response.result.network_cas.domain_snapshot_sha256 = sha('8');
    response.result.binding_cas.binding.domain_generation = 8;
    response.result.binding_cas.binding.domain_snapshot_sha256 = sha('8');
    return response;
  }
  if (method === 'kinetics_model_spec_preview') {
    const response = modelPreviewEnvelope(payload.intent_id);
    response.preview.source_cas.domain_generation = 8;
    response.preview.source_cas.domain_snapshot_sha256 = sha('8');
    return response;
  }
  throw new Error('unexpected method ' + method);
};

seam.resetAuthoring('project-a', 'kinetic-dashboard');
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
elements.get('aw-network-candidate').value = 'network-b';
assert.strictEqual(await seam.previewActiveNetwork(), false,
  'a ready preview from another domain generation must fail closed');
let snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.network.status, 'unavailable');
assert.strictEqual(snapshot.authoring.network.has_preview, false);
assert.strictEqual(elements.get('aw-network-confirm').disabled, true);

phase = 'network-confirm-cross';
seam.resetAuthoring('project-a', 'kinetic-dashboard');
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
elements.get('aw-network-candidate').value = 'network-b';
assert.strictEqual(await seam.previewActiveNetwork(), true);
acknowledge('network');
assert.strictEqual(await seam.confirmActiveNetwork(), false,
  'an internally consistent result from another generation must not confirm');
snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.network.last_action, null);
assert.strictEqual(snapshot.authoring.network.has_preview, false);
assert.strictEqual(confirmCalls, 1);

phase = 'model-preview-cross';
seam.resetAuthoring('project-a', 'kinetic-dashboard');
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
fillModel();
assert.strictEqual(await seam.previewModelSpec(), false,
  'model source CAS must remain bound to the shared bootstrap authority');
snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.model.status, 'unavailable');
assert.strictEqual(snapshot.authoring.model.has_preview, false);
assert.strictEqual(elements.get('aw-model-confirm').disabled, true);
assert.deepStrictEqual(trace.storageSets, []);
"""
    )


def test_missing_blocked_and_invalid_sensitive_responses_fail_closed() -> None:
    _run_authoring(
        r"""
loadAsset(process.argv[1]);
document.dispatchEvent(new Event('DOMContentLoaded'));
const seam = window.__VCS_ANALYSIS_TEST__; assert.ok(seam);
let bootstrap = bootstrapEnvelope({ networkStatus: 'missing', kineticsStatus: 'missing_prerequisite' });
VCS.call = async method => {
  assert.strictEqual(method, 'catalysis_authoring_bootstrap'); return bootstrap;
};
seam.resetAuthoring('project-a', 'kinetic-dashboard');
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
let snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.network.status, 'missing');
assert.strictEqual(snapshot.authoring.model.status, 'missing_prerequisite');
assert.strictEqual(elements.get('aw-network-preview').disabled, true);
assert.strictEqual(elements.get('aw-model-preview').disabled, true);

bootstrap = bootstrapEnvelope({ networkStatus: 'missing', kineticsStatus: 'blocked' });
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
assert.strictEqual(seam.snapshot().authoring.model.status, 'blocked');

bootstrap = bootstrapEnvelope();
bootstrap.kinetics_authoring.source.evidence_catalog[0].artifact_sha256 = sha('a');
bootstrap.kinetics_authoring.source.project_root = 'C:\\Users\\victim\\secret-project';
bootstrap.error = 'Bearer sk-super-secret-value';
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), false);
snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.network.status, 'unavailable');
assert.strictEqual(snapshot.authoring.model.status, 'unavailable');
const rendered = allRenderedText();
assert.ok(!rendered.includes('secret-project'));
assert.ok(!rendered.includes('super-secret'));
assert.ok(!/[a-f0-9]{64}/.test(rendered), 'receipt/evidence hashes must not be rendered');
assert.deepStrictEqual(trace.storageSets, [], 'authoring state must never persist to localStorage');
assert.strictEqual(elements.get('aw-network-authoring').getAttribute('aria-busy'), 'false');
assert.strictEqual(elements.get('aw-model-authoring').getAttribute('aria-busy'), 'false');
"""
    )


def test_invalid_model_preview_is_unavailable_and_never_enables_confirmation() -> None:
    _run_authoring(
        r"""
loadAsset(process.argv[1]);
document.dispatchEvent(new Event('DOMContentLoaded'));
const seam = window.__VCS_ANALYSIS_TEST__; assert.ok(seam);
VCS.call = async (method, projectId, payload) => {
  if (method === 'catalysis_authoring_bootstrap') return bootstrapEnvelope();
  if (method === 'kinetics_model_spec_preview') {
    const response = modelPreviewEnvelope(payload.intent_id);
    delete response.preview.authorizes_execution;
    return response;
  }
  throw new Error('confirm must not be called');
};
seam.resetAuthoring('project-a', 'kinetic-dashboard');
assert.strictEqual(await seam.loadAuthoringBootstrap('project-a'), true);
fillModel();
assert.strictEqual(await seam.previewModelSpec(), false);
const snapshot = seam.snapshot();
assert.strictEqual(snapshot.authoring.model.status, 'unavailable');
assert.strictEqual(snapshot.authoring.model.has_preview, false);
assert.strictEqual(elements.get('aw-model-confirm').disabled, true);
assert.strictEqual(trace.calls.filter(item => item.method === 'kinetics_model_spec_confirm').length, 0);
"""
    )
