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
  function projectIdentity(project) {
    return safeId(project && (project.project_id || project.id || project.project_uuid));
  }
  function currentProject() {
    if (window.Project && typeof window.Project.current === 'function') {
      const project = window.Project.current(); if (project && project.path) return project;
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
      button.disabled = State.busy;
      button.dataset.favorite = favorites.has(record.id) ? 'true' : 'false';
      button.setAttribute('aria-current', record.id === State.analysisId ? 'true' : 'false');
      const label = String(record.label_zh || record.id || '分析');
      button.setAttribute('aria-label', label + (favorites.has(record.id) ? '，已收藏' : ''));
      const title = document.createElement('b');
      title.textContent = (favorites.has(record.id) ? '★ ' : '') + label;
      const desc = document.createElement('small');
      desc.id = `aw-analysis-desc-${String(record.id || '').replace(/[^A-Za-z0-9_-]/g, '-')}`;
      desc.textContent = String(record.description_zh || '');
      button.setAttribute('aria-describedby', desc.id);
      button.append(title, desc); item.appendChild(button); list.appendChild(item);
    });
    list.setAttribute('aria-busy', State.busy || !State.catalog ? 'true' : 'false');
  }

  function renderTemplates() {
    const list = $('aw-template-list'); if (!list) return; list.innerHTML = '';
    const builtins = plain(State.catalog).view_templates || [];
    const user = plain(State.preferences).templates || [];
    const templates = [...builtins, ...user];
    const compatible = templates.filter(item => String(item.analysis_id || '') === State.analysisId);
    if (!compatible.length) { const item = document.createElement('li'); item.textContent = '当前分析没有视图模板。'; list.appendChild(item); return; }
    compatible.forEach(template => {
      const item = document.createElement('li'); const button = document.createElement('button');
      button.type = 'button'; button.dataset.templateId = safeId(template.id);
      button.disabled = State.busy;
      const title = document.createElement('b'); title.textContent = String(template.label_zh || template.name || template.id);
      const desc = document.createElement('small'); desc.textContent = template.schema === 'vcstudio.analysis-view-template/v1' ? '内置模板' : '用户模板';
      button.append(title, desc); item.appendChild(button); list.appendChild(item);
    });
  }

  function replaceOptions(select, values, selected) {
    if (!select) return; const current = String(selected == null ? '' : selected); select.innerHTML = '';
    values.forEach(value => {
      const record = typeof value === 'string' ? { id: value, label: value } : plain(value);
      const option = document.createElement('option'); option.value = String(record.id || '');
      option.textContent = String(record.label_zh || record.label_en || record.label || record.id || '');
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
    baseline.innerHTML = '<option value="">不设基准</option>';
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
    })), spec.data_mode);
    if ($('aw-deadband')) $('aw-deadband').value = String(spec.near_degenerate_eV == null ? 0.15 : spec.near_degenerate_eV);
    if ($('aw-precision')) $('aw-precision').value = String(spec.precision == null ? 4 : spec.precision);
    if ($('aw-missing-policy')) $('aw-missing-policy').value = String(spec.missing_policy || 'show_missing');
    const sortKeys = supportsSort && Array.isArray(record && record.sort_keys) ? record.sort_keys : [];
    replaceOptions($('aw-sort-key'), sortKeys.map(id => ({ id, label: id })), plain(spec.sort).key);
    if ($('aw-sort-direction')) $('aw-sort-direction').value = String(plain(spec.sort).direction || 'asc');
    renderProjects(); renderSensitivityControls();
    const comparison = State.analysisId === 'multi-project-comparison';
    ['aw-projects', 'aw-baseline'].forEach(id => { if ($(id)) $(id).disabled = !comparison || State.busy; });
    ['aw-sort-key', 'aw-sort-direction'].forEach(id => { if ($(id)) $(id).disabled = !supportsSort || State.busy; });
    if ($('aw-deadband')) $('aw-deadband').disabled = !parameters.includes('near_degenerate_eV') || State.busy;
    document.querySelectorAll('#aw-sensitivity input').forEach(input => {
      input.disabled = !(record && record.supports_sensitivity === true) || State.busy;
    });
  }

  function renderDenominator(view) {
    const box = $('aw-denominator'); if (!box) return; box.innerHTML = '';
    const source = plain(view && view.denominator);
    const labels = {
      input_configurations: '输入构型', numeric_configurations: '数值构型',
      missing_configurations: '缺失构型', species_with_numeric_results: '有效物种',
      visible_rows: '可见行', near_degenerate_groups: '近简并组',
      selected_projects: '所选项目', ready_projects: '有效项目', blocked_projects: '阻断项目',
      species_columns: '物种列', possible_numeric_cells: '应有数值',
      observed_numeric_cells: '已有数值', missing_numeric_cells: '缺失数值',
      requested_paths: '请求路径', available_paths: '可用路径',
      input_steps: '输入台阶', numeric_steps: '数值台阶', missing_steps: '缺失台阶',
      input_transitions: '输入转化', valid_transitions: '有效转化',
    };
    Object.entries(source).forEach(([key, value]) => {
      const item = document.createElement('span'); const label = document.createElement('small');
      const number = document.createElement('b'); label.textContent = labels[key] || key; number.textContent = String(value);
      item.append(label, number); box.appendChild(item);
    });
    if (!box.children.length) box.innerHTML = '<span><small>分母</small><b>未知</b></span>';
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
        row.relative_to_minimum_display, row.near_degenerate ? '近简并' : row.is_minimum ? '最低' : '',
        row.method_status, row.state, row.note],
    }));
    if (!rows.length) { box.innerHTML = '<div class="aw-empty">当前范围没有可显示构型；请查看阻断项与分母。</div>'; return; }
    box.innerHTML = ''; box.appendChild(tableElement([
      { label: '物种' }, { label: '构型' }, { label: 'E_ads / eV', numeric: true },
      { label: 'ΔΔE / eV', numeric: true }, { label: '稳定性' }, { label: '方法' },
      { label: '作业状态' }, { label: '说明' },
    ], rows));
  }

  function renderComparisonTable(view, box) {
    const matrix = plain(view.matrix); const projectIds = matrix.project_ids || [];
    const projects = new Map((view.projects || []).map(project => [String(project.project_id || ''), project]));
    const headers = [{ label: '项目' }, ...(matrix.cols || []).map(label => ({ label, numeric: true }))];
    const rows = projectIds.map((projectId, index) => ({
      cells: [plain(projects.get(projectId)).display_name || plain(projects.get(projectId)).name || projectId,
        ...((matrix.display_values || [])[index] || [])],
    }));
    if (!rows.length) { box.innerHTML = '<div class="aw-empty">没有通过比较门禁的项目；请查看阻断项。</div>'; return; }
    box.innerHTML = ''; box.appendChild(tableElement(headers, rows));
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
    summary.setAttribute('aria-label', '服务器确定的自由能路径摘要');
    const heading = document.createElement('h3'); heading.textContent = '自由能路径摘要';
    const metrics = document.createElement('dl'); metrics.className = 'aw-free-energy-metrics';
    const pdsText = pds.index === null || pds.index === undefined
      ? null
      : `#${String(pds.index)} · ${String(pds.from_label || '—')} → ${String(pds.to_label || '—')}`;
    appendFreeEnergyMetric(metrics, '路径可用性', view.available === true ? '可用' : '阻断');
    appendFreeEnergyMetric(metrics, '科学状态', view.scientific_status);
    appendFreeEnergyMetric(metrics, '势决定步（PDS）', pdsText);
    appendFreeEnergyMetric(metrics, 'U_L', view.u_l_display, 'V');
    appendFreeEnergyMetric(metrics, 'μLi', view.mu_li_display, 'eV');
    appendFreeEnergyMetric(metrics, '热校正状态', thermo.status);
    appendFreeEnergyMetric(metrics, '温度', thermo.temperature_display, 'K');
    appendFreeEnergyMetric(metrics, '热校正指纹', thermo.correction_fingerprint);
    appendFreeEnergyMetric(metrics, '方法状态', view.method_status || method.status);
    appendFreeEnergyMetric(metrics, '方法受管', Object.prototype.hasOwnProperty.call(method, 'managed') ? (method.managed === true ? '是' : '否') : null);
    appendFreeEnergyMetric(metrics, '参考电极', view.reference);
    appendFreeEnergyMetric(metrics, '反应路径标识', view.reaction_path_id);
    summary.append(heading, metrics); box.appendChild(summary);

    const evidence = document.createElement('div'); evidence.className = 'aw-free-energy-evidence-grid';
    appendFreeEnergyMessages(evidence, '缺失数据', view.missing, '服务器未报告缺失数据。');
    appendFreeEnergyMessages(evidence, '门禁阻断', view.blocking, '当前无阻断项。');
    appendFreeEnergyMessages(evidence, '方法错误', method.errors, '服务器未报告方法错误。');
    appendFreeEnergyMessages(evidence, '方法告警', method.warnings, '服务器未报告方法告警。');
    box.appendChild(evidence);

    if (!rows.length) {
      const empty = document.createElement('div'); empty.className = 'aw-empty';
      empty.textContent = String(view.reason || '服务器未返回可展示的自由能台阶；请查看缺失数据与门禁阻断。');
      box.appendChild(empty); return;
    }

    const table = tableElement([
      { label: '台阶序号', numeric: true }, { label: '状态' }, { label: '补充标注' },
      { label: 'G / eV', numeric: true },
    ], rows.map(row => ({
      cells: [row.step_index, row.label, row.sub_label, row.G_display],
    })));
    const caption = document.createElement('caption');
    caption.textContent = '服务器确定的有序自由能台阶（未在浏览器中重排或重算）';
    table.prepend(caption); box.appendChild(table);
  }

  function renderSensitivity(view) {
    const box = $('aw-sensitivity-results'); if (!box) return; box.innerHTML = '';
    const sensitivity = plain(view && view.sensitivity); const points = sensitivity.points || [];
    if (!points.length) { box.innerHTML = '<div class="aw-empty">当前分析没有敏感性结果。</div>'; return; }
    points.forEach(point => {
      const card = document.createElement('div'); card.className = 'aw-sensitivity-point';
      const sets = (point.lowest_energy_sets || []).map(item =>
        `${item.species}: ${(item.within_deadband_project_ids || []).join(', ') || '无数值'} (n=${item.observed_denominator || 0})`);
      card.textContent = `deadband ${Number(point.deadband_eV).toFixed(2)} eV · ${sets.join('；')}`; box.appendChild(card);
    });
  }

  function renderInspector(view) {
    const methods = $('aw-method-matrix'); if (methods) { methods.innerHTML = '';
      (view.method_matrix || []).forEach(record => {
        const card = document.createElement('div'); card.className = 'aw-method';
        const title = document.createElement('b'); title.textContent = `${record.name || record.species || record.configuration_id || '记录'} · ${record.status || 'unknown'}`;
        const detail = document.createElement('code'); detail.textContent = [record.functional, record.dispersion, record.encut_eV != null ? `ENCUT ${record.encut_eV}` : '', record.kpoints_scheme].filter(Boolean).join(' · ') || (record.issues || record.warnings || []).join('；') || '没有完整方法投影';
        card.append(title, detail); methods.appendChild(card);
      });
      if (!methods.children.length) methods.innerHTML = '<div class="aw-empty">没有方法矩阵记录。</div>';
    }
    const gate = plain(view.comparison_gate); const blocking = view.blocking || gate.blocking || [];
    const warnings = view.warnings || gate.warnings || [];
    [['aw-blocking', blocking, '当前无阻断项'], ['aw-warnings', warnings, '当前无告警']].forEach(([id, values, empty]) => {
      const list = $(id); if (!list) return; list.innerHTML = '';
      (values || []).forEach(value => { const item = document.createElement('li'); item.textContent = String(value); list.appendChild(item); });
      if (!list.children.length) { const item = document.createElement('li'); item.textContent = empty; list.appendChild(item); }
    });
  }

  function renderView() {
    const view = plain(State.view); const box = $('aw-table-scroll');
    renderDenominator(view); renderSensitivity(view); renderInspector(view);
    const record = analysisRecord(); setText('aw-status-analysis', record && record.label_zh || State.analysisId, '未读取');
    setText('aw-status-science', view.scientific_status, '未知');
    const denominator = plain(view.denominator);
    const inputDenominator = denominator.input_configurations != null ? denominator.input_configurations
      : denominator.selected_projects != null ? denominator.selected_projects
        : denominator.requested_paths != null ? denominator.requested_paths : denominator.input_steps;
    setText('aw-status-input', inputDenominator, 0);
    setText('aw-status-visible', denominator.visible_rows != null ? denominator.visible_rows : denominator.observed_numeric_cells, 0);
    setText('aw-status-hash', view.data_fingerprint ? String(view.data_fingerprint).slice(0, 12) : '—');
    if (!box) return;
    if (State.analysisId === 'adsorption-energy') renderAdsorptionTable(view, box);
    else if (State.analysisId === 'free-energy-path') renderFreeEnergyView(view, box);
    else if (State.analysisId === 'multi-project-comparison') renderComparisonTable(view, box);
    else box.innerHTML = `<div class="aw-empty">${esc(view.error || '该注册分析已建立能力入口；请选择实际作业后使用对应解析器。')}</div>`;
  }

  function renderAll() {
    renderRegistry(); renderTemplates(); renderControls(); renderView();
    setText('aw-project-name', State.projectName ? `${State.projectName} · ${State.projectId}` : '当前项目不可用');
    const refresh = $('aw-refresh'); if (refresh) refresh.disabled = State.busy || !State.spec;
    const save = $('aw-save-view'); if (save) save.disabled = State.busy || !State.spec || State.preferenceRevision == null;
    const favorite = $('aw-favorite'); if (favorite) {
      const active = (plain(State.preferences).favorites || []).includes(State.analysisId);
      favorite.disabled = State.busy || State.preferenceRevision == null;
      favorite.setAttribute('aria-pressed', active ? 'true' : 'false'); favorite.textContent = active ? '取消收藏' : '收藏分析';
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
    showAlert(''); operation('正在由服务端重建分析视图…', 'busy'); renderAll();
    try {
      const result = await VCS.call('analysis_workbench_preview', projectId, request);
      if (intent !== State.intentGeneration || generation !== State.previewGeneration ||
          analysisId !== State.analysisId || !sameProject(projectId)) return false;
      if (!result || result.ok === false || result.error) throw new Error(result && result.error || '分析接口没有返回有效结果');
      if (safeId(result.project_id) !== projectId) throw new Error('分析结果项目身份与当前项目不一致');
      if (JSON.stringify(request) !== requestFingerprint) return false;
      State.spec = clone(result.spec || plain(result.view).spec || request); State.view = clone(result.view || result.analysis_view || {});
      State.dirty = false; operation('分析视图已由服务器数据与门禁重新生成。', 'ok'); renderAll(); return true;
    } catch (error) {
      if (intent !== State.intentGeneration || generation !== State.previewGeneration) return false;
      showAlert(error && error.message || String(error)); operation('分析失败；保留上一份已验证视图。', 'bad'); return false;
    } finally { endBusy(busyToken); }
  }

  async function loadBootstrap(analysisId) {
    const project = currentProject(); const id = projectIdentity(project);
    if (!id) { showAlert('当前项目尚未加载。请先从全局项目栏选择项目。'); operation('等待项目上下文。', 'bad'); return false; }
    const intent = ++State.intentGeneration; const generation = ++State.bootstrapGeneration;
    State.previewGeneration += 1; const busyToken = beginBusy();
    State.projectId = id;
    State.analysisId = safeId(analysisId) || 'adsorption-energy'; showAlert(''); operation('正在读取分析注册表与项目证据…', 'busy'); renderAll();
    try {
      const result = await VCS.call('analysis_workbench_bootstrap', id, State.analysisId);
      if (intent !== State.intentGeneration || generation !== State.bootstrapGeneration ||
          !sameProject(id)) return false;
      if (!result || result.ok === false || result.error) throw new Error(result && result.error || '分析工作台 bootstrap 失败');
      if (safeId(result.project_id) !== id) throw new Error('bootstrap 项目身份不一致');
      State.projectName = String(result.project_name || plain(result.project).name || project.name || '');
      State.catalog = clone(result.catalog || {}); State.projects = clone(result.projects || []);
      State.spec = clone(result.default_spec || result.spec || {});
      State.view = clone(result.view || result.analysis_view || {});
      State.analysisId = safeId(State.spec.analysis_id) || State.analysisId;
      const preferences = plain(result.preferences); State.preferences = clone(preferences.preferences || preferences);
      State.preferenceRevision = Number.isInteger(result.preference_revision) ? result.preference_revision : Number.isInteger(preferences.revision) ? preferences.revision : null;
      State.dirty = false; operation('分析工作台已加载。', 'ok'); renderAll(); return true;
    } catch (error) {
      if (intent !== State.intentGeneration || generation !== State.bootstrapGeneration) return false;
      State.spec = null; State.view = null; showAlert(error && error.message || String(error)); operation('分析工作台加载失败。', 'bad'); renderAll(); return false;
    } finally { endBusy(busyToken); }
  }

  async function applyTemplate(templateId) {
    const templates = [...(plain(State.catalog).view_templates || []), ...(plain(State.preferences).templates || [])];
    const template = templates.find(item => safeId(item.id) === safeId(templateId) && item.analysis_id === State.analysisId);
    if (!template || State.busy) return; const values = clone(template.spec || template.values || {});
    State.spec = Object.assign({}, State.spec, values, { view_id: safeId(template.id) || null }); renderControls(); await previewCurrent();
  }

  async function saveView() {
    if (State.preferenceRevision == null || State.busy) { VCS.toast('分析偏好服务尚未可用', 'fail'); return; }
    const name = window.prompt('视图模板名称', `${analysisRecord() && analysisRecord().label_zh || '分析'}视图`);
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
      if (!result || result.ok === false) throw new Error(result && result.error || '保存视图失败');
      State.preferenceRevision = result.revision; State.preferences = clone(result.preferences || {}); renderAll(); VCS.toast('分析视图已保存');
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
      if (!result || result.ok === false) throw new Error(result && result.error || '更新收藏失败');
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
  }

  window.AnalysisWorkbench = { refresh: previewCurrent, open: analysisId => loadBootstrap(analysisId) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true });
  else wire();
})();
