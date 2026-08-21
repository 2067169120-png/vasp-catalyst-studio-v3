// analysis-workbench.js — Phase D registry-driven analysis UI.
// Scientific rows, rankings, gates and sensitivity sets are server-owned.
'use strict';

(function () {
  const VCS = window.VCS;
  if (!VCS) return;
  const $ = id => document.getElementById(id);
  const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
  const AUTHORING_ID = /^[A-Za-z0-9][A-Za-z0-9._~:+-]{0,159}$/;
  const SAFE_SHA256 = /^[a-f0-9]{64}$/;
  const AUTHORING_STATUSES = new Set([
    'missing', 'missing_prerequisite', 'unavailable', 'blocked', 'conflict', 'ready',
  ]);
  const AUTHORING_BOOTSTRAP_STATUSES = new Set([
    'available', 'missing', 'missing_prerequisite', 'unavailable', 'blocked', 'conflict',
  ]);
  const RATE_POLICY_KEYS = Object.freeze([
    'activity', 'reversibility', 'detailed_balance', 'prefactor',
    'electrochemical', 'reactor',
  ]);
  const ASSUMPTION_POLICY_KEYS = Object.freeze([
    'site_uniformity', 'lateral_interactions', 'mechanism_completeness',
  ]);
  const ROUTE_ANALYSIS = Object.freeze({
    'analyze-energy': 'adsorption-energy',
    'analyze-thermo': 'free-energy-path',
    'analyze-kinetics': 'kinetic-dashboard',
    'analyze-electronic': 'electronic-structure',
    'analyze-charge': 'charge-wavefunction',
    'analyze-comparison': 'multi-project-comparison',
    'analyze-custom': 'task-results',
    'analyze-properties': 'property-calculators',
  });
  const CAPABILITY_LABEL_KEYS = Object.freeze({
    available: 'analysis.capability.available',
    missing_prerequisite: 'analysis.capability.missing_prerequisite',
    mode_mismatch: 'analysis.capability.mode_mismatch',
    not_implemented: 'analysis.capability.not_implemented',
    unavailable: 'analysis.capability.unavailable',
  });
  const CAPABILITY_ACTION_KEYS = Object.freeze({
    available: 'analysis.capability.action.available',
    missing_prerequisite: 'analysis.capability.action.missing_prerequisite',
    mode_mismatch: 'analysis.capability.action.mode_mismatch',
    not_implemented: 'analysis.capability.action.not_implemented',
    unavailable: 'analysis.capability.action.unavailable',
  });
  function freshAuthoringAxis() {
    return {
      intentGeneration: 0, dirty: false, previewReceipt: null, previewRequest: null,
      intentId: '', acknowledged: false, previewing: false, confirming: false,
      status: 'missing', message: '', conflict: null, lastResult: null,
    };
  }

  const State = {
    projectId: '', projectName: '', analysisId: '',
    catalog: null, spec: null, view: null, projects: [],
    busy: false, dirty: false, busyToken: 0, intentGeneration: 0,
    bootstrapGeneration: 0, previewGeneration: 0,
    preferenceRevision: null, preferences: null,
    kineticsPreviewSha: '', kineticsSelectionRevision: null,
    kineticsSelectedExportSha: '', kineticsAuditPreviewSha: '',
    kineticsUploadedResult: null,
    authoringEpoch: 0, authoringIntentCounter: 0,
    authoringProjectId: '', authoringAnalysisId: '',
    authoringBootstrap: null, authoringBootstrapStatus: 'missing',
    networkAuthoring: freshAuthoringAxis(), modelAuthoring: freshAuthoringAxis(),
    modelAuthoringControls: Object.create(null),
  };

  function plain(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }
  function clone(value) {
    try { return JSON.parse(JSON.stringify(value)); } catch (_) { return null; }
  }
  function safeId(value) {
    const text = String(value || '').trim(); return SAFE_ID.test(text) ? text : '';
  }
  function safeSha(value) {
    const text = String(value || '').trim();
    return SAFE_SHA256.test(text) ? text : '';
  }
  function safeAuthoringId(value) {
    const text = String(value || '').trim(); return AUTHORING_ID.test(text) ? text : '';
  }
  function esc(value) { return VCS.esc ? VCS.esc(String(value == null ? '' : value)) : String(value || ''); }
  function tr(key, fallback, params) {
    return typeof VCS.t === 'function' ? VCS.t(key, params || {}, fallback) : fallback;
  }
  function localized(record, field, fallback = '') {
    const source = plain(record); const suffix = VCS.i18n && VCS.i18n.lang === 'en' ? '_en' : '_zh';
    return String(source[field + suffix] || source[field] || fallback || '');
  }
  function projectIdentity(project) {
    return safeId(project && project.project_id);
  }
  function currentProject() {
    if (window.Project && typeof window.Project.current === 'function') {
      const project = window.Project.current(); if (projectIdentity(project)) return project;
    }
    const workspace = VCS.workspace; const id = safeId(workspace && workspace.state && workspace.state.project_id);
    if (!id || !workspace || !Array.isArray(workspace.projects)) return null;
    return workspace.projects.find(project => projectIdentity(project) === id) || null;
  }
  function sameProject(id) {
    const current = currentProject();
    return !!current && projectIdentity(current) === id;
  }
  function analysisRecord(id = State.analysisId) {
    return (plain(State.catalog).analyses || []).find(item => String(item.id || '') === String(id || '')) || null;
  }
  function routeAnalysis(detail) {
    return ROUTE_ANALYSIS[String(detail && detail.id || '')] || State.analysisId || 'adsorption-energy';
  }
  function showAlert(message) {
    const box = $('aw-alert'); if (!box) return; const text = String(message || '').trim();
    box.hidden = !text; box.textContent = text;
  }
  function operation(message, tone = '') {
    const box = $('aw-operation'); if (!box) return; box.textContent = String(message || '');
    box.className = 'aw-operation' + (tone ? ` ${tone}` : '');
  }
  function beginBusy() {
    const token = ++State.busyToken; State.busy = true; return token;
  }
  function endBusy(token) {
    if (token !== State.busyToken) return;
    State.busy = false; renderAll();
  }
  function setText(id, value, fallback = '—') {
    const node = $(id); if (node) node.textContent = value === null || value === undefined || value === '' ? fallback : String(value);
  }

  function exactObject(value, keys) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const actual = Object.keys(value).sort(); const expected = [...keys].sort();
    return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
  }

  function stableJson(value) {
    function normalized(item) {
      if (Array.isArray(item)) return item.map(normalized);
      if (item && typeof item === 'object') {
        return Object.keys(item).sort().reduce((result, key) => {
          result[key] = normalized(item[key]); return result;
        }, {});
      }
      return item;
    }
    return JSON.stringify(normalized(value));
  }

  function frozenJson(value) {
    const result = clone(value);
    function freeze(item) {
      if (!item || typeof item !== 'object' || Object.isFrozen(item)) return item;
      Object.values(item).forEach(freeze); return Object.freeze(item);
    }
    return freeze(result);
  }

  function safeAuthoringPublicValue(value, depth = 0) {
    if (depth > 16) return false;
    if (value === null || typeof value === 'boolean') return true;
    if (typeof value === 'number') return Number.isFinite(value);
    if (typeof value === 'string') {
      if (value.length > 4096) return false;
      if (/(?:[A-Za-z]:[\\/]|file:\s*\/|\\\\[^\\\s]+\\)/i.test(value)) return false;
      if (/^\s*\//.test(value) || /\\/.test(value) ||
          /(?:^|[\\/])\.\.(?:[\\/]|$)/.test(value) ||
          (value.match(/\//g) || []).length > 1) return false;
      return !/(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{8,}|-----BEGIN[^\n]{0,40}PRIVATE KEY-----|Bearer\s+\S+/i.test(value);
    }
    if (Array.isArray(value)) {
      return value.length <= 2048 && value.every(item => safeAuthoringPublicValue(item, depth + 1));
    }
    if (!value || typeof value !== 'object' || Object.keys(value).length > 512) return false;
    const denied = new Set([
      'path', 'paths', 'root', 'roots', ['project', 'path'].join('_'),
      ['project', 'root'].join('_'),
      'evidence_bytes', 'raw_evidence', 'raw_domain', 'credential', 'credentials',
      'artifact_bytes', 'directory', 'dir', 'locator', 'source_job',
      'password', 'secret', 'api_key', 'token',
    ]);
    return Object.entries(value).every(([key, item]) =>
      !denied.has(String(key).toLowerCase()) && safeAuthoringPublicValue(item, depth + 1));
  }

  function clearNode(node) {
    if (!node) return;
    node.innerHTML = '';
    if (Array.isArray(node.children)) node.children.length = 0;
    if (Array.isArray(node.options)) node.options.length = 0;
  }

  function authoringAxis(kind) {
    return kind === 'network' ? State.networkAuthoring : State.modelAuthoring;
  }

  function authoringStatusText(status) {
    const labels = {
      missing: tr('analysis.authoring.status.missing', 'missing'),
      missing_prerequisite: tr(
        'analysis.authoring.status.missing_prerequisite', 'missing prerequisite'),
      unavailable: tr('analysis.authoring.status.unavailable', 'unavailable'),
      blocked: tr('analysis.authoring.status.blocked', 'blocked'),
      conflict: tr('analysis.authoring.status.conflict', 'conflict'),
      ready: tr('analysis.authoring.status.ready', 'ready'),
    };
    return labels[AUTHORING_STATUSES.has(status) ? status : 'unavailable'];
  }

  function setAuthoringStatus(kind, status, message = '', tone = '', focus = false) {
    const axis = authoringAxis(kind);
    const normalized = AUTHORING_STATUSES.has(status) ? status : 'unavailable';
    axis.status = normalized; axis.message = String(message || '');
    const statusNode = $(`aw-${kind}-authoring-status`);
    if (statusNode) {
      statusNode.dataset.status = normalized;
      statusNode.textContent = authoringStatusText(normalized);
    }
    const operationNode = $(`aw-${kind}-operation`);
    if (operationNode) {
      operationNode.textContent = axis.message || authoringStatusText(normalized);
      operationNode.className = 'aw-authoring-operation' + (tone ? ` ${tone}` : '');
      if (focus && typeof operationNode.focus === 'function') operationNode.focus();
    }
  }

  function resetAuthoring(projectId = '', analysisId = '') {
    State.authoringEpoch += 1;
    State.authoringProjectId = safeId(projectId);
    State.authoringAnalysisId = safeId(analysisId);
    State.authoringBootstrap = null;
    State.authoringBootstrapStatus = 'missing';
    State.networkAuthoring = freshAuthoringAxis();
    State.modelAuthoring = freshAuthoringAxis();
    State.modelAuthoringControls = Object.create(null);
    if ($('aw-network-candidate')) $('aw-network-candidate').value = '';
    if ($('aw-network-ack')) $('aw-network-ack').checked = false;
    if ($('aw-model-ack')) $('aw-model-ack').checked = false;
    renderNetworkBootstrap(); renderModelCatalog();
    renderList('aw-model-issues', [], tr('analysis.authoring.not_loaded', '尚未读取。'));
    renderAuthoring();
    return State.authoringEpoch;
  }

  function invalidateAuthoring(kind, { dirty = true } = {}) {
    const axis = authoringAxis(kind);
    axis.intentGeneration += 1;
    axis.dirty = dirty;
    axis.previewReceipt = null; axis.previewRequest = null; axis.intentId = '';
    axis.acknowledged = false;
    axis.conflict = null; axis.lastResult = null;
    const checkbox = $(`aw-${kind}-ack`); if (checkbox) checkbox.checked = false;
    const nextStatus = ['missing', 'missing_prerequisite', 'unavailable'].includes(axis.status) ?
      axis.status : 'blocked';
    setAuthoringStatus(kind, nextStatus,
      dirty ? tr('analysis.authoring.changed', '表单已变化；必须重新预览并确认。') : '',
      dirty ? 'bad' : '');
    renderAuthoringControls();
  }

  function newAuthoringIntent(kind, generation) {
    State.authoringIntentCounter += 1;
    return safeId(`aw-${kind}-${State.authoringEpoch}-${generation}-${State.authoringIntentCounter}`);
  }

  function authoringOwner(kind, draft, intentId) {
    const axis = authoringAxis(kind);
    return Object.freeze({
      kind, projectId: State.authoringProjectId, analysisId: State.authoringAnalysisId,
      epoch: State.authoringEpoch, generation: axis.intentGeneration,
      draftText: stableJson(draft), intentId,
    });
  }

  function currentAuthoringDraftText(kind) {
    try {
      const draft = kind === 'network' ? networkDraftFromControls() : modelDraftFromControls();
      return stableJson(draft);
    } catch (_) { return ''; }
  }

  function authoringOwnerIsCurrent(owner) {
    const axis = authoringAxis(owner.kind);
    return owner.projectId === State.authoringProjectId &&
      owner.analysisId === State.authoringAnalysisId &&
      owner.epoch === State.authoringEpoch &&
      owner.generation === axis.intentGeneration &&
      owner.intentId === axis.intentId &&
      owner.draftText === currentAuthoringDraftText(owner.kind) &&
      sameProject(owner.projectId);
  }

  function settleAuthoringFlight(kind, axis, owner, field) {
    if (authoringAxis(kind) !== axis) return;
    if (axis.intentId === owner.intentId && !authoringOwnerIsCurrent(owner)) {
      invalidateAuthoring(kind);
    }
    axis[field] = false; renderAuthoringControls();
  }

  function authoringInvalid() {
    return new Error(tr(
      'analysis.authoring.invalid_response',
      'Authoring 服务返回了不完整或不安全的响应；本次操作已按 unavailable 关闭。'));
  }

  function requireAuthoring(condition) {
    if (!condition) throw authoringInvalid();
  }

  function validAuthoringId(value) {
    return typeof value === 'string' && safeAuthoringId(value) === value;
  }
  function validAuthority(value) { return typeof value === 'string' && /^[a-f0-9]{32}$/.test(value); }
  function validInteger(value, minimum = 0) {
    return Number.isInteger(value) && value >= minimum && value <= Number.MAX_SAFE_INTEGER;
  }
  function validIdArray(value, { nonempty = false } = {}) {
    return Array.isArray(value) && (!nonempty || value.length > 0) && value.length <= 512 &&
      value.every(validAuthoringId) && new Set(value).size === value.length;
  }

  function validateBindingRecord(value) {
    const keys = [
      'schema', 'authority_id', 'revision', 'parent_revision', 'expected_current_hash',
      'domain_authority_id', 'domain_generation', 'domain_snapshot_sha256', 'network_id',
      'network_revision_id', 'network_semantic_sha256', 'intent_id', 'confirmed',
      'job_source_of_truth', 'authorizes_execution', 'binding_sha256',
    ];
    requireAuthoring(exactObject(value, keys));
    requireAuthoring(value.schema === 'vcstudio.catalysis-active-network-binding/v1');
    requireAuthoring(validAuthority(value.authority_id) && validInteger(value.revision, 1));
    requireAuthoring((value.revision === 1 && value.parent_revision === null &&
      value.expected_current_hash === null) || (value.revision > 1 &&
      value.parent_revision === value.revision - 1 && safeSha(value.expected_current_hash)));
    requireAuthoring(validAuthority(value.domain_authority_id) &&
      validInteger(value.domain_generation, 1) && safeSha(value.domain_snapshot_sha256));
    requireAuthoring(validAuthoringId(value.network_id) &&
      validAuthoringId(value.network_revision_id) && safeSha(value.network_semantic_sha256));
    requireAuthoring(validAuthoringId(value.intent_id) && value.confirmed === true &&
      value.job_source_of_truth === 'job.yaml' && value.authorizes_execution === false &&
      safeSha(value.binding_sha256));
    return clone(value);
  }

  function validateBindingCAS(value) {
    requireAuthoring(exactObject(value, [
      'schema', 'authority_id', 'revision', 'current_hash', 'binding', 'snapshot_sha256',
    ]));
    requireAuthoring(value.schema === 'vcstudio.catalysis-binding-authority-snapshot/v1' &&
      validInteger(value.revision) && safeSha(value.snapshot_sha256));
    if (value.revision === 0) {
      requireAuthoring(value.authority_id === null && value.current_hash === null &&
        value.binding === null);
    } else {
      requireAuthoring(validAuthority(value.authority_id) && safeSha(value.current_hash));
      const binding = validateBindingRecord(value.binding);
      requireAuthoring(binding.authority_id === value.authority_id &&
        binding.revision === value.revision && binding.binding_sha256 === value.current_hash);
    }
    return clone(value);
  }

  function validateNetworkCAS(value) {
    requireAuthoring(exactObject(value, [
      'schema', 'domain_authority_id', 'domain_generation', 'domain_snapshot_sha256',
      'network_id', 'network_status', 'network_revision_id', 'network_semantic_sha256',
      'snapshot_sha256',
    ]));
    requireAuthoring(value.schema === 'vcstudio.catalysis-network-head-cas/v1' &&
      validAuthority(value.domain_authority_id) && validInteger(value.domain_generation, 1) &&
      safeSha(value.domain_snapshot_sha256) && validAuthoringId(value.network_id) &&
      ['available', 'missing', 'wrong_type'].includes(value.network_status) &&
      safeSha(value.snapshot_sha256));
    if (value.network_status === 'available') {
      requireAuthoring(validAuthoringId(value.network_revision_id) &&
        safeSha(value.network_semantic_sha256));
    } else {
      requireAuthoring(value.network_revision_id === null && value.network_semantic_sha256 === null);
    }
    return clone(value);
  }

  function validateGapSummary(value) {
    requireAuthoring(exactObject(value, ['total', 'returned', 'omitted', 'by_code']));
    requireAuthoring(validInteger(value.total) && validInteger(value.returned) &&
      validInteger(value.omitted) && value.total === value.returned + value.omitted &&
      value.by_code && typeof value.by_code === 'object' && !Array.isArray(value.by_code));
    const entries = Object.entries(value.by_code);
    requireAuthoring(entries.length <= 256 && entries.every(([code, count]) =>
      validAuthoringId(code) && validInteger(count, 1)) &&
      entries.reduce((total, [, count]) => total + count, 0) === value.total);
    return clone(value);
  }

  function validateGap(value) {
    requireAuthoring(exactObject(value, ['status', 'code', 'object_type', 'object_id', 'context']));
    requireAuthoring(['missing', 'unavailable', 'blocked'].includes(value.status) &&
      validAuthoringId(value.code) && validAuthoringId(value.object_type) &&
      (value.object_id === null || validAuthoringId(value.object_id)) &&
      value.context && typeof value.context === 'object' && !Array.isArray(value.context) &&
      safeAuthoringPublicValue(value.context));
    return clone(value);
  }

  function validateNetworkCandidate(value, domainCAS) {
    requireAuthoring(exactObject(value, [
      'schema', 'network_head', 'projection_status', 'gap_summary', 'closure_sha256',
    ]));
    requireAuthoring(value.schema === 'vcstudio.catalysis-authoring-network-candidate/v1');
    const head = validateNetworkCAS(value.network_head);
    const summary = validateGapSummary(value.gap_summary);
    requireAuthoring(head.network_status === 'available' &&
      ['available', 'unavailable'].includes(value.projection_status) &&
      safeSha(value.closure_sha256) &&
      (value.projection_status === 'available') === (summary.total === 0));
    if (domainCAS) requireAuthoring(head.domain_authority_id === domainCAS.authority_id &&
      head.domain_generation === domainCAS.generation &&
      head.domain_snapshot_sha256 === domainCAS.snapshot_sha256);
    return clone(value);
  }

  function validateNetworkBootstrap(value, projectId) {
    requireAuthoring(exactObject(value, [
      'schema', 'project_id', 'status', 'reason', 'domain_cas', 'binding_cas', 'candidates',
      'active_network_id', 'active_status', 'snapshot_sha256',
    ]));
    requireAuthoring(value.schema === 'vcstudio.catalysis-authoring-bootstrap/v1' &&
      value.project_id === projectId && ['available', 'missing', 'unavailable'].includes(value.status) &&
      (value.reason === null || validAuthoringId(value.reason)) && safeSha(value.snapshot_sha256));
    const binding = validateBindingCAS(value.binding_cas);
    let domain = null;
    if (value.domain_cas !== null) {
      requireAuthoring(exactObject(value.domain_cas,
        ['authority_id', 'generation', 'snapshot_sha256']));
      requireAuthoring(validAuthority(value.domain_cas.authority_id) &&
        validInteger(value.domain_cas.generation, 1) && safeSha(value.domain_cas.snapshot_sha256));
      domain = clone(value.domain_cas);
    }
    requireAuthoring(Array.isArray(value.candidates) && value.candidates.length <= 512);
    const candidates = value.candidates.map(item => validateNetworkCandidate(item, domain));
    const ids = candidates.map(item => item.network_head.network_id);
    requireAuthoring(new Set(ids).size === ids.length &&
      ids.every((id, index) => index === 0 || ids[index - 1] < id));
    requireAuthoring(['none', 'available', 'missing', 'stale'].includes(value.active_status) &&
      (value.active_network_id === null || validAuthoringId(value.active_network_id)));
    if (value.status === 'available') requireAuthoring(domain !== null && value.reason === null);
    else requireAuthoring(value.reason !== null);
    if (binding.binding === null) {
      requireAuthoring(value.active_network_id === null && value.active_status === 'none');
    } else {
      const activeId = binding.binding.network_id;
      const activeHead = candidates.find(item => item.network_head.network_id === activeId);
      let derivedStatus = 'missing';
      if (activeHead) {
        const head = activeHead.network_head;
        derivedStatus = domain && binding.binding.domain_authority_id === domain.authority_id &&
          binding.binding.domain_generation === domain.generation &&
          binding.binding.domain_snapshot_sha256 === domain.snapshot_sha256 &&
          binding.binding.network_revision_id === head.network_revision_id &&
          binding.binding.network_semantic_sha256 === head.network_semantic_sha256 ?
          'available' : 'stale';
      }
      requireAuthoring(value.active_network_id === activeId && value.active_status === derivedStatus);
    }
    return { ...clone(value), binding_cas: binding, domain_cas: domain, candidates };
  }

  function validateChoiceCatalog(value) {
    requireAuthoring(exactObject(value, [
      'schema', 'rate_law_policy', 'assumptions', 'feed_reservoir_units',
      'site_total_units', 'site_total_bases', 'prefactor_units',
    ]) && value.schema === 'vcstudio.kinetics-authoring-choice-catalog/v1');
    requireAuthoring(exactObject(value.rate_law_policy, RATE_POLICY_KEYS));
    requireAuthoring(exactObject(value.assumptions, [
      'mean_field', 'steady_state', ...ASSUMPTION_POLICY_KEYS,
    ]));
    const expectedRatePolicy = {
      activity: ['ideal'], reversibility: ['explicit_reverse'],
      detailed_balance: ['enforced'], prefactor: ['explicit_per_step'],
      electrochemical: ['none'], reactor: ['mean_field_steady_state'],
    };
    RATE_POLICY_KEYS.forEach(key => requireAuthoring(
      stableJson(value.rate_law_policy[key]) === stableJson(expectedRatePolicy[key])));
    const expectedAssumptions = {
      mean_field: [true], steady_state: [true], site_uniformity: ['uniform'],
      lateral_interactions: ['neglected', 'parameterized'],
      mechanism_completeness: ['claimed_complete', 'partial', 'unknown'],
    };
    ASSUMPTION_POLICY_KEYS.forEach(key => requireAuthoring(
      stableJson(value.assumptions[key]) === stableJson(expectedAssumptions[key])));
    ['mean_field', 'steady_state'].forEach(key => requireAuthoring(
      Array.isArray(value.assumptions[key]) && value.assumptions[key].length > 0 &&
      value.assumptions[key].every(item => typeof item === 'boolean')));
    requireAuthoring(stableJson(value.feed_reservoir_units) ===
      stableJson(['bar', 'mol/L', 'dimensionless']));
    requireAuthoring(stableJson(value.site_total_units) === stableJson(['sites', 'dimensionless']));
    requireAuthoring(stableJson(value.site_total_bases) ===
      stableJson(['surface_unit_cell', 'normalized_site_population']));
    requireAuthoring(stableJson(value.prefactor_units) ===
      stableJson(['s^-1', 'bar^-1 s^-1', 'mol^-1 L s^-1']));
    return clone(value);
  }

  function validatePublicSource(value) {
    requireAuthoring(exactObject(value, [
      'status', 'required_feed_reservoir_ids', 'allowed_target_product_ids',
      'required_step_ids', 'required_site_type_ids', 'saddle_candidates_by_step',
      'evidence_catalog', 'solver_ready', 'solver_readiness_reasons',
    ]));
    requireAuthoring(['available', 'missing', 'missing_prerequisite', 'unavailable', 'blocked']
      .includes(value.status));
    ['required_feed_reservoir_ids', 'allowed_target_product_ids', 'required_step_ids',
      'required_site_type_ids'].forEach(key => requireAuthoring(
        validIdArray(value[key], { nonempty: true })));
    requireAuthoring(value.saddle_candidates_by_step &&
      typeof value.saddle_candidates_by_step === 'object' &&
      !Array.isArray(value.saddle_candidates_by_step));
    Object.entries(value.saddle_candidates_by_step).forEach(([step, candidates]) => {
      requireAuthoring(validAuthoringId(step) && value.required_step_ids.includes(step) &&
        validIdArray(candidates));
    });
    requireAuthoring(Array.isArray(value.evidence_catalog) && value.evidence_catalog.length <= 256);
    const evidenceIds = [];
    value.evidence_catalog.forEach(item => {
      requireAuthoring(exactObject(item, ['reference_id', 'kind']) &&
        validAuthoringId(item.reference_id) && validAuthoringId(item.kind));
      evidenceIds.push(item.reference_id);
    });
    requireAuthoring(new Set(evidenceIds).size === evidenceIds.length &&
      typeof value.solver_ready === 'boolean' && validIdArray(value.solver_readiness_reasons));
    if (value.solver_ready) requireAuthoring(value.solver_readiness_reasons.length === 0);
    else requireAuthoring(value.solver_readiness_reasons.length > 0);
    return clone(value);
  }

  function validateKineticsBootstrap(value) {
    requireAuthoring(exactObject(value,
      ['status', 'reason', 'source', 'model_store', 'selector', 'choice_catalog']));
    requireAuthoring(['available', 'missing', 'missing_prerequisite', 'unavailable', 'blocked',
      'conflict']
      .includes(value.status) && (value.reason === null || validAuthoringId(value.reason)));
    const source = value.source === null ? null : validatePublicSource(value.source);
    requireAuthoring(exactObject(value.model_store, ['status']) &&
      ['available', 'unavailable'].includes(value.model_store.status));
    requireAuthoring(exactObject(value.selector, ['status', 'migration_state', 'selected']) &&
      ['none', 'selected', 'legacy_read_only', 'unavailable'].includes(value.selector.status) &&
      ['none', 'legacy_v1_read_only'].includes(value.selector.migration_state));
    if (value.selector.selected !== null) {
      requireAuthoring(exactObject(value.selector.selected,
        ['project_id', 'spec_id', 'spec_revision']) &&
        validAuthoringId(value.selector.selected.project_id) &&
        validAuthoringId(value.selector.selected.spec_id) &&
        validAuthoringId(value.selector.selected.spec_revision));
    }
    if (value.selector.migration_state === 'legacy_v1_read_only') {
      requireAuthoring(value.selector.status === 'legacy_read_only' &&
        value.selector.selected === null);
    } else if (value.selector.selected === null) {
      requireAuthoring(['none', 'unavailable'].includes(value.selector.status));
    } else {
      requireAuthoring(value.selector.status === 'selected');
    }
    const catalog = validateChoiceCatalog(value.choice_catalog);
    if (value.status === 'available') requireAuthoring(
      value.reason === null && source !== null && source.status === 'available');
    else requireAuthoring(value.reason !== null);
    if (value.status === 'conflict') requireAuthoring(source === null);
    return { ...clone(value), source, choice_catalog: catalog };
  }

  function validateSharedAuthoringAuthority(value, active) {
    if (value === null) {
      requireAuthoring(active.domain_cas === null);
      return null;
    }
    requireAuthoring(exactObject(value, [
      'schema', 'domain_authority_id', 'domain_generation', 'domain_snapshot_sha256',
      'network_id', 'network_revision_id', 'network_semantic_sha256',
    ]) && value.schema === 'vcstudio.catalysis-authoring-shared-authority/v1' &&
      validAuthority(value.domain_authority_id) && validInteger(value.domain_generation, 1) &&
      safeSha(value.domain_snapshot_sha256) && active.domain_cas !== null &&
      value.domain_authority_id === active.domain_cas.authority_id &&
      value.domain_generation === active.domain_cas.generation &&
      value.domain_snapshot_sha256 === active.domain_cas.snapshot_sha256);
    const networkValues = [
      value.network_id, value.network_revision_id, value.network_semantic_sha256,
    ];
    const networkIsNull = networkValues.every(item => item === null);
    const networkIsBound = validAuthoringId(value.network_id) &&
      validAuthoringId(value.network_revision_id) && safeSha(value.network_semantic_sha256);
    requireAuthoring(networkIsNull || networkIsBound);
    const binding = active.binding_cas.binding;
    if (binding === null) requireAuthoring(networkIsNull);
    else requireAuthoring(networkIsBound &&
      value.network_id === binding.network_id &&
      value.network_revision_id === binding.network_revision_id &&
      value.network_semantic_sha256 === binding.network_semantic_sha256);
    if (binding !== null && active.active_status === 'available') requireAuthoring(
      value.domain_authority_id === binding.domain_authority_id &&
      value.domain_generation === binding.domain_generation &&
      value.domain_snapshot_sha256 === binding.domain_snapshot_sha256);
    return clone(value);
  }

  function validateBootstrapEnvelope(value, projectId) {
    requireAuthoring(safeAuthoringPublicValue(value) && exactObject(value, [
      'ok', 'schema', 'project_id', 'bootstrap_status', 'reason', 'shared_authority',
      'active_network', 'kinetics_authoring', 'error_code', 'error_field', 'error',
    ]));
    requireAuthoring(value.schema === 'vcstudio.catalysis-authoring-bootstrap-api/v1' &&
      value.project_id === projectId && typeof value.ok === 'boolean');
    if (value.ok !== true) throw authoringInvalid();
    requireAuthoring(value.error_code === null && value.error_field === null &&
      value.error === null && AUTHORING_BOOTSTRAP_STATUSES.has(value.bootstrap_status) &&
      (value.reason === null || validAuthoringId(value.reason)) &&
      ((value.bootstrap_status === 'available') === (value.reason === null)));
    const active = validateNetworkBootstrap(value.active_network, projectId);
    const kinetics = validateKineticsBootstrap(value.kinetics_authoring);
    const shared = validateSharedAuthoringAuthority(value.shared_authority, active);
    if (value.bootstrap_status === 'available') requireAuthoring(
      active.status === 'available' && active.active_status === 'available' &&
      kinetics.status === 'available' && shared !== null && shared.network_id !== null);
    if (value.bootstrap_status === 'conflict') requireAuthoring(
      active.active_status === 'stale' && kinetics.status === 'conflict' &&
      shared !== null && shared.network_id !== null);
    return {
      bootstrap_status: value.bootstrap_status,
      reason: value.reason,
      shared_authority: shared,
      active_network: active,
      kinetics_authoring: kinetics,
    };
  }

  function validateNetworkPreviewEnvelope(value, projectId, expectedRequest) {
    const networkId = expectedRequest.network_id; const intentId = expectedRequest.intent_id;
    const expectedBinding = validateBindingCAS(expectedRequest.expected_binding);
    const expectedNetwork = validateNetworkCAS(expectedRequest.expected_network_head);
    requireAuthoring(safeAuthoringPublicValue(value) && exactObject(value, [
      'ok', 'schema', 'project_id', 'preview', 'error_code', 'error_field', 'error',
    ]) && value.ok === true &&
      value.schema === 'vcstudio.catalysis-active-network-preview-api/v1' &&
      value.project_id === projectId && value.error_code === null &&
      value.error_field === null && value.error === null);
    const preview = value.preview;
    requireAuthoring(exactObject(preview, [
      'schema', 'project_id', 'network_id', 'intent_id', 'request_sha256', 'status',
      'reason', 'action', 'binding_cas', 'network_cas', 'projection_status', 'gaps',
      'gap_summary', 'closure_sha256', 'issued_at_ms', 'expires_at_ms', 'preview_sha256',
      'can_confirm', 'confirmed', 'authorizes_execution',
    ]) && preview.schema === 'vcstudio.catalysis-active-network-preview/v1' &&
      preview.project_id === projectId && preview.network_id === networkId &&
      preview.intent_id === intentId && safeSha(preview.request_sha256) &&
      ['ready', 'conflict', 'missing', 'unavailable'].includes(preview.status) &&
      (preview.reason === null || validAuthoringId(preview.reason)) &&
      [null, 'create', 'advance'].includes(preview.action) &&
      ['available', 'unavailable'].includes(preview.projection_status) &&
      safeSha(preview.closure_sha256) && validInteger(preview.issued_at_ms) &&
      validInteger(preview.expires_at_ms) && preview.expires_at_ms > preview.issued_at_ms &&
      preview.expires_at_ms - preview.issued_at_ms <= 900000 &&
      safeSha(preview.preview_sha256) && typeof preview.can_confirm === 'boolean' &&
      preview.confirmed === false && preview.authorizes_execution === false);
    const binding = validateBindingCAS(preview.binding_cas);
    const network = validateNetworkCAS(preview.network_cas);
    requireAuthoring(network.network_id === networkId && Array.isArray(preview.gaps) &&
      preview.gaps.length <= 64);
    preview.gaps.forEach(validateGap); const summary = validateGapSummary(preview.gap_summary);
    requireAuthoring(summary.returned === preview.gaps.length &&
      (preview.projection_status === 'available') === (summary.total === 0) &&
      preview.can_confirm === (preview.status === 'ready') &&
      (!preview.can_confirm || preview.action !== null));
    if (preview.status === 'ready') requireAuthoring(
      preview.reason === null && preview.projection_status === 'available' &&
      stableJson(binding) === stableJson(expectedBinding) &&
      stableJson(network) === stableJson(expectedNetwork));
    else requireAuthoring(preview.reason !== null);
    return clone(preview);
  }

  function validateNetworkConflict(value) {
    requireAuthoring(exactObject(value, [
      'schema', 'reason', 'latest_binding_cas', 'latest_network_cas', 'retry_automatically',
    ]) && value.schema === 'vcstudio.catalysis-active-network-conflict/v1' &&
      validAuthoringId(value.reason) && value.retry_automatically === false);
    validateBindingCAS(value.latest_binding_cas);
    if (value.latest_network_cas !== null) validateNetworkCAS(value.latest_network_cas);
    return clone(value);
  }

  function validateNetworkConfirmEnvelope(value, projectId, expectedRequest) {
    const expectedBinding = validateBindingCAS(expectedRequest.expected_binding);
    const expectedNetwork = validateNetworkCAS(expectedRequest.expected_network_head);
    requireAuthoring(safeAuthoringPublicValue(value) && exactObject(value, [
      'ok', 'schema', 'project_id', 'result', 'error_code', 'error_field', 'error',
    ]) && value.ok === true &&
      value.schema === 'vcstudio.catalysis-active-network-confirm-api/v1' &&
      value.project_id === projectId && value.error_code === null &&
      value.error_field === null && value.error === null);
    const result = value.result;
    requireAuthoring(exactObject(result,
      ['schema', 'action', 'binding_cas', 'network_cas', 'conflict', 'reason']) &&
      result.schema === 'vcstudio.catalysis-active-network-confirm-result/v1' &&
      ['created', 'advanced', 'replayed', 'conflict', 'unavailable'].includes(result.action) &&
      (result.reason === null || validAuthoringId(result.reason)));
    const binding = result.binding_cas === null ? null : validateBindingCAS(result.binding_cas);
    const network = result.network_cas === null ? null : validateNetworkCAS(result.network_cas);
    if (result.action === 'conflict') requireAuthoring(result.conflict !== null);
    if (result.conflict !== null) validateNetworkConflict(result.conflict);
    if (['created', 'advanced', 'replayed'].includes(result.action)) {
      requireAuthoring(binding !== null && binding.binding !== null && network !== null &&
        network.network_status === 'available' && result.conflict === null && result.reason === null &&
        network.network_id === expectedRequest.network_id &&
        stableJson(network) === stableJson(expectedNetwork) &&
        binding.binding.network_id === expectedRequest.network_id &&
        binding.binding.intent_id === expectedRequest.intent_id &&
        binding.revision === expectedBinding.revision + 1 &&
        binding.binding.parent_revision ===
          (expectedBinding.revision === 0 ? null : expectedBinding.revision) &&
        binding.binding.expected_current_hash === expectedBinding.current_hash &&
        binding.binding.domain_authority_id === network.domain_authority_id &&
        binding.binding.domain_generation === network.domain_generation &&
        binding.binding.domain_snapshot_sha256 === network.domain_snapshot_sha256 &&
        binding.binding.network_revision_id === network.network_revision_id &&
        binding.binding.network_semantic_sha256 === network.network_semantic_sha256);
      if (expectedBinding.revision > 0) requireAuthoring(
        binding.authority_id === expectedBinding.authority_id);
      if (result.action === 'created') requireAuthoring(binding.revision === 1);
      if (result.action === 'advanced') requireAuthoring(binding.revision > 1);
    }
    if (result.action === 'unavailable') requireAuthoring(result.reason !== null);
    return clone(result);
  }

  function validateSourceCAS(value, projectId) {
    requireAuthoring(exactObject(value, [
      'schema', 'project_id', 'domain_authority_id', 'domain_generation',
      'domain_snapshot_sha256', 'network_id', 'network_revision',
      'network_semantic_sha256', 'source_projection_sha256', 'snapshot_sha256',
    ]) && value.schema === 'vcstudio.kinetics-authoring-source-cas/v1' &&
      value.project_id === projectId && validAuthority(value.domain_authority_id) &&
      validInteger(value.domain_generation) && safeSha(value.domain_snapshot_sha256) &&
      validAuthoringId(value.network_id) && validAuthoringId(value.network_revision) &&
      safeSha(value.network_semantic_sha256) && safeSha(value.source_projection_sha256) &&
      safeSha(value.snapshot_sha256));
    return clone(value);
  }

  function validateStoreCAS(value) {
    requireAuthoring(exactObject(value, [
      'schema', 'authority_id', 'generation', 'heads', 'chain_sha256', 'store_sha256',
      'anchor_chain_sha256', 'snapshot_sha256',
    ]) && value.schema === 'vcstudio.kinetics-model-spec-store-snapshot/v1' &&
      validAuthority(value.authority_id) && validInteger(value.generation) &&
      Array.isArray(value.heads) && value.heads.length <= 512 &&
      safeSha(value.chain_sha256) && safeSha(value.store_sha256) &&
      safeSha(value.anchor_chain_sha256) && safeSha(value.snapshot_sha256));
    value.heads.forEach(head => requireAuthoring(exactObject(head,
      ['project_id', 'spec_id', 'revision', 'current_hash']) &&
      validAuthoringId(head.project_id) && validAuthoringId(head.spec_id) &&
      validAuthoringId(head.revision) && safeSha(head.current_hash)));
    return clone(value);
  }

  function validateSelectorCAS(value) {
    requireAuthoring(exactObject(value, [
      'schema', 'authority_id', 'revision', 'current_hash', 'selection', 'migration_state',
      'legacy_selection_sha256', 'snapshot_sha256',
    ]) && value.schema === 'vcstudio.kinetics-active-spec-selector-snapshot/v2' &&
      validInteger(value.revision) && ['none', 'legacy_v1_read_only'].includes(value.migration_state) &&
      safeSha(value.snapshot_sha256));
    if (value.revision === 0) requireAuthoring(value.authority_id === null &&
      value.current_hash === null && value.selection === null && value.legacy_selection_sha256 === null);
    else requireAuthoring(validAuthority(value.authority_id) && safeSha(value.current_hash) &&
      (value.selection === null || safeAuthoringPublicValue(value.selection)));
    if (value.legacy_selection_sha256 !== null) requireAuthoring(safeSha(value.legacy_selection_sha256));
    return clone(value);
  }

  function validateIssue(value) {
    requireAuthoring(exactObject(value, ['code', 'path', 'message']) &&
      validAuthoringId(value.code) && typeof value.path === 'string' &&
      /^\$[A-Za-z0-9_.\[\]-]{0,511}$/.test(value.path) &&
      typeof value.message === 'string' && value.message.length <= 512 &&
      safeAuthoringPublicValue(value.message));
    return { code: value.code, path: value.path };
  }

  function validateModelPreviewEnvelope(value, projectId, intentId, draft, sharedAuthority) {
    requireAuthoring(exactObject(value, [
      'ok', 'schema', 'project_id', 'preview', 'error_code', 'error_field', 'error',
    ]) && value.ok === true && value.schema === 'vcstudio.kinetics-model-spec-preview-api/v1' &&
      value.project_id === projectId && value.error_code === null &&
      value.error_field === null && value.error === null);
    const preview = value.preview;
    requireAuthoring(exactObject(preview, [
      'schema', 'intent_id', 'draft_sha256', 'preview_sha256', 'source_cas', 'store_cas',
      'selector_cas', 'target_spec', 'issues', 'can_confirm', 'solver_ready',
      'solver_readiness_reasons', 'authorizes_execution',
    ]) && preview.schema === 'vcstudio.kinetics-authoring-preview/v1' &&
      preview.intent_id === intentId && safeSha(preview.draft_sha256) &&
      safeSha(preview.preview_sha256) && Array.isArray(preview.issues) &&
      preview.issues.length <= 64 && typeof preview.can_confirm === 'boolean' &&
      typeof preview.solver_ready === 'boolean' && validIdArray(preview.solver_readiness_reasons) &&
      preview.authorizes_execution === false);
    const issues = preview.issues.map(validateIssue);
    const sourceCAS = preview.source_cas === null ? null : validateSourceCAS(preview.source_cas, projectId);
    const storeCAS = preview.store_cas === null ? null : validateStoreCAS(preview.store_cas);
    const selectorCAS = preview.selector_cas === null ? null : validateSelectorCAS(preview.selector_cas);
    if (preview.target_spec !== null) requireAuthoring(exactObject(preview.target_spec,
      ['project_id', 'spec_id', 'revision', 'spec_sha256']) &&
      preview.target_spec.project_id === projectId &&
      preview.target_spec.spec_id === draft.spec_id &&
      validAuthoringId(preview.target_spec.revision) && safeSha(preview.target_spec.spec_sha256));
    if (preview.can_confirm) requireAuthoring(issues.length === 0 && sourceCAS && storeCAS &&
      selectorCAS && preview.target_spec !== null);
    if (sourceCAS !== null) requireAuthoring(sharedAuthority !== null &&
      sourceCAS.domain_authority_id === sharedAuthority.domain_authority_id &&
      sourceCAS.domain_generation === sharedAuthority.domain_generation &&
      sourceCAS.domain_snapshot_sha256 === sharedAuthority.domain_snapshot_sha256 &&
      sourceCAS.network_id === sharedAuthority.network_id &&
      sourceCAS.network_revision === sharedAuthority.network_revision_id &&
      sourceCAS.network_semantic_sha256 === sharedAuthority.network_semantic_sha256);
    if (preview.solver_ready) requireAuthoring(preview.solver_readiness_reasons.length === 0);
    return { ...clone(preview), issues, source_cas: sourceCAS,
      store_cas: storeCAS, selector_cas: selectorCAS };
  }

  function validateModelConflict(value, projectId) {
    requireAuthoring(exactObject(value, [
      'schema', 'reason', 'latest_source_cas', 'latest_store_cas',
      'latest_selector_cas', 'retry_automatically',
    ]) && value.schema === 'vcstudio.kinetics-authoring-conflict/v1' &&
      validAuthoringId(value.reason) && value.retry_automatically === false);
    if (value.latest_source_cas !== null) validateSourceCAS(value.latest_source_cas, projectId);
    if (value.latest_store_cas !== null) validateStoreCAS(value.latest_store_cas);
    if (value.latest_selector_cas !== null) validateSelectorCAS(value.latest_selector_cas);
    return clone(value);
  }

  function validateCommittedModelResult(result, projectId, draft, preview) {
    const spec = result.spec; const selector = result.selector; const receipt = result.receipt;
    requireAuthoring(exactObject(spec, [
      'schema', 'project_id', 'spec_id', 'revision', 'parent_revision',
      'expected_current_hash', 'source_binding', 'rate_law_policy', 'assumptions',
      'feed_reservoirs', 'target_products', 'steps', 'site_population_totals',
      'saddle_selector',
    ]) && spec.schema === 'vcstudio.kinetics-model-spec/v1' &&
      spec.project_id === projectId && spec.spec_id === draft.spec_id &&
      validAuthoringId(spec.revision) &&
      (spec.parent_revision === null || validAuthoringId(spec.parent_revision)) &&
      (spec.expected_current_hash === null || safeSha(spec.expected_current_hash)) &&
      spec.source_binding && typeof spec.source_binding === 'object' &&
      spec.rate_law_policy && typeof spec.rate_law_policy === 'object' &&
      spec.assumptions && typeof spec.assumptions === 'object' &&
      Array.isArray(spec.feed_reservoirs) && Array.isArray(spec.target_products) &&
      Array.isArray(spec.steps) && Array.isArray(spec.site_population_totals) &&
      (spec.saddle_selector === null || typeof spec.saddle_selector === 'object'));
    requireAuthoring(exactObject(selector, [
      'schema', 'authority_id', 'revision', 'parent_revision', 'expected_current_hash',
      'project_id', 'spec_id', 'spec_revision', 'spec_sha256', 'intent_id',
      'transaction_id', 'confirmed', 'selection_sha256',
    ]) && selector.schema === 'vcstudio.kinetics-active-spec-selection/v2' &&
      validAuthority(selector.authority_id) && validInteger(selector.revision, 1) &&
      (selector.parent_revision === null || validInteger(selector.parent_revision, 1)) &&
      (selector.expected_current_hash === null || safeSha(selector.expected_current_hash)) &&
      selector.project_id === projectId && selector.spec_id === draft.spec_id &&
      selector.spec_revision === spec.revision && safeSha(selector.spec_sha256) &&
      selector.intent_id === preview.intent_id && validAuthoringId(selector.transaction_id) &&
      selector.confirmed === true && safeSha(selector.selection_sha256));
    requireAuthoring(exactObject(receipt, [
      'schema', 'outcome', 'transaction_id', 'intent_id', 'draft_sha256',
      'preview_sha256', 'request_sha256', 'source_snapshot_sha256',
      'base_store_snapshot_sha256', 'base_selector_snapshot_sha256', 'project_id',
      'spec_id', 'spec_revision', 'spec_sha256', 'selector_sha256', 'receipt_sha256',
    ]) && receipt.schema === 'vcstudio.kinetics-authoring-commit-receipt/v1' &&
      receipt.outcome === 'committed' && receipt.transaction_id === selector.transaction_id &&
      receipt.intent_id === preview.intent_id && receipt.draft_sha256 === preview.draft_sha256 &&
      receipt.preview_sha256 === preview.preview_sha256 && safeSha(receipt.request_sha256) &&
      receipt.source_snapshot_sha256 === preview.source_cas.snapshot_sha256 &&
      receipt.base_store_snapshot_sha256 === preview.store_cas.snapshot_sha256 &&
      receipt.base_selector_snapshot_sha256 === preview.selector_cas.snapshot_sha256 &&
      receipt.project_id === projectId && receipt.spec_id === draft.spec_id &&
      receipt.spec_revision === spec.revision && receipt.spec_sha256 === selector.spec_sha256 &&
      receipt.selector_sha256 === selector.selection_sha256 && safeSha(receipt.receipt_sha256));
    requireAuthoring(preview.target_spec && preview.target_spec.project_id === projectId &&
      preview.target_spec.spec_id === draft.spec_id &&
      preview.target_spec.revision === spec.revision &&
      preview.target_spec.spec_sha256 === receipt.spec_sha256);
  }

  function validateModelConfirmEnvelope(value, projectId, draft, preview) {
    requireAuthoring(safeAuthoringPublicValue(value) && exactObject(value, [
      'ok', 'schema', 'project_id', 'result', 'error_code', 'error_field', 'error',
    ]) && value.ok === true && value.schema === 'vcstudio.kinetics-model-spec-confirm-api/v1' &&
      value.project_id === projectId && value.error_code === null &&
      value.error_field === null && value.error === null);
    const result = value.result;
    requireAuthoring(exactObject(result,
      ['schema', 'action', 'spec', 'selector', 'receipt', 'conflict', 'issues']) &&
      result.schema === 'vcstudio.kinetics-authoring-confirm-result/v1' &&
      ['committed', 'replayed', 'conflict', 'unavailable', 'needs_repreview'].includes(result.action) &&
      Array.isArray(result.issues) && result.issues.length <= 64);
    const issues = result.issues.map(validateIssue);
    ['spec', 'selector', 'receipt'].forEach(key => {
      if (result[key] !== null) requireAuthoring(result[key] &&
        typeof result[key] === 'object' && !Array.isArray(result[key]) &&
        safeAuthoringPublicValue(result[key]));
    });
    if (result.action === 'conflict') requireAuthoring(result.conflict !== null);
    if (result.conflict !== null) validateModelConflict(result.conflict, projectId);
    if (['committed', 'replayed'].includes(result.action)) {
      requireAuthoring(result.spec !== null && result.selector !== null &&
        result.receipt !== null && result.conflict === null);
      validateCommittedModelResult(result, projectId, draft, preview);
    }
    return { action: result.action, issues, conflict: result.conflict && clone(result.conflict) };
  }

  function appendOption(select, value, label) {
    const option = document.createElement('option'); option.value = String(value);
    option.textContent = String(label); select.appendChild(option);
    if (Array.isArray(select.options)) select.options.push(option);
    return option;
  }

  function makeSelect(id, labelText, choices, { multiple = false } = {}) {
    const field = document.createElement('div'); field.className = 'aw-field';
    const label = document.createElement('label'); label.setAttribute('for', id);
    label.textContent = labelText;
    const select = document.createElement('select'); select.id = id; select.className = 'ipt';
    select.multiple = multiple;
    if (!multiple) appendOption(select, '', tr('analysis.authoring.choose', '请选择…'));
    choices.forEach(item => appendOption(select,
      typeof item === 'object' ? item.value : item,
      typeof item === 'object' ? item.label : item));
    field.append(label, select); return { field, control: select };
  }

  function makeInput(id, labelText, { type = 'text', placeholder = '' } = {}) {
    const field = document.createElement('div'); field.className = 'aw-field';
    const label = document.createElement('label'); label.setAttribute('for', id);
    label.textContent = labelText;
    const input = document.createElement('input'); input.id = id; input.type = type;
    input.className = 'ipt'; input.autocomplete = 'off'; input.value = '';
    if (placeholder) input.placeholder = placeholder;
    field.append(label, input); return { field, control: input };
  }

  function rememberModelControl(key, control) {
    State.modelAuthoringControls[key] = control; return control;
  }

  function evidenceChoices(source) {
    return source.evidence_catalog.map(item => ({
      value: item.reference_id, label: `${item.reference_id} · ${item.kind}`,
    }));
  }

  function renderModelCatalog() {
    const bootstrap = State.authoringBootstrap;
    const kinetics = bootstrap && bootstrap.kinetics_authoring;
    const source = kinetics && kinetics.source;
    const catalog = kinetics && kinetics.choice_catalog;
    State.modelAuthoringControls = Object.create(null);
    ['aw-model-rate-policy', 'aw-model-assumptions', 'aw-model-reservoirs',
      'aw-model-targets', 'aw-model-steps', 'aw-model-sites', 'aw-model-saddles']
      .forEach(id => clearNode($(id)));
    if (!source || !catalog || source.status !== 'available') return;
    const evidence = evidenceChoices(source);
    RATE_POLICY_KEYS.forEach(key => {
      const built = makeSelect(`aw-model-rate-${key}`,
        tr(`analysis.authoring.model.policy.${key}`, key), catalog.rate_law_policy[key]);
      rememberModelControl(`rate.${key}`, built.control);
      $('aw-model-rate-policy').appendChild(built.field);
    });
    ['mean_field', 'steady_state'].forEach(key => {
      const choices = catalog.assumptions[key].map(item => ({
        value: item ? 'true' : 'false', label: item ? tr('common.yes', 'Yes') : tr('common.no', 'No'),
      }));
      const built = makeSelect(`aw-model-assumption-${key}`,
        tr(`analysis.authoring.model.assumption.${key}`, key), choices);
      rememberModelControl(`assumption.${key}`, built.control);
      $('aw-model-assumptions').appendChild(built.field);
    });
    ASSUMPTION_POLICY_KEYS.forEach(key => {
      const built = makeSelect(`aw-model-assumption-${key}`,
        tr(`analysis.authoring.model.assumption.${key}`, key), catalog.assumptions[key]);
      rememberModelControl(`assumption.${key}`, built.control);
      $('aw-model-assumptions').appendChild(built.field);
    });
    const assumptionEvidence = makeSelect('aw-model-assumption-evidence',
      tr('analysis.authoring.model.evidence_refs', 'Evidence references'), evidence, { multiple: true });
    assumptionEvidence.field.className += ' aw-field-wide';
    rememberModelControl('assumption.evidence', assumptionEvidence.control);
    $('aw-model-assumptions').appendChild(assumptionEvidence.field);

    source.required_feed_reservoir_ids.forEach((speciesId, index) => {
      const row = document.createElement('div'); row.className = 'aw-model-row';
      const title = document.createElement('strong'); title.textContent = speciesId; row.appendChild(title);
      const activity = makeInput(`aw-feed-${index}-activity`,
        tr('analysis.authoring.model.activity', 'Activity'), { type: 'number' });
      const unit = makeSelect(`aw-feed-${index}-unit`, tr('analysis.authoring.model.unit', 'Unit'),
        catalog.feed_reservoir_units);
      const sourceRef = makeSelect(`aw-feed-${index}-source`,
        tr('analysis.authoring.model.source_ref', 'Source evidence'), evidence);
      rememberModelControl(`feed.${index}.activity`, activity.control);
      rememberModelControl(`feed.${index}.unit`, unit.control);
      rememberModelControl(`feed.${index}.source`, sourceRef.control);
      row.append(activity.field, unit.field, sourceRef.field); $('aw-model-reservoirs').appendChild(row);
    });
    source.allowed_target_product_ids.forEach((targetId, index) => {
      const label = document.createElement('label');
      const input = document.createElement('input'); input.type = 'checkbox'; input.checked = false;
      input.id = `aw-target-${index}`; const text = document.createElement('span'); text.textContent = targetId;
      label.setAttribute('for', input.id); label.append(input, text);
      rememberModelControl(`target.${index}`, input); $('aw-model-targets').appendChild(label);
    });
    source.required_step_ids.forEach((stepId, index) => {
      const row = document.createElement('div'); row.className = 'aw-model-row';
      const title = document.createElement('strong'); title.textContent = stepId; row.appendChild(title);
      ['forward', 'reverse'].forEach(direction => {
        const value = makeInput(`aw-step-${index}-${direction}-value`,
          tr(`analysis.authoring.model.prefactor_${direction}`, `${direction} prefactor`),
          { type: 'number' });
        const unit = makeSelect(`aw-step-${index}-${direction}-unit`,
          tr('analysis.authoring.model.unit', 'Unit'), catalog.prefactor_units);
        const ref = makeSelect(`aw-step-${index}-${direction}-source`,
          tr('analysis.authoring.model.source_ref', 'Source evidence'), evidence);
        rememberModelControl(`step.${index}.${direction}.value`, value.control);
        rememberModelControl(`step.${index}.${direction}.unit`, unit.control);
        rememberModelControl(`step.${index}.${direction}.source`, ref.control);
        row.append(value.field, unit.field, ref.field);
      });
      ['bep', 'scaling'].forEach(model => {
        const used = makeSelect(`aw-step-${index}-${model}-used`,
          tr(`analysis.authoring.model.${model}`, model.toUpperCase()), [
            { value: 'false', label: tr('analysis.authoring.model.not_used', 'Not used') },
            { value: 'true', label: tr('analysis.authoring.model.used', 'Used') },
          ]);
        const sourceRef = makeSelect(`aw-step-${index}-${model}-source`,
          tr('analysis.authoring.model.source_ref', 'Source evidence'), evidence);
        const parametersRef = makeSelect(`aw-step-${index}-${model}-parameters`,
          tr('analysis.authoring.model.parameters_ref', 'Parameters evidence'), evidence);
        rememberModelControl(`step.${index}.${model}.used`, used.control);
        rememberModelControl(`step.${index}.${model}.source`, sourceRef.control);
        rememberModelControl(`step.${index}.${model}.parameters`, parametersRef.control);
        row.append(used.field, sourceRef.field, parametersRef.field);
      });
      const uncertainty = makeInput(`aw-step-${index}-uncertainty`,
        tr('analysis.authoring.model.uncertainty', 'Uncertainty (eV)'), { type: 'number' });
      const refs = makeSelect(`aw-step-${index}-evidence`,
        tr('analysis.authoring.model.evidence_refs', 'Evidence references'), evidence, { multiple: true });
      rememberModelControl(`step.${index}.uncertainty`, uncertainty.control);
      rememberModelControl(`step.${index}.evidence`, refs.control);
      row.append(uncertainty.field, refs.field); $('aw-model-steps').appendChild(row);
    });
    source.required_site_type_ids.forEach((siteType, index) => {
      const row = document.createElement('div'); row.className = 'aw-model-row';
      const title = document.createElement('strong'); title.textContent = siteType; row.appendChild(title);
      const value = makeInput(`aw-site-${index}-value`, tr('analysis.authoring.model.value', 'Value'),
        { type: 'number' });
      const unit = makeSelect(`aw-site-${index}-unit`, tr('analysis.authoring.model.unit', 'Unit'),
        catalog.site_total_units);
      const basis = makeSelect(`aw-site-${index}-basis`, tr('analysis.authoring.model.basis', 'Basis'),
        catalog.site_total_bases);
      const refs = makeSelect(`aw-site-${index}-evidence`,
        tr('analysis.authoring.model.evidence_refs', 'Evidence references'), evidence, { multiple: true });
      rememberModelControl(`site.${index}.value`, value.control);
      rememberModelControl(`site.${index}.unit`, unit.control);
      rememberModelControl(`site.${index}.basis`, basis.control);
      rememberModelControl(`site.${index}.evidence`, refs.control);
      row.append(value.field, unit.field, basis.field, refs.field); $('aw-model-sites').appendChild(row);
    });
    const saddleMode = makeSelect('aw-model-saddle-mode',
      tr('analysis.authoring.model.saddle_mode', 'Saddle selector mode'), [
        { value: 'omit', label: tr('analysis.authoring.model.saddle_omit', 'Explicitly omit') },
        { value: 'explicit_species_by_step', label: tr('analysis.authoring.model.saddle_explicit', 'Explicit species by step') },
      ]);
    rememberModelControl('saddle.mode', saddleMode.control); $('aw-model-saddles').appendChild(saddleMode.field);
    source.required_step_ids.forEach((stepId, index) => {
      const choices = source.saddle_candidates_by_step[stepId] || [];
      const built = makeSelect(`aw-saddle-${index}`, stepId, choices);
      rememberModelControl(`saddle.${index}`, built.control); $('aw-model-saddles').appendChild(built.field);
    });
    const saddleEvidence = makeSelect('aw-model-saddle-evidence',
      tr('analysis.authoring.model.evidence_refs', 'Evidence references'), evidence, { multiple: true });
    rememberModelControl('saddle.evidence', saddleEvidence.control);
    $('aw-model-saddles').appendChild(saddleEvidence.field);
  }

  function controlValue(key) {
    const control = State.modelAuthoringControls[key];
    return control ? String(control.value || '').trim() : '';
  }

  function selectedControlValues(key) {
    const control = State.modelAuthoringControls[key];
    if (!control) return [];
    const options = Array.from(control.selectedOptions || control.options || []);
    return options.filter(option => option.selected === true).map(option => safeAuthoringId(option.value))
      .filter(Boolean);
  }

  function requiredChoice(key) {
    const value = safeAuthoringId(controlValue(key));
    if (!value) throw new Error(tr('analysis.authoring.missing_fields',
      '请显式填写所有必需选择后再预览。'));
    return value;
  }

  function requiredLiteralChoice(key, allowed) {
    const value = controlValue(key);
    if (!value || !allowed.includes(value)) throw new Error(tr(
      'analysis.authoring.missing_fields', '请显式填写所有必需选择后再预览。'));
    return value;
  }

  function requiredNumber(key, { positive = false } = {}) {
    const raw = controlValue(key); const value = Number(raw);
    if (raw === '' || !Number.isFinite(value) || value < 0 || (positive && value <= 0)) {
      throw new Error(tr('analysis.authoring.invalid_number', '请填写有效的有限数值。'));
    }
    return value;
  }

  function requiredEvidence(key, allowed = null) {
    const values = selectedControlValues(key);
    if (!values.length || new Set(values).size !== values.length ||
        (allowed && values.some(value => !allowed.has(value)))) throw new Error(tr(
      'analysis.authoring.missing_evidence', '每个科学选择都必须显式绑定服务端 evidence reference。'));
    return values;
  }

  function networkDraftFromControls() {
    const networkId = safeAuthoringId($('aw-network-candidate') && $('aw-network-candidate').value);
    if (!networkId) throw new Error(tr(
      'analysis.authoring.network.choose_required', '请明确选择一个 ReactionNetwork 候选。'));
    return { network_id: networkId };
  }

  function modelDraftFromControls() {
    const bootstrap = State.authoringBootstrap; const kinetics = bootstrap && bootstrap.kinetics_authoring;
    const source = kinetics && kinetics.source;
    if (!source || source.status !== 'available') throw new Error(tr(
      'analysis.authoring.model.source_missing', 'Kinetics authoring source 尚不可用。'));
    const specId = safeAuthoringId($('aw-model-spec-id') && $('aw-model-spec-id').value);
    const mode = $('aw-model-mode') && $('aw-model-mode').value;
    if (!specId || !['create', 'advance'].includes(mode)) throw new Error(tr(
      'analysis.authoring.missing_fields', '请显式填写所有必需选择后再预览。'));
    const evidenceIds = new Set(source.evidence_catalog.map(item => item.reference_id));
    const rateLaw = {}; RATE_POLICY_KEYS.forEach(key => {
      rateLaw[key] = requiredLiteralChoice(`rate.${key}`, kinetics.choice_catalog.rate_law_policy[key]);
    });
    const assumptions = {
      mean_field: requiredLiteralChoice('assumption.mean_field', ['true']) === 'true',
      steady_state: requiredLiteralChoice('assumption.steady_state', ['true']) === 'true',
      evidence_ref_ids: requiredEvidence('assumption.evidence', evidenceIds),
    };
    ASSUMPTION_POLICY_KEYS.forEach(key => {
      assumptions[key] = requiredLiteralChoice(
        `assumption.${key}`, kinetics.choice_catalog.assumptions[key]);
    });
    const reservoirs = source.required_feed_reservoir_ids.map((speciesId, index) => ({
      species_id: speciesId,
      activity: requiredNumber(`feed.${index}.activity`),
      unit: requiredLiteralChoice(`feed.${index}.unit`, kinetics.choice_catalog.feed_reservoir_units),
      source_ref_id: requiredLiteralChoice(`feed.${index}.source`, [...evidenceIds]),
    }));
    const targets = source.allowed_target_product_ids.filter((_id, index) => {
      const input = State.modelAuthoringControls[`target.${index}`]; return input && input.checked === true;
    });
    if (!targets.length) throw new Error(tr('analysis.authoring.model.target_required',
      '请显式选择至少一个 target product。'));
    const steps = source.required_step_ids.map((stepId, index) => {
      function empirical(name) {
        const used = requiredLiteralChoice(`step.${index}.${name}.used`, ['false', 'true']) === 'true';
        return {
          used,
          source_ref_id: used ? requiredLiteralChoice(
            `step.${index}.${name}.source`, [...evidenceIds]) : null,
          parameters_ref_id: used ? requiredLiteralChoice(
            `step.${index}.${name}.parameters`, [...evidenceIds]) : null,
        };
      }
      function prefactor(direction) {
        return {
          value: requiredNumber(`step.${index}.${direction}.value`, { positive: true }),
          unit: requiredLiteralChoice(`step.${index}.${direction}.unit`,
            kinetics.choice_catalog.prefactor_units),
          source_ref_id: requiredLiteralChoice(
            `step.${index}.${direction}.source`, [...evidenceIds]),
        };
      }
      return {
        step_id: stepId,
        prefactors: { forward: prefactor('forward'), reverse: prefactor('reverse') },
        bep: empirical('bep'), scaling: empirical('scaling'),
        uncertainty_eV: requiredNumber(`step.${index}.uncertainty`),
        evidence_ref_ids: requiredEvidence(`step.${index}.evidence`, evidenceIds),
      };
    });
    const siteTotals = source.required_site_type_ids.map((siteType, index) => ({
      site_type: siteType, value: requiredNumber(`site.${index}.value`, { positive: true }),
      unit: requiredLiteralChoice(`site.${index}.unit`, kinetics.choice_catalog.site_total_units),
      basis: requiredLiteralChoice(`site.${index}.basis`, kinetics.choice_catalog.site_total_bases),
      evidence_ref_ids: requiredEvidence(`site.${index}.evidence`, evidenceIds),
    }));
    const draft = {
      schema: 'vcstudio.kinetics-model-spec-draft/v1', spec_id: specId, mode,
      rate_law_policy: rateLaw, assumptions, feed_reservoirs: reservoirs,
      target_product_ids: targets, steps, site_population_totals: siteTotals,
    };
    const saddleMode = requiredChoice('saddle.mode');
    if (saddleMode === 'explicit_species_by_step') {
      const byStep = {};
      source.required_step_ids.forEach((stepId, index) => {
        const selected = requiredChoice(`saddle.${index}`);
        requireAuthoring((source.saddle_candidates_by_step[stepId] || []).includes(selected));
        byStep[stepId] = selected;
      });
      draft.saddle_selector = {
        mode: saddleMode, by_step: byStep,
        evidence_ref_ids: requiredEvidence('saddle.evidence', evidenceIds),
      };
    } else if (saddleMode === 'omit') {
      draft.saddle_selector = null;
    } else throw authoringInvalid();
    requireAuthoring(safeAuthoringPublicValue(draft));
    return draft;
  }

  function renderList(id, values, fallback) {
    const list = $(id); if (!list) return; clearNode(list);
    (values || []).slice(0, 64).forEach(value => {
      const item = document.createElement('li'); item.textContent = String(value); list.appendChild(item);
    });
    if (!list.children.length) {
      const item = document.createElement('li'); item.textContent = fallback; list.appendChild(item);
    }
  }

  function gapLabels(gaps, summary) {
    if (Array.isArray(gaps) && gaps.length) return gaps.map(item =>
      [item.status, item.code, item.object_type, item.object_id].filter(Boolean).join(' · '));
    const byCode = plain(summary).by_code;
    const values = Object.entries(plain(byCode)).map(([code, count]) => `${code} × ${count}`);
    if (plain(summary).omitted) values.push(tr('analysis.authoring.omitted',
      '{count} additional items omitted', { count: summary.omitted }));
    return values;
  }

  function renderNetworkBootstrap() {
    const bootstrap = State.authoringBootstrap && State.authoringBootstrap.active_network;
    const select = $('aw-network-candidate');
    if (select) {
      clearNode(select);
      appendOption(select, '', tr('analysis.authoring.choose', '请选择…'));
      (bootstrap && bootstrap.candidates || []).forEach(candidate => appendOption(
        select, candidate.network_head.network_id,
        `${candidate.network_head.network_id} · ${candidate.projection_status}`));
      select.value = '';
    }
    setText('aw-network-active', bootstrap && bootstrap.active_network_id ?
      `${bootstrap.active_network_id} · ${bootstrap.active_status}` : tr('analysis.authoring.network.none', 'none'));
    setText('aw-network-authority', bootstrap && bootstrap.domain_cas &&
      bootstrap.domain_cas.authority_id, '—');
    setText('aw-network-generation', bootstrap && bootstrap.domain_cas &&
      bootstrap.domain_cas.generation, '—');
    setText('aw-network-closure', tr('analysis.authoring.network.choose_closure',
      '选择候选后显示 closure 摘要'));
    renderList('aw-network-gaps', [], tr('analysis.authoring.not_loaded', '尚未读取。'));
  }

  function renderSelectedNetworkSummary() {
    const bootstrap = State.authoringBootstrap && State.authoringBootstrap.active_network;
    const selected = safeAuthoringId($('aw-network-candidate') && $('aw-network-candidate').value);
    const candidate = bootstrap && bootstrap.candidates.find(item =>
      item.network_head.network_id === selected);
    if (!candidate) {
      setText('aw-network-closure', tr('analysis.authoring.network.choose_closure',
        '选择候选后显示 closure 摘要'));
      renderList('aw-network-gaps', [], tr('analysis.authoring.not_loaded', '尚未读取。')); return;
    }
    setText('aw-network-closure', `${candidate.projection_status} · gaps ${candidate.gap_summary.total}`);
    renderList('aw-network-gaps', gapLabels([], candidate.gap_summary),
      tr('analysis.authoring.no_gaps', '无 closure / evidence gaps。'));
  }

  function renderModelReadiness(saveStatus, solverStatus) {
    const save = $('aw-model-save-status'); const solver = $('aw-model-solver-status');
    if (save) { save.dataset.status = saveStatus; save.textContent = authoringStatusText(saveStatus); }
    if (solver) { solver.dataset.status = solverStatus; solver.textContent = authoringStatusText(solverStatus); }
  }

  function renderAuthoringControls() {
    const network = State.networkAuthoring; const model = State.modelAuthoring;
    const networkBootstrap = State.authoringBootstrap && State.authoringBootstrap.active_network;
    const modelBootstrap = State.authoringBootstrap && State.authoringBootstrap.kinetics_authoring;
    const selectedNetwork = safeAuthoringId($('aw-network-candidate') && $('aw-network-candidate').value);
    const bootstrapReady = State.authoringBootstrapStatus === 'ready';
    const networkLoaded = bootstrapReady && networkBootstrap &&
      networkBootstrap.status === 'available';
    const modelLoaded = bootstrapReady && modelBootstrap && modelBootstrap.status === 'available' &&
      modelBootstrap.source && modelBootstrap.source.status === 'available';
    const networkCard = $('aw-network-authoring'); const modelCard = $('aw-model-authoring');
    if (networkCard) networkCard.setAttribute('aria-busy',
      network.previewing || network.confirming ? 'true' : 'false');
    if (modelCard) modelCard.setAttribute('aria-busy',
      model.previewing || model.confirming ? 'true' : 'false');
    const networkFields = $('aw-network-authoring-fields');
    if (networkFields) networkFields.disabled = !networkLoaded || network.confirming;
    const networkPreview = $('aw-network-preview');
    if (networkPreview) networkPreview.disabled = !networkLoaded || !selectedNetwork ||
      network.previewing || network.confirming;
    const networkAck = $('aw-network-ack');
    if (networkAck) networkAck.disabled = !network.previewReceipt ||
      network.previewReceipt.can_confirm !== true || network.dirty || network.confirming;
    const networkConfirm = $('aw-network-confirm');
    if (networkConfirm) networkConfirm.disabled = !network.previewReceipt ||
      network.previewReceipt.can_confirm !== true || network.dirty ||
      network.acknowledged !== true || !(networkAck && networkAck.checked) ||
      network.previewing || network.confirming;
    ['aw-model-identity-fields', 'aw-model-rate-fields', 'aw-model-assumption-fields']
      .forEach(id => { const node = $(id); if (node) node.disabled = !modelLoaded || model.confirming; });
    Object.values(State.modelAuthoringControls).forEach(control => {
      control.disabled = !modelLoaded || model.confirming;
    });
    const specId = $('aw-model-spec-id'); const mode = $('aw-model-mode');
    if (specId) specId.disabled = !modelLoaded || model.confirming;
    if (mode) mode.disabled = !modelLoaded || model.confirming;
    const modelPreview = $('aw-model-preview');
    if (modelPreview) modelPreview.disabled = !modelLoaded || model.previewing || model.confirming;
    const modelAck = $('aw-model-ack');
    if (modelAck) modelAck.disabled = !model.previewReceipt ||
      model.previewReceipt.can_confirm !== true || model.dirty || model.confirming;
    const modelConfirm = $('aw-model-confirm');
    if (modelConfirm) modelConfirm.disabled = !model.previewReceipt ||
      model.previewReceipt.can_confirm !== true || model.dirty ||
      model.acknowledged !== true || !(modelAck && modelAck.checked) ||
      model.previewing || model.confirming;
  }

  function renderAuthoring() {
    setText('aw-authoring-epoch', State.authoringProjectId ?
      `${State.authoringProjectId} · ${State.authoringEpoch}` : '—');
    ['network', 'model'].forEach(kind => {
      const axis = authoringAxis(kind); setAuthoringStatus(kind, axis.status, axis.message);
    });
    const model = State.modelAuthoring; const source = State.authoringBootstrap &&
      State.authoringBootstrap.kinetics_authoring &&
      State.authoringBootstrap.kinetics_authoring.source;
    const saveStatus = model.previewReceipt ? (model.previewReceipt.can_confirm ? 'ready' : 'blocked')
      : model.status;
    const solverStatus = model.previewReceipt ?
      (model.previewReceipt.solver_ready ? 'ready' : 'blocked') :
      (source && source.solver_ready ? 'ready' : source ? 'blocked' : 'missing');
    renderModelReadiness(saveStatus, solverStatus);
    renderAuthoringControls();
  }

  function authoringUnavailable(kind, error, owner = null) {
    if (owner && !authoringOwnerIsCurrent(owner)) return false;
    const axis = authoringAxis(kind);
    axis.previewing = false; axis.confirming = false; axis.previewReceipt = null;
    axis.previewRequest = null; axis.acknowledged = false; axis.dirty = true;
    const ack = $(`aw-${kind}-ack`); if (ack) ack.checked = false;
    setAuthoringStatus(kind, 'unavailable',
      error === authoringInvalid ? error.message : tr(
        'analysis.authoring.operation_failed', 'Authoring 操作未完成；没有写入，也没有授权执行。'),
      'bad', true);
    renderAuthoring(); return false;
  }

  function adoptAndStop(kind, conflict, message) {
    const axis = authoringAxis(kind);
    axis.intentGeneration += 1; axis.intentId = ''; axis.dirty = true;
    axis.previewReceipt = null; axis.previewRequest = null; axis.acknowledged = false;
    axis.previewing = false; axis.confirming = false;
    axis.conflict = clone(conflict); axis.lastResult = null;
    const ack = $(`aw-${kind}-ack`); if (ack) ack.checked = false;
    setAuthoringStatus(kind, 'blocked', message || tr(
      'analysis.authoring.conflict', 'Authority 已变化。已采纳最新 CAS 并停止；请检查表单后重新显式预览。'),
      'conflict', true);
    renderAuthoring();
  }

  function adoptNetworkCAS(bindingCAS, networkCAS) {
    const bootstrap = State.authoringBootstrap && State.authoringBootstrap.active_network;
    if (!bootstrap) return;
    if (bindingCAS) bootstrap.binding_cas = clone(bindingCAS);
    if (!networkCAS || networkCAS.network_status !== 'available') return;
    const candidate = bootstrap.candidates.find(item =>
      item.network_head.network_id === networkCAS.network_id);
    if (candidate) candidate.network_head = clone(networkCAS);
  }

  function authoringBootstrapCurrent(projectId, analysisId, epoch) {
    return projectId === State.authoringProjectId && analysisId === State.authoringAnalysisId &&
      epoch === State.authoringEpoch && sameProject(projectId);
  }

  async function loadAuthoringBootstrap(projectId, epoch = State.authoringEpoch,
    analysisId = State.authoringAnalysisId) {
    if (!projectId || !authoringBootstrapCurrent(projectId, analysisId, epoch)) return false;
    State.authoringBootstrapStatus = 'missing';
    setAuthoringStatus('network', 'missing', tr(
      'analysis.authoring.loading', '正在读取 authoring authority 摘要…'), 'busy');
    setAuthoringStatus('model', 'missing', tr(
      'analysis.authoring.loading', '正在读取 authoring authority 摘要…'), 'busy');
    renderAuthoringControls();
    try {
      const response = await VCS.call('catalysis_authoring_bootstrap', projectId);
      if (!authoringBootstrapCurrent(projectId, analysisId, epoch)) return false;
      const parsed = validateBootstrapEnvelope(response, projectId);
      State.authoringBootstrap = parsed;
      State.authoringBootstrapStatus = parsed.bootstrap_status === 'conflict' ?
        'conflict' : 'ready';
      State.networkAuthoring = freshAuthoringAxis(); State.modelAuthoring = freshAuthoringAxis();
      renderNetworkBootstrap(); renderModelCatalog();
      const network = parsed.active_network; const networkAxis = State.networkAuthoring;
      if (parsed.bootstrap_status === 'conflict') {
        networkAxis.status = 'conflict'; networkAxis.message = parsed.reason || tr(
          'analysis.authoring.conflict',
          'Authority 已变化。已停止；请重新读取并检查当前选择。');
      } else if (network.status === 'available' && network.candidates.length) {
        networkAxis.status = 'blocked'; networkAxis.message = tr(
          'analysis.authoring.network.explicit_required',
          'Authority 已加载；请选择候选并生成只读预览。');
      } else if (network.status === 'available') {
        networkAxis.status = 'missing'; networkAxis.message = tr(
          'analysis.authoring.network.no_candidates', '当前 authority 没有 ReactionNetwork head 候选。');
      } else {
        networkAxis.status = network.status === 'missing' ? 'missing' : 'unavailable';
        networkAxis.message = network.reason || authoringStatusText(networkAxis.status);
      }
      const kinetics = parsed.kinetics_authoring; const modelAxis = State.modelAuthoring;
      if (parsed.bootstrap_status === 'conflict' || kinetics.status === 'conflict') {
        modelAxis.status = 'conflict'; modelAxis.message = parsed.reason || kinetics.reason || tr(
          'analysis.authoring.conflict',
          'Authority 已变化。已停止；请重新读取并检查当前选择。');
      } else if (kinetics.status === 'available' && kinetics.source &&
          kinetics.source.status === 'available' && kinetics.model_store.status === 'available' &&
          !['unavailable', 'legacy_read_only'].includes(kinetics.selector.status)) {
        modelAxis.status = 'blocked'; modelAxis.message = tr(
          'analysis.authoring.model.explicit_required',
          'Authoritative source 已加载；请逐项填写，不会自动选择默认值。');
      } else if (kinetics.status === 'unavailable' || kinetics.model_store.status === 'unavailable' ||
          kinetics.selector.status === 'unavailable') {
        modelAxis.status = 'unavailable'; modelAxis.message = kinetics.reason ||
          tr('analysis.authoring.model.unavailable', 'Kinetics authoring authority 当前不可用。');
      } else {
        modelAxis.status = kinetics.status === 'blocked' ||
          (kinetics.source && kinetics.source.status === 'blocked') ? 'blocked' :
          kinetics.status === 'missing' ? 'missing' : 'missing_prerequisite';
        modelAxis.message =
          kinetics.reason === 'evidence_resolver_unavailable' ? tr(
            'analysis.authoring.model.evidence_resolver_missing',
            '请先导入并绑定可解析为 exact bytes 的可信科学证据；canonical envelope 不是科学 artifact。') :
            kinetics.reason || tr('analysis.authoring.model.missing_prerequisite',
              '缺少 authoritative source 或可写 selector prerequisite。');
      }
      const sourceReasons = kinetics.source && kinetics.source.solver_readiness_reasons || [];
      renderList('aw-model-issues', sourceReasons,
        tr('analysis.authoring.model.no_initial_issues', '尚未生成 model preview。'));
      renderAuthoring(); return true;
    } catch (_) {
      if (!authoringBootstrapCurrent(projectId, analysisId, epoch)) return false;
      State.authoringBootstrap = null; State.authoringBootstrapStatus = 'unavailable';
      State.networkAuthoring = freshAuthoringAxis(); State.modelAuthoring = freshAuthoringAxis();
      State.networkAuthoring.status = 'unavailable'; State.modelAuthoring.status = 'unavailable';
      const message = tr('analysis.authoring.bootstrap_invalid',
        'Authoring bootstrap 不完整或不安全；已 fail closed。');
      State.networkAuthoring.message = message; State.modelAuthoring.message = message;
      renderNetworkBootstrap(); renderModelCatalog(); renderAuthoring(); return false;
    }
  }

  async function previewActiveNetwork() {
    const axis = State.networkAuthoring;
    if (axis.previewing || axis.confirming || State.authoringBootstrapStatus !== 'ready' ||
        !sameProject(State.authoringProjectId)) return false;
    let draft;
    try { draft = networkDraftFromControls(); }
    catch (error) {
      setAuthoringStatus('network', 'missing_prerequisite', error.message, 'bad', true);
      renderAuthoringControls(); return false;
    }
    const bootstrap = State.authoringBootstrap.active_network;
    const candidate = bootstrap.candidates.find(item => item.network_head.network_id === draft.network_id);
    if (!candidate) return authoringUnavailable('network', authoringInvalid);
    axis.intentGeneration += 1; const generation = axis.intentGeneration;
    const intentId = newAuthoringIntent('network', generation);
    const request = {
      schema: 'vcstudio.catalysis-active-network-preview-request/v1',
      network_id: draft.network_id, intent_id: intentId,
      expected_binding: clone(bootstrap.binding_cas),
      expected_network_head: clone(candidate.network_head),
    };
    requireAuthoring(safeAuthoringPublicValue(request));
    axis.intentId = intentId; axis.dirty = true; axis.previewReceipt = null;
    axis.previewRequest = null; axis.acknowledged = false; axis.conflict = null;
    axis.previewing = true; const owner = authoringOwner('network', draft, intentId);
    setAuthoringStatus('network', 'blocked', tr(
      'analysis.authoring.network.previewing', '正在生成只读 network preview…'), 'busy');
    renderAuthoringControls();
    try {
      const response = await VCS.call(
        'catalysis_active_network_preview', owner.projectId, clone(request));
      if (!authoringOwnerIsCurrent(owner)) return false;
      const preview = validateNetworkPreviewEnvelope(response, owner.projectId, request);
      axis.previewing = false;
      if (preview.status === 'conflict') {
        adoptNetworkCAS(preview.binding_cas, preview.network_cas);
        adoptAndStop('network', {
          reason: preview.reason, binding_cas: preview.binding_cas,
          network_cas: preview.network_cas,
        }, tr(
          'analysis.authoring.network.preview_conflict',
          'Network authority 在预览前已变化；已停止，请重新检查并预览。'));
        return false;
      }
      axis.previewReceipt = frozenJson(preview); axis.previewRequest = frozenJson(request);
      axis.dirty = false; axis.lastResult = null;
      renderList('aw-network-gaps', gapLabels(preview.gaps, preview.gap_summary),
        tr('analysis.authoring.no_gaps', '无 closure / evidence gaps。'));
      setText('aw-network-closure', `${preview.projection_status} · gaps ${preview.gap_summary.total}`);
      if (preview.status === 'ready') setAuthoringStatus('network', 'ready', tr(
        'analysis.authoring.network.preview_ready',
        '预览已冻结；它不授权执行。勾选确认框后才能提交 binding。'), 'ok', true);
      else setAuthoringStatus('network', preview.status === 'missing' ? 'missing' : 'unavailable',
        preview.reason || authoringStatusText(preview.status), 'bad', true);
      renderAuthoring(); return preview.status === 'ready';
    } catch (error) {
      return authoringUnavailable('network', error, owner);
    } finally {
      settleAuthoringFlight('network', axis, owner, 'previewing');
    }
  }

  async function confirmActiveNetwork() {
    const axis = State.networkAuthoring; const preview = axis.previewReceipt;
    if (!preview || preview.can_confirm !== true || axis.dirty || axis.previewing || axis.confirming ||
        axis.acknowledged !== true || !($('aw-network-ack') && $('aw-network-ack').checked) ||
        !sameProject(State.authoringProjectId)) return false;
    let draft;
    try { draft = networkDraftFromControls(); } catch (_) { return false; }
    const owner = authoringOwner('network', draft, axis.intentId);
    if (!authoringOwnerIsCurrent(owner)) return false;
    const receiptIdentity = stableJson({
      request_sha256: preview.request_sha256, preview_sha256: preview.preview_sha256,
      issued_at_ms: preview.issued_at_ms, expires_at_ms: preview.expires_at_ms,
    });
    const confirmation = {
      schema: 'vcstudio.catalysis-active-network-confirmation/v1',
      request_sha256: preview.request_sha256, preview_sha256: preview.preview_sha256,
      issued_at_ms: preview.issued_at_ms, expires_at_ms: preview.expires_at_ms, confirmed: true,
    };
    const payload = { request: clone(axis.previewRequest), confirmation };
    axis.confirming = true; setAuthoringStatus('network', 'ready', tr(
      'analysis.authoring.network.confirming', '正在以冻结 receipt 执行 CAS confirm…'), 'busy');
    renderAuthoringControls();
    try {
      const response = await VCS.call(
        'catalysis_active_network_confirm', owner.projectId, clone(payload));
      if (!authoringOwnerIsCurrent(owner) || axis.previewReceipt !== preview ||
          stableJson({ request_sha256: preview.request_sha256,
            preview_sha256: preview.preview_sha256, issued_at_ms: preview.issued_at_ms,
            expires_at_ms: preview.expires_at_ms }) !== receiptIdentity ||
          preview.can_confirm !== true || preview.confirmed !== false ||
          preview.authorizes_execution !== false || axis.acknowledged !== true ||
          !($('aw-network-ack') && $('aw-network-ack').checked)) return false;
      const result = validateNetworkConfirmEnvelope(
        response, owner.projectId, axis.previewRequest);
      if (result.action === 'conflict') {
        adoptNetworkCAS(result.conflict.latest_binding_cas,
          result.conflict.latest_network_cas);
        adoptAndStop('network', result.conflict, tr('analysis.authoring.conflict',
          'Authority 已变化。已采纳最新 CAS 并停止；请检查表单后重新显式预览。'));
        return false;
      }
      if (result.action === 'unavailable') {
        axis.confirming = false; return authoringUnavailable('network', authoringInvalid, owner);
      }
      axis.intentGeneration += 1; axis.intentId = ''; axis.previewReceipt = null;
      axis.previewRequest = null; axis.dirty = false; axis.acknowledged = false;
      axis.confirming = false; axis.lastResult = { action: result.action };
      $('aw-network-ack').checked = false;
      setAuthoringStatus('network', 'ready', tr(
        'analysis.authoring.network.confirmed', 'Active network binding 已由服务端 CAS 确认。'), 'ok', true);
      renderAuthoring(); return true;
    } catch (error) { return authoringUnavailable('network', error, owner); }
    finally {
      settleAuthoringFlight('network', axis, owner, 'confirming');
    }
  }

  async function previewModelSpec() {
    const axis = State.modelAuthoring;
    if (axis.previewing || axis.confirming || State.authoringBootstrapStatus !== 'ready' ||
        !sameProject(State.authoringProjectId)) return false;
    let draft;
    try { draft = modelDraftFromControls(); }
    catch (error) {
      setAuthoringStatus('model', 'missing_prerequisite', error.message, 'bad', true);
      renderAuthoringControls(); return false;
    }
    axis.intentGeneration += 1; const generation = axis.intentGeneration;
    const intentId = newAuthoringIntent('model', generation);
    axis.intentId = intentId; axis.dirty = true; axis.previewReceipt = null;
    axis.previewRequest = null; axis.acknowledged = false; axis.conflict = null;
    axis.previewing = true; const owner = authoringOwner('model', draft, intentId);
    setAuthoringStatus('model', 'blocked', tr(
      'analysis.authoring.model.previewing', '正在编译只读 model preview…'), 'busy');
    renderAuthoringControls();
    try {
      const response = await VCS.call('kinetics_model_spec_preview', owner.projectId,
        { draft: clone(draft), intent_id: intentId });
      if (!authoringOwnerIsCurrent(owner)) return false;
      const preview = validateModelPreviewEnvelope(
        response, owner.projectId, intentId, draft, State.authoringBootstrap.shared_authority);
      axis.previewing = false; axis.previewReceipt = frozenJson(preview);
      axis.previewRequest = frozenJson(draft);
      axis.dirty = false; axis.lastResult = null;
      renderList('aw-model-issues', preview.issues.map(item => `${item.code} · ${item.path}`),
        tr('analysis.authoring.model.no_issues', '无 model validation issues。'));
      renderModelReadiness(preview.can_confirm ? 'ready' : 'blocked',
        preview.solver_ready ? 'ready' : 'blocked');
      if (preview.can_confirm) setAuthoringStatus('model', 'ready', tr(
        'analysis.authoring.model.preview_ready',
        'Spec 可保存；solver-ready 仍是独立结论，预览不授权执行。'), 'ok', true);
      else setAuthoringStatus('model', 'blocked', tr(
        'analysis.authoring.model.preview_blocked',
        'Model preview 有 validation issue；不能确认保存。'), 'bad', true);
      renderAuthoring(); return preview.can_confirm === true;
    } catch (error) { return authoringUnavailable('model', error, owner); }
    finally {
      settleAuthoringFlight('model', axis, owner, 'previewing');
    }
  }

  async function confirmModelSpec() {
    const axis = State.modelAuthoring; const preview = axis.previewReceipt;
    if (!preview || preview.can_confirm !== true || axis.dirty || axis.previewing || axis.confirming ||
        axis.acknowledged !== true || !($('aw-model-ack') && $('aw-model-ack').checked) ||
        !sameProject(State.authoringProjectId)) return false;
    let draft;
    try { draft = modelDraftFromControls(); } catch (_) { return false; }
    const owner = authoringOwner('model', draft, axis.intentId);
    if (!authoringOwnerIsCurrent(owner)) return false;
    const source = preview.source_cas; const store = preview.store_cas;
    const selector = preview.selector_cas;
    if (!source || !store || !selector) return authoringUnavailable('model', authoringInvalid, owner);
    const receiptIdentity = stableJson({
      intent_id: preview.intent_id, draft_sha256: preview.draft_sha256,
      preview_sha256: preview.preview_sha256,
      source_snapshot_sha256: source.snapshot_sha256,
      store_snapshot_sha256: store.snapshot_sha256,
      selector_snapshot_sha256: selector.snapshot_sha256,
    });
    const confirmation = {
      schema: 'vcstudio.kinetics-authoring-confirmation/v1', intent_id: preview.intent_id,
      draft_sha256: preview.draft_sha256, preview_sha256: preview.preview_sha256,
      source_snapshot_sha256: source.snapshot_sha256,
      store_snapshot_sha256: store.snapshot_sha256,
      selector_snapshot_sha256: selector.snapshot_sha256, confirmed: true,
    };
    axis.confirming = true; setAuthoringStatus('model', 'ready', tr(
      'analysis.authoring.model.confirming', '正在以冻结三轴 CAS receipt 保存 spec…'), 'busy');
    renderAuthoringControls();
    try {
      const response = await VCS.call('kinetics_model_spec_confirm', owner.projectId,
        { draft: clone(draft), confirmation });
      if (!authoringOwnerIsCurrent(owner) || axis.previewReceipt !== preview ||
          stableJson({ intent_id: preview.intent_id, draft_sha256: preview.draft_sha256,
            preview_sha256: preview.preview_sha256,
            source_snapshot_sha256: source.snapshot_sha256,
            store_snapshot_sha256: store.snapshot_sha256,
            selector_snapshot_sha256: selector.snapshot_sha256 }) !== receiptIdentity ||
          preview.can_confirm !== true || preview.authorizes_execution !== false ||
          axis.acknowledged !== true || !($('aw-model-ack') && $('aw-model-ack').checked)) return false;
      const result = validateModelConfirmEnvelope(response, owner.projectId, draft, preview);
      if (['conflict', 'needs_repreview'].includes(result.action)) {
        adoptAndStop('model', result.conflict || { reason: result.action }, tr(
          'analysis.authoring.conflict',
          'Authority 已变化。已采纳最新 CAS 并停止；请检查表单后重新显式预览。'));
        return false;
      }
      if (result.action === 'unavailable') {
        axis.confirming = false; axis.previewReceipt = null; axis.previewRequest = null;
        axis.acknowledged = false; axis.dirty = true; $('aw-model-ack').checked = false;
        renderList('aw-model-issues', result.issues.map(item => `${item.code} · ${item.path}`),
          tr('analysis.authoring.model.unavailable', 'Kinetics authoring authority 当前不可用。'));
        setAuthoringStatus('model', result.issues.length ? 'blocked' : 'unavailable', tr(
          'analysis.authoring.model.confirm_unavailable',
          'Spec 未保存；请解决 issue 后重新显式预览。'), 'bad', true);
        renderAuthoring(); return false;
      }
      axis.intentGeneration += 1; axis.intentId = ''; axis.previewReceipt = null;
      axis.previewRequest = null; axis.dirty = false; axis.acknowledged = false;
      axis.confirming = false; axis.lastResult = { action: result.action };
      $('aw-model-ack').checked = false;
      setAuthoringStatus('model', 'ready', tr(
        'analysis.authoring.model.confirmed', 'Kinetics model spec 已由服务端事务确认并选择。'), 'ok', true);
      renderAuthoring(); return true;
    } catch (error) { return authoringUnavailable('model', error, owner); }
    finally {
      settleAuthoringFlight('model', axis, owner, 'confirming');
    }
  }

  function requestFromControls() {
    const record = analysisRecord(); const source = plain(State.spec);
    const parameters = Array.isArray(record && record.parameters) ? record.parameters : [];
    const request = {
      schema: 'vcstudio.analysis-spec/v1',
      analysis_id: State.analysisId,
      project_id: State.projectId,
      view_id: source.view_id || null,
    };
    if (Array.isArray(record && record.data_modes) && record.data_modes.length > 1) {
      request.data_mode = $('aw-data-mode') && $('aw-data-mode').value || source.data_mode || 'stable';
    }
    if (parameters.includes('near_degenerate_eV')) {
      request.near_degenerate_eV = Number($('aw-deadband') && $('aw-deadband').value);
    }
    if (parameters.includes('precision')) {
      request.precision = Number($('aw-precision') && $('aw-precision').value);
    }
    if (parameters.includes('missing_policy')) {
      request.missing_policy = $('aw-missing-policy') && $('aw-missing-policy').value || 'show_missing';
    }
    if (parameters.includes('sort')) {
      request.sort = {
        key: $('aw-sort-key') && $('aw-sort-key').value || plain(source.sort).key || 'species',
        direction: $('aw-sort-direction') && $('aw-sort-direction').value || 'asc',
      };
    }
    if (record && record.supports_sensitivity === true) {
      request.sensitivity_deadbands_eV = Array.from(
        document.querySelectorAll('#aw-sensitivity input[type=checkbox]:checked'))
        .map(input => Number(input.value)).filter(Number.isFinite);
    }
    if (State.analysisId === 'multi-project-comparison') {
      const selected = Array.from($('aw-projects') && $('aw-projects').selectedOptions || [])
        .map(option => safeId(option.value)).filter(Boolean);
      if (!selected.includes(State.projectId)) selected.unshift(State.projectId);
      request.comparison_project_ids = selected;
      request.baseline_project_id = safeId($('aw-baseline') && $('aw-baseline').value) || null;
    }
    if (State.analysisId === 'free-energy-path' && parameters.includes('conditions')) {
      const conditions = {};
      [
        ['aw-temperature', 'temperature_k'], ['aw-pressure', 'pressure_pa'],
        ['aw-ph', 'ph'], ['aw-potential', 'electrode_potential_v'],
        ['aw-coverage', 'coverage'],
      ].forEach(([id, key]) => {
        const raw = $(id) && String($(id).value || '').trim();
        if (!raw) return;
        const value = Number(raw); if (Number.isFinite(value)) conditions[key] = value;
      });
      request.conditions = conditions;
    }
    return request;
  }

  function renderRegistry() {
    const list = $('aw-registry'); if (!list) return; list.innerHTML = '';
    const favorites = new Set(plain(State.preferences).favorites || []);
    const records = [...(plain(State.catalog).analyses || [])].sort((left, right) =>
      Number(favorites.has(right.id)) - Number(favorites.has(left.id)));
    records.forEach(record => {
      const item = document.createElement('li'); const button = document.createElement('button');
      button.type = 'button'; button.dataset.analysisId = String(record.id || '');
      const capabilityStatus = String(record.capability_status || 'unavailable');
      const activatable = record.activatable === true && capabilityStatus === 'available';
      button.disabled = State.busy || !activatable;
      button.dataset.capabilityStatus = capabilityStatus;
      button.dataset.favorite = favorites.has(record.id) ? 'true' : 'false';
      button.setAttribute('aria-current', record.id === State.analysisId ? 'true' : 'false');
      const label = localized(record, 'label', record.id || tr('analysis.generic', '分析'));
      button.setAttribute('aria-label', label + (favorites.has(record.id)
        ? tr('analysis.favorite.suffix', '，已收藏') : ''));
      const title = document.createElement('b');
      title.textContent = (favorites.has(record.id) ? '★ ' : '') + label;
      const desc = document.createElement('small');
      desc.id = `aw-analysis-desc-${String(record.id || '').replace(/[^A-Za-z0-9_-]/g, '-')}`;
      desc.textContent = localized(record, 'description');
      const status = document.createElement('span'); status.className = 'aw-capability-status';
      status.dataset.status = capabilityStatus;
      const statusKey = CAPABILITY_LABEL_KEYS[capabilityStatus] || CAPABILITY_LABEL_KEYS.unavailable;
      status.textContent = tr(statusKey, capabilityStatus);
      const action = document.createElement('small'); action.className = 'aw-next-action';
      const actionKey = CAPABILITY_ACTION_KEYS[capabilityStatus] || CAPABILITY_ACTION_KEYS.unavailable;
      action.textContent = tr(actionKey, String(record.next_action || ''));
      button.setAttribute('aria-describedby', desc.id);
      button.append(title, status, desc, action); item.appendChild(button); list.appendChild(item);
    });
    list.setAttribute('aria-busy', State.busy || !State.catalog ? 'true' : 'false');
  }

  function renderTemplates() {
    const list = $('aw-template-list'); if (!list) return; list.innerHTML = '';
    const builtins = plain(State.catalog).view_templates || [];
    const user = plain(State.preferences).templates || [];
    const templates = [...builtins, ...user];
    const compatible = templates.filter(item => String(item.analysis_id || '') === State.analysisId);
    if (!compatible.length) { const item = document.createElement('li');
      item.textContent = tr('analysis.templates.empty', '当前分析没有视图模板。'); list.appendChild(item); return; }
    compatible.forEach(template => {
      const item = document.createElement('li'); const button = document.createElement('button');
      button.type = 'button'; button.dataset.templateId = safeId(template.id);
      button.disabled = State.busy;
      const title = document.createElement('b'); title.textContent = localized(
        template, 'label', template.name || template.id);
      const desc = document.createElement('small'); desc.textContent = template.schema === 'vcstudio.analysis-view-template/v1'
        ? tr('analysis.templates.builtin', '内置模板') : tr('analysis.templates.user', '用户模板');
      button.append(title, desc); item.appendChild(button); list.appendChild(item);
    });
  }

  function replaceOptions(select, values, selected) {
    if (!select) return; const current = String(selected == null ? '' : selected); select.innerHTML = '';
    values.forEach(value => {
      const record = typeof value === 'string' ? { id: value, label: value } : plain(value);
      const option = document.createElement('option'); option.value = String(record.id || '');
      option.textContent = localized(record, 'label', record.id || '');
      if (option.value === current) option.selected = true; select.appendChild(option);
    });
  }

  function selectedComparisonProjectIds() {
    const select = $('aw-projects');
    const selected = Array.from(select && select.selectedOptions || [])
      .map(option => safeId(option.value)).filter(Boolean);
    if (State.projectId && !selected.includes(State.projectId)) selected.unshift(State.projectId);
    return selected;
  }

  function renderBaselineOptions(selectedIds, selectedBaseline) {
    const baseline = $('aw-baseline'); if (!baseline) return;
    const selected = new Set((selectedIds || []).map(safeId));
    baseline.innerHTML = '';
    const none = document.createElement('option'); none.value = '';
    none.textContent = tr('analysis.baseline.none', '不设基准'); baseline.appendChild(none);
    State.projects.forEach(project => {
      const id = projectIdentity(project); if (!id || !selected.has(id)) return;
      const option = document.createElement('option'); option.value = id;
      option.textContent = String(project.name || id); baseline.appendChild(option);
    });
    const wanted = safeId(selectedBaseline);
    baseline.value = wanted && selected.has(wanted) ? wanted : '';
  }

  function renderProjects() {
    const select = $('aw-projects'); const baseline = $('aw-baseline'); if (!select || !baseline) return;
    const selected = new Set((State.spec && State.spec.comparison_project_ids || []).map(safeId));
    selected.add(State.projectId); select.innerHTML = '';
    State.projects.forEach(project => {
      const id = projectIdentity(project); if (!id) return; const label = String(project.name || id);
      const option = document.createElement('option'); option.value = id; option.textContent = label;
      option.selected = selected.has(id); select.appendChild(option);
      if (id === State.projectId) option.disabled = true;
    });
    renderBaselineOptions(Array.from(selected), State.spec && State.spec.baseline_project_id);
  }

  function renderSensitivityControls() {
    const box = $('aw-sensitivity'); if (!box) return; box.innerHTML = '';
    const selected = new Set((State.spec && State.spec.sensitivity_deadbands_eV || []).map(Number));
    [0.05, 0.10, 0.15, 0.20, 0.25, 0.30].forEach(value => {
      const label = document.createElement('label'); const input = document.createElement('input');
      input.type = 'checkbox'; input.value = String(value); input.checked = selected.has(value);
      label.append(input, document.createTextNode(value.toFixed(2))); box.appendChild(label);
    });
  }

  function renderControls() {
    const spec = plain(State.spec); const record = analysisRecord();
    const parameters = Array.isArray(record && record.parameters) ? record.parameters : [];
    const supportsSort = parameters.includes('sort');
    replaceOptions($('aw-data-mode'), (record && record.data_modes || ['stable']).map(id => ({
      id, label_zh: id === 'stable' ? '最稳/近简并' : '全部构型',
      label_en: id === 'stable' ? 'Stable / near-degenerate' : 'All configurations',
    })), spec.data_mode);
    if ($('aw-deadband')) $('aw-deadband').value = String(spec.near_degenerate_eV == null ? 0.15 : spec.near_degenerate_eV);
    if ($('aw-precision')) $('aw-precision').value = String(spec.precision == null ? 4 : spec.precision);
    if ($('aw-missing-policy')) $('aw-missing-policy').value = String(spec.missing_policy || 'show_missing');
    const sortKeys = supportsSort && Array.isArray(record && record.sort_keys) ? record.sort_keys : [];
    replaceOptions($('aw-sort-key'), sortKeys.map(id => ({ id, label: id })), plain(spec.sort).key);
    if ($('aw-sort-direction')) $('aw-sort-direction').value = String(plain(spec.sort).direction || 'asc');
    const conditions = plain(spec.conditions);
    [
      ['aw-temperature', 'temperature_k'], ['aw-pressure', 'pressure_pa'],
      ['aw-ph', 'ph'], ['aw-potential', 'electrode_potential_v'],
      ['aw-coverage', 'coverage'],
    ].forEach(([id, key]) => {
      if ($(id)) $(id).value = conditions[key] == null ? '' : String(conditions[key]);
    });
    renderProjects(); renderSensitivityControls();
    const comparison = State.analysisId === 'multi-project-comparison';
    if ($('aw-data-mode')) $('aw-data-mode').disabled =
      !Array.isArray(record && record.data_modes) || record.data_modes.length <= 1 || State.busy;
    ['aw-projects', 'aw-baseline'].forEach(id => { if ($(id)) $(id).disabled = !comparison || State.busy; });
    ['aw-sort-key', 'aw-sort-direction'].forEach(id => { if ($(id)) $(id).disabled = !supportsSort || State.busy; });
    if ($('aw-deadband')) $('aw-deadband').disabled = !parameters.includes('near_degenerate_eV') || State.busy;
    const supportsConditions = State.analysisId === 'free-energy-path' && parameters.includes('conditions');
    if ($('aw-condition-fields')) $('aw-condition-fields').hidden = !supportsConditions;
    ['aw-temperature', 'aw-pressure', 'aw-ph', 'aw-potential', 'aw-coverage'].forEach(id => {
      if ($(id)) $(id).disabled = !supportsConditions || State.busy;
    });
    if ($('aw-precision')) $('aw-precision').disabled = !parameters.includes('precision') || State.busy;
    if ($('aw-missing-policy')) $('aw-missing-policy').disabled = !parameters.includes('missing_policy') || State.busy;
    document.querySelectorAll('#aw-sensitivity input').forEach(input => {
      input.disabled = !(record && record.supports_sensitivity === true) || State.busy;
    });
  }

  function renderDenominator(view) {
    const box = $('aw-denominator'); if (!box) return; box.innerHTML = '';
    const source = plain(view && view.denominator);
    const labels = {
      input_configurations: tr('analysis.denominator.input_configurations', '输入构型'),
      numeric_configurations: tr('analysis.denominator.numeric_configurations', '数值构型'),
      missing_configurations: tr('analysis.denominator.missing_configurations', '缺失构型'),
      species_with_numeric_results: tr('analysis.denominator.species_numeric', '有效物种'),
      visible_rows: tr('analysis.denominator.visible_rows', '可见行'),
      near_degenerate_groups: tr('analysis.denominator.near_degenerate', '近简并组'),
      selected_projects: tr('analysis.denominator.selected_projects', '所选项目'),
      ready_projects: tr('analysis.denominator.ready_projects', '有效项目'),
      blocked_projects: tr('analysis.denominator.blocked_projects', '阻断项目'),
      species_columns: tr('analysis.denominator.species_columns', '物种列'),
      possible_numeric_cells: tr('analysis.denominator.possible_cells', '应有数值'),
      observed_numeric_cells: tr('analysis.denominator.observed_cells', '已有数值'),
      missing_numeric_cells: tr('analysis.denominator.missing_cells', '缺失数值'),
      requested_paths: tr('analysis.denominator.requested_paths', '请求路径'),
      available_paths: tr('analysis.denominator.available_paths', '可用路径'),
      input_steps: tr('analysis.denominator.input_steps', '输入台阶'),
      numeric_steps: tr('analysis.denominator.numeric_steps', '数值台阶'),
      missing_steps: tr('analysis.denominator.missing_steps', '缺失台阶'),
      input_transitions: tr('analysis.denominator.input_transitions', '输入转化'),
      valid_transitions: tr('analysis.denominator.valid_transitions', '有效转化'),
      condition_points: tr('analysis.denominator.condition_points', '工况点'),
      converged_points: tr('analysis.denominator.converged_points', '收敛工况点'),
      elementary_steps: tr('analysis.denominator.elementary_steps', '基元步骤'),
      species: tr('analysis.denominator.species', '物种'),
    };
    Object.entries(source).forEach(([key, value]) => {
      const item = document.createElement('span'); const label = document.createElement('small');
      const number = document.createElement('b'); label.textContent = labels[key] || key; number.textContent = String(value);
      item.append(label, number); box.appendChild(item);
    });
    if (!box.children.length) {
      const item = document.createElement('span'); const label = document.createElement('small');
      const value = document.createElement('b'); label.textContent = tr('analysis.denominator.label', '分母');
      value.textContent = tr('common.unknown', '未知'); item.append(label, value); box.appendChild(item);
    }
  }

  function tableElement(headers, rows) {
    const table = document.createElement('table'); table.className = 'aw-table';
    const thead = document.createElement('thead'); const head = document.createElement('tr');
    headers.forEach(header => { const th = document.createElement('th'); th.textContent = header.label; if (header.numeric) th.className = 'num'; head.appendChild(th); });
    thead.appendChild(head); const tbody = document.createElement('tbody');
    rows.forEach(row => {
      const tr = document.createElement('tr'); if (row.missing) tr.className = 'missing';
      row.cells.forEach((value, index) => { const td = document.createElement('td'); if (headers[index] && headers[index].numeric) td.className = 'num'; td.textContent = String(value == null ? '—' : value); tr.appendChild(td); });
      tbody.appendChild(tr);
    });
    table.append(thead, tbody); return table;
  }

  function renderAdsorptionTable(view, box) {
    const rows = (view.rows || []).map(row => ({
      missing: row.delta_e_eV == null,
      cells: [row.species, row.name || row.configuration_id, row.delta_e_display,
        row.relative_to_minimum_display,
        row.near_degenerate ? tr('analysis.stability.near_degenerate', '近简并')
          : row.is_minimum ? tr('analysis.stability.minimum', '最低') : '',
        row.method_status, row.state, row.note],
    }));
    if (!rows.length) { box.innerHTML = '';
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = tr('analysis.adsorption.empty', '当前范围没有可显示构型；请查看阻断项与分母。');
      box.appendChild(empty); return; }
    box.innerHTML = ''; box.appendChild(tableElement([
      { label: tr('analysis.table.species', '物种') },
      { label: tr('analysis.table.configuration', '构型') },
      { label: 'E_ads / eV', numeric: true }, { label: 'ΔΔE / eV', numeric: true },
      { label: tr('analysis.table.stability', '稳定性') },
      { label: tr('analysis.table.method', '方法') },
      { label: tr('analysis.table.job_status', '作业状态') },
      { label: tr('analysis.table.notes', '说明') },
    ], rows));
  }

  function renderComparisonTable(view, box) {
    const matrix = plain(view.matrix); const projectIds = matrix.project_ids || [];
    const projects = new Map((view.projects || []).map(project => [String(project.project_id || ''), project]));
    const headers = [{ label: tr('analysis.table.project', '项目') },
      ...(matrix.cols || []).map(label => ({ label, numeric: true }))];
    const rows = projectIds.map((projectId, index) => ({
      cells: [plain(projects.get(projectId)).display_name || plain(projects.get(projectId)).name || projectId,
        ...((matrix.display_values || [])[index] || [])],
    }));
    if (!rows.length) { box.innerHTML = '';
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = tr('analysis.comparison.empty', '没有通过比较门禁的项目；请查看阻断项。');
      box.appendChild(empty); return; }
    box.innerHTML = ''; box.appendChild(tableElement(headers, rows));
  }

  function renderServerResults(view, box) {
    const rows = Array.isArray(view.rows) ? view.rows : [];
    box.innerHTML = '';
    if (!rows.length) {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = String(view.reason || (view.missing || view.blocking || [])[0] ||
        tr('analysis.results.empty', '没有服务器最终确认的结果；请检查前置条件。'));
      box.appendChild(empty); return;
    }
    rows.forEach(row => {
      const card = document.createElement('section'); card.className = 'aw-result-card';
      const header = document.createElement('header');
      const title = document.createElement('b');
      const source = plain(row.source); const sourceIds = row.source_ids || [];
      title.textContent = String(row.kind || source.task_type || tr('analysis.record', '记录'));
      const status = document.createElement('span'); status.className = 'aw-capability-status';
      status.dataset.status = String(row.status || (row.available ? 'available' : 'unavailable'));
      status.textContent = String(row.status || (row.available ? 'available' : 'unavailable'));
      header.append(title, status); card.appendChild(header);
      const identity = document.createElement('code');
      identity.textContent = String(source.source_id || sourceIds.join(', ') || '—');
      card.appendChild(identity);
      const values = Array.isArray(row.values) ? row.values : [];
      if (values.length) card.appendChild(tableElement([
        { label: tr('analysis.results.quantity', '量') },
        { label: tr('analysis.results.value', '服务器结果'), numeric: true },
        { label: tr('analysis.results.unit', '单位') },
      ], values.map(value => ({ cells: [value.label || value.key, value.display, value.unit] }))));
      const summary = document.createElement('p'); summary.textContent = String(row.summary || '');
      card.appendChild(summary);
      const serverResult = plain(row.result);
      if (serverResult.analysis_kind === 'elf_distribution_summary') {
        const quantiles = Array.isArray(serverResult.display_quantiles)
          ? serverResult.display_quantiles : [];
        if (quantiles.length) card.appendChild(tableElement([
          { label: tr('analysis.elf.quantile', '分位点') },
          { label: tr('analysis.elf.value', '服务器 ELF 值'), numeric: true },
        ], quantiles.map(item => ({ cells: [item.fraction, item.value] }))));
        const histogram = Array.isArray(serverResult.display_histogram)
          ? serverResult.display_histogram : [];
        if (histogram.length) card.appendChild(tableElement([
          { label: tr('analysis.elf.bin', 'ELF 区间') },
          { label: tr('analysis.elf.count', '服务器网格点数'), numeric: true },
        ], histogram.map(item => ({ cells: [`${item.low}–${item.high}`, item.count] }))));
        const boundary = document.createElement('small'); boundary.className = 'aw-elf-boundary';
        boundary.textContent = tr('analysis.elf.boundary',
          '仅显示 ELFCAR 网格分布摘要；不据此宣称成键、盆、临界点或拓扑结论。');
        card.appendChild(boundary);
      }
      box.appendChild(card);
    });
  }

  function quantityText(value) {
    const record = plain(value);
    return record.display === null || record.display === undefined || record.display === ''
      ? '—' : String(record.display);
  }

  function appendDiagnosticStrip(parent, text, status = 'diagnostic') {
    const strip = document.createElement('div'); strip.className = 'aw-diagnostic-strip';
    strip.dataset.status = String(status || 'diagnostic'); strip.setAttribute('role', 'status');
    strip.textContent = String(text || 'Diagnostic evidence only.'); parent.appendChild(strip);
  }

  function appendEvidenceList(parent, titleText, values) {
    const section = document.createElement('section'); section.className = 'aw-specialized-evidence';
    const title = document.createElement('h4'); const list = document.createElement('ul');
    title.textContent = String(titleText || 'Evidence notes');
    (Array.isArray(values) ? values : []).forEach(value => {
      const item = document.createElement('li'); item.textContent = String(value); list.appendChild(item);
    });
    if (!list.children.length) {
      const item = document.createElement('li'); item.textContent = '—'; list.appendChild(item);
    }
    section.append(title, list); parent.appendChild(section);
  }

  function appendSourceDetails(parent, sourceValue, parserValue) {
    const source = plain(sourceValue); const parser = plain(parserValue);
    const details = document.createElement('details'); details.className = 'aw-source-details';
    const summary = document.createElement('summary');
    summary.textContent = `Evidence · ${String(source.source_id || '—')}`; details.appendChild(summary);
    const identity = document.createElement('code');
    identity.textContent = [parser.module, parser.version].filter(Boolean).join(' @ ') || 'parser unavailable';
    details.appendChild(identity);
    const list = document.createElement('ul');
    (Array.isArray(source.files) ? source.files : []).forEach(file => {
      const item = document.createElement('li'); const name = document.createElement('span');
      const hash = document.createElement('code'); name.textContent = String(file.name || 'file');
      hash.textContent = String(file.sha256 || 'hash unavailable'); item.append(name, hash); list.appendChild(item);
    });
    if (!list.children.length) {
      const item = document.createElement('li'); item.textContent = 'No hashed file evidence.'; list.appendChild(item);
    }
    details.appendChild(list); parent.appendChild(details);
  }

  function appendSpecializedHeader(card, label, statusValue, sourceId) {
    const header = document.createElement('header'); const title = document.createElement('b');
    const status = document.createElement('span'); status.className = 'aw-capability-status';
    title.textContent = String(label || sourceId || tr('analysis.record', '记录'));
    status.dataset.status = String(statusValue || 'unavailable');
    status.textContent = String(statusValue || 'unavailable'); header.append(title, status); card.appendChild(header);
  }

  function renderNebView(view, box) {
    const paths = Array.isArray(view.paths) ? view.paths : [];
    box.innerHTML = '';
    if (!paths.length) {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = String((view.missing || view.blocking || [])[0] ||
        tr('analysis.results.empty', '没有服务器最终确认的结果；请检查前置条件。'));
      box.appendChild(empty); return;
    }
    paths.forEach(pathValue => {
      const path = plain(pathValue); const source = plain(path.source); const barriers = plain(path.barriers);
      const quality = plain(path.path_quality); const points = Array.isArray(path.points) ? path.points : [];
      const card = document.createElement('section'); card.className = 'aw-specialized-card aw-neb-card';
      appendSpecializedHeader(card, 'NEB image path', path.status, source.source_id);
      appendDiagnosticStrip(card, path.scientific_boundary, quality.status);
      const metrics = document.createElement('dl'); metrics.className = 'aw-specialized-metrics';
      [
        ['Forward barrier', plain(barriers.forward)],
        ['Reverse barrier', plain(barriers.reverse)],
      ].forEach(([label, quantity]) => {
        const group = document.createElement('div'); const term = document.createElement('dt');
        const detail = document.createElement('dd'); term.textContent = label;
        detail.textContent = `${quantityText(quantity)} ${String(quantity.unit || '')}`.trim();
        group.append(term, detail); metrics.appendChild(group);
      });
      card.appendChild(metrics);
      if (points.length) {
        const table = tableElement([
          { label: 'Image' }, { label: 'Reaction coordinate', numeric: true },
          { label: 'Relative energy / eV', numeric: true },
          { label: 'Max force / eV/Å', numeric: true },
          { label: 'Electronic convergence' }, { label: 'Ionic convergence' },
        ], points.map(point => ({ cells: [
          point.image_label, quantityText(point.reaction_coordinate),
          quantityText(point.relative_energy), quantityText(point.max_force),
          point.electronic_convergence, point.ionic_convergence,
        ] })));
        table.setAttribute('aria-label', 'Server-finalized NEB image curve data'); card.appendChild(table);
      }
      appendEvidenceList(card, 'Path-quality diagnostics', quality.issues);
      appendEvidenceList(card, 'Path warnings', quality.warnings);
      appendSourceDetails(card, source, path.parser);
      Object.values(plain(path.endpoint_evidence)).forEach(endpoint => {
        const endpointSource = plain(endpoint.source);
        if (endpointSource.source_id) appendSourceDetails(card, endpointSource, path.parser);
      });
      box.appendChild(card);
    });
  }

  function renderConvergenceView(view, box) {
    const records = Array.isArray(view.series) ? view.series : [];
    box.innerHTML = '';
    if (!records.length) {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = String((view.missing || view.blocking || [])[0] ||
        tr('analysis.results.empty', '没有服务器最终确认的结果；请检查前置条件。'));
      box.appendChild(empty); return;
    }
    records.forEach(recordValue => {
      const record = plain(recordValue); const platform = plain(record.platform);
      const points = Array.isArray(record.points) ? record.points : [];
      const card = document.createElement('section'); card.className = 'aw-specialized-card aw-convergence-card';
      appendSpecializedHeader(card, `Convergence · ${String(record.kind || '')}`, record.status, record.series_id);
      appendDiagnosticStrip(card, record.scientific_boundary, platform.status);
      const recommendation = document.createElement('p'); recommendation.className = 'aw-recommendation';
      const recommended = plain(platform.recommendation);
      recommendation.textContent = `Threshold ${String(platform.threshold_mev_per_atom == null ? '—' : platform.threshold_mev_per_atom)} meV/atom · recommended ${quantityText(recommended)} ${String(recommended.unit || '')}${recommended.reason ? ` · ${String(recommended.reason)}` : ''}`;
      card.appendChild(recommendation);
      if (points.length) {
        const table = tableElement([
          { label: 'Point' }, { label: 'Parameter', numeric: true },
          { label: 'E / eV', numeric: true }, { label: 'Atoms', numeric: true },
          { label: 'E / atom', numeric: true },
          { label: 'Δ terminal / meV/atom', numeric: true },
          { label: 'Platform' }, { label: 'Missing / anomaly' },
        ], points.map(point => ({
          missing: plain(point.absolute_energy).value == null,
          cells: [point.label, quantityText(point.parameter), quantityText(point.absolute_energy),
            quantityText(point.atom_count),
            quantityText(point.energy_per_atom), quantityText(point.delta_per_atom),
            point.platform_member === true ? 'yes' : 'no', (point.anomalies || []).join('; ')],
        })));
        table.setAttribute('aria-label', 'Server-finalized convergence curve data'); card.appendChild(table);
      }
      const sensitivity = Array.isArray(record.sensitivity) ? record.sensitivity : [];
      if (sensitivity.length) {
        const table = tableElement([
          { label: 'Threshold / meV/atom', numeric: true },
          { label: 'Server recommendation', numeric: true },
          { label: 'Denominator' },
        ], sensitivity.map(item => ({ cells: [
          quantityText(item.threshold), quantityText(item.recommended_parameter),
          plain(item.recommended_parameter).denominator,
        ] })));
        table.setAttribute('aria-label', 'Server-finalized convergence threshold sensitivity');
        card.appendChild(table);
      }
      appendEvidenceList(card, 'Missing points and anomalies', record.issues);
      points.forEach(point => appendSourceDetails(card, point.source, record.parser));
      box.appendChild(card);
    });
  }

  function renderAimdView(view, box) {
    const trajectories = Array.isArray(view.trajectories) ? view.trajectories : [];
    box.innerHTML = '';
    if (!trajectories.length) {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = String((view.missing || view.blocking || [])[0] ||
        tr('analysis.results.empty', '没有服务器最终确认的结果；请检查前置条件。'));
      box.appendChild(empty); return;
    }
    trajectories.forEach(trajectoryValue => {
      const trajectory = plain(trajectoryValue); const source = plain(trajectory.source);
      const metrics = Object.values(plain(trajectory.metrics));
      const samples = Array.isArray(trajectory.samples) ? trajectory.samples : [];
      const segments = Array.isArray(trajectory.segments) ? trajectory.segments : [];
      const card = document.createElement('section'); card.className = 'aw-specialized-card aw-aimd-card';
      appendSpecializedHeader(card, 'AIMD diagnostics', trajectory.status, source.source_id);
      appendDiagnosticStrip(card, trajectory.scientific_boundary, 'diagnostic');
      if (metrics.length) {
        const table = tableElement([
          { label: tr('analysis.results.quantity', '量') },
          { label: tr('analysis.results.value', '服务器结果'), numeric: true },
          { label: tr('analysis.results.unit', '单位') }, { label: 'Denominator' },
        ], metrics.map(metric => ({ cells: [
          metric.label || metric.key, quantityText(metric), metric.unit, metric.denominator,
        ] })));
        table.setAttribute('aria-label', 'Server-finalized AIMD diagnostic metrics'); card.appendChild(table);
      }
      if (segments.length) {
        const table = tableElement([
          { label: 'Segment', numeric: true }, { label: 'Start step', numeric: true },
          { label: 'End step', numeric: true }, { label: 'Samples', numeric: true },
          { label: 'Duration / ps', numeric: true },
        ], segments.map(segment => ({ cells: [
          segment.segment_index, quantityText(segment.start_step),
          quantityText(segment.end_step), quantityText(segment.sample_count),
          quantityText(segment.duration),
        ] })));
        table.setAttribute('aria-label', 'Server-finalized AIMD monotonic step segments');
        card.appendChild(table);
      }
      if (samples.length) {
        const table = tableElement([
          { label: 'Sample', numeric: true }, { label: 'Step', numeric: true },
          { label: 'Segment', numeric: true }, { label: 'Time / ps', numeric: true },
          { label: 'Total energy / eV', numeric: true },
          { label: 'Temperature / K', numeric: true },
        ], samples.map(sample => ({ cells: [
          sample.sample_index, quantityText(sample.step), sample.segment_index,
          quantityText(sample.time),
          quantityText(sample.total_energy), quantityText(sample.temperature),
        ] })));
        table.setAttribute('aria-label', 'Server-finalized AIMD curve data'); card.appendChild(table);
      }
      appendEvidenceList(card, 'Diagnostic limitations', trajectory.issues);
      appendEvidenceList(card, 'Sampling warnings', trajectory.warnings);
      appendSourceDetails(card, source, trajectory.parser); box.appendChild(card);
    });
  }

  function appendFreeEnergyMetric(list, labelText, value, unit = '') {
    const group = document.createElement('div'); group.className = 'aw-free-energy-metric';
    const term = document.createElement('dt'); const detail = document.createElement('dd');
    const missing = value === null || value === undefined || value === '';
    term.textContent = labelText;
    detail.textContent = missing ? '—' : `${String(value)}${unit ? ` ${unit}` : ''}`;
    group.append(term, detail); list.appendChild(group);
  }

  function appendFreeEnergyMessages(parent, titleText, values, emptyText) {
    const section = document.createElement('section'); section.className = 'aw-free-energy-evidence';
    const title = document.createElement('h4'); const list = document.createElement('ul');
    title.textContent = titleText;
    (Array.isArray(values) ? values : []).forEach(value => {
      const item = document.createElement('li'); item.textContent = String(value); list.appendChild(item);
    });
    if (!list.children.length) {
      const item = document.createElement('li'); item.textContent = emptyText; list.appendChild(item);
    }
    section.append(title, list); parent.appendChild(section);
  }

  function reactionSection(parent, id, titleText, description) {
    const section = document.createElement('section'); section.className = 'aw-reaction-section';
    section.setAttribute('role', 'region'); section.setAttribute('aria-labelledby', id);
    const heading = document.createElement('h3'); heading.id = id; heading.textContent = titleText;
    section.appendChild(heading);
    if (description) {
      const note = document.createElement('p'); note.className = 'aw-reaction-note';
      note.textContent = description; section.appendChild(note);
    }
    parent.appendChild(section); return section;
  }

  function ledgerTermDisplay(row, key) {
    const term = (row.terms || []).find(item => String(item.key || '') === key);
    return String(term && term.display || 'unavailable');
  }

  function renderReactionWorkbench(view, box) {
    box.innerHTML = '';
    const graph = plain(view.graph); const ledger = plain(view.ledger);
    const revision = plain(view.condition_revision);
    const mapSection = reactionSection(box, 'aw-reaction-map-title',
      tr('analysis.reaction.map.title', 'Reaction Map'),
      tr('analysis.reaction.map.boundary', '拓扑、哈希、缺边和状态均由服务器冻结；图完整不等于机理完整。'));
    const nodes = graph.nodes || []; const edges = graph.edges || [];
    if (nodes.length) {
      const table = tableElement([
        { label: tr('analysis.reaction.object_id', 'Opaque ID') },
        { label: tr('analysis.reaction.entity_type', '类型') },
        { label: tr('analysis.reaction.label', '标签') },
        { label: tr('analysis.reaction.structure_hash', '结构 hash') },
        { label: tr('analysis.reaction.method_hash', '方法 hash') },
        { label: tr('analysis.reaction.evidence_hash', '证据 hash') },
        { label: tr('analysis.reaction.status', '状态') },
      ], nodes.map(node => ({ cells: [
        node.node_id, node.entity_type, node.label, node.structure_sha256,
        node.method_sha256, node.evidence_sha256, node.artifact_status,
      ] })));
      const caption = document.createElement('caption');
      caption.textContent = tr('analysis.reaction.map.nodes', '绑定节点');
      table.prepend(caption); mapSection.appendChild(table);
    }
    const edgeTable = tableElement([
      { label: tr('analysis.reaction.step', 'ElementaryStep') },
      { label: tr('analysis.reaction.reactants', '反应物') },
      { label: tr('analysis.reaction.products', '产物') },
      { label: 'TS' }, { label: 'ΔG / eV', numeric: true },
      { label: 'Observed ΔE‡ / eV', numeric: true },
      { label: 'Thermal ΔG‡ / eV', numeric: true },
      { label: tr('analysis.reaction.status', '状态') },
      { label: tr('analysis.reaction.missing', '缺失') },
    ], edges.map(edge => {
      const display = plain(edge.display); const thermo = plain(edge.thermochemistry);
      return { cells: [edge.edge_id, display.reactants, display.products,
        display.transition_state, thermo.reaction_delta_g_display,
        thermo.observed_activation_delta_e_display,
        thermo.thermal_activation_delta_g_display, edge.artifact_status,
        (edge.missing || []).join(', ') || '—'] };
    }));
    const edgeCaption = document.createElement('caption');
    edgeCaption.textContent = tr('analysis.reaction.map.edges', '绑定边与显式缺边');
    edgeTable.prepend(edgeCaption); mapSection.appendChild(edgeTable);
    appendFreeEnergyMessages(mapSection,
      tr('analysis.reaction.map.missing_edges', 'Missing edges'),
      (graph.missing_edges || []).map(item => `${item.edge_id}: ${item.reason}`),
      tr('analysis.reaction.map.missing_edges_empty', '服务器未报告缺边。'));

    const ledgerSection = reactionSection(box, 'aw-thermo-ledger-title',
      tr('analysis.reaction.ledger.title', 'Thermochemistry Ledger'),
      tr('analysis.reaction.ledger.boundary', 'E0、ZPE、ΔH、-TΔS、标准态与最终 ΔG 逐项显示；缺证据即 unavailable。'));
    const ledgerRows = ledger.rows || [];
    const ledgerTable = tableElement([
      { label: tr('analysis.reaction.entity', '实体') }, { label: 'E0 / eV', numeric: true },
      { label: 'ZPE / eV', numeric: true }, { label: 'ΔH / eV', numeric: true },
      { label: '-TΔS / eV', numeric: true },
      { label: tr('analysis.reaction.standard_state', '标准态') },
      { label: tr('analysis.reaction.conditions', 'T / P') },
      { label: tr('analysis.reaction.models', '模型') },
      { label: 'ΔG / eV', numeric: true },
      { label: tr('analysis.reaction.status', '状态') },
    ], ledgerRows.map(row => ({ cells: [
      `${row.label} (${row.entity_id})`, ledgerTermDisplay(row, 'electronic_energy_e0_eV'),
      ledgerTermDisplay(row, 'zpe_eV'), ledgerTermDisplay(row, 'delta_h_thermal_eV'),
      ledgerTermDisplay(row, 'minus_t_delta_s_eV'), plain(row.standard_state).display,
      `${row.temperature_display} / ${row.pressure_display}`, row.models_display,
      row.final_delta_g_display, row.artifact_status,
    ] })));
    const ledgerCaption = document.createElement('caption');
    ledgerCaption.textContent = tr('analysis.reaction.ledger.caption', '服务器最终确定的热化学账本');
    ledgerTable.prepend(ledgerCaption); ledgerSection.appendChild(ledgerTable);
    const modeGrid = document.createElement('div'); modeGrid.className = 'aw-reaction-evidence-grid';
    ledgerRows.forEach(row => {
      const card = document.createElement('article'); card.className = 'aw-reaction-evidence-card';
      const title = document.createElement('h4'); title.textContent = `${row.label} · ${row.entity_type}`;
      const low = plain(row.low_frequency); const frequency = plain(row.frequency_qualification);
      const original = document.createElement('p'); original.textContent =
        `${tr('analysis.reaction.low_frequency.original', '原始低频')}: ${low.original_frequencies_display || 'unavailable'}`;
      const rule = document.createElement('p'); rule.textContent =
        `${tr('analysis.reaction.low_frequency.rule', '规则')}: ${low.rule || 'none'} · ${low.reason || '—'}`;
      const qualification = document.createElement('p'); qualification.textContent =
        `${tr('analysis.reaction.ts_qualification', 'TS 频率/模式资格')}: ${frequency.display || 'unavailable'} · ${frequency.reason || '—'}`;
      card.append(title, original, rule, qualification); modeGrid.appendChild(card);
    });
    ledgerSection.appendChild(modeGrid);

    const conditionSection = reactionSection(box, 'aw-condition-revision-title',
      tr('analysis.reaction.condition_revision.title', 'Condition Explorer derived revision'),
      tr('analysis.reaction.condition_revision.boundary', '参数只生成 hash-bound derived revision；不改写 canonical DTO 或原始 evidence。'));
    const revisionMeta = document.createElement('dl'); revisionMeta.className = 'aw-free-energy-metrics';
    appendFreeEnergyMetric(revisionMeta, tr('analysis.reaction.revision_id', 'Revision ID'), revision.revision_id);
    appendFreeEnergyMetric(revisionMeta, tr('analysis.reaction.revision_hash', 'Revision hash'), revision.revision_sha256);
    appendFreeEnergyMetric(revisionMeta, tr('analysis.reaction.applicability', '适用范围'),
      Object.entries(plain(revision.applicability_display)).map(([key, value]) => `${key}: ${value}`).join(' · '));
    appendFreeEnergyMetric(revisionMeta, tr('analysis.reaction.source_mutated', '原始证据改写'),
      revision.source_evidence_mutated === false ? tr('common.no', '否') : 'unavailable');
    conditionSection.appendChild(revisionMeta);
    const conditionTable = tableElement([
      { label: tr('analysis.reaction.entity', '实体') },
      { label: tr('analysis.reaction.base_g', '基准 ΔG / eV'), numeric: true },
      { label: tr('analysis.reaction.condition_delta', '条件修正 / eV'), numeric: true },
      { label: tr('analysis.reaction.derived_g', '派生 ΔG / eV'), numeric: true },
      { label: tr('analysis.reaction.status', '状态') },
      { label: tr('analysis.reaction.missing', '缺失') },
    ], (revision.rows || []).map(row => ({ cells: [
      row.label, row.base_delta_g_display, row.condition_delta_g_display,
      row.derived_delta_g_display, row.status, (row.missing || []).join(', ') || '—',
    ] })));
    const conditionCaption = document.createElement('caption');
    conditionCaption.textContent = tr('analysis.reaction.condition_revision.caption',
      '服务器确定的条件派生值；浏览器不重算');
    conditionTable.prepend(conditionCaption); conditionSection.appendChild(conditionTable);

    const sensitivitySection = reactionSection(box, 'aw-parameter-sensitivity-title',
      tr('analysis.reaction.sensitivity.title', 'Parameter sensitivity'),
      tr('analysis.reaction.sensitivity.boundary', '保留低频原值、处理规则、理由及 evidence-bound sensitivity。'));
    const sensitivityRows = [];
    ledgerRows.forEach(row => (plain(row.low_frequency).sensitivity || []).forEach(item => {
      sensitivityRows.push({ cells: [row.label, item.parameter, item.value,
        item.unit, item.delta_g_eV, item.evidence_sha256] });
    }));
    if (sensitivityRows.length) sensitivitySection.appendChild(tableElement([
      { label: tr('analysis.reaction.entity', '实体') },
      { label: tr('analysis.reaction.parameter', '参数') },
      { label: tr('analysis.reaction.value', '取值'), numeric: true },
      { label: tr('analysis.reaction.unit', '单位') },
      { label: 'ΔG sensitivity / eV', numeric: true },
      { label: tr('analysis.reaction.evidence_hash', '证据 hash') },
    ], sensitivityRows));
    else {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = tr('analysis.reaction.sensitivity.empty', '没有 evidence-bound 参数敏感性结果。');
      sensitivitySection.appendChild(empty);
    }
  }

  function renderFreeEnergyView(view, box) {
    if (view.schema === 'vcstudio.reaction-workbench-view/v1') {
      renderReactionWorkbench(view, box); return;
    }
    const rows = Array.isArray(view.rows) ? view.rows : [];
    const pds = plain(view.pds); const thermo = plain(view.thermo); const method = plain(view.method);
    box.innerHTML = '';

    const summary = document.createElement('section'); summary.className = 'aw-free-energy-summary';
    summary.setAttribute('aria-label', tr('analysis.free_energy.summary_aria', '服务器确定的自由能路径摘要'));
    const heading = document.createElement('h3'); heading.textContent = tr('analysis.free_energy.summary', '自由能路径摘要');
    const metrics = document.createElement('dl'); metrics.className = 'aw-free-energy-metrics';
    const pdsText = pds.index === null || pds.index === undefined
      ? null
      : `#${String(pds.index)} · ${String(pds.from_label || '—')} → ${String(pds.to_label || '—')}`;
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.availability', '路径可用性'),
      view.available === true ? tr('common.available', '可用') : tr('common.blocked', '阻断'));
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.scientific_status', '科学状态'), view.scientific_status);
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.pds', '势决定步（PDS）'), pdsText);
    appendFreeEnergyMetric(metrics, 'U_L', view.u_l_display, 'V');
    appendFreeEnergyMetric(metrics, 'μLi', view.mu_li_display, 'eV');
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.thermo_status', '热校正状态'), thermo.status);
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.temperature', '温度'), thermo.temperature_display, 'K');
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.thermo_fingerprint', '热校正指纹'), thermo.correction_fingerprint);
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.method_status', '方法状态'), view.method_status || method.status);
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.method_managed', '方法受管'),
      Object.prototype.hasOwnProperty.call(method, 'managed')
        ? (method.managed === true ? tr('common.yes', '是') : tr('common.no', '否')) : null);
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.reference', '参考电极'), view.reference);
    appendFreeEnergyMetric(metrics, tr('analysis.free_energy.path_id', '反应路径标识'), view.reaction_path_id);
    summary.append(heading, metrics); box.appendChild(summary);

    const evidence = document.createElement('div'); evidence.className = 'aw-free-energy-evidence-grid';
    appendFreeEnergyMessages(evidence, tr('analysis.free_energy.missing', '缺失数据'), view.missing,
      tr('analysis.free_energy.missing_empty', '服务器未报告缺失数据。'));
    appendFreeEnergyMessages(evidence, tr('analysis.free_energy.blocking', '门禁阻断'), view.blocking,
      tr('analysis.blocking.empty', '当前无阻断项。'));
    appendFreeEnergyMessages(evidence, tr('analysis.free_energy.method_errors', '方法错误'), method.errors,
      tr('analysis.free_energy.method_errors_empty', '服务器未报告方法错误。'));
    appendFreeEnergyMessages(evidence, tr('analysis.free_energy.method_warnings', '方法告警'), method.warnings,
      tr('analysis.free_energy.method_warnings_empty', '服务器未报告方法告警。'));
    box.appendChild(evidence);

    if (!rows.length) {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = String(view.reason || tr('analysis.free_energy.rows_empty',
        '服务器未返回可展示的自由能台阶；请查看缺失数据与门禁阻断。'));
      box.appendChild(empty); return;
    }

    const table = tableElement([
      { label: tr('analysis.free_energy.step_index', '台阶序号'), numeric: true },
      { label: tr('analysis.free_energy.state', '状态') },
      { label: tr('analysis.free_energy.annotation', '补充标注') },
      { label: 'G / eV', numeric: true },
    ], rows.map(row => ({
      cells: [row.step_index, row.label, row.sub_label, row.G_display],
    })));
    const caption = document.createElement('caption');
    caption.textContent = tr('analysis.free_energy.caption', '服务器确定的有序自由能台阶（未在浏览器中重排或重算）');
    table.prepend(caption); box.appendChild(table);
  }

  function appendKineticTable(parent, titleText, identityLabel, records, identity, unit) {
    if (!Array.isArray(records) || !records.length) return;
    const section = document.createElement('section'); section.className = 'aw-result-card';
    const heading = document.createElement('header'); const title = document.createElement('b');
    title.textContent = titleText; heading.appendChild(title); section.appendChild(heading);
    section.appendChild(tableElement([
      { label: identityLabel },
      { label: tr('analysis.kinetic.server_value', '服务端结果'), numeric: true },
      { label: tr('analysis.kinetic.unit', '单位') },
    ], records.map(record => ({
      cells: [identity(record), record.display, unit || '—'],
    }))));
    parent.appendChild(section);
  }

  function renderKineticDashboard(view, box) {
    const points = Array.isArray(view.points) ? view.points : [];
    const units = plain(view.units); const audit = plain(view.audit);
    const adapter = plain(view.adapter); const configuredAdapter = plain(view.configured_adapter);
    const limitations = plain(view.limitations);
    box.innerHTML = '';
    const summary = document.createElement('section'); summary.className = 'aw-free-energy-summary';
    const title = document.createElement('h3');
    title.textContent = tr('analysis.kinetic.title', '微观动力学诊断 Dashboard');
    const metrics = document.createElement('dl'); metrics.className = 'aw-free-energy-metrics';
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.scientific_status', '科学状态'), view.scientific_status);
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.input_audit', '输入审计'), view.input_audit_status);
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.solver_status', '求解器状态'), view.solver_status);
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.result_status', '结果状态'), view.result_status);
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.catmap_version', 'CatMAP 版本'), adapter.version);
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.configured_catmap_version', '当前配置版本'),
      configuredAdapter.version);
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.tool_identity_match', '工具身份匹配'),
      configuredAdapter.matches_confirmed_export);
    appendFreeEnergyMetric(metrics, tr('analysis.kinetic.input_hash', '输入哈希'), audit.input_sha256);
    summary.append(title, metrics);
    const boundary = document.createElement('p'); boundary.className = 'aw-readonly-note';
    boundary.textContent = tr('analysis.kinetic.diagnostic_boundary',
      '只读诊断结果；浏览器不求解，且结果不能直接进入 accepted/final。');
    summary.appendChild(boundary); box.appendChild(summary);

    const evidence = document.createElement('div'); evidence.className = 'aw-free-energy-evidence-grid';
    appendFreeEnergyMessages(evidence, tr('analysis.kinetic.audit', '输入审计'),
      (audit.issues || []).map(item => `${String(item.code || '')}: ${String(item.message || '')}`),
      tr('analysis.kinetic.audit_empty', '输入审计未报告问题。'));
    appendFreeEnergyMessages(evidence, tr('analysis.kinetic.reason_codes', '不可用原因'),
      view.reason_codes || [], tr('analysis.kinetic.reason_codes_empty', '未报告不可用原因。'));
    box.appendChild(evidence);

    if (!points.length) {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = tr('analysis.kinetic.unavailable',
        '没有可展示的哈希匹配动力学结果；审计报告仍可预览和导出。');
      box.appendChild(empty);
    }
    points.forEach(point => {
      const pointSection = document.createElement('section'); pointSection.className = 'aw-kinetic-point';
      const pointTitle = document.createElement('h3');
      pointTitle.textContent = String(point.condition_display || tr('analysis.kinetic.condition', '工况'));
      pointSection.appendChild(pointTitle);
      const convergence = plain(point.convergence); const convergenceText = document.createElement('p');
      convergenceText.className = 'aw-readonly-note';
      convergenceText.textContent = [
        `${tr('analysis.kinetic.convergence', '数值收敛')}: ${String(convergence.status || '—')}`,
        `${tr('analysis.kinetic.residual', '残差')}: ${String(convergence.residual_display || '—')} ${String(units.residual || '')}`,
        `${tr('analysis.kinetic.iterations', '迭代')}: ${String(convergence.iterations == null ? '—' : convergence.iterations)}`,
        `${tr('analysis.kinetic.solver', '求解器')}: ${String(convergence.solver || '—')}`,
      ].join(' · ');
      pointSection.appendChild(convergenceText);
      const grid = document.createElement('div'); grid.className = 'aw-kinetic-grid';
      appendKineticTable(grid, tr('analysis.kinetic.tof', 'TOF'),
        tr('analysis.table.species', '物种'), point.tof,
        record => record.species_id, units.tof);
      appendKineticTable(grid, tr('analysis.kinetic.coverage', '覆盖度'),
        tr('analysis.table.species', '物种'), point.coverage,
        record => `${String(record.species_id || '')} @ ${String(record.site_type || '')}`, units.coverage);
      appendKineticTable(grid, tr('analysis.kinetic.selectivity', '选择性'),
        tr('analysis.table.species', '物种'), point.selectivity,
        record => record.species_id, units.selectivity);
      appendKineticTable(grid, tr('analysis.kinetic.drc', '速率控制度 DRC'),
        tr('analysis.kinetic.target_step_axis', '目标产物 / 步骤'), point.drc,
        record => `${tr('analysis.kinetic.target_product', '目标产物')}: ${String(record.target_species_id || '')}` +
          ` · ${tr('analysis.kinetic.step', '步骤')}: ${String(record.step_id || '')}`, units.drc);
      appendKineticTable(grid, tr('analysis.kinetic.dsc', '选择性控制度 DSC'),
        tr('analysis.kinetic.target_step_axis', '目标产物 / 步骤'), point.dsc,
        record => `${tr('analysis.kinetic.target_product', '目标产物')}: ${String(record.target_species_id || '')}` +
          ` · ${tr('analysis.kinetic.step', '步骤')}: ${String(record.step_id || '')}`, units.dsc);
      appendKineticTable(grid, tr('analysis.kinetic.reaction_order', '反应级数'),
        tr('analysis.kinetic.target_species_axis', '目标产物 / 扰动物种'), point.reaction_order,
        record => `${tr('analysis.kinetic.target_product', '目标产物')}: ${String(record.target_species_id || '')}` +
          ` · ${tr('analysis.kinetic.perturbed_species', '扰动物种')}: ${String(record.species_id || '')}`,
        units.reaction_order);
      appendKineticTable(grid, tr('analysis.kinetic.apparent_activation_energy', '表观活化能'),
        tr('analysis.table.species', '物种'), point.apparent_activation_energy,
        record => record.species_id, units.apparent_activation_energy);
      appendKineticTable(grid, tr('analysis.kinetic.free_energy', '自由能图'),
        tr('analysis.kinetic.state', '状态'), point.free_energy_diagram,
        record => record.state_id, units.free_energy);
      pointSection.appendChild(grid); box.appendChild(pointSection);
    });

    const limits = document.createElement('section'); limits.className = 'aw-free-energy-evidence';
    const limitsTitle = document.createElement('h4');
    limitsTitle.textContent = tr('analysis.kinetic.limitations', '模型与报告边界');
    const list = document.createElement('ul');
    [
      `${tr('analysis.kinetic.mean_field', '平均场')}: ${String(limitations.mean_field)}`,
      `${tr('analysis.kinetic.steady_state', '稳态')}: ${String(limitations.steady_state)}`,
      `${tr('analysis.kinetic.uniform_sites', '均一位点')}: ${String(limitations.uniform_sites)}`,
      `${tr('analysis.kinetic.lateral_interactions', '横向相互作用')}: ${String(limitations.lateral_interactions || '—')}`,
      `${tr('analysis.kinetic.mechanism_completeness', '机制完整性')}: ${String(limitations.mechanism_completeness || '—')}`,
      tr('analysis.kinetic.mechanism_claim', '机制完整性是上游声明，不是未遗漏步骤的证明。'),
      tr('analysis.kinetic.no_final', '结果固定为 diagnostic，不能直接进入 accepted/final。'),
    ].forEach(value => { const item = document.createElement('li'); item.textContent = value; list.appendChild(item); });
    limits.append(limitsTitle, list); box.appendChild(limits);
  }

  function renderSensitivity(view) {
    const box = $('aw-sensitivity-results'); if (!box) return; box.innerHTML = '';
    if (State.analysisId === 'kinetic-dashboard') {
      const sensitivity = plain(view && view.kinetic_sensitivity);
      const analyses = Array.isArray(sensitivity.analyses) ? sensitivity.analyses : [];
      const status = document.createElement('div'); status.className = 'aw-sensitivity-point';
      status.textContent = `${tr('analysis.kinetic.sensitivity', '数值敏感性')}: ${String(sensitivity.status || 'unavailable')}`;
      box.appendChild(status);
      analyses.forEach(item => {
        const card = document.createElement('div'); card.className = 'aw-sensitivity-point';
        card.textContent = `${String(item.kind || '')}: ${String(item.max_relative_change_display || '—')}`;
        box.appendChild(card);
      });
      (sensitivity.warnings || []).forEach(value => {
        const card = document.createElement('div'); card.className = 'aw-sensitivity-point';
        card.textContent = String(value); box.appendChild(card);
      });
      return;
    }
    const sensitivity = plain(view && view.sensitivity); const points = sensitivity.points || [];
    if (!points.length) { const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = tr('analysis.sensitivity.empty', '当前分析没有敏感性结果。');
      box.appendChild(empty); return; }
    points.forEach(point => {
      const card = document.createElement('div'); card.className = 'aw-sensitivity-point';
      const sets = (point.lowest_energy_sets || []).map(item =>
        `${item.species}: ${(item.within_deadband_project_ids || []).join(', ') || tr('analysis.no_numeric', '无数值')} (n=${item.observed_denominator || 0})`);
      card.textContent = `deadband ${Number(point.deadband_eV).toFixed(2)} eV · ${sets.join('；')}`; box.appendChild(card);
    });
  }

  function renderNextCalculation(view) {
    const box = $('aw-next-calculation'); if (!box) return; box.innerHTML = '';
    const governed = plain(view && view.next_calculation);
    const status = document.createElement('span'); status.className = 'aw-capability-status';
    status.dataset.status = governed.available === true ? 'available' : 'unavailable';
    status.textContent = governed.available === true
      ? tr('analysis.next.governed', '已通过治理门槛')
      : tr('analysis.next.blocked', '建议不可用');
    box.appendChild(status);
    if (governed.available !== true) {
      const list = document.createElement('ul');
      (governed.blocking || []).forEach(reason => {
        const item = document.createElement('li'); item.textContent = String(reason); list.appendChild(item);
      });
      if (!list.children.length) {
        const item = document.createElement('li');
        item.textContent = tr('analysis.next.validation_missing', '缺少与当前数据指纹绑定的人审 ValidationResult。');
        list.appendChild(item);
      }
      box.appendChild(list); return;
    }
    const recommendations = Array.isArray(governed.recommendations)
      ? governed.recommendations : [];
    if (!recommendations.length) {
      const empty = document.createElement('p');
      empty.textContent = tr('analysis.next.none', '当前没有需要追加计算的可审计不确定性信号。');
      box.appendChild(empty); return;
    }
    recommendations.forEach(recommendation => {
      const card = document.createElement('article'); card.className = 'aw-next-card';
      const title = document.createElement('b'); title.textContent = String(recommendation.title || '');
      const reason = document.createElement('p'); reason.textContent = String(recommendation.reason || '');
      const refs = document.createElement('code');
      refs.textContent = (recommendation.evidence_refs || []).map(String).join(' · ');
      const policy = document.createElement('small');
      policy.textContent = tr('analysis.next.policy', '仅建议 · 需用户确认 · 不授权提交');
      card.append(title, reason, refs, policy); box.appendChild(card);
    });
  }

  function renderInspector(view) {
    renderNextCalculation(view);
    const methods = $('aw-method-matrix'); if (methods) { methods.innerHTML = '';
      (view.method_matrix || []).forEach(record => {
        const card = document.createElement('div'); card.className = 'aw-method';
        const title = document.createElement('b'); title.textContent = `${record.name || record.species || record.configuration_id || tr('analysis.record', '记录')} · ${record.status || 'unknown'}`;
        const detail = document.createElement('code'); detail.textContent = [
          record.functional, record.dispersion,
          record.encut_eV != null ? `ENCUT ${record.encut_eV}` : '',
          record.kpoints_scheme, record.energy_basis, record.thermochemistry,
          record.solvation, record.potential_model,
        ].filter(Boolean).join(' · ') || (record.issues || record.warnings || []).join('；') || tr('analysis.method_projection.empty', '没有完整方法投影');
        card.append(title, detail); methods.appendChild(card);
      });
      if (!methods.children.length) { const empty = document.createElement('div');
        empty.className = 'aw-empty'; empty.textContent = tr('analysis.method_matrix.empty', '没有方法矩阵记录。');
        methods.appendChild(empty); }
    }
    const gate = plain(view.comparison_gate); const blocking = view.blocking || gate.blocking || [];
    const warnings = view.warnings || gate.warnings || [];
    [['aw-blocking', blocking, tr('analysis.blocking.empty', '当前无阻断项')],
      ['aw-warnings', warnings, tr('analysis.warnings.empty', '当前无告警')]].forEach(([id, values, empty]) => {
      const list = $(id); if (!list) return; list.innerHTML = '';
      (values || []).forEach(value => { const item = document.createElement('li'); item.textContent = String(value); list.appendChild(item); });
      if (!list.children.length) { const item = document.createElement('li'); item.textContent = empty; list.appendChild(item); }
    });
  }

  function renderView() {
    const view = plain(State.view); const box = $('aw-table-scroll');
    renderDenominator(view); renderSensitivity(view); renderInspector(view);
    const record = analysisRecord(); setText('aw-status-analysis', localized(record, 'label', State.analysisId),
      tr('common.not_loaded', '未读取'));
    setText('aw-status-science', view.scientific_status, tr('common.unknown', '未知'));
    const denominator = plain(view.denominator);
    const inputDenominator = denominator.input_configurations != null ? denominator.input_configurations
      : denominator.selected_projects != null ? denominator.selected_projects
        : denominator.requested_paths != null ? denominator.requested_paths
          : denominator.resolved_targets != null ? denominator.resolved_targets
            : denominator.condition_points != null ? denominator.condition_points : denominator.input_steps;
    setText('aw-status-input', inputDenominator, 0);
    setText('aw-status-visible', denominator.visible_rows != null ? denominator.visible_rows : denominator.observed_numeric_cells, 0);
    setText('aw-status-hash', view.data_fingerprint ? String(view.data_fingerprint).slice(0, 12) : '—');
    if (!box) return;
    if (State.analysisId === 'adsorption-energy') renderAdsorptionTable(view, box);
    else if (State.analysisId === 'free-energy-path') renderFreeEnergyView(view, box);
    else if (State.analysisId === 'kinetic-dashboard') renderKineticDashboard(view, box);
    else if (State.analysisId === 'multi-project-comparison') renderComparisonTable(view, box);
    else if (State.analysisId === 'neb-path') renderNebView(view, box);
    else if (State.analysisId === 'convergence-scan') renderConvergenceView(view, box);
    else if (State.analysisId === 'aimd-diagnostics') renderAimdView(view, box);
    else renderServerResults(view, box);
  }

  function renderKineticsActions() {
    const box = $('aw-kinetics-actions'); if (!box) return;
    const active = State.analysisId === 'kinetic-dashboard'; box.hidden = !active;
    if (!active) {
      State.kineticsPreviewSha = ''; State.kineticsAuditPreviewSha = '';
      State.kineticsUploadedResult = null;
    }
    const preview = $('aw-kinetics-preview-export');
    const confirm = $('aw-kinetics-confirm-export');
    const auditPreview = $('aw-kinetics-preview-audit');
    const auditConfirm = $('aw-kinetics-confirm-audit');
    const resultSelect = $('aw-kinetics-select-result');
    const file = $('aw-kinetics-result-file');
    const fileLabel = $('aw-kinetics-result-label');
    if (preview) preview.disabled = !active || State.busy || !State.projectId;
    if (confirm) confirm.disabled = !active || State.busy || !State.kineticsPreviewSha;
    if (auditPreview) auditPreview.disabled = !active || State.busy || !State.projectId;
    if (auditConfirm) auditConfirm.disabled = !active || State.busy || !State.kineticsAuditPreviewSha;
    if (resultSelect) resultSelect.disabled = !active || State.busy || !State.kineticsUploadedResult;
    if (file) file.disabled = !active || State.busy || !State.projectId;
    if (fileLabel) fileLabel.setAttribute(
      'aria-disabled', (!active || State.busy || !State.projectId) ? 'true' : 'false');
  }

  function renderAll() {
    renderRegistry(); renderTemplates(); renderControls(); renderView(); renderKineticsActions();
    renderAuthoring();
    setText('aw-project-name', State.projectName ? `${State.projectName} · ${State.projectId}`
      : tr('analysis.project.unavailable', '当前项目不可用'));
    const refresh = $('aw-refresh'); if (refresh) refresh.disabled = State.busy || !State.spec;
    const save = $('aw-save-view'); if (save) save.disabled = State.busy || !State.spec || State.preferenceRevision == null;
    const favorite = $('aw-favorite'); if (favorite) {
      const active = (plain(State.preferences).favorites || []).includes(State.analysisId);
      favorite.disabled = State.busy || State.preferenceRevision == null;
      favorite.setAttribute('aria-pressed', active ? 'true' : 'false');
      favorite.textContent = active ? tr('analysis.favorite.remove', '取消收藏')
        : tr('analysis.favorite.add', '收藏分析');
    }
    const form = $('aw-spec-form'); if (form) form.setAttribute('aria-busy', State.busy ? 'true' : 'false');
    const controls = $('aw-controls'); if (controls) controls.disabled = State.busy || !State.spec;
  }

  async function previewCurrent() {
    if (State.busy || !State.spec || !sameProject(State.projectId)) return false;
    const request = requestFromControls(); const requestFingerprint = JSON.stringify(request);
    const projectId = State.projectId;
    const analysisId = State.analysisId; const intent = ++State.intentGeneration;
    const generation = ++State.previewGeneration; const busyToken = beginBusy();
    showAlert(''); operation(tr('analysis.operation.rebuilding', '正在由服务端重建分析视图…'), 'busy'); renderAll();
    try {
      const result = await VCS.call('analysis_workbench_preview', projectId, request);
      if (intent !== State.intentGeneration || generation !== State.previewGeneration ||
          analysisId !== State.analysisId || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error) throw new Error(result && result.error || tr(
        'analysis.error.invalid_response', '分析接口没有返回有效结果'));
      if (safeId(result.project_id) !== projectId) throw new Error(tr(
        'analysis.error.project_mismatch', '分析结果项目身份与当前项目不一致'));
      if (JSON.stringify(request) !== requestFingerprint) return false;
      State.spec = clone(result.spec || plain(result.view).spec || request); State.view = clone(result.view || result.analysis_view || {});
      State.dirty = false; operation(tr('analysis.operation.rebuilt', '分析视图已由服务器数据与门禁重新生成。'), 'ok'); renderAll(); return true;
    } catch (error) {
      if (intent !== State.intentGeneration || generation !== State.previewGeneration) return false;
      showAlert(error && error.message || String(error)); operation(tr(
        'analysis.operation.failed', '分析失败；保留上一份已验证视图。'), 'bad'); return false;
    } finally { endBusy(busyToken); }
  }

  function kineticsOperation(message, tone = '') {
    const box = $('aw-kinetics-adapter-status'); if (!box) return;
    box.textContent = String(message || '');
    box.className = 'aw-operation' + (tone ? ` ${tone}` : '');
  }

  async function previewKineticsExport() {
    if (State.busy || State.analysisId !== 'kinetic-dashboard' ||
        !sameProject(State.projectId)) return false;
    const projectId = State.projectId; const intent = ++State.intentGeneration;
    const busyToken = beginBusy(); State.kineticsPreviewSha = '';
    State.kineticsSelectionRevision = null; State.kineticsSelectedExportSha = '';
    kineticsOperation(tr('analysis.kinetic.preview_loading',
      '正在审计并生成固定导出预览…'), 'busy'); renderAll();
    try {
      const result = await VCS.call('kinetics_export_preview', projectId);
      if (intent !== State.intentGeneration || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error) throw new Error(result && result.error || tr(
        'analysis.kinetic.preview_failed', 'CatMAP 导出预览失败。'));
      if (safeId(result.project_id) !== projectId) throw new Error(tr(
        'analysis.error.project_mismatch', '分析结果项目身份与当前项目不一致'));
      if (result.schema !== 'vcstudio.catmap-export-preview/v3' ||
          result.export_kind !== 'model' || result.export_ready !== true) {
        kineticsOperation(tr('analysis.kinetic.model_unavailable',
          '模型导出不可用；可独立预览并导出审计报告。'), 'bad');
        return false;
      }
      const previewSha = String(result.preview_sha256 || '');
      if (!/^[a-f0-9]{64}$/.test(previewSha)) throw new Error(tr(
        'analysis.kinetic.preview_hash_invalid', '服务端未返回有效的固定预览哈希。'));
      State.kineticsPreviewSha = previewSha;
      State.kineticsSelectionRevision = Number(result.selection_revision);
      State.kineticsSelectedExportSha = String(result.selected_export_sha256 || '');
      if (!Number.isInteger(State.kineticsSelectionRevision) ||
          State.kineticsSelectionRevision < 0 ||
          (State.kineticsSelectedExportSha &&
           !/^[a-f0-9]{64}$/.test(State.kineticsSelectedExportSha))) {
        throw new Error(tr('analysis.kinetic.selection_invalid',
          '服务端未返回有效的导出选择版本。'));
      }
      const names = (Array.isArray(result.artifacts) ? result.artifacts : [])
        .map(item => String(item && item.name || '')).filter(Boolean).join(', ');
      kineticsOperation(`${tr('analysis.kinetic.preview_ready',
        '预览已冻结；请核对后明确确认。')} ${names}`, 'ok');
      renderAll(); return true;
    } catch (error) {
      if (intent !== State.intentGeneration) return false;
      State.kineticsPreviewSha = '';
      State.kineticsSelectionRevision = null; State.kineticsSelectedExportSha = '';
      kineticsOperation(error && error.message || String(error), 'bad'); return false;
    } finally { endBusy(busyToken); }
  }

  async function confirmKineticsExport() {
    const previewSha = State.kineticsPreviewSha;
    if (State.busy || State.analysisId !== 'kinetic-dashboard' ||
        !/^[a-f0-9]{64}$/.test(previewSha) || !sameProject(State.projectId)) return false;
    if (!window.confirm(tr('analysis.kinetic.confirm_prompt',
      '确认导出当前预览中已冻结的输入、适配器、版本与哈希？'))) {
      kineticsOperation(tr('analysis.kinetic.confirm_cancelled', '已取消导出确认。')); return false;
    }
    const projectId = State.projectId; const intent = ++State.intentGeneration;
    const busyToken = beginBusy();
    kineticsOperation(tr('analysis.kinetic.confirm_loading', '正在发布固定导出包…'), 'busy');
    renderAll();
    try {
      const result = await VCS.call(
        'kinetics_export_confirm', projectId, previewSha,
        State.kineticsSelectionRevision, State.kineticsSelectedExportSha || null, true);
      if (intent !== State.intentGeneration || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error) throw new Error(result && result.error || tr(
        'analysis.kinetic.confirm_failed', '固定导出包发布失败。'));
      if (safeId(result.project_id) !== projectId) throw new Error(tr(
        'analysis.error.project_mismatch', '分析结果项目身份与当前项目不一致'));
      State.kineticsPreviewSha = '';
      State.kineticsSelectionRevision = null; State.kineticsSelectedExportSha = '';
      kineticsOperation(tr('analysis.kinetic.confirmed',
        '已发布固定导出包；应用未执行 CatMAP。'), 'ok');
      return true;
    } catch (error) {
      if (intent !== State.intentGeneration) return false;
      kineticsOperation(error && error.message || String(error), 'bad'); return false;
    } finally { endBusy(busyToken); }
  }

  async function previewKineticsAudit() {
    if (State.busy || State.analysisId !== 'kinetic-dashboard' ||
        !sameProject(State.projectId)) return false;
    const projectId = State.projectId; const intent = ++State.intentGeneration;
    const busyToken = beginBusy(); State.kineticsAuditPreviewSha = '';
    kineticsOperation(tr('analysis.kinetic.audit_preview_loading',
      '正在生成独立审计报告预览…'), 'busy'); renderAll();
    try {
      const result = await VCS.call('kinetics_audit_export_preview', projectId);
      if (intent !== State.intentGeneration || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error ||
          result.schema !== 'vcstudio.kinetics-audit-export-preview/v1' ||
          result.export_kind !== 'audit_report' || result.model_published !== false) {
        throw new Error(result && result.error || tr(
          'analysis.kinetic.audit_preview_failed', '审计报告预览失败。'));
      }
      const sha = String(result.preview_sha256 || '');
      if (!/^[a-f0-9]{64}$/.test(sha)) throw new Error(tr(
        'analysis.kinetic.preview_hash_invalid', '服务端未返回有效的固定预览哈希。'));
      State.kineticsAuditPreviewSha = sha;
      kineticsOperation(tr('analysis.kinetic.audit_preview_ready',
        '审计报告预览已冻结；它不会发布模型。'), 'ok');
      renderAll(); return true;
    } catch (error) {
      if (intent === State.intentGeneration) {
        State.kineticsAuditPreviewSha = '';
        kineticsOperation(error && error.message || String(error), 'bad');
      }
      return false;
    } finally { endBusy(busyToken); }
  }

  async function confirmKineticsAudit() {
    const sha = State.kineticsAuditPreviewSha;
    if (State.busy || !/^[a-f0-9]{64}$/.test(sha) ||
        !sameProject(State.projectId)) return false;
    if (!window.confirm(tr('analysis.kinetic.audit_confirm_prompt',
      '确认只导出审计报告（不会发布 CatMAP 模型）？'))) return false;
    const projectId = State.projectId; const intent = ++State.intentGeneration;
    const busyToken = beginBusy(); renderAll();
    try {
      const result = await VCS.call(
        'kinetics_audit_export_confirm', projectId, sha, true);
      if (intent !== State.intentGeneration || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error ||
          result.model_published !== false ||
          result.audit_export_status !== 'published') {
        throw new Error(result && result.error || tr(
          'analysis.kinetic.audit_confirm_failed', '审计报告导出失败。'));
      }
      State.kineticsAuditPreviewSha = '';
      kineticsOperation(tr('analysis.kinetic.audit_confirmed',
        '审计报告已导出；没有模型被发布。'), 'ok');
      return true;
    } catch (error) {
      if (intent === State.intentGeneration) {
        kineticsOperation(error && error.message || String(error), 'bad');
      }
      return false;
    } finally { endBusy(busyToken); }
  }

  async function importKineticsResult(event) {
    const input = event && event.target; const file = input && input.files && input.files[0];
    if (!file || State.busy || State.analysisId !== 'kinetic-dashboard' ||
        !sameProject(State.projectId)) return false;
    const maxBytes = 20 * 1024 * 1024;
    if (!Number.isFinite(file.size) || file.size < 1 || file.size > maxBytes) {
      kineticsOperation(tr('analysis.kinetic.result_size_invalid',
        '结果 JSON 必须非空且不超过 20 MiB。'), 'bad'); input.value = ''; return false;
    }
    const projectId = State.projectId; const intent = ++State.intentGeneration;
    const busyToken = beginBusy(); State.kineticsUploadedResult = null;
    kineticsOperation(tr('analysis.kinetic.import_loading',
      '正在由服务端校验 schema、单位与冻结输入哈希…'), 'busy'); renderAll();
    try {
      const resultObject = JSON.parse(await file.text());
      if (!resultObject || typeof resultObject !== 'object' || Array.isArray(resultObject)) {
        throw new Error(tr('analysis.kinetic.result_object_required',
          '结果 JSON 顶层必须是对象。'));
      }
      const result = await VCS.call('kinetics_result_import', projectId, resultObject);
      if (intent !== State.intentGeneration || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error) throw new Error(result && result.error || tr(
        'analysis.kinetic.import_failed', '动力学结果导入失败。'));
      if (safeId(result.project_id) !== projectId) throw new Error(tr(
        'analysis.error.project_mismatch', '分析结果项目身份与当前项目不一致'));
      if (result.selected !== false) throw new Error(tr(
        'analysis.kinetic.upload_selection_invalid', '上传接口错误地选择了结果。'));
      const uploaded = {
        resultSha: String(result.result_sha256 || ''),
        exportSha: String(result.confirmed_export_sha256 || ''),
        latestSha: String(result.latest_result_sha256 || ''),
        revision: Number(result.selection_revision),
      };
      if (!/^[a-f0-9]{64}$/.test(uploaded.resultSha) ||
          !/^[a-f0-9]{64}$/.test(uploaded.exportSha) ||
          (uploaded.latestSha && !/^[a-f0-9]{64}$/.test(uploaded.latestSha)) ||
          !Number.isInteger(uploaded.revision)) {
        throw new Error(tr('analysis.kinetic.upload_receipt_invalid',
          '服务端上传回执无效。'));
      }
      State.kineticsUploadedResult = uploaded;
      kineticsOperation(tr('analysis.kinetic.imported',
        '结果已校验并上传，但尚未选择为当前 diagnostic 结果。'), 'ok');
      renderAll();
    } catch (error) {
      if (intent === State.intentGeneration) {
        State.kineticsUploadedResult = null;
        kineticsOperation(error && error.message || String(error), 'bad');
      }
    } finally {
      input.value = ''; endBusy(busyToken);
    }
    return !!State.kineticsUploadedResult;
  }

  async function selectKineticsResult() {
    const uploaded = State.kineticsUploadedResult;
    if (!uploaded || State.busy || !sameProject(State.projectId)) return false;
    if (!window.confirm(tr('analysis.kinetic.result_select_prompt',
      '将这份已上传结果选择为当前 diagnostic Dashboard 结果？'))) return false;
    const projectId = State.projectId; const intent = ++State.intentGeneration;
    const busyToken = beginBusy(); renderAll();
    try {
      const result = await VCS.call(
        'kinetics_result_select', projectId, uploaded.resultSha, uploaded.exportSha,
        uploaded.latestSha || null, uploaded.revision, true);
      if (intent !== State.intentGeneration || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error || result.selected !== true) {
        throw new Error(result && result.error || tr(
          'analysis.kinetic.result_select_failed', '结果选择失败。'));
      }
      State.kineticsUploadedResult = null;
      kineticsOperation(tr('analysis.kinetic.result_selected',
        '结果已通过 CAS 选择；正在刷新 Dashboard。'), 'ok');
    } catch (error) {
      if (intent === State.intentGeneration) {
        kineticsOperation(error && error.message || String(error), 'bad');
      }
      return false;
    } finally { endBusy(busyToken); }
    return previewCurrent();
  }

  async function loadBootstrap(analysisId) {
    const project = currentProject(); const id = projectIdentity(project);
    if (!id) { showAlert(tr('analysis.bootstrap.project_missing', '当前项目尚未加载。请先从全局项目栏选择项目。'));
      operation(tr('analysis.bootstrap.waiting_context', '等待项目上下文。'), 'bad'); return false; }
    const intent = ++State.intentGeneration; const generation = ++State.bootstrapGeneration;
    State.previewGeneration += 1; const busyToken = beginBusy();
    State.kineticsPreviewSha = ''; State.kineticsAuditPreviewSha = '';
    State.kineticsUploadedResult = null;
    State.projectId = id;
    State.analysisId = safeId(analysisId) || 'adsorption-energy';
    let authoringEpoch = resetAuthoring(id, State.analysisId); showAlert('');
    operation(tr('analysis.bootstrap.loading', '正在读取分析注册表与项目证据…'), 'busy'); renderAll();
    try {
      const result = await VCS.call('analysis_workbench_bootstrap', id, State.analysisId);
      if (intent !== State.intentGeneration || generation !== State.bootstrapGeneration ||
          !sameProject(id)) return false;
      if (!result || result.ok === false || result.error) throw new Error(result && result.error || tr(
        'analysis.bootstrap.failed', '分析工作台 bootstrap 失败'));
      if (safeId(result.project_id) !== id) throw new Error(tr(
        'analysis.bootstrap.project_mismatch', 'bootstrap 项目身份不一致'));
      State.projectName = String(result.project_name || plain(result.project).name || project.name || '');
      State.catalog = clone(result.catalog || {});
      State.projects = (Array.isArray(result.projects) ? result.projects : []).map(project => ({
        project_id: safeId(project && project.project_id),
        name: String(project && (project.name || project.display_name) || ''),
        counts: clone(project && project.counts || {}) || {},
      })).filter(project => project.project_id);
      State.spec = clone(result.default_spec || result.spec || {});
      State.view = clone(result.view || result.analysis_view || {});
      State.analysisId = safeId(State.spec.analysis_id) || State.analysisId;
      if (State.authoringAnalysisId !== State.analysisId) {
        authoringEpoch = resetAuthoring(id, State.analysisId);
      }
      const preferences = plain(result.preferences); State.preferences = clone(preferences.preferences || preferences);
      State.preferenceRevision = Number.isInteger(result.preference_revision) ? result.preference_revision : Number.isInteger(preferences.revision) ? preferences.revision : null;
      State.dirty = false; operation(tr('analysis.bootstrap.loaded', '分析工作台已加载。'), 'ok');
      renderAll();
      await loadAuthoringBootstrap(id, authoringEpoch, State.analysisId);
      return true;
    } catch (error) {
      if (intent !== State.intentGeneration || generation !== State.bootstrapGeneration) return false;
      State.spec = null; State.view = null; showAlert(error && error.message || String(error));
      State.authoringBootstrapStatus = 'unavailable';
      State.networkAuthoring.status = 'unavailable'; State.modelAuthoring.status = 'unavailable';
      State.networkAuthoring.message = tr('analysis.authoring.bootstrap_invalid',
        'Authoring bootstrap 不完整或不安全，已拒绝采用。');
      State.modelAuthoring.message = State.networkAuthoring.message;
      operation(tr('analysis.bootstrap.load_failed', '分析工作台加载失败。'), 'bad'); renderAll(); return false;
    } finally { endBusy(busyToken); }
  }

  async function applyTemplate(templateId) {
    const templates = [...(plain(State.catalog).view_templates || []), ...(plain(State.preferences).templates || [])];
    const template = templates.find(item => safeId(item.id) === safeId(templateId) && item.analysis_id === State.analysisId);
    if (!template || State.busy) return; const values = clone(template.spec || template.values || {});
    State.spec = Object.assign({}, State.spec, values, { view_id: safeId(template.id) || null }); renderControls(); await previewCurrent();
  }

  async function saveView() {
    if (State.preferenceRevision == null || State.busy) {
      VCS.toast(tr('analysis.preferences.unavailable', '分析偏好服务尚未可用'), 'fail'); return;
    }
    const analysisLabel = localized(analysisRecord(), 'label', tr('analysis.generic', '分析'));
    const name = window.prompt(tr('analysis.templates.name_prompt', '视图模板名称'),
      tr('analysis.templates.default_name', '{analysis}视图', { analysis: analysisLabel }));
    if (!name) return; const id = `view-${Date.now().toString(36)}`;
    const busyToken = beginBusy(); renderAll();
    try {
      const result = await VCS.call('analysis_preferences_update', {
        id, name: String(name).slice(0, 80), analysis_id: State.analysisId,
        spec: requestFromControls(),
      }, State.projectId, State.preferenceRevision, false);
      if (result && result.conflict === true) {
        State.preferenceRevision = result.revision;
        State.preferences = clone(result.preferences || {});
      }
      if (!result || result.ok === false) throw new Error(result && result.error || tr(
        'analysis.templates.save_failed', '保存视图失败'));
      State.preferenceRevision = result.revision; State.preferences = clone(result.preferences || {});
      renderAll(); VCS.toast(tr('analysis.templates.saved', '分析视图已保存'));
    } catch (error) { showAlert(error && error.message || String(error)); }
    finally { endBusy(busyToken); }
  }

  async function toggleFavorite() {
    if (State.preferenceRevision == null || State.busy) return;
    const active = (plain(State.preferences).favorites || []).includes(State.analysisId);
    const busyToken = beginBusy(); renderAll();
    try {
      const result = await VCS.call('analysis_preferences_favorite', State.analysisId, !active, State.preferenceRevision);
      if (result && result.conflict === true) {
        State.preferenceRevision = result.revision;
        State.preferences = clone(result.preferences || {});
      }
      if (!result || result.ok === false) throw new Error(result && result.error || tr(
        'analysis.favorite.update_failed', '更新收藏失败'));
      State.preferenceRevision = result.revision; State.preferences = clone(result.preferences || {}); renderAll();
    } catch (error) { showAlert(error && error.message || String(error)); }
    finally { endBusy(busyToken); }
  }

  async function enterWorkbench(detail) {
    const wanted = routeAnalysis(detail);
    if (wanted !== State.analysisId || !State.spec || !sameProject(State.projectId)) return loadBootstrap(wanted);
    renderAll(); return true;
  }

  function wire() {
    const registry = $('aw-registry'); if (registry) registry.addEventListener('click', event => {
      const button = event.target.closest('[data-analysis-id]'); if (!button || State.busy) return;
      const record = analysisRecord(button.dataset.analysisId); if (!record) return;
      if (VCS.workspace && typeof VCS.workspace.navigateRoute === 'function' && record.route &&
          ROUTE_ANALYSIS[record.route] === record.id) {
        VCS.workspace.navigateRoute(record.route, { projectId: State.projectId, source: 'analysis-registry' });
      } else loadBootstrap(record.id);
    });
    const templates = $('aw-template-list'); if (templates) templates.addEventListener('click', event => {
      const button = event.target.closest('[data-template-id]'); if (button) applyTemplate(button.dataset.templateId);
    });
    const form = $('aw-spec-form'); if (form) form.addEventListener('change', event => {
      if (State.busy) return;
      if (event.target.id === 'aw-projects') {
        renderBaselineOptions(
          selectedComparisonProjectIds(), $('aw-baseline') && $('aw-baseline').value);
      }
      State.dirty = true; previewCurrent();
    });
    if ($('aw-refresh')) $('aw-refresh').addEventListener('click', previewCurrent);
    if ($('aw-save-view')) $('aw-save-view').addEventListener('click', saveView);
    if ($('aw-favorite')) $('aw-favorite').addEventListener('click', toggleFavorite);
    if ($('aw-kinetics-preview-export')) {
      $('aw-kinetics-preview-export').addEventListener('click', previewKineticsExport);
    }
    if ($('aw-kinetics-confirm-export')) {
      $('aw-kinetics-confirm-export').addEventListener('click', confirmKineticsExport);
    }
    if ($('aw-kinetics-preview-audit')) {
      $('aw-kinetics-preview-audit').addEventListener('click', previewKineticsAudit);
    }
    if ($('aw-kinetics-confirm-audit')) {
      $('aw-kinetics-confirm-audit').addEventListener('click', confirmKineticsAudit);
    }
    if ($('aw-kinetics-select-result')) {
      $('aw-kinetics-select-result').addEventListener('click', selectKineticsResult);
    }
    if ($('aw-kinetics-result-file')) {
      $('aw-kinetics-result-file').addEventListener('change', importKineticsResult);
    }
    const networkForm = $('aw-network-authoring-form');
    if (networkForm) {
      networkForm.addEventListener('submit', event => event.preventDefault());
      networkForm.addEventListener('change', event => {
        if (event.target && event.target.id === 'aw-network-ack') {
          State.networkAuthoring.acknowledged = event.target.checked === true;
          renderAuthoringControls(); return;
        }
        invalidateAuthoring('network'); renderSelectedNetworkSummary();
      });
    }
    const modelForm = $('aw-model-authoring-form');
    if (modelForm) {
      modelForm.addEventListener('submit', event => event.preventDefault());
      const changed = event => {
        if (event.target && event.target.id === 'aw-model-ack') {
          State.modelAuthoring.acknowledged = event.target.checked === true;
          renderAuthoringControls(); return;
        }
        invalidateAuthoring('model');
      };
      modelForm.addEventListener('input', changed);
      modelForm.addEventListener('change', changed);
    }
    if ($('aw-network-preview')) $('aw-network-preview').addEventListener('click', previewActiveNetwork);
    if ($('aw-network-confirm')) $('aw-network-confirm').addEventListener('click', confirmActiveNetwork);
    if ($('aw-model-preview')) $('aw-model-preview').addEventListener('click', previewModelSpec);
    if ($('aw-model-confirm')) $('aw-model-confirm').addEventListener('click', confirmModelSpec);
    document.addEventListener('vcs:route', event => {
      if (event.detail && event.detail.page === 'analysis-workbench') enterWorkbench(event.detail);
      else resetAuthoring();
    });
    document.addEventListener('vcs:page', event => {
      if (!VCS.workspace && event.detail && event.detail.page === 'analysis-workbench') {
        setTimeout(() => enterWorkbench(event.detail), 0);
      } else if (event.detail && event.detail.page !== 'analysis-workbench') resetAuthoring();
    });
    document.addEventListener('vcs:workspace-project', () => {
      resetAuthoring();
      const page = $('page-analysis-workbench'); if (page && !page.hidden) enterWorkbench();
    });
    document.addEventListener('vcs:language', () => { if (State.catalog) renderAll(); });
  }

  if (window.__VCS_TEST__ === true) {
    window.__VCS_ANALYSIS_TEST__ = Object.freeze({
      renderRegistry,
      renderNebView,
      renderConvergenceView,
      renderAimdView,
      appendSourceDetails,
      renderReactionWorkbench,
      requestFromControls,
      renderKineticDashboard,
      renderSensitivity,
      previewKineticsExport,
      importKineticsResult,
      loadAuthoringBootstrap,
      previewActiveNetwork,
      confirmActiveNetwork,
      previewModelSpec,
      confirmModelSpec,
      invalidateAuthoring,
      resetAuthoring,
      renderAuthoring,
      renderModelCatalog,
      modelDraftFromControls,
      configure({ catalog = null, preferences = null, analysisId = '',
        projectId = '', busy = false } = {}) {
        State.catalog = clone(catalog);
        State.preferences = clone(preferences) || {};
        State.analysisId = String(analysisId || '');
        State.projectId = String(projectId || '');
        State.busy = busy === true;
      },
      configureAuthoring({ projectId = '', analysisId = '', bootstrap = null } = {}) {
        State.projectId = safeId(projectId); State.analysisId = safeId(analysisId);
        const epoch = resetAuthoring(projectId, analysisId);
        if (bootstrap) {
          State.authoringBootstrap = clone(bootstrap);
          State.authoringBootstrapStatus = 'ready';
          renderNetworkBootstrap(); renderModelCatalog(); renderAuthoring();
        }
        return epoch;
      },
      snapshot() {
        return {
          analysis_id: State.analysisId,
          project_id: State.projectId,
          busy: State.busy,
          kinetics_preview_sha256: State.kineticsPreviewSha,
          kinetics_uploaded_result: clone(State.kineticsUploadedResult),
          authoring: {
            epoch: State.authoringEpoch,
            project_id: State.authoringProjectId,
            analysis_id: State.authoringAnalysisId,
            bootstrap_status: State.authoringBootstrapStatus,
            network: {
              generation: State.networkAuthoring.intentGeneration,
              dirty: State.networkAuthoring.dirty,
              has_preview: !!State.networkAuthoring.previewReceipt,
              acknowledged: State.networkAuthoring.acknowledged,
              previewing: State.networkAuthoring.previewing,
              confirming: State.networkAuthoring.confirming,
              status: State.networkAuthoring.status,
              conflict: !!State.networkAuthoring.conflict,
              last_action: State.networkAuthoring.lastResult && State.networkAuthoring.lastResult.action,
            },
            model: {
              generation: State.modelAuthoring.intentGeneration,
              dirty: State.modelAuthoring.dirty,
              has_preview: !!State.modelAuthoring.previewReceipt,
              acknowledged: State.modelAuthoring.acknowledged,
              previewing: State.modelAuthoring.previewing,
              confirming: State.modelAuthoring.confirming,
              status: State.modelAuthoring.status,
              conflict: !!State.modelAuthoring.conflict,
              last_action: State.modelAuthoring.lastResult && State.modelAuthoring.lastResult.action,
            },
          },
          catalog: clone(State.catalog),
        };
      },
    });
  }

  window.AnalysisWorkbench = { refresh: previewCurrent, open: analysisId => loadBootstrap(analysisId) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true });
  else wire();
})();
