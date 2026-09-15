// Research Explorer — one cross-project, server-finalized surface on Home/Project.
'use strict';

(function () {
  const VCS = window.VCS;
  if (!VCS) return;

  const State = {
    result: null,
    provenance: null,
    savedViews: null,
    request: {
      schema: 'vcstudio.research-query/v1',
      filters: { method_compatible: true },
      sort: { key: 'project', direction: 'asc' },
      axes: { x: 'energy_eV', y: 'barrier_eV' },
      limit: 50,
    },
    busy: false,
    generation: 0,
    currentProjectId: '',
    operation: '',
  };

  function t(key, zh, en, params = {}) {
    const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
    const fallback = english ? en : zh;
    return typeof VCS.t === 'function' ? VCS.t(key, params, fallback) : fallback;
  }

  function esc(value) {
    return VCS.esc(value == null ? '' : String(value));
  }

  function safePercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '0';
    return String(Math.max(0, Math.min(100, number)));
  }

  function hosts() {
    return Array.from(document.querySelectorAll('[data-research-explorer-host]'));
  }

  function statusLabel(status) {
    return {
      ready: t('research.status.ready', '索引新鲜', 'Index fresh'),
      partial: t('research.status.partial', '索引不完整', 'Index partial'),
      stale: t('research.status.stale', '索引已陈旧', 'Index stale'),
      unavailable: t('research.status.unavailable', '索引不可用', 'Index unavailable'),
    }[status] || t('research.status.loading', '正在读取', 'Loading');
  }

  function listText(value) {
    return Array.isArray(value) ? value.join(', ') : '';
  }

  function optionLabel(value, zh, en) {
    switch (value) {
      case 'project': return t('research.option.project', zh, en);
      case 'job': return t('research.option.job', zh, en);
      case 'formula': return t('research.option.formula', zh, en);
      case 'facet': return t('research.option.facet', zh, en);
      case 'adsorbate': return t('research.option.adsorbate', zh, en);
      case 'task': return t('research.option.task', zh, en);
      case 'state': return t('research.option.state', zh, en);
      case 'method': return t('research.option.method', zh, en);
      case 'evidence': return t('research.option.evidence', zh, en);
      case 'energy_eV': return t('research.option.energy_eV', zh, en);
      case 'barrier_eV': return t('research.option.barrier_eV', zh, en);
      case 'asc': return t('research.option.asc', zh, en);
      case 'desc': return t('research.option.desc', zh, en);
      default: return (VCS.i18n && VCS.i18n.lang === 'en') ? en : zh;
    }
  }

  function option(value, selected, zh, en = zh) {
    return `<option value="${esc(value)}"${value === selected ? ' selected' : ''}>` +
      `${esc(optionLabel(value, zh, en))}</option>`;
  }

  function controlMarkup(request) {
    const filters = request.filters || {};
    const sort = request.sort || { key: 'project', direction: 'asc' };
    const axes = request.axes || { x: 'energy_eV', y: 'barrier_eV' };
    return `
      <div class="rex-controls" role="search" aria-label="${esc(t(
        'research.filters', '研究筛选', 'Research filters'))}">
        <label class="rex-field"><span>${esc(t('research.elements', '元素', 'Elements'))}</span>
          <input class="ipt" data-rex-field="elements" value="${esc(listText(filters.elements))}"
            placeholder="Pt, Ni, S" autocomplete="off"></label>
        <label class="rex-field"><span>${esc(t('research.formula', '化学式', 'Formula'))}</span>
          <input class="ipt" data-rex-field="formula" value="${esc(filters.formula || '')}"
            placeholder="Pt4S" autocomplete="off"></label>
        <label class="rex-field"><span>${esc(t('research.facet', '晶面 facet', 'Facet'))}</span>
          <input class="ipt" data-rex-field="facet" value="${esc(filters.facet || '')}"
            placeholder="111" autocomplete="off"></label>
        <label class="rex-field"><span>${esc(t('research.adsorbate', '吸附物', 'Adsorbate'))}</span>
          <input class="ipt" data-rex-field="adsorbate" value="${esc(filters.adsorbate || '')}"
            placeholder="Li2S8" autocomplete="off"></label>
        <label class="rex-field"><span>${esc(t('research.task', '任务', 'Task'))}</span>
          <input class="ipt" data-rex-field="task_types" value="${esc(listText(filters.task_types))}"
            placeholder="static, neb" autocomplete="off"></label>
        <label class="rex-field"><span>${esc(t('research.state', '状态', 'State'))}</span>
          <input class="ipt" data-rex-field="states" value="${esc(listText(filters.states))}"
            placeholder="DONE" autocomplete="off"></label>
        <label class="rex-field"><span>${esc(t('research.evidence', '证据等级', 'Evidence level'))}</span>
          <input class="ipt" data-rex-field="evidence_levels"
            value="${esc(listText(filters.evidence_levels))}"
            placeholder="verified, observed" autocomplete="off"></label>
        <label class="rex-field"><span>${esc(t('research.energy_min', '能量下限 (eV)', 'Energy min (eV)'))}</span>
          <input class="ipt" inputmode="decimal" data-rex-field="energy_min_eV"
            value="${esc(filters.energy_min_eV == null ? '' : filters.energy_min_eV)}"></label>
        <label class="rex-field"><span>${esc(t('research.energy_max', '能量上限 (eV)', 'Energy max (eV)'))}</span>
          <input class="ipt" inputmode="decimal" data-rex-field="energy_max_eV"
            value="${esc(filters.energy_max_eV == null ? '' : filters.energy_max_eV)}"></label>
        <label class="rex-field"><span>${esc(t('research.barrier_min', '能垒下限 (eV)', 'Barrier min (eV)'))}</span>
          <input class="ipt" inputmode="decimal" data-rex-field="barrier_min_eV"
            value="${esc(filters.barrier_min_eV == null ? '' : filters.barrier_min_eV)}"></label>
        <label class="rex-field"><span>${esc(t('research.barrier_max', '能垒上限 (eV)', 'Barrier max (eV)'))}</span>
          <input class="ipt" inputmode="decimal" data-rex-field="barrier_max_eV"
            value="${esc(filters.barrier_max_eV == null ? '' : filters.barrier_max_eV)}"></label>
        <label class="rex-field"><span>${esc(t('research.sort', '排序', 'Sort'))}</span>
          <select class="ipt" data-rex-field="sort_key">
            ${option('project', sort.key, '项目', 'Project')}
            ${option('job', sort.key, '作业', 'Job')}
            ${option('formula', sort.key, '化学式', 'Formula')}
            ${option('facet', sort.key, '晶面', 'Facet')}
            ${option('adsorbate', sort.key, '吸附物', 'Adsorbate')}
            ${option('task', sort.key, '任务', 'Task')}
            ${option('state', sort.key, '状态', 'State')}
            ${option('method', sort.key, '方法', 'Method')}
            ${option('evidence', sort.key, '证据', 'Evidence')}
            ${option('energy_eV', sort.key, '能量', 'Energy')}
            ${option('barrier_eV', sort.key, '能垒', 'Barrier')}
          </select></label>
        <label class="rex-field"><span>${esc(t('research.direction', '方向', 'Direction'))}</span>
          <select class="ipt" data-rex-field="sort_direction">
            ${option('asc', sort.direction, '升序', 'Ascending')}
            ${option('desc', sort.direction, '降序', 'Descending')}
          </select></label>
        <label class="rex-field"><span>${esc(t('research.axis_x', 'X 轴', 'X axis'))}</span>
          <select class="ipt" data-rex-field="axis_x">
            ${option('energy_eV', axes.x, '能量 (eV)', 'Energy (eV)')}
            ${option('barrier_eV', axes.x, '能垒 (eV)', 'Barrier (eV)')}
          </select></label>
        <label class="rex-field"><span>${esc(t('research.axis_y', 'Y 轴', 'Y axis'))}</span>
          <select class="ipt" data-rex-field="axis_y">
            ${option('energy_eV', axes.y, '能量 (eV)', 'Energy (eV)')}
            ${option('barrier_eV', axes.y, '能垒 (eV)', 'Barrier (eV)')}
          </select></label>
        <label class="rex-method-toggle">
          <input type="checkbox" data-rex-field="method_compatible"${
            filters.method_compatible !== false ? ' checked' : ''}>
          <span>${esc(t('research.method_default', '默认只保留方法兼容 cohort',
            'Keep one method-compatible cohort by default'))}</span>
        </label>
        <div class="rex-control-actions">
          <button class="btn primary" type="button" data-rex-action="apply">${esc(t(
            'research.apply', '应用筛选', 'Apply filters'))}</button>
          <button class="btn" type="button" data-rex-action="current-project">${esc(t(
            'research.current_project', '仅当前项目', 'Current project only'))}</button>
          <button class="btn quiet" type="button" data-rex-action="clear">${esc(t(
            'research.clear', '清除', 'Clear'))}</button>
        </div>
      </div>`;
  }

  function methodMarkup(result) {
    const compatibility = result && result.method_compatibility;
    if (!compatibility) return '';
    const selected = compatibility.selected_method_fingerprint || t(
      'research.method_missing', '未验证方法指纹', 'Unverified method fingerprint');
    return `<span><b>${esc(t('research.method_cohort', '方法 cohort', 'Method cohort'))}：</b>` +
      `${esc(selected)}</span><span><b>${esc(t('research.samples', '样本', 'Samples'))}：</b>` +
      `${esc(compatibility.compatible_rows)} / ${esc(compatibility.input_rows)}</span>` +
      `<span>${esc(t('research.method_excluded', '方法不兼容排除 {count} 条',
        '{count} method-incompatible rows excluded', { count: compatibility.excluded_rows }))}</span>`;
  }

  function energyMarkup(result) {
    const compatibility = result && result.energy_compatibility;
    if (!compatibility) return '';
    const identity = compatibility.selected_energy_contract_id || t(
      'research.energy_contract_unavailable', '口径未证明一致', 'Contract compatibility unproven');
    return `<span><b>${esc(t('research.energy_contract', '能量口径', 'Energy contract'))}：</b>` +
      `${esc(identity)}</span><span><b>${esc(t('research.numeric_samples', '数值样本',
        'Numeric samples'))}：</b>${esc(compatibility.numeric_rows)} · ${esc(t(
        'research.unverified_contracts', '未验证口径 {count}', '{count} unverified contracts',
        { count: compatibility.unverified_contract_rows }))}</span>`;
  }

  function aggregationMessage(status) {
    if (status === 'unavailable_incompatible_energy_contract') return t(
      'research.energy_aggregate_unavailable',
      '能量 quantity / reference contract 不一致或无法证明，科学聚合不可用。',
      'Scientific aggregation is unavailable because energy quantity/reference contracts differ or are unproven.');
    if (status === 'unavailable_method_compatibility' ||
        status === 'blocked_mixed_or_unverified_methods') return t(
      'research.method_aggregate_unavailable',
      '方法或 engine cohort 未验证一致，科学聚合不可用。',
      'Scientific aggregation is unavailable because the method/engine cohort is not verified.');
    return t('research.aggregate_missing', '没有可聚合的数值。', 'No numeric values to aggregate.');
  }

  function tableRowsMarkup(result) {
    const table = result && result.table;
    const rows = table && Array.isArray(table.rows) ? table.rows : [];
    if (!rows.length) return `<tr><td colspan="10" class="rex-empty">${esc(t(
      'research.no_rows', '当前条件下没有可显示的完整索引行。',
      'No complete index rows match the current filters.'))}</td></tr>`;
    return rows.map(row => {
      const display = row.display || {};
      const quantityEvidence = Object.entries(row.quantity_evidence || {})
        .map(([quantity, status]) => `${quantity}:${status}`).join(' · ');
      return `<tr>
        <td><b>${esc(row.project_name)}</b><span class="rex-identity">${esc(row.project_id)}</span></td>
        <td>${esc(row.formula || '—')}</td><td>${esc(row.facet || '—')}</td>
        <td>${esc(row.adsorbate || '—')}</td><td>${esc(row.task_type || '—')}</td>
        <td>${esc(row.state || '—')}</td>
        <td><span class="rex-identity">${esc(row.method_fingerprint || '—')}</span></td>
        <td>${esc(row.evidence_level || '—')}<span class="rex-identity">${esc(
          row.provenance_status || '')}${quantityEvidence ? ` · ${esc(quantityEvidence)}` : ''}</span></td>
        <td class="num">${esc(display.energy_eV || '—')} eV<span class="rex-identity">${esc(
          row.energy_quantity || 'missing')} · ${esc(row.energy_contract_status || 'missing')}</span></td>
        <td class="num">${esc(display.barrier_eV || '—')} eV<br>
          <button class="btn quiet" type="button" data-rex-action="provenance"
            data-project-id="${esc(row.project_id)}" data-job-id="${esc(row.job_id)}"
            data-source-id="${esc(row.source_id)}">${esc(t(
              'research.provenance', '溯源', 'Provenance'))}</button></td>
      </tr>`;
    }).join('');
  }

  function histogramMarkup(result) {
    const histogram = result && result.histogram;
    if (!histogram || histogram.status !== 'ready') return `<div class="rex-empty">${esc(
      aggregationMessage(histogram && histogram.status))}</div>`;
    return `<div class="rex-chart-body">${histogram.bins.map(bin => `
      <div class="rex-hist-row"><span>${esc(bin.low_display)}–${esc(bin.high_display)}</span>
        <span class="rex-hist-track"><span class="rex-hist-bar" style="width:${esc(
          safePercent(bin.percent_of_peak))}%"></span></span><b>${esc(bin.count)}</b></div>`).join('')}
      <p class="rex-prov-note">${esc(t('research.hist_denominator',
        '样本 {count}；缺失 {missing}；单位 {unit}',
        'Samples {count}; missing {missing}; unit {unit}', {
          count: histogram.sample_count, missing: histogram.missing_count,
          unit: histogram.unit,
        }))}</p></div>`;
  }

  function scatterMarkup(result) {
    const scatter = result && result.scatter;
    if (!scatter || scatter.status !== 'ready') {
      return `<div class="rex-empty">${esc(scatter && scatter.reason ||
        aggregationMessage(scatter && scatter.status))}</div>`;
    }
    return `<div class="rex-scatter" role="img" aria-label="${esc(t(
      'research.scatter', '服务端定稿散点图', 'Server-finalized scatter plot'))}">
      ${scatter.points.map(point => `<button type="button" class="rex-point"
        style="left:${esc(safePercent(point.x_percent))}%;top:${esc(safePercent(point.top_percent))}%"
        title="${esc(point.label)}: ${esc(point.x_display)}, ${esc(point.y_display)}"
        data-rex-action="provenance" data-project-id="${esc(point.project_id)}"
        data-job-id="${esc(point.job_id)}" data-source-id="${esc(point.source_id)}"></button>`).join('')}
      </div><p class="rex-prov-note">${esc(t('research.scatter_denominator',
        '完整样本 {count}；缺失 {missing}', 'Complete samples {count}; missing {missing}', {
          count: scatter.sample_count, missing: scatter.missing_count,
        }))}</p>`;
  }

  function periodicMarkup(result) {
    const periodic = result && result.periodic_table;
    if (!periodic || periodic.status !== 'ready') return `<div class="rex-empty">${esc(
      aggregationMessage(periodic && periodic.status))}</div>`;
    return `<div class="rex-periodic-wrap" tabindex="0" role="region" aria-label="${esc(t(
      'research.periodic', '元素周期表聚合', 'Periodic-table aggregation'))}">
      <div class="rex-periodic">${periodic.cells.map(cell => `<div class="rex-element"
        style="grid-column:${esc(cell.group)};grid-row:${esc(cell.period)}"
        title="${esc(t('research.element_samples', '{element}：{count} 个样本',
          '{element}: {count} samples', { element: cell.element, count: cell.sample_count }))}">
        <b>${esc(cell.element)}</b><span>${esc(cell.sample_count)}</span></div>`).join('')}</div>
      </div><p class="rex-prov-note">${esc(t('research.periodic_denominator',
        '元素 {elements}；无元素信息的行 {missing}',
        '{elements} elements; {missing} rows lack element identity', {
          elements: periodic.element_count, missing: periodic.missing_element_rows,
        }))}</p>`;
  }

  function provenanceMarkup(graph) {
    if (!graph) return `<p class="rex-prov-note">${esc(t(
      'research.prov_prompt', '点选表格行或散点查看 live provenance。',
      'Select a table row or point to inspect live provenance.'))}</p>`;
    if (graph.ok !== true) return `<p class="rex-alert">${esc(graph.error || statusLabel(graph.status))}</p>`;
    return `<p class="rex-prov-note">${esc(t('research.prov_layers',
      'data provenance 与 logical provenance 分层显示；frozen report graph 保持独立。',
      'Data and logical provenance are layered; the frozen report graph remains separate.'))}</p>
      <div class="rex-provenance-grid">${(graph.nodes || []).map(node => `
        <div class="rex-prov-node" data-layer="${esc(node.layer)}">
          <b>${esc(node.type)}</b><span>${esc(node.label)}</span>
          <span>${esc(node.origin_status)} · ${esc(node.layer)}</span>
        </div>`).join('')}</div>
      <p class="rex-prov-note">${esc(t('research.prov_denominator',
        '节点 {nodes}；边 {edges}；缺失链接 {missing}',
        'Nodes {nodes}; edges {edges}; missing links {missing}', {
          nodes: graph.denominator && graph.denominator.nodes,
          edges: graph.denominator && graph.denominator.edges,
          missing: graph.denominator && graph.denominator.missing,
        }))}</p>`;
  }

  function savedViewsMarkup() {
    const authority = State.savedViews || {};
    const views = Array.isArray(authority.views) ? authority.views : [];
    return `<option value="">${esc(t('research.saved_choose', '选择保存的视图…',
      'Choose a saved view…'))}</option>` + views.map(view =>
      `<option value="${esc(view.id)}">${esc(view.name)}</option>`).join('');
  }

  function shellMarkup(scope, result) {
    const freshness = result && result.freshness || { status: 'unavailable' };
    const table = result && result.table || { sample_count: 0, visible_count: 0 };
    const error = result && result.ok === false ? result.error : '';
    return `<div class="rex-shell" data-rex-scope="${esc(scope)}">
      <div class="rex-head"><div><span class="rex-kicker">Research Explorer</span>
        <h2 id="rex-${esc(scope)}-title">${esc(t(
          'research.title', '跨项目研究浏览器', 'Cross-project Research Explorer'))}</h2>
        <p>${esc(t('research.subtitle',
          '可重建索引只读派生自 registry / project.yaml / job.yaml / manifest / validation；它不是事实源。',
          'A rebuildable read-only index is derived from registry, project.yaml, job.yaml, manifests and validation; it is not a fact source.'))}</p></div>
        <div class="rex-head-actions"><span class="rex-freshness" data-status="${esc(
          freshness.status)}">${esc(statusLabel(freshness.status))}</span>
          <button class="btn" type="button" data-rex-action="rebuild"${State.busy ? ' disabled' : ''}>${esc(t(
            'research.rebuild', '重建索引', 'Rebuild index'))}</button></div></div>
      ${error ? `<p class="rex-alert" role="alert">${esc(error)}</p>` : ''}
      ${controlMarkup(State.request)}
      <div class="rex-meta" aria-live="polite">${methodMarkup(result)}${energyMarkup(result)}
        <span><b>${esc(t('research.index_freshness', '索引 freshness', 'Index freshness'))}：</b>` +
          `${esc(freshness.age_seconds == null ? '—' : freshness.age_seconds)} s</span>
        <span><b>${esc(t('research.registry_state', '注册表状态', 'Registry state'))}：</b>` +
          `${esc(freshness.registry_state || 'unknown')}</span>
        <span><b>${esc(t('research.denominator', '分母', 'Denominator'))}：</b>` +
          `${esc(table.visible_count)} / ${esc(table.sample_count)} · ` +
          `${esc(freshness.indexed_projects || 0)} ${esc(t('research.projects', '项目', 'projects'))}</span>
        <span role="status">${esc(State.operation)}</span></div>
      <div class="rex-main"><div class="rex-panel"><div class="rex-panel-head"><h3>${esc(t(
        'research.table', '研究表格', 'Research table'))}</h3><span>${esc(t(
          'research.server_finalized', 'server-finalized DTO', 'server-finalized DTO'))}</span></div>
        <div class="rex-table-wrap" tabindex="0" role="region" aria-label="${esc(t(
          'research.table_region', '跨项目研究结果', 'Cross-project research results'))}">
          <table class="rex-table"><thead><tr>
            <th>${esc(t('research.project', '项目', 'Project'))}</th>
            <th>${esc(t('research.formula', '化学式', 'Formula'))}</th>
            <th>${esc(t('research.facet', '晶面', 'Facet'))}</th>
            <th>${esc(t('research.adsorbate', '吸附物', 'Adsorbate'))}</th>
            <th>${esc(t('research.task', '任务', 'Task'))}</th>
            <th>${esc(t('research.state', '状态', 'State'))}</th>
            <th>${esc(t('research.method', '方法指纹', 'Method fingerprint'))}</th>
            <th>${esc(t('research.evidence', '证据', 'Evidence'))}</th>
            <th>${esc(t('research.energy', '能量', 'Energy'))}</th>
            <th>${esc(t('research.barrier', '能垒', 'Barrier'))}</th>
          </tr></thead><tbody>${tableRowsMarkup(result)}</tbody></table></div>
        <div class="rex-pager"><span>${esc(t('research.stable_paging',
          '稳定排序 · 单页上限 200', 'Stable ordering · maximum 200 per page'))}</span>
          <button class="btn" type="button" data-rex-action="next"${
            table.next_cursor ? '' : ' disabled'}>${esc(t('research.next', '下一页', 'Next page'))}</button></div></div>
        <aside class="rex-side">
          <section class="rex-panel"><h3>${esc(t('research.histogram', 'Histogram', 'Histogram'))}</h3>${histogramMarkup(result)}</section>
          <section class="rex-panel"><h3>${esc(t('research.scatter_title', 'Scatter', 'Scatter'))}</h3>${scatterMarkup(result)}</section>
          <section class="rex-panel"><h3>${esc(t('research.periodic', '元素周期表聚合', 'Periodic-table aggregation'))}</h3>${periodicMarkup(result)}</section>
          <section class="rex-panel"><h3>${esc(t('research.live_provenance', 'Live provenance', 'Live provenance'))}</h3>
            <div class="rex-provenance" aria-live="polite">${provenanceMarkup(State.provenance)}</div></section>
        </aside></div>
      <div class="rex-view-bar"><label class="rex-field"><span>${esc(t(
        'research.saved_views', '已保存 research view', 'Saved research view'))}</span>
        <select class="ipt" data-rex-field="saved_view">${savedViewsMarkup()}</select></label>
        <label class="rex-field"><span>${esc(t('research.view_name', '视图名称', 'View name'))}</span>
          <input class="ipt" data-rex-field="view_name" maxlength="96" autocomplete="off"></label>
        <div class="rex-view-actions"><button class="btn" type="button" data-rex-action="load-view">${esc(t(
          'research.load_view', '载入', 'Load'))}</button>
          <button class="btn primary" type="button" data-rex-action="save-view">${esc(t(
            'research.save_view', '保存当前 filters / sort / axes', 'Save filters / sort / axes'))}</button>
          <button class="btn quiet" type="button" data-rex-action="delete-view">${esc(t(
            'research.delete_view', '删除', 'Delete'))}</button></div></div>
    </div>`;
  }

  function splitValues(value, upper = false) {
    const values = String(value || '').split(',').map(item => item.trim()).filter(Boolean);
    return Array.from(new Set(values.map(item => upper ? item.toUpperCase() : item)));
  }

  function hostField(host, name) {
    return host.querySelector(`[data-rex-field="${name}"]`);
  }

  function requestFromHost(host) {
    const filters = { method_compatible: !!hostField(host, 'method_compatible').checked };
    for (const key of ['elements', 'task_types', 'evidence_levels']) {
      const values = splitValues(hostField(host, key).value);
      if (values.length) filters[key] = values;
    }
    const states = splitValues(hostField(host, 'states').value, true);
    if (states.length) filters.states = states;
    for (const key of ['formula', 'facet', 'adsorbate']) {
      const value = hostField(host, key).value.trim();
      if (value) filters[key] = value;
    }
    for (const key of ['energy_min_eV', 'energy_max_eV', 'barrier_min_eV', 'barrier_max_eV']) {
      const raw = hostField(host, key).value.trim();
      if (raw !== '' && Number.isFinite(Number(raw))) filters[key] = Number(raw);
    }
    return {
      schema: 'vcstudio.research-query/v1', filters,
      sort: {
        key: hostField(host, 'sort_key').value,
        direction: hostField(host, 'sort_direction').value,
      },
      axes: { x: hostField(host, 'axis_x').value, y: hostField(host, 'axis_y').value },
      limit: 50,
    };
  }

  function renderAll() {
    for (const host of hosts()) {
      const scope = host.dataset.researchExplorerHost || 'home';
      host.innerHTML = shellMarkup(scope, State.result);
      bindHost(host);
    }
  }

  async function callQuery(method = 'research_explorer_query', request = State.request) {
    const generation = ++State.generation;
    State.busy = true;
    State.operation = t('research.loading', '正在读取服务端定稿 DTO…',
      'Loading server-finalized DTOs…');
    renderAll();
    const result = await VCS.call(method, request);
    if (generation !== State.generation) return false;
    State.busy = false;
    State.operation = '';
    State.result = result;
    if (result && result.saved_views) State.savedViews = result.saved_views;
    renderAll();
    return !!(result && result.ok);
  }

  async function bootstrap() {
    const generation = ++State.generation;
    State.busy = true;
    renderAll();
    const result = await VCS.call('research_explorer_bootstrap');
    if (generation !== State.generation) return false;
    State.busy = false;
    State.result = result;
    State.savedViews = result && result.saved_views || State.savedViews;
    renderAll();
    return !!(result && result.ok);
  }

  async function showProvenance(button) {
    const generation = ++State.generation;
    State.operation = t('research.prov_loading', '正在读取 live provenance…',
      'Loading live provenance…');
    renderAll();
    const graph = await VCS.call(
      'research_explorer_provenance', button.dataset.projectId,
      button.dataset.jobId || null, button.dataset.sourceId || null);
    if (generation !== State.generation) return;
    State.provenance = graph;
    State.operation = '';
    renderAll();
  }

  async function saveView(host) {
    const authority = State.savedViews || {};
    const name = hostField(host, 'view_name').value.trim();
    if (!name) {
      State.operation = t('research.view_name_required', '请填写视图名称。',
        'Enter a view name.');
      renderAll();
      return;
    }
    const selected = hostField(host, 'saved_view').value;
    const viewId = selected || `view-${Date.now().toString(36)}`;
    const result = await VCS.call('research_view_save', {
      id: viewId, name,
      filters: State.request.filters,
      sort: State.request.sort,
      axes: State.request.axes,
    }, authority.authority_id, authority.revision);
    if (result && (result.ok || result.conflict)) State.savedViews = result;
    State.operation = result && result.ok
      ? t('research.view_saved', '视图已保存。', 'View saved.')
      : esc(result && result.error || t('research.view_save_failed', '视图保存失败。',
        'View save failed.'));
    renderAll();
  }

  async function deleteView(host) {
    const viewId = hostField(host, 'saved_view').value;
    const authority = State.savedViews || {};
    if (!viewId) return;
    const result = await VCS.call(
      'research_view_delete', viewId, authority.authority_id, authority.revision);
    if (result && (result.ok || result.conflict)) State.savedViews = result;
    State.operation = result && result.ok
      ? t('research.view_deleted', '视图已删除。', 'View deleted.')
      : String(result && result.error || '');
    renderAll();
  }

  function loadView(host) {
    const viewId = hostField(host, 'saved_view').value;
    const views = State.savedViews && State.savedViews.views || [];
    const view = views.find(item => item.id === viewId);
    if (!view) return;
    State.request = {
      schema: 'vcstudio.research-query/v1',
      filters: JSON.parse(JSON.stringify(view.filters)),
      sort: JSON.parse(JSON.stringify(view.sort)),
      axes: JSON.parse(JSON.stringify(view.axes)),
      limit: 50,
    };
    State.provenance = null;
    callQuery();
  }

  function bindHost(host) {
    if (host.dataset.researchExplorerBound === '1') return;
    host.dataset.researchExplorerBound = '1';
    host.addEventListener('click', event => {
      const button = event.target.closest('[data-rex-action]');
      if (!button) return;
      const action = button.dataset.rexAction;
      if (action === 'apply') {
        State.request = requestFromHost(host);
        State.provenance = null;
        callQuery();
      } else if (action === 'clear') {
        State.request = {
          schema: 'vcstudio.research-query/v1',
          filters: { method_compatible: true },
          sort: { key: 'project', direction: 'asc' },
          axes: { x: 'energy_eV', y: 'barrier_eV' }, limit: 50,
        };
        State.provenance = null;
        callQuery();
      } else if (action === 'current-project' && State.currentProjectId) {
        State.request = requestFromHost(host);
        State.request.filters.project_ids = [State.currentProjectId];
        callQuery();
      } else if (action === 'rebuild') {
        callQuery('research_explorer_rebuild');
      } else if (action === 'next' && State.result && State.result.table) {
        const cursor = State.result.table.next_cursor;
        if (cursor) callQuery('research_explorer_query', { ...State.request, cursor });
      } else if (action === 'provenance') {
        showProvenance(button);
      } else if (action === 'save-view') {
        saveView(host);
      } else if (action === 'load-view') {
        loadView(host);
      } else if (action === 'delete-view') {
        deleteView(host);
      }
    });
  }

  function init() {
    renderAll();
    VCS.ready.then(bootstrap);
  }

  document.addEventListener('vcs:project-context', event => {
    State.currentProjectId = String(event.detail && event.detail.project_id || '');
  });
  document.addEventListener('vcs:language', renderAll);
  document.addEventListener('vcs:page', event => {
    const page = event.detail && event.detail.page;
    if ((page === 'dashboard' || page === 'project') && !State.result && !State.busy) bootstrap();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  if (window.__VCS_TEST__) {
    window.__VCS_RESEARCH_EXPLORER_TEST__ = {
      State,
      configure(value = {}) {
        if (Object.prototype.hasOwnProperty.call(value, 'result')) State.result = value.result;
        if (Object.prototype.hasOwnProperty.call(value, 'savedViews')) State.savedViews = value.savedViews;
        if (Object.prototype.hasOwnProperty.call(value, 'request')) State.request = value.request;
        if (Object.prototype.hasOwnProperty.call(value, 'provenance')) State.provenance = value.provenance;
      },
      shellMarkup,
      tableRowsMarkup,
      histogramMarkup,
      aggregationMessage,
      scatterMarkup,
      periodicMarkup,
      provenanceMarkup,
      requestFromHost,
      renderAll,
      bootstrap,
      callQuery,
    };
  }
})();
