// analysis-workbench.js — Phase D registry-driven analysis UI.
// Scientific rows, rankings, gates and sensitivity sets are server-owned.
'use strict';

(function () {
  const VCS = window.VCS;
  if (!VCS) return;
  const $ = id => document.getElementById(id);
  const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
  const ROUTE_ANALYSIS = Object.freeze({
    'analyze-energy': 'adsorption-energy',
    'analyze-thermo': 'free-energy-path',
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
  const State = {
    projectId: '', projectName: '', analysisId: '',
    catalog: null, spec: null, view: null, projects: [],
    busy: false, dirty: false, busyToken: 0, intentGeneration: 0,
    bootstrapGeneration: 0, previewGeneration: 0,
    preferenceRevision: null, preferences: null,
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

  function requestFromControls() {
    const record = analysisRecord(); const source = plain(State.spec);
    const parameters = Array.isArray(record && record.parameters) ? record.parameters : [];
    const request = {
      schema: 'vcstudio.analysis-spec/v1',
      analysis_id: State.analysisId,
      project_id: State.projectId,
      data_mode: $('aw-data-mode') && $('aw-data-mode').value || source.data_mode || 'stable',
      near_degenerate_eV: Number($('aw-deadband') && $('aw-deadband').value),
      precision: Number($('aw-precision') && $('aw-precision').value),
      missing_policy: $('aw-missing-policy') && $('aw-missing-policy').value || 'show_missing',
      view_id: source.view_id || null,
    };
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
    ['aw-projects', 'aw-baseline'].forEach(id => { if ($(id)) $(id).disabled = !comparison || State.busy; });
    ['aw-sort-key', 'aw-sort-direction'].forEach(id => { if ($(id)) $(id).disabled = !supportsSort || State.busy; });
    if ($('aw-deadband')) $('aw-deadband').disabled = !parameters.includes('near_degenerate_eV') || State.busy;
    const supportsConditions = State.analysisId === 'free-energy-path' && parameters.includes('conditions');
    if ($('aw-condition-fields')) $('aw-condition-fields').hidden = !supportsConditions;
    ['aw-temperature', 'aw-pressure', 'aw-ph', 'aw-potential', 'aw-coverage'].forEach(id => {
      if ($(id)) $(id).disabled = !supportsConditions || State.busy;
    });
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

  function renderSensitivity(view) {
    const box = $('aw-sensitivity-results'); if (!box) return; box.innerHTML = '';
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
        const detail = document.createElement('code'); detail.textContent = [record.functional, record.dispersion, record.encut_eV != null ? `ENCUT ${record.encut_eV}` : '', record.kpoints_scheme].filter(Boolean).join(' · ') || (record.issues || record.warnings || []).join('；') || tr('analysis.method_projection.empty', '没有完整方法投影');
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
          : denominator.resolved_targets != null ? denominator.resolved_targets : denominator.input_steps;
    setText('aw-status-input', inputDenominator, 0);
    setText('aw-status-visible', denominator.visible_rows != null ? denominator.visible_rows : denominator.observed_numeric_cells, 0);
    setText('aw-status-hash', view.data_fingerprint ? String(view.data_fingerprint).slice(0, 12) : '—');
    if (!box) return;
    if (State.analysisId === 'adsorption-energy') renderAdsorptionTable(view, box);
    else if (State.analysisId === 'free-energy-path') renderFreeEnergyView(view, box);
    else if (State.analysisId === 'multi-project-comparison') renderComparisonTable(view, box);
    else if (State.analysisId === 'neb-path') renderNebView(view, box);
    else if (State.analysisId === 'convergence-scan') renderConvergenceView(view, box);
    else if (State.analysisId === 'aimd-diagnostics') renderAimdView(view, box);
    else renderServerResults(view, box);
  }

  function renderAll() {
    renderRegistry(); renderTemplates(); renderControls(); renderView();
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

  async function loadBootstrap(analysisId) {
    const project = currentProject(); const id = projectIdentity(project);
    if (!id) { showAlert(tr('analysis.bootstrap.project_missing', '当前项目尚未加载。请先从全局项目栏选择项目。'));
      operation(tr('analysis.bootstrap.waiting_context', '等待项目上下文。'), 'bad'); return false; }
    const intent = ++State.intentGeneration; const generation = ++State.bootstrapGeneration;
    State.previewGeneration += 1; const busyToken = beginBusy();
    State.projectId = id;
    State.analysisId = safeId(analysisId) || 'adsorption-energy'; showAlert('');
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
      const preferences = plain(result.preferences); State.preferences = clone(preferences.preferences || preferences);
      State.preferenceRevision = Number.isInteger(result.preference_revision) ? result.preference_revision : Number.isInteger(preferences.revision) ? preferences.revision : null;
      State.dirty = false; operation(tr('analysis.bootstrap.loaded', '分析工作台已加载。'), 'ok');
      renderAll(); return true;
    } catch (error) {
      if (intent !== State.intentGeneration || generation !== State.bootstrapGeneration) return false;
      State.spec = null; State.view = null; showAlert(error && error.message || String(error));
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
    document.addEventListener('vcs:route', event => {
      if (event.detail && event.detail.page === 'analysis-workbench') enterWorkbench(event.detail);
    });
    document.addEventListener('vcs:page', event => {
      if (!VCS.workspace && event.detail && event.detail.page === 'analysis-workbench') {
        setTimeout(() => enterWorkbench(event.detail), 0);
      }
    });
    document.addEventListener('vcs:workspace-project', () => {
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
      configure({ catalog = null, preferences = null, analysisId = '', busy = false } = {}) {
        State.catalog = clone(catalog);
        State.preferences = clone(preferences) || {};
        State.analysisId = String(analysisId || '');
        State.busy = busy === true;
      },
      snapshot() {
        return {
          analysis_id: State.analysisId,
          busy: State.busy,
          catalog: clone(State.catalog),
        };
      },
    });
  }

  window.AnalysisWorkbench = { refresh: previewCurrent, open: analysisId => loadBootstrap(analysisId) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true });
  else wire();
})();
