// report-workbench.js — Phase C report configuration, bound preview and publishing UI.
// Scientific snapshot/validation/revision fields are server-owned and are never
// accepted from editable controls or reconstructed by the browser.
'use strict';

(function () {
  const VCS = window.VCS;
  if (!VCS) return;

  const $ = id => document.getElementById(id);
  const PROJECT_ID_RE = /^[A-Za-z0-9._~-]{1,160}$/;
  const FORMAT_ORDER = Object.freeze(['html', 'docx', 'pdf']);
  const STEP_ORDER = Object.freeze([
    'scope', 'audience', 'gates', 'outline', 'content', 'language', 'export',
  ]);
  const REQUEST_KEYS = Object.freeze([
    'preset_id', 'requested_kind', 'audience', 'locale', 'formats',
    'scope', 'outline', 'theme_id', 'options',
  ]);

  const State = {
    projectId: '',
    projectPath: '',
    projectName: '',
    mode: 'report',
    appliedMode: '',
    loadedSpecQuery: '',
    routeQuery: {},
    pendingIntent: null,
    bootstrap: null,
    catalog: null,
    spec: null,
    capabilities: Object.create(null),
    capabilityReady: false,
    preview: null,
    history: [],
    historyStatus: 'loading',
    historyError: '',
    outputDir: '',
    outputProjectId: '',
    lastPublish: null,
    publishedPreviewId: '',
    dirty: true,
    bootstrapBusy: false,
    previewBusy: false,
    publishBusy: false,
    bootstrapGeneration: 0,
    previewGeneration: 0,
    publishGeneration: 0,
    routeGeneration: 0,
    previewTimer: null,
    activeStep: 'scope',
    formatStates: Object.create(null),
  };

  function plain(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function clone(value) {
    try { return JSON.parse(JSON.stringify(value)); } catch (_) { return null; }
  }

  function safeId(value) {
    const text = String(value || '').trim();
    return PROJECT_ID_RE.test(text) ? text : '';
  }

  function safeText(value, fallback = '—') {
    if (value === null || value === undefined || value === '') return fallback;
    return String(value);
  }

  function sameProject(projectId, projectPath) {
    const current = currentProject();
    return !!current && projectIdentity(current) === projectId &&
      String(current.path || '') === String(projectPath || '');
  }

  function projectIdentity(project) {
    return safeId(project && (project.project_id || project.id || project.project_uuid));
  }

  function currentProject() {
    if (window.Project && typeof window.Project.current === 'function') {
      const project = window.Project.current();
      if (project && project.path) return project;
    }
    const workspace = VCS.workspace;
    const id = safeId(workspace && workspace.state && workspace.state.project_id);
    if (!id || !workspace || !Array.isArray(workspace.projects)) return null;
    return workspace.projects.find(project => safeId(project.id || project.project_id) === id) || null;
  }

  function draftKey(projectId = State.projectId, mode = State.mode) {
    const suffix = String(projectId || '').replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 60);
    const modeSuffix = String(mode || 'report').replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 24);
    return suffix && modeSuffix ? `report-spec-${suffix}-${modeSuffix}` : '';
  }

  function setOperation(message, kind = '') {
    const box = $('rw-operation-status');
    if (!box) return;
    box.textContent = String(message || '');
    box.className = 'rw-operation-status' + (kind ? ` ${kind}` : '');
  }

  function showAlert(message) {
    const box = $('rw-alert');
    if (!box) return;
    const text = String(message || '').trim();
    box.hidden = !text;
    box.textContent = text;
  }

  function pathValue(object, path) {
    return String(path || '').split('.').reduce((value, part) =>
      value && typeof value === 'object' ? value[part] : undefined, object);
  }

  function setPath(object, path, value) {
    const parts = String(path || '').split('.').filter(Boolean);
    if (!object || !parts.length) return;
    let target = object;
    parts.slice(0, -1).forEach(part => {
      if (!target[part] || typeof target[part] !== 'object' || Array.isArray(target[part])) {
        target[part] = {};
      }
      target = target[part];
    });
    target[parts[parts.length - 1]] = value;
  }

  function requestFromSpec() {
    const source = plain(State.spec);
    const request = {};
    REQUEST_KEYS.forEach(key => {
      if (Object.prototype.hasOwnProperty.call(source, key)) request[key] = clone(source[key]);
    });
    return request;
  }

  function specFingerprint() {
    try { return JSON.stringify(requestFromSpec()); } catch (_) { return ''; }
  }

  function publicSnapshotId(preview) {
    const snapshot = plain(preview && preview.public_snapshot);
    return String(snapshot.snapshot_id || snapshot.id || snapshot.semantic_sha256 ||
      snapshot.input_fingerprint || '');
  }

  function hasCompletePreviewToken(preview) {
    const token = plain(preview && preview.preview_token);
    const hash = value => /^[0-9a-f]{64}$/.test(String(value || ''));
    return token.schema === 'vcstudio.report-preview-token/v1' &&
      String(token.preview_id || '') === String(preview && preview.preview_id || '') &&
      safeId(token.project_id) === State.projectId &&
      ['spec_sha256', 'snapshot_sha256', 'validation_sha256', 'report_model_sha256']
        .every(key => hash(token[key])) &&
      Number.isSafeInteger(Number(token.base_revision)) && Number(token.base_revision) >= 0 &&
      (token.base_manifest_sha256 === null || hash(token.base_manifest_sha256));
  }

  function revisionNumber(value) {
    if (value === null || value === undefined) return null;
    if (typeof value === 'number' || typeof value === 'string') {
      const number = Number(value);
      return Number.isSafeInteger(number) && number >= 0 ? number : null;
    }
    const record = plain(value);
    return revisionNumber(record.sequence != null ? record.sequence : record.revision);
  }

  function catalogRecord(value) {
    if (Array.isArray(value)) return value;
    return Object.entries(plain(value)).map(([id, record]) =>
      Object.assign({ id }, plain(record)));
  }

  function uiIsEnglish() {
    return !!(VCS.i18n && VCS.i18n.lang === 'en');
  }

  function labelOf(record, fallback) {
    return String(uiIsEnglish()
      ? (record.label_en || record.label || record.name || fallback)
      : (record.label_zh || record.label || record.name || fallback));
  }

  function normalizeHistory(value) {
    if (Array.isArray(value)) return value.filter(item => item && typeof item === 'object');
    const body = plain(value);
    const rows = body.revisions || body.history || body.items || body.entries || [];
    return Array.isArray(rows) ? rows.filter(item => item && typeof item === 'object') : [];
  }

  function normalizeAccessibility(value) {
    const record = plain(value);
    const statuses = new Set(['conditional', 'partial', 'unsupported', 'unknown']);
    const status = statuses.has(record.status) ? record.status : 'unknown';
    const normalized = {
      status,
      reason: String(record.reason || '服务端未声明该格式的可访问性能力。'),
      reason_zh: String(record.reason_zh || record.reason || '服务端未声明该格式的可访问性能力。'),
      reason_en: String(record.reason_en || record.reason ||
        'The server did not declare accessibility support for this format.'),
    };
    [
      'visual', 'searchable', 'semantic_structure', 'document_language',
      'metadata', 'image_alt', 'tagged', 'pdf_ua', 'manual_review_required',
    ].forEach(name => {
      normalized[name] = typeof record[name] === 'boolean' ? record[name] : null;
    });
    return normalized;
  }

  function accessibilitySummary(value) {
    const accessibility = normalizeAccessibility(value);
    const labels = uiIsEnglish()
      ? { conditional: 'conditional', partial: 'partial', unsupported: 'unsupported', unknown: 'unknown' }
      : { conditional: '有条件', partial: '部分', unsupported: '不支持', unknown: '未知' };
    const reason = uiIsEnglish() ? accessibility.reason_en : accessibility.reason_zh;
    return VCS.t('report.accessibility.summary', {
      status: labels[accessibility.status], reason,
    }, '可访问性：{status}。{reason}');
  }

  function explicitCapabilities(bootstrap, capabilityResult) {
    const catalog = plain(bootstrap && (bootstrap.catalog || bootstrap.report_catalog));
    const sources = [
      plain(capabilityResult && capabilityResult.formats),
      plain(bootstrap && bootstrap.capabilities && bootstrap.capabilities.formats),
      plain(bootstrap && bootstrap.formats),
      plain(catalog.formats),
    ];
    const accessibilitySources = [
      plain(capabilityResult && capabilityResult.accessibility),
      plain(bootstrap && bootstrap.capabilities && bootstrap.capabilities.accessibility),
      plain(bootstrap && bootstrap.accessibility),
      plain(catalog.accessibility),
    ];
    const normalized = Object.create(null);
    FORMAT_ORDER.forEach(format => {
      const record = sources.map(source => source[format]).find(item =>
        item && typeof item === 'object' && typeof item.available === 'boolean');
      const nestedAccessibility = sources.map(source => plain(source[format]).accessibility)
        .find(item => item && typeof item === 'object');
      const topLevelAccessibility = accessibilitySources.map(source => source[format])
        .find(item => item && typeof item === 'object');
      normalized[format] = {
        available: record ? record.available === true : false,
        reason: record ? String(record.reason || '') : '服务端未明确确认此格式可用。',
        accessibility: normalizeAccessibility(nestedAccessibility || topLevelAccessibility),
      };
    });
    return normalized;
  }

  function selectedFormatsAreAvailable() {
    const formats = Array.isArray(State.spec && State.spec.formats) ? State.spec.formats : [];
    return State.capabilityReady && formats.length > 0 && formats.every(format =>
      FORMAT_ORDER.includes(format) && State.capabilities[format] &&
      State.capabilities[format].available === true);
  }

  function activateStep(step, { focus = false } = {}) {
    const wanted = STEP_ORDER.includes(step) ? step : 'scope';
    State.activeStep = wanted;
    document.querySelectorAll('#rw-step-list [data-rw-step]').forEach(button => {
      const active = button.dataset.rwStep === wanted;
      if (active) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
      button.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll('#rw-spec-form [data-rw-panel]').forEach(panel => {
      panel.hidden = panel.dataset.rwPanel !== wanted;
    });
    if (focus) {
      const panel = $(`rw-step-${wanted}`);
      const target = panel && panel.querySelector('input:not([disabled]),select:not([disabled]),button:not([disabled])');
      if (target) target.focus();
    }
  }

  function replaceOptions(select, records, current, fallbackLabel) {
    if (!select) return;
    select.innerHTML = '';
    records.forEach(raw => {
      const record = typeof raw === 'string' ? { id: raw } : plain(raw);
      const id = String(record.id || record.value || '').trim();
      if (!id) return;
      const option = document.createElement('option');
      option.value = id;
      option.textContent = labelOf(record, fallbackLabel ? fallbackLabel(id) : id);
      select.appendChild(option);
    });
    if (Array.from(select.options).some(option => option.value === String(current || ''))) {
      select.value = String(current);
    }
  }

  function capabilityAvailable(record) {
    return !!record && typeof record === 'object' && record.available === true;
  }

  function capabilityReason(record, availableLabel = '服务端已确认可用') {
    const value = plain(record);
    if (value.available === true) return String(value.reason || availableLabel);
    return String(value.reason || '服务端未明确声明 available=true，当前控制已禁用。');
  }

  function replaceCapabilityOptions(select, records, current, fallbackLabel) {
    if (!select) return;
    const wanted = String(current || '');
    select.innerHTML = '';
    records.forEach(raw => {
      const record = typeof raw === 'string' ? { id: raw } : plain(raw);
      const id = String(record.id || record.value || '').trim();
      if (!id) return;
      const option = document.createElement('option');
      option.value = id;
      option.textContent = labelOf(record, fallbackLabel ? fallbackLabel(id) : id);
      option.disabled = !capabilityAvailable(record);
      option.title = capabilityReason(record);
      option.setAttribute('data-capability-available', record.available === true ? 'true' : 'false');
      select.appendChild(option);
    });
    if (Array.from(select.options).some(option => option.value === wanted)) select.value = wanted;
    const anyAvailable = Array.from(select.options).some(option => !option.disabled);
    select.setAttribute('data-capability-available', anyAvailable ? 'true' : 'false');
    select.disabled = !anyAvailable;
  }

  function catalogCapability(records, id) {
    return catalogRecord(records).find(record =>
      String(record.id || record.value || '') === String(id || '')) || null;
  }

  function capabilitySummary(records, selected) {
    const current = capabilityReason(selected);
    const blocked = catalogRecord(records).filter(record => !capabilityAvailable(record));
    if (!blocked.length) return current;
    const details = blocked.map(record =>
      `${labelOf(record, record.id || '选项')}（${capabilityReason(record)}）`);
    return VCS.t('report.capability.unavailable_summary', {
      current, details: details.join('、'),
    }, '{current}；当前不可用：{details}');
  }

  function syncCapabilityControls() {
    const catalog = plain(State.catalog); const spec = plain(State.spec);
    const locale = catalogCapability(catalog.locales, spec.locale);
    const theme = catalogCapability(catalog.themes, spec.theme_id);
    const bilingualId = pathValue(spec, 'options.bilingual_mode') || 'none';
    const bilingual = catalogCapability(catalog.bilingual_modes, bilingualId);
    replaceCapabilityOptions($('rw-locale'), catalogRecord(catalog.locales), spec.locale);
    replaceCapabilityOptions($('rw-theme'), catalogRecord(catalog.themes), spec.theme_id);
    replaceCapabilityOptions($('rw-bilingual'), catalogRecord(catalog.bilingual_modes), bilingualId);
    [['rw-locale-capability', locale, catalog.locales],
      ['rw-theme-capability', theme, catalog.themes],
      ['rw-bilingual-capability', bilingual, catalog.bilingual_modes]]
      .forEach(([id, record, records]) => {
      const note = $(id); if (!note) return;
      note.textContent = capabilitySummary(records, record);
      note.classList.toggle('blocked', !capabilityAvailable(record));
      note.classList.toggle('limited', capabilityAvailable(record) &&
        catalogRecord(records).some(item => !capabilityAvailable(item)));
    });
    const precision = plain(catalog.precision); const precisionControl = $('rw-precision');
    if (precisionControl) {
      if (Number.isSafeInteger(Number(precision.minimum))) precisionControl.min = String(precision.minimum);
      if (Number.isSafeInteger(Number(precision.maximum))) precisionControl.max = String(precision.maximum);
      precisionControl.setAttribute('data-capability-available', precision.available === true ? 'true' : 'false');
      precisionControl.disabled = precision.available !== true;
    }
    const precisionNote = $('rw-precision-capability');
    if (precisionNote) {
      precisionNote.textContent = capabilityReason(precision);
      precisionNote.classList.toggle('blocked', precision.available !== true);
    }
  }

  function specCapabilitiesSatisfied() {
    const catalog = plain(State.catalog); const spec = plain(State.spec);
    const selected = [
      catalogCapability(catalog.locales, spec.locale),
      catalogCapability(catalog.themes, spec.theme_id),
      catalogCapability(catalog.bilingual_modes,
        pathValue(spec, 'options.bilingual_mode') || 'none'),
      plain(catalog.precision),
    ];
    return selected.every(capabilityAvailable);
  }

  function renderPresets() {
    const fieldset = $('rw-presets');
    if (!fieldset) return;
    const catalog = plain(State.catalog);
    const presets = catalogRecord(catalog.presets || State.bootstrap && State.bootstrap.presets);
    fieldset.innerHTML = '<legend>内置预设</legend>';
    fieldset.setAttribute('aria-busy', 'false');
    if (!presets.length) {
      const p = document.createElement('p');
      p.textContent = '服务端没有返回可用预设；已阻止预览和发布。';
      fieldset.appendChild(p);
      return;
    }
    const list = document.createElement('div');
    list.className = 'rw-preset-list';
    presets.forEach(record => {
      const id = String(record.id || '').trim();
      if (!id) return;
      const label = document.createElement('label');
      label.className = 'rw-preset';
      const input = document.createElement('input');
      input.type = 'radio'; input.name = 'rw-preset'; input.value = id;
      input.checked = id === String(State.spec && State.spec.preset_id || '');
      input.setAttribute('data-preset-id', id);
      const title = document.createElement('b'); title.textContent = labelOf(record, id);
      const description = document.createElement('small');
      description.textContent = String(uiIsEnglish()
        ? (record.description_en || record.description || record.description_zh || '')
        : (record.description_zh || record.description || record.description_en || ''));
      label.append(input, title, description); list.appendChild(label);
    });
    fieldset.appendChild(list);
  }

  function renderOutline() {
    const fieldset = $('rw-outline');
    if (!fieldset) return;
    const catalog = plain(State.catalog);
    const sections = catalogRecord(catalog.sections);
    const selected = new Set(Array.isArray(State.spec && State.spec.outline) ? State.spec.outline : []);
    fieldset.innerHTML = '<legend>选择并按预设顺序生成章节</legend>';
    fieldset.setAttribute('aria-busy', 'false');
    if (!sections.length) {
      const p = document.createElement('p'); p.textContent = '服务端没有返回章节目录。';
      fieldset.appendChild(p); return;
    }
    const list = document.createElement('div'); list.className = 'rw-outline-list';
    sections.forEach(record => {
      const id = String(record.id || '').trim(); if (!id) return;
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = id; input.checked = selected.has(id);
      input.setAttribute('data-outline-id', id);
      const span = document.createElement('span'); span.textContent = labelOf(record, id);
      label.append(input, span); list.appendChild(label);
    });
    fieldset.appendChild(list);
  }

  function renderFormats() {
    const fieldset = $('rw-formats');
    if (!fieldset) return;
    const catalog = plain(State.catalog);
    const records = plain(catalog.formats);
    const selected = new Set(Array.isArray(State.spec && State.spec.formats) ? State.spec.formats : []);
    fieldset.setAttribute('aria-busy', State.capabilityReady ? 'false' : 'true');
    let list = fieldset.querySelector('.rw-format-list');
    if (!list) {
      Array.from(fieldset.children).forEach(child => {
        if (child.tagName !== 'LEGEND') child.remove();
      });
      list = document.createElement('div'); list.className = 'rw-format-list';
      fieldset.appendChild(list);
    }
    FORMAT_ORDER.forEach(format => {
      const record = Object.assign({ id: format }, plain(records[format]));
      const capability = State.capabilities[format] || {
        available: false,
        reason: '能力状态未知。',
        accessibility: normalizeAccessibility(null),
      };
      let input = list.querySelector(`[data-format-id="${format}"]`);
      let label = input && input.closest('label');
      if (!label) {
        label = document.createElement('label');
        input = document.createElement('input');
        input.type = 'checkbox'; input.value = format;
        input.setAttribute('data-format-id', format);
        const body = document.createElement('span');
        const title = document.createElement('b'); title.setAttribute('data-format-title', format);
        const reason = document.createElement('small'); reason.setAttribute('data-format-reason', format);
        body.append(title, reason); label.append(input, body); list.appendChild(label);
      }
      label.classList.toggle('unavailable', capability.available !== true);
      input.checked = selected.has(format);
      input.disabled = capability.available !== true || State.previewBusy || State.publishBusy;
      input.setAttribute('aria-disabled', input.disabled ? 'true' : 'false');
      const title = label.querySelector(`[data-format-title="${format}"]`);
      const reason = label.querySelector(`[data-format-reason="${format}"]`);
      if (title) title.textContent = labelOf(record, format.toUpperCase());
      const renderStatus = capability.available === true
        ? (uiIsEnglish() ? 'Rendering available' : '生成可用')
        : `${uiIsEnglish() ? 'Rendering unavailable' : '生成不可用'}：${
          capability.reason || (uiIsEnglish()
            ? 'The server did not confirm availability.' : '服务端未明确确认可用')}`;
      const accessibility = normalizeAccessibility(capability.accessibility);
      label.setAttribute('data-accessibility-status', accessibility.status);
      reason.textContent = `${renderStatus}${uiIsEnglish() ? '; ' : '；'}${
        accessibilitySummary(accessibility)}`;
    });
  }

  function syncSimpleControls() {
    const spec = plain(State.spec);
    const values = {
      'rw-stable-only': !!pathValue(spec, 'scope.stable_only'),
      'rw-include-failed': !!pathValue(spec, 'scope.include_failed'),
      'rw-species': (pathValue(spec, 'scope.species') || []).join(', '),
      'rw-configurations': (pathValue(spec, 'scope.configuration_ids') || []).join(', '),
      'rw-requested-kind': spec.requested_kind || '',
      'rw-audience': spec.audience || '',
      'rw-include-thermo': !!pathValue(spec, 'options.include_thermochemistry'),
      'rw-precision': pathValue(spec, 'options.precision') || 4,
      'rw-locale': spec.locale || '',
      'rw-bilingual': pathValue(spec, 'options.bilingual_mode') || 'none',
      'rw-theme': spec.theme_id || '',
      'rw-version-policy': pathValue(spec, 'options.version_policy') || 'new_revision',
    };
    Object.entries(values).forEach(([id, value]) => {
      const control = $(id); if (!control) return;
      if (control.type === 'checkbox') control.checked = !!value;
      else control.value = String(value == null ? '' : value);
    });
    const catalog = plain(State.catalog);
    replaceOptions($('rw-audience'), catalogRecord(catalog.audiences), spec.audience);
    syncCapabilityControls();
    const scope = $('rw-scope-project'); if (scope) scope.textContent = State.projectName || State.projectId || '—';
    const project = $('rw-project-name');
    if (project) project.textContent = State.projectName
      ? `${State.projectName} · ${State.projectId}` : '当前项目无法解析。';
  }

  function renderSpec() {
    renderPresets(); syncSimpleControls(); renderOutline(); renderFormats();
    const preset = $('rw-meta-preset');
    if (preset) {
      const records = catalogRecord(plain(State.catalog).presets);
      const record = records.find(item => String(item.id || '') === String(State.spec && State.spec.preset_id || ''));
      preset.textContent = record ? labelOf(record, record.id) : safeText(State.spec && State.spec.preset_id);
    }
  }

  function stateTone(kind, value) {
    const text = String(value || '').toLowerCase();
    if (kind === 'artifact') {
      if (['generated', 'ready', 'complete', 'current', 'succeeded'].includes(text)) return 'ok';
      if (['failed', 'missing'].includes(text)) return 'bad';
      if (['stale', 'generated_unrecorded', 'pending'].includes(text)) return 'warn';
    }
    if (kind === 'science') {
      if (['verified', 'reviewed', 'final', 'human_scientific_reviewed'].includes(text)) return 'ok';
      if (['blocked'].includes(text)) return 'bad';
      if (['diagnostic', 'draft', 'machine_checked'].includes(text)) return 'warn';
    }
    if (kind === 'gate') {
      if (['eligible', 'verified', 'pass', 'passed'].includes(text)) return 'ok';
      if (['blocked', 'failed', 'fail'].includes(text)) return 'bad';
      if (['pending', 'unknown'].includes(text)) return 'warn';
    }
    return '';
  }

  function renderTopStatus(source) {
    const value = plain(source || State.preview || State.bootstrap);
    const status = plain(value.status);
    const records = {
      artifact: value.artifact_status || status.artifact_status || 'pending',
      science: value.scientific_qualification || value.scientific_status ||
        status.scientific_qualification || status.scientific_status || 'unknown',
      gate: value.publication_gate_status || status.publication_gate_status || 'unknown',
    };
    Object.entries(records).forEach(([kind, raw]) => {
      const item = document.querySelector(`[data-rw-state="${kind}"]`);
      if (!item) return;
      const label = item.querySelector('i'); if (label) label.textContent = safeText(raw, '未知');
      item.className = `rw-state ${stateTone(kind, raw)}`.trim();
    });
    const snapshot = $('rw-meta-snapshot');
    const preview = State.preview;
    const publicSnapshot = plain(preview && preview.public_snapshot);
    if (snapshot) snapshot.textContent = safeText(publicSnapshot.created_at_utc ||
      publicSnapshot.snapshot_at || publicSnapshotId(preview));
    const revision = $('rw-meta-revision');
    const rawRevision = revisionNumber(value.revision != null ? value.revision : value.base_revision);
    if (revision) revision.textContent = rawRevision == null ? '尚未发布' : `Rev. ${rawRevision}`;
    const model = $('rw-meta-model');
    const hash = String(value.report_model_sha256 || '');
    if (model) { model.textContent = hash ? hash.slice(0, 16) + '…' : '—'; model.title = hash; }
  }

  function gateMessages(value, key) {
    const raw = value && value[key];
    if (Array.isArray(raw)) return raw.map(item => typeof item === 'string' ? item :
      (item && (item.message || item.reason || item.code)) || JSON.stringify(item));
    return [];
  }

  function validationChecks(validation) {
    const value = plain(validation);
    const rows = value.checks || value.results || value.items || [];
    return Array.isArray(rows) ? rows : [];
  }

  function renderValidation(preview) {
    const box = $('rw-validation-summary'); if (!box) return;
    const validation = plain(preview && preview.validation);
    const blocking = gateMessages(preview, 'blocking');
    const warnings = gateMessages(preview, 'warnings');
    const checks = validationChecks(validation);
    box.innerHTML = '';
    if (!Object.keys(validation).length && !blocking.length && !warnings.length) {
      box.textContent = '服务端没有返回可核对的 ValidationResult；发布保持阻止。';
      return;
    }
    const summary = document.createElement('div'); summary.className = 'rw-gate-item';
    const heading = document.createElement('b'); heading.textContent = '验证结果';
    const detail = document.createElement('span');
    detail.textContent = safeText(validation.status || validation.verdict ||
      preview.publication_gate_status, '未知');
    summary.append(heading, detail); box.appendChild(summary);
    blocking.forEach(message => {
      const item = document.createElement('div'); item.className = 'rw-gate-item blocked';
      item.innerHTML = `<b>阻断</b><span>${VCS.esc(message)}</span>`; box.appendChild(item);
    });
    warnings.forEach(message => {
      const item = document.createElement('div'); item.className = 'rw-gate-item warning';
      item.innerHTML = `<b>提示</b><span>${VCS.esc(message)}</span>`; box.appendChild(item);
    });
    checks.slice(0, 20).forEach(check => {
      const row = plain(check); const item = document.createElement('div');
      const rawStatus = String(row.status || row.result || 'unknown').toLowerCase();
      item.className = 'rw-gate-item' + (['blocked', 'failed', 'fail'].includes(rawStatus)
        ? ' blocked' : ['warning', 'limited', 'unknown'].includes(rawStatus) ? ' warning' : '');
      const title = document.createElement('b'); title.textContent = safeText(row.label || row.code || row.id, '检查项');
      const description = document.createElement('span');
      description.textContent = safeText(row.message || row.reason || rawStatus);
      item.append(title, description); box.appendChild(item);
    });
  }

  function evidenceValue(value) {
    if (value === null || value === undefined || value === '') return '—';
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    try {
      const text = JSON.stringify(value);
      return text.length > 800 ? text.slice(0, 797) + '…' : text;
    } catch (_) { return '—'; }
  }

  function evidenceGroup(title, entries) {
    const section = document.createElement('section'); section.className = 'rw-evidence-group';
    const heading = document.createElement('h3'); heading.textContent = title; section.appendChild(heading);
    const dl = document.createElement('dl');
    entries.forEach(([label, value]) => {
      const dt = document.createElement('dt'); dt.textContent = label;
      const dd = document.createElement('dd'); dd.textContent = evidenceValue(value);
      dl.append(dt, dd);
    });
    section.appendChild(dl); return section;
  }

  function evidenceList(title, values, tone) {
    if (!values.length) return null;
    const section = document.createElement('section'); section.className = 'rw-evidence-group';
    const heading = document.createElement('h3'); heading.textContent = title; section.appendChild(heading);
    const list = document.createElement('ul'); list.className = `rw-evidence-list ${tone || ''}`.trim();
    values.forEach(value => { const li = document.createElement('li'); li.textContent = String(value); list.appendChild(li); });
    section.appendChild(list); return section;
  }

  function renderEvidence(preview) {
    const box = $('rw-evidence-body'); if (!box) return;
    box.innerHTML = '';
    if (!preview) {
      box.innerHTML = '<div class="rw-evidence-empty"><b>尚无可检查证据</b><span>预览完成后在这里核对公式、来源与哈希。</span></div>';
      return;
    }
    const snapshot = plain(preview.public_snapshot);
    const validation = plain(preview.validation);
    const refs = plain(preview.contract_refs);
    box.appendChild(evidenceGroup('冻结快照', [
      ['快照 ID', publicSnapshotId(preview)],
      ['项目 ID', preview.project_id],
      ['冻结时间', snapshot.created_at_utc || snapshot.snapshot_at],
      ['输入指纹', snapshot.input_fingerprint],
      ['解析范围', snapshot.resolved_scope || snapshot.scope],
    ]));
    box.appendChild(evidenceGroup('验证与合同', [
      ['Validation schema', validation.schema],
      ['Validation hash', validation.semantic_sha256 || validation.sha256],
      ['Report model hash', preview.report_model_sha256],
      ['ReportSpec', refs.report_spec || refs.spec],
      ['ReportSnapshot', refs.report_snapshot || refs.snapshot],
      ['ValidationResult', refs.validation_result || refs.validation],
      ['Operation ID', preview.operation_id],
    ]));
    const blocked = evidenceList('阻断项', gateMessages(preview, 'blocking'), 'blocked');
    const warnings = evidenceList('限制与提示', gateMessages(preview, 'warnings'), 'warning');
    if (blocked) box.appendChild(blocked); if (warnings) box.appendChild(warnings);
  }

  function sectionRecords() {
    const catalog = plain(State.catalog);
    const byId = Object.fromEntries(catalogRecord(catalog.sections).map(item => [item.id, item]));
    return (Array.isArray(State.spec && State.spec.outline) ? State.spec.outline : [])
      .map(id => byId[id] || { id, label_zh: id });
  }

  function renderSectionNav() {
    const nav = $('rw-section-nav'); if (!nav) return; nav.innerHTML = '';
    sectionRecords().forEach(record => {
      const item = document.createElement('li');
      item.textContent = labelOf(record, record.id); nav.appendChild(item);
    });
  }

  function renderPreview(preview) {
    const frame = $('rw-preview-frame'); const empty = $('rw-preview-empty');
    if (!frame || !empty) return;
    if (!preview || typeof preview.html !== 'string' || !preview.html.trim()) {
      frame.hidden = true; frame.removeAttribute('srcdoc'); empty.hidden = false;
    } else {
      // The report document runs with an opaque origin and no scripts/forms/popups.
      frame.setAttribute('sandbox', '');
      frame.srcdoc = preview.html; frame.hidden = false; empty.hidden = true;
    }
    const binding = $('rw-preview-binding');
    if (binding) binding.textContent = preview
      ? `preview ${String(preview.preview_id || '').slice(0, 12)} · base Rev. ${preview.base_revision == null ? '—' : preview.base_revision}`
      : '未绑定';
    renderSectionNav(); renderEvidence(preview); renderValidation(preview); renderTopStatus(preview);
  }

  function formatState(format, label, tone = '') {
    State.formatStates[format] = { label: String(label || ''), tone: String(tone || '') };
  }

  function renderFormatStates() {
    FORMAT_ORDER.forEach(format => {
      if (State.formatStates[format]) return;
      const capability = State.capabilities[format];
      if (!capability || capability.available !== true) {
        formatState(format, capability && capability.reason || '能力未确认', 'bad');
      } else if (State.preview && format === 'html') formatState(format, '预览完成', 'ok');
      else formatState(format, '未请求');
    });
    ['model_json', 'validation_json'].forEach(format => {
      if (!State.formatStates[format]) formatState(format, State.preview ? '预览已绑定' : '等待预览', State.preview ? 'ok' : '');
    });
    if (!State.formatStates.manifest) formatState('manifest', State.lastPublish ? '已登记' : '等待发布', State.lastPublish ? 'ok' : '');
    document.querySelectorAll('#rw-format-status [data-format]').forEach(item => {
      const record = State.formatStates[item.dataset.format] || { label: '未请求', tone: '' };
      item.className = record.tone;
      const text = item.querySelector('span'); if (text) text.textContent = record.label;
    });
  }

  function applyServerFormatStates(records) {
    const formats = plain(records);
    const toneByState = {
      ready_to_generate: '', not_requested: '', unavailable: 'bad',
      queued: 'busy', rendering: 'busy', generated: 'ok', complete: 'ok', failed: 'bad',
    };
    const labelByState = {
      ready_to_generate: '等待发布', not_requested: '未请求', unavailable: '不可用',
      queued: '排队中', rendering: '生成中', generated: '完成', complete: '完成', failed: '失败',
    };
    Object.entries(formats).forEach(([rawFormat, raw]) => {
      const format = rawFormat === 'model' ? 'model_json'
        : rawFormat === 'validation' ? 'validation_json' : rawFormat;
      if (!['html', 'docx', 'pdf', 'model_json', 'validation_json', 'manifest'].includes(format)) return;
      const record = plain(raw); const state = String(record.state || record.status || '');
      const label = labelByState[state] || state || (record.available === false ? '不可用' : '状态未知');
      const reason = String(record.reason || '');
      formatState(format, reason && (state === 'unavailable' || state === 'failed')
        ? `${label}：${reason}` : label, toneByState[state] || (record.available === false ? 'bad' : ''));
    });
  }

  function renderHistory() {
    const box = $('rw-history-list'); if (!box) return; box.innerHTML = '';
    if (State.historyStatus === 'loading') {
      const loading = document.createElement('div'); loading.className = 'rw-history-empty';
      loading.textContent = '正在读取版本历史…'; box.appendChild(loading);
    } else if (State.historyStatus === 'unavailable') {
      const unavailable = document.createElement('div'); unavailable.className = 'rw-history-empty unavailable';
      unavailable.textContent = '版本历史暂时不可用；不能据此判断当前项目没有 revision。';
      if (State.historyError) unavailable.title = State.historyError;
      box.appendChild(unavailable);
    } else if (!State.history.length) {
      const empty = document.createElement('div'); empty.className = 'rw-history-empty';
      empty.textContent = '当前项目还没有已登记的报告 revision。'; box.appendChild(empty);
    } else {
      const wantedRevision = String(State.routeQuery.revision || '');
      const queriedSpec = safeId(State.routeQuery.spec);
      const publishedRevision = plain(State.lastPublish && State.lastPublish.revision);
      const boundReportId = safeId(publishedRevision.report_id ||
        State.lastPublish && State.lastPublish.report_id ||
        State.preview && State.preview.report_id || State.bootstrap && State.bootstrap.report_id);
      const activeSpec = queriedSpec || String(State.spec && State.spec.preset_id || '');
      const highlightCandidates = State.history.filter(raw => {
        const row = plain(raw); const revision = row.sequence != null ? row.sequence
          : row.revision != null ? revisionNumber(row.revision) : row.number;
        if (!wantedRevision || String(revision) !== wantedRevision) return false;
        if (boundReportId) return String(row.report_id || '') === boundReportId;
        if (!activeSpec) return false;
        return [row.report_id, row.spec_id, row.spec_sha256, row.preset_id]
          .some(value => String(value || '') === activeSpec);
      });
      const highlightedRevisionId = highlightCandidates.length === 1
        ? String(highlightCandidates[0].revision_id ||
          `${highlightCandidates[0].report_id || ''}:${highlightCandidates[0].sequence || ''}`)
        : '';
      State.history.forEach((raw, index) => {
        const row = plain(raw); const card = document.createElement('article'); card.className = 'rw-history-item';
        const revision = row.sequence != null ? row.sequence
          : row.revision != null ? revisionNumber(row.revision)
            : row.number != null ? row.number : index + 1;
        const revisionIdentity = String(row.revision_id || `${row.report_id || ''}:${revision}`);
        if (highlightedRevisionId && revisionIdentity === highlightedRevisionId) {
          card.setAttribute('aria-current', 'true');
        }
        if (row.report_id) card.setAttribute('data-report-id', String(row.report_id));
        if (row.preset_id) card.setAttribute('data-spec-id', String(row.preset_id));
        const header = document.createElement('header');
        const title = document.createElement('b'); title.textContent = `Rev. ${revision}`;
        const time = document.createElement('time'); time.textContent = safeText(row.created_at_utc || row.created_at || row.timestamp, '时间未知');
        header.append(title, time);
        const status = document.createElement('p');
        status.textContent = `${safeText(row.scientific_qualification || row.scientific_status, '未判定')} · ${safeText(row.artifact_status, '产物未知')}`;
        const hash = document.createElement('code');
        hash.textContent = safeText(row.report_model_sha256 || row.model_sha256 || row.manifest_sha256, '无模型哈希');
        card.append(header, status, hash); box.appendChild(card);
      });
    }
    const compare = $('rw-compare-revision');
    if (compare) {
      compare.disabled = true;
      compare.title = State.historyStatus === 'unavailable' ? '版本历史当前不可用'
        : State.history.length < 2 ? '至少需要两个 revision'
          : '当前 MVP 只提供历史列表，尚未开放差异 API';
    }
  }

  function renderActions() {
    const previewButton = $('rw-preview'); const publishButton = $('rw-publish');
    const pick = $('rw-pick-output'); const open = $('rw-open-output');
    const canPreview = !!State.spec && State.capabilityReady && selectedFormatsAreAvailable() &&
      specCapabilitiesSatisfied() &&
      !State.bootstrapBusy && !State.previewBusy && !State.publishBusy;
    if (previewButton) previewButton.disabled = !canPreview;
    const bound = !!State.preview && !!State.preview.preview_id &&
      hasCompletePreviewToken(State.preview) && !State.dirty &&
      State.preview.project_id === State.projectId &&
      State.publishedPreviewId !== State.preview.preview_id;
    if (publishButton) {
      publishButton.disabled = !bound || !selectedFormatsAreAvailable() ||
        !specCapabilitiesSatisfied() || State.publishBusy;
      publishButton.textContent = State.publishBusy ? '正在生成…' : '生成所选格式';
    }
    if (pick) pick.disabled = State.publishBusy;
    const hasProjectOutput = State.outputProjectId === State.projectId && !!State.outputDir;
    if (open) open.disabled = !(hasProjectOutput || State.lastPublish && State.lastPublish.out_dir);
    const discard = $('rw-discard-draft'); const key = draftKey();
    const hasDraft = !!(key && VCS.workspace && VCS.workspace.drafts &&
      VCS.workspace.drafts.load(key));
    if (discard) discard.disabled = State.publishBusy || State.bootstrapBusy || !hasDraft;
    const form = $('rw-spec-form');
    if (form) {
      form.setAttribute('aria-busy', State.previewBusy || State.publishBusy ? 'true' : 'false');
      form.querySelectorAll('input, select, textarea').forEach(control => {
        if (State.publishBusy) control.disabled = true;
        else if (control.hasAttribute('data-capability-available')) {
          control.disabled = control.getAttribute('data-capability-available') !== 'true';
        } else control.disabled = false;
      });
    }
    renderFormats(); renderFormatStates();
  }

  function persistDraft() {
    const key = draftKey();
    if (!key || !State.spec || !VCS.workspace || !VCS.workspace.drafts) return;
    try {
      VCS.workspace.drafts.save(key, JSON.stringify({
        schema: 'vcstudio.report-workbench-draft/v1',
        project_id: State.projectId,
        mode: State.mode,
        report_spec: requestFromSpec(),
      }), { label: '报告配置尚未绑定新预览' });
    } catch (error) {
      setOperation(VCS.t('report.draft.save_failed', {
        error: error && error.message || error,
      }, '报告配置草稿未能保存：{error}'), 'bad');
    }
  }

  function recoveredDraft() {
    const key = draftKey();
    if (!key || !VCS.workspace || !VCS.workspace.drafts) return null;
    const record = VCS.workspace.drafts.load(key);
    if (!record || String(record.project_id || '') !== State.projectId) return null;
    try {
      const value = JSON.parse(String(record.text || ''));
      if (value && value.project_id === State.projectId && value.mode === State.mode &&
          plain(value.report_spec)) return value;
    } catch (_) { /* 无效本地草稿不能覆盖服务端默认 */ }
    return null;
  }

  async function discardDraft({ reload = true } = {}) {
    if (State.publishBusy) return false;
    const projectId = State.projectId; const projectPath = State.projectPath;
    const mode = State.mode; const key = draftKey(projectId, mode);
    if (!key || !VCS.workspace || !VCS.workspace.drafts) return false;
    VCS.workspace.drafts.remove(key);
    if (!reload) { renderActions(); return true; }
    setOperation('已丢弃当前项目与工作台模式的本地报告草稿，正在恢复服务端配置。', 'busy');
    const explicitSpec = safeId(State.routeQuery.spec);
    const loaded = await loadBootstrap(explicitSpec || null, {
      recoverDraft: false,
      explicitSpecQuery: explicitSpec,
    });
    return loaded !== false && sameProject(projectId, projectPath) && State.mode === mode;
  }

  function markSpecDirty({ schedule = true } = {}) {
    if (!State.spec) return;
    State.dirty = true; State.publishedPreviewId = '';
    const binding = $('rw-preview-binding'); if (binding) binding.textContent = '配置已变化，等待新预览';
    persistDraft();
    if (schedule) {
      if (State.previewTimer) clearTimeout(State.previewTimer);
      State.previewTimer = setTimeout(() => {
        State.previewTimer = null; previewCurrentSpec({ automatic: true });
      }, 450);
    }
    setOperation('配置已修改；发布已锁定，正在等待新的绑定预览。', 'busy');
    renderActions();
  }

  function applyPendingIntent() {
    const intent = State.pendingIntent;
    if (!intent || !State.spec) return false;
    State.pendingIntent = null;
    let changed = false;
    if (Array.isArray(intent.formats) && intent.formats.length) {
      const formats = intent.formats.filter(format => FORMAT_ORDER.includes(format) &&
        State.capabilities[format] && State.capabilities[format].available === true);
      if (formats.length) { State.spec.formats = formats; changed = true; }
    }
    if (intent.mode === 'comparison' && Array.isArray(intent.comparisonProjectIds) &&
        intent.comparisonProjectIds.length > 1) {
      showAlert(VCS.t('report.comparison.single_project_boundary', {
        count: intent.comparisonProjectIds.length,
      }, '已从多项目入口进入并保留 {count} 个选择。当前 Phase C ReportSpec 只允许服务端冻结当前主项目；' +
        '不会把其它项目路径或身份静默写入单项目快照。'));
    }
    return changed;
  }

  function presetForMode(mode, catalog) {
    const presets = catalogRecord(plain(catalog).presets);
    const wanted = mode === 'si' ? 'supporting-information'
      : mode === 'draftpack' ? 'manuscript-materials' : '';
    return wanted && presets.some(record => record.id === wanted) ? wanted : null;
  }

  async function fetchHistory(projectId, projectPath, generation) {
    try {
      const result = await VCS.call('report_workbench_history', projectPath);
      if (generation !== State.bootstrapGeneration || !sameProject(projectId, projectPath)) {
        return { status: 'stale', revisions: [], error: '' };
      }
      if (!result || result.ok === false || result.error) {
        return { status: 'unavailable', revisions: [],
          error: String(result && result.error || '版本历史接口没有返回有效结果') };
      }
      return { status: 'ready', revisions: normalizeHistory(result), error: '' };
    } catch (error) {
      return { status: 'unavailable', revisions: [],
        error: String(error && error.message || error) };
    }
  }

  async function loadBootstrap(presetId = null, options = {}) {
    const project = currentProject();
    const projectId = projectIdentity(project); const projectPath = String(project && project.path || '');
    if (!projectId || !projectPath) {
      State.spec = null; State.preview = null;
      State.history = []; State.historyStatus = 'unavailable';
      State.historyError = '当前报告路由没有可解析的项目上下文';
      State.outputDir = ''; State.outputProjectId = '';
      const output = $('rw-output-path');
      if (output) output.textContent = '发布时尚未选择目录';
      showAlert('当前报告路由没有可解析的项目路径和稳定项目 ID。');
      setOperation('无法加载报告工作台。', 'bad'); renderHistory(); renderActions(); return false;
    }
    const projectChanged = projectId !== State.projectId || projectPath !== State.projectPath;
    if (projectChanged) {
      State.outputDir = ''; State.outputProjectId = '';
      const output = $('rw-output-path');
      if (output) output.textContent = '发布时尚未选择目录';
    }
    State.projectId = projectId; State.projectPath = projectPath;
    State.projectName = String(project.name || project.display_name || projectId);
    const generation = ++State.bootstrapGeneration;
    State.previewGeneration += 1; State.publishGeneration += 1;
    State.bootstrapBusy = true; State.previewBusy = false; State.publishBusy = false;
    State.preview = null; State.lastPublish = null; State.publishedPreviewId = '';
    State.history = []; State.historyStatus = 'loading'; State.historyError = '';
    State.formatStates = Object.create(null); State.dirty = true;
    showAlert(''); setOperation('正在读取版本化预设、格式能力与报告状态…', 'busy');
    renderHistory(); renderActions();
    try {
      const bootstrapPromise = VCS.call('report_workbench_bootstrap', projectPath, presetId || null);
      const capabilityPromise = VCS.call('proj_report_capabilities');
      const historyPromise = fetchHistory(projectId, projectPath, generation);
      const [bootstrap, capabilities, history] = await Promise.all(
        [bootstrapPromise, capabilityPromise, historyPromise]);
      if (generation !== State.bootstrapGeneration || !sameProject(projectId, projectPath)) return false;
      if (!bootstrap || bootstrap.ok === false || bootstrap.error) {
        throw new Error(bootstrap && bootstrap.error || '报告工作台 bootstrap 没有返回有效结果');
      }
      const returnedProjectId = safeId(bootstrap.project_id || projectId);
      if (returnedProjectId !== projectId) {
        throw new Error('bootstrap 返回的项目 ID 与当前项目不一致，已丢弃响应');
      }
      State.bootstrap = bootstrap;
      State.catalog = plain(bootstrap.catalog || bootstrap.report_catalog);
      State.capabilities = explicitCapabilities(bootstrap, capabilities);
      State.capabilityReady = FORMAT_ORDER.every(format =>
        State.capabilities[format] && typeof State.capabilities[format].available === 'boolean');
      const spec = clone(bootstrap.report_spec || bootstrap.spec);
      if (!plain(spec) || !spec.preset_id) throw new Error('bootstrap 缺少 ReportSpec');
      State.spec = spec;
      if (history && history.status === 'ready') {
        State.history = history.revisions; State.historyStatus = 'ready'; State.historyError = '';
      } else {
        State.history = []; State.historyStatus = 'unavailable';
        State.historyError = String(history && history.error || '版本历史不可用');
      }
      const modePreset = !presetId ? presetForMode(State.mode, State.catalog) : null;
      if (modePreset && modePreset !== State.spec.preset_id && options.applyModePreset !== false) {
        State.bootstrapBusy = false;
        return loadBootstrap(modePreset, { applyModePreset: false, recoverDraft: false });
      }
      const draft = options.recoverDraft !== false && !presetId && recoveredDraft();
      if (draft && plain(draft.report_spec)) State.spec = clone(draft.report_spec);
      const intentChanged = applyPendingIntent();
      State.spec.formats = (Array.isArray(State.spec.formats) ? State.spec.formats : [])
        .filter(format => FORMAT_ORDER.includes(format) && State.capabilities[format] &&
          State.capabilities[format].available === true);
      renderSpec(); renderHistory(); renderPreview(null); renderTopStatus(bootstrap);
      State.appliedMode = ['si', 'draftpack'].includes(State.mode) ? State.mode : 'report';
      State.loadedSpecQuery = String(options.explicitSpecQuery || '');
      State.bootstrapBusy = false;
      if (!selectedFormatsAreAvailable()) {
        showAlert('没有至少一种经服务端明确确认可用的输出格式；预览和发布保持阻止。');
        setOperation('格式能力检查未通过。', 'bad'); renderActions(); return false;
      }
      if (!specCapabilitiesSatisfied()) {
        showAlert('当前语言、主题、双语或精度设置没有得到服务端 available=true 的能力确认；预览和发布保持阻止。');
        setOperation('报告配置能力检查未通过。', 'bad'); renderActions(); return false;
      }
      State.dirty = true;
      if (draft || intentChanged) persistDraft();
      setOperation('工作台已加载，正在生成与当前配置绑定的 HTML 预览。', 'busy');
      renderActions();
      await previewCurrentSpec({ automatic: true });
      return true;
    } catch (error) {
      if (generation !== State.bootstrapGeneration) return false;
      State.bootstrapBusy = false; State.spec = null; State.capabilityReady = false;
      State.history = []; State.historyStatus = 'unavailable';
      State.historyError = String(error && error.message || error);
      showAlert(error && error.message || String(error));
      setOperation('报告工作台加载失败；没有进行任何发布。', 'bad');
      renderHistory(); renderActions(); return false;
    }
  }

  async function selectPreset(presetId) {
    const id = safeId(presetId);
    if (!id || State.bootstrapBusy || State.previewBusy || State.publishBusy) return;
      await loadBootstrap(id, {
        recoverDraft: false, applyModePreset: false, explicitSpecQuery: '',
      });
  }

  async function previewCurrentSpec({ automatic = false } = {}) {
    if (!State.spec || State.bootstrapBusy || State.publishBusy) return false;
    if (!selectedFormatsAreAvailable()) {
      showAlert('请至少选择一种服务端明确可用的格式。');
      activateStep('export'); renderActions(); return false;
    }
    if (!specCapabilitiesSatisfied()) {
      showAlert('当前语言、主题、双语或精度能力未得到服务端明确确认。');
      activateStep('language'); renderActions(); return false;
    }
    const request = requestFromSpec(); const fingerprint = JSON.stringify(request);
    const projectId = State.projectId; const projectPath = State.projectPath;
    const generation = ++State.previewGeneration;
    State.previewBusy = true; State.dirty = true; showAlert('');
    setOperation(automatic ? '配置已保存，正在刷新绑定预览…' : '正在生成绑定 HTML 预览…', 'busy');
    FORMAT_ORDER.forEach(format => {
      if (request.formats.includes(format)) formatState(format, format === 'html' ? '生成预览中' : '等待发布', format === 'html' ? 'busy' : '');
    });
    renderActions();
    try {
      const result = await VCS.call('report_workbench_preview', projectPath, request);
      if (generation !== State.previewGeneration || !sameProject(projectId, projectPath) ||
          fingerprint !== specFingerprint()) return false;
      if (!result || result.ok === false || result.error) {
        throw new Error(result && result.error || '报告预览没有返回有效结果');
      }
      if (safeId(result.project_id) !== projectId) {
        throw new Error('预览项目 ID 与当前项目不一致，已丢弃响应');
      }
      if (!result.preview_id || typeof result.html !== 'string') {
        throw new Error('预览缺少 preview_id 或 sandbox HTML');
      }
      if (!hasCompletePreviewToken(result)) {
        throw new Error('预览缺少完整、可核对的发布绑定 token');
      }
      if (plain(result.report_spec).preset_id) State.spec = clone(result.report_spec);
      State.preview = result; State.dirty = false; State.publishedPreviewId = '';
      applyServerFormatStates(result.format_status);
      formatState('html', '预览完成', 'ok');
      formatState('model_json', result.report_model_sha256 ? '预览已绑定' : '模型绑定未知', result.report_model_sha256 ? 'ok' : 'bad');
      formatState('validation_json', plain(result.validation).schema ? '预览已绑定' : '验证绑定未知', plain(result.validation).schema ? 'ok' : 'bad');
      const key = draftKey();
      if (key && VCS.unsaved) VCS.unsaved.clear('draft-' + key);
      renderSpec(); renderPreview(result);
      setOperation('HTML 预览已与当前 ReportSpec、快照和 ValidationResult 绑定。');
      renderActions(); return true;
    } catch (error) {
      if (generation !== State.previewGeneration) return false;
      State.preview = null; State.dirty = true;
      formatState('html', '预览失败', 'bad');
      showAlert(error && error.message || String(error));
      setOperation('预览失败；发布保持阻止。', 'bad'); renderPreview(null); renderActions();
      return false;
    } finally {
      if (generation === State.previewGeneration) { State.previewBusy = false; renderActions(); }
    }
  }

  function expectedBindings(preview) {
    const token = plain(preview && preview.preview_token);
    const keys = [
      'schema', 'preview_id', 'project_id', 'spec_sha256', 'snapshot_sha256',
      'validation_sha256', 'report_model_sha256', 'base_revision',
      'base_manifest_sha256',
    ];
    const expected = {};
    keys.forEach(key => {
      if (Object.prototype.hasOwnProperty.call(token, key)) expected[key] = clone(token[key]);
    });
    return expected;
  }

  async function pickOutputDirectory(expectedProjectId = State.projectId,
                                     expectedProjectPath = State.projectPath) {
    if (State.publishBusy) return '';
    const result = await VCS.call('pick_dir');
    if (!sameProject(expectedProjectId, expectedProjectPath) ||
        State.projectId !== expectedProjectId || State.projectPath !== expectedProjectPath) return '';
    if (!result || result.cancelled || !result.path) return '';
    if (result.error) { showAlert(result.error); return ''; }
    State.outputDir = String(result.path);
    State.outputProjectId = expectedProjectId;
    const output = $('rw-output-path'); if (output) output.textContent = State.outputDir;
    renderActions(); return State.outputDir;
  }

  function collectPublishedFiles(value, output) {
    if (typeof value === 'string') {
      const match = value.match(/\.([A-Za-z0-9]+)$/);
      if (match) output[match[1].toLowerCase()] = value;
      return;
    }
    if (Array.isArray(value)) { value.forEach(item => collectPublishedFiles(item, output)); return; }
    Object.values(plain(value)).forEach(item => collectPublishedFiles(item, output));
  }

  function applyPublishResult(result) {
    applyServerFormatStates(result && result.format_status);
    const files = Object.create(null); collectPublishedFiles(result && result.files, files);
    FORMAT_ORDER.forEach(format => {
      if (files[format]) formatState(format, '完成', 'ok');
      else if (State.spec.formats.includes(format)) formatState(format, '未返回已登记产物', 'bad');
    });
    const modelFile = plain(result && result.model_file);
    if (modelFile.available === true) formatState('model_json', '完成', 'ok');
    const contractFiles = plain(result && result.contract_files);
    const validationFile = plain(contractFiles.validation || contractFiles.validation_result);
    if (validationFile.available === true) formatState('validation_json', '完成', 'ok');
    formatState('manifest', result && (result.manifest || result.manifest_path || result.revision != null)
      ? '已登记' : '登记状态未知', result && (result.manifest || result.manifest_path || result.revision != null) ? 'ok' : 'bad');
    renderFormatStates();
  }

  async function publishBoundPreview() {
    if (State.publishBusy || State.dirty || !State.preview || !State.preview.preview_id ||
        !hasCompletePreviewToken(State.preview) ||
        State.preview.project_id !== State.projectId || !selectedFormatsAreAvailable()) {
      showAlert('当前配置没有可发布的绑定预览。请先刷新预览。'); renderActions(); return false;
    }
    const projectId = State.projectId; const projectPath = State.projectPath;
    const outputDir = State.outputProjectId === projectId && State.outputDir
      ? State.outputDir : await pickOutputDirectory(projectId, projectPath);
    if (!outputDir) return false;
    const previewId = String(State.preview.preview_id); const generation = ++State.publishGeneration;
    const expected = expectedBindings(State.preview);
    const publishFingerprint = specFingerprint(); const publishDraftKey = draftKey();
    const publishMode = State.mode;
    State.publishBusy = true; showAlert(''); setOperation('正在发布绑定预览；不会重建门禁或 ValidationResult。', 'busy');
    State.spec.formats.forEach(format => formatState(format, '生成中', 'busy'));
    renderActions();
    let result = null;
    try {
      // Security boundary: publish receives only preview_id plus optimistic bindings;
      // no editable or server-owned scientific contracts are resubmitted.
      result = await VCS.call(
        'report_workbench_publish', projectPath, outputDir, previewId, expected);
      if (generation !== State.publishGeneration || !sameProject(projectId, projectPath)) return false;
      if (!result || result.ok === false || result.error) {
        throw new Error(result && result.error || '报告发布没有返回有效结果');
      }
      const specUnchanged = publishFingerprint === specFingerprint() &&
        publishDraftKey === draftKey() && State.mode === publishMode;
      State.lastPublish = result; State.publishedPreviewId = previewId;
      State.dirty = !specUnchanged;
      applyPublishResult(result); renderTopStatus(result);
      if (specUnchanged && publishDraftKey && VCS.workspace && VCS.workspace.drafts) {
        VCS.workspace.drafts.remove(publishDraftKey);
      } else if (!specUnchanged) persistDraft();
      const revision = revisionNumber(result.revision);
      const revisionText = revision != null ? VCS.t('report.publish.revision_suffix', {
        revision,
      }, '为 Rev. {revision}') : '';
      const statusText = specUnchanged
        ? VCS.t('report.publish.formats_below', {}, '各格式状态见下方。')
        : VCS.t('report.publish.preview_stale', {}, '编辑内容已变化，仍需生成新的绑定预览。');
      setOperation(VCS.t('report.publish.completed', {
        revision: revisionText, status: statusText,
      }, '报告已发布{revision}；{status}'));
      VCS.toast('报告 revision 已生成');
      try {
        const history = await VCS.call('report_workbench_history', projectPath);
        if (generation === State.publishGeneration && sameProject(projectId, projectPath)) {
          if (history && history.ok !== false && !history.error) {
            State.history = normalizeHistory(history); State.historyStatus = 'ready'; State.historyError = '';
          } else {
            State.history = []; State.historyStatus = 'unavailable';
            State.historyError = String(history && history.error || '版本历史接口没有返回有效结果');
          }
          renderHistory();
        }
      } catch (historyError) {
        if (generation === State.publishGeneration && sameProject(projectId, projectPath)) {
          State.history = []; State.historyStatus = 'unavailable';
          State.historyError = String(historyError && historyError.message || historyError);
          renderHistory();
        }
      }
      renderActions(); return true;
    } catch (error) {
      if (generation !== State.publishGeneration) return false;
      const publishedFiles = Object.create(null);
      collectPublishedFiles(result && result.files, publishedFiles);
      if (Object.keys(publishedFiles).length) {
        applyPublishResult(result);
        State.lastPublish = result;
        State.publishedPreviewId = previewId;
        renderTopStatus(result);
      } else {
        State.spec.formats.forEach(format => formatState(format, '生成失败', 'bad'));
      }
      if (result && (result.stale_preview || result.revision_conflict)) State.dirty = true;
      showAlert(error && error.message || String(error));
      setOperation(Object.keys(publishedFiles).length
        ? '部分产物已经生成，但发布登记失败；请保留文件并按提示刷新，不要重复发布。'
        : '发布失败；已有成功 revision 和预览未被覆盖。', 'bad');
      renderActions();
      return false;
    } finally {
      if (generation === State.publishGeneration) { State.publishBusy = false; renderActions(); }
    }
  }

  function handleFormChange(event) {
    const target = event.target; if (!target || !State.spec) return;
    if (target.matches('[data-preset-id]')) { selectPreset(target.value); return; }
    if (target.matches('[data-outline-id]')) {
      const checked = Array.from(document.querySelectorAll('#rw-outline [data-outline-id]:checked'))
        .map(input => input.value);
      State.spec.outline = checked; markSpecDirty(); return;
    }
    if (target.matches('[data-format-id]')) {
      State.spec.formats = FORMAT_ORDER.filter(format => {
        const input = document.querySelector(`#rw-formats [data-format-id="${format}"]`);
        return input && input.checked && !input.disabled;
      });
      markSpecDirty(); return;
    }
    const listPath = target.getAttribute('data-spec-list');
    if (listPath) {
      const values = String(target.value || '').split(',').map(item => item.trim()).filter(Boolean);
      setPath(State.spec, listPath, values); markSpecDirty(); return;
    }
    const path = target.getAttribute('data-spec-path'); if (!path) return;
    let value = target.type === 'checkbox' ? target.checked : target.value;
    if (target.type === 'number') value = Number.parseInt(target.value, 10);
    setPath(State.spec, path, value); markSpecDirty();
  }

  function modeFromRoute(routeId) {
    const modes = {
      'publish-report': 'report', 'publish-si': 'si', 'publish-draftpack': 'draftpack',
      'publish-versions': 'versions', 'publish-export': 'export',
    };
    return modes[String(routeId || '')] || 'report';
  }

  function applyModeFocus() {
    if (State.mode === 'versions') {
      const heading = $('rw-history-heading'); if (heading) heading.focus({ preventScroll: true });
    } else if (State.mode === 'export' || State.mode === 'draftpack') activateStep('export');
    else if (State.mode === 'si') activateStep('outline');
  }

  async function enterWorkbench(routeDetail) {
    const routeGeneration = ++State.routeGeneration;
    const route = routeDetail || VCS.workspace && VCS.workspace.current || {};
    State.mode = modeFromRoute(route.id);
    State.routeQuery = plain(route.query);
    const project = currentProject();
    if (!project || !project.path) {
      if (window.Project && typeof window.Project.reload === 'function') await window.Project.reload();
    }
    if (routeGeneration !== State.routeGeneration) return false;
    const refreshed = currentProject(); const id = projectIdentity(refreshed);
    if (!id || !refreshed || !refreshed.path) {
      showAlert('当前项目尚未加载。请从全局项目栏选择项目后重试。');
      setOperation('等待项目上下文。', 'bad'); return;
    }
    const requestedSpec = safeId(State.routeQuery.spec);
    const requestedRevision = /^[1-9][0-9]{0,8}$/.test(String(State.routeQuery.revision || ''))
      ? String(State.routeQuery.revision) : '';
    const configMode = ['si', 'draftpack'].includes(State.mode) ? State.mode : 'report';
    if (id !== State.projectId || String(refreshed.path) !== State.projectPath || !State.spec ||
        requestedSpec !== State.loadedSpecQuery || configMode !== State.appliedMode ||
        !!State.pendingIntent) {
      const loaded = await loadBootstrap(requestedSpec || null, {
        recoverDraft: !requestedSpec,
        explicitSpecQuery: requestedSpec,
      });
      if (loaded === false && routeGeneration === State.routeGeneration) return false;
    }
    if (routeGeneration !== State.routeGeneration) return false;
    State.routeQuery = Object.assign({}, State.routeQuery,
      requestedRevision ? { revision: requestedRevision } : { revision: '' });
    renderHistory();
    applyModeFocus();
    return true;
  }

  function open(intent = {}) {
    const project = currentProject();
    const projectId = safeId(intent.projectId || projectIdentity(project));
    if (!projectId || !VCS.workspace || typeof VCS.workspace.navigateRoute !== 'function') {
      VCS.toast('报告工作台需要有效项目上下文', 'fail');
      return Promise.resolve({ ok: false, missingProject: true });
    }
    State.pendingIntent = {
      mode: String(intent.mode || 'report'),
      formats: Array.isArray(intent.formats) ? intent.formats.slice() : [],
      comparisonProjectIds: Array.isArray(intent.comparisonProjectIds)
        ? intent.comparisonProjectIds.map(safeId).filter(Boolean) : [],
    };
    const route = State.pendingIntent.mode === 'draftpack' ? 'publish-draftpack' : 'publish-report';
    return VCS.workspace.navigateRoute(route, {
      projectId,
      query: { project: projectId },
      source: String(intent.source || 'report-workbench-open'),
    });
  }

  function wire() {
    const steps = $('rw-step-list');
    if (steps) {
      steps.addEventListener('click', event => {
        const button = event.target.closest('[data-rw-step]');
        if (button) activateStep(button.dataset.rwStep, { focus: false });
      });
      steps.addEventListener('keydown', event => {
        const button = event.target.closest('[data-rw-step]'); if (!button) return;
        const index = STEP_ORDER.indexOf(button.dataset.rwStep); let next = index;
        if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (index + 1) % STEP_ORDER.length;
        else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = (index - 1 + STEP_ORDER.length) % STEP_ORDER.length;
        else if (event.key === 'Home') next = 0; else if (event.key === 'End') next = STEP_ORDER.length - 1;
        else return;
        event.preventDefault(); activateStep(STEP_ORDER[next]);
        const target = $(`rw-step-button-${STEP_ORDER[next]}`); if (target) target.focus();
      });
    }
    const form = $('rw-spec-form'); if (form) form.addEventListener('change', handleFormChange);
    const preview = $('rw-preview'); if (preview) preview.addEventListener('click', () => previewCurrentSpec());
    const publish = $('rw-publish'); if (publish) publish.addEventListener('click', publishBoundPreview);
    const pick = $('rw-pick-output'); if (pick) pick.addEventListener('click', () =>
      pickOutputDirectory(State.projectId, State.projectPath));
    const discard = $('rw-discard-draft');
    if (discard) discard.addEventListener('click', () => discardDraft({ reload: true }));
    const openOutput = $('rw-open-output'); if (openOutput) openOutput.addEventListener('click', () => {
      const path = State.outputProjectId === State.projectId ? State.outputDir : '';
      if (path) VCS.call('open_dir', path);
    });
    const compare = $('rw-compare-revision'); if (compare) compare.addEventListener('click', () => {
      VCS.toast('当前 MVP 已保留 revision 历史；差异 API 尚未开放。');
    });
    document.addEventListener('vcs:page', event => {
      if (event.detail && event.detail.page === 'report-workbench') {
        setTimeout(() => enterWorkbench(), 0);
      }
    });
    document.addEventListener('vcs:route', event => {
      if (event.detail && event.detail.page === 'report-workbench') enterWorkbench(event.detail);
    });
    document.addEventListener('vcs:workspace-project', () => {
      const page = $('page-report-workbench');
      if (page && !page.hidden) enterWorkbench();
    });
    document.addEventListener('vcs:language', () => {
      if (State.spec) renderSpec();
      renderPreview(State.preview); renderHistory(); renderFormatStates();
    });
    document.addEventListener('vcs:report-workbench-discard-draft', event => {
      const detail = plain(event.detail);
      if (detail.project_id && safeId(detail.project_id) !== State.projectId) return;
      if (detail.mode && String(detail.mode) !== State.mode) return;
      discardDraft({ reload: detail.reload !== false });
    });
  }

  window.ReportWorkbench = {
    open,
    refresh: () => loadBootstrap(null, { recoverDraft: true }),
    preview: previewCurrentSpec,
    publish: publishBoundPreview,
    discardDraft,
    get state() {
      return {
        project_id: State.projectId,
        mode: State.mode,
        dirty: State.dirty,
        preview_id: State.preview && State.preview.preview_id || null,
        output_dir: State.outputProjectId === State.projectId ? State.outputDir || null : null,
        history_status: State.historyStatus,
      };
    },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true });
  else wire();
})();
