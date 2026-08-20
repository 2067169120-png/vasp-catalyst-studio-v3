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
  const State = {
    projectId: '', projectName: '', analysisId: '',
    catalog: null, spec: null, view: null, projects: [],
    busy: false, dirty: false, busyToken: 0, intentGeneration: 0,
    bootstrapGeneration: 0, previewGeneration: 0,
    preferenceRevision: null, preferences: null,
    kineticsPreviewSha: '', kineticsSelectionRevision: null,
    kineticsSelectedExportSha: '', kineticsAuditPreviewSha: '',
    kineticsUploadedResult: null,
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
    renderProjects(); renderSensitivityControls();
    const comparison = State.analysisId === 'multi-project-comparison';
    if ($('aw-data-mode')) $('aw-data-mode').disabled =
      !Array.isArray(record && record.data_modes) || record.data_modes.length <= 1 || State.busy;
    ['aw-projects', 'aw-baseline'].forEach(id => { if ($(id)) $(id).disabled = !comparison || State.busy; });
    ['aw-sort-key', 'aw-sort-direction'].forEach(id => { if ($(id)) $(id).disabled = !supportsSort || State.busy; });
    if ($('aw-deadband')) $('aw-deadband').disabled = !parameters.includes('near_degenerate_eV') || State.busy;
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

  function renderFreeEnergyView(view, box) {
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
        tr('analysis.kinetic.step', '步骤'), point.drc,
        record => record.step_id, units.drc);
      appendKineticTable(grid, tr('analysis.kinetic.dsc', '选择性控制度 DSC'),
        tr('analysis.kinetic.step', '步骤'), point.dsc,
        record => record.step_id, units.dsc);
      appendKineticTable(grid, tr('analysis.kinetic.reaction_order', '反应级数'),
        tr('analysis.table.species', '物种'), point.reaction_order,
        record => record.species_id, units.reaction_order);
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
    if (preview) preview.disabled = !active || State.busy || !State.projectId;
    if (confirm) confirm.disabled = !active || State.busy || !State.kineticsPreviewSha;
    if (auditPreview) auditPreview.disabled = !active || State.busy || !State.projectId;
    if (auditConfirm) auditConfirm.disabled = !active || State.busy || !State.kineticsAuditPreviewSha;
    if (resultSelect) resultSelect.disabled = !active || State.busy || !State.kineticsUploadedResult;
    if (file) file.disabled = !active || State.busy || !State.projectId;
  }

  function renderAll() {
    renderRegistry(); renderTemplates(); renderControls(); renderView(); renderKineticsActions();
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
      if (result.schema !== 'vcstudio.catmap-export-preview/v2' ||
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
      State.kineticsUploadedResult = {
        resultSha: String(result.result_sha256 || ''),
        exportSha: String(result.confirmed_export_sha256 || ''),
        latestSha: String(result.latest_result_sha256 || ''),
        revision: Number(result.selection_revision),
      };
      if (!/^[a-f0-9]{64}$/.test(State.kineticsUploadedResult.resultSha) ||
          !/^[a-f0-9]{64}$/.test(State.kineticsUploadedResult.exportSha) ||
          !Number.isInteger(State.kineticsUploadedResult.revision)) {
        throw new Error(tr('analysis.kinetic.upload_receipt_invalid',
          '服务端上传回执无效。'));
      }
      kineticsOperation(tr('analysis.kinetic.imported',
        '结果已校验并上传，但尚未选择为当前 diagnostic 结果。'), 'ok');
      renderAll();
    } catch (error) {
      if (intent === State.intentGeneration) {
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
      if (VCS.workspace && typeof VCS.workspace.navigateRoute === 'function' && record.route) {
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
      renderKineticDashboard,
      renderSensitivity,
      previewKineticsExport,
      configure({ catalog = null, preferences = null, analysisId = '',
        projectId = '', busy = false } = {}) {
        State.catalog = clone(catalog);
        State.preferences = clone(preferences) || {};
        State.analysisId = String(analysisId || '');
        State.projectId = String(projectId || '');
        State.busy = busy === true;
      },
      snapshot() {
        return {
          analysis_id: State.analysisId,
          project_id: State.projectId,
          busy: State.busy,
          kinetics_preview_sha256: State.kineticsPreviewSha,
          catalog: clone(State.catalog),
        };
      },
    });
  }

  window.AnalysisWorkbench = { refresh: previewCurrent, open: analysisId => loadBootstrap(analysisId) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true });
  else wire();
})();
