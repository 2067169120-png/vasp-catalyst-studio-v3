// reference-browser.js — UI adapter for the narrow ExternalReferenceProtocol.
// Endpoint URLs, GraphQL documents, response fields and cache paths stay server-owned.
'use strict';

(function () {
  const VCS = window.VCS;
  if (!VCS) return;
  const $ = id => document.getElementById(id);
  const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
  const State = {
    catalog: null, providerId: '', projectId: '', projectName: '',
    result: null, selected: new Set(), comparison: null,
    busy: false, generation: 0, importConsumed: false,
  };
  const FILTERS = Object.freeze({
    materials_project: [
      { key: 'formula', label: 'reference.filter.formula', fallback: 'Formula', kind: 'text' },
      { key: 'chemsys', label: 'reference.filter.chemsys', fallback: 'Chemical system', kind: 'text' },
      { key: 'elements', label: 'reference.filter.elements', fallback: 'Elements (comma separated)', kind: 'list' },
      { key: 'material_ids', label: 'reference.filter.material_ids', fallback: 'Material IDs (comma separated)', kind: 'list' },
    ],
    optimade: [
      { key: 'formula', label: 'reference.filter.formula', fallback: 'Reduced formula', kind: 'text' },
      { key: 'elements', label: 'reference.filter.elements', fallback: 'Elements (comma separated)', kind: 'list' },
      { key: 'nelements_min', label: 'reference.filter.nelements_min', fallback: 'Minimum elements', kind: 'integer' },
      { key: 'nelements_max', label: 'reference.filter.nelements_max', fallback: 'Maximum elements', kind: 'integer' },
    ],
    catalysis_hub: [
      { key: 'reactants', label: 'reference.filter.reactants', fallback: 'Reactants', kind: 'text' },
      { key: 'products', label: 'reference.filter.products', fallback: 'Products', kind: 'text' },
      { key: 'chemical_composition', label: 'reference.filter.composition', fallback: 'Chemical composition', kind: 'text' },
      { key: 'surface', label: 'reference.filter.surface', fallback: 'Surface', kind: 'text' },
      { key: 'facet', label: 'reference.filter.facet', fallback: 'Facet', kind: 'text' },
    ],
  });

  function plain(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }
  function tr(key, fallback, params) {
    return typeof VCS.t === 'function' ? VCS.t(key, params || {}, fallback) : fallback;
  }
  function safeId(value) {
    const text = String(value || '').trim(); return SAFE_ID.test(text) ? text : '';
  }
  function currentProject() {
    if (window.Project && typeof window.Project.current === 'function') {
      const project = window.Project.current();
      if (safeId(project && project.project_id)) return project;
    }
    const workspace = VCS.workspace;
    const id = safeId(workspace && workspace.state && workspace.state.project_id);
    if (!id || !workspace || !Array.isArray(workspace.projects)) return null;
    return workspace.projects.find(item => safeId(item && item.project_id) === id) || null;
  }
  function localized(record, field, fallback = '') {
    const suffix = VCS.i18n && VCS.i18n.lang === 'en' ? '_en' : '_zh';
    return String(plain(record)[field + suffix] || plain(record)[field] || fallback || '');
  }
  function providerRecord(id = State.providerId) {
    return (plain(State.catalog).providers || []).find(item => String(item.id || '') === id) || null;
  }
  function setText(id, value) { const node = $(id); if (node) node.textContent = String(value == null ? '' : value); }
  function clear(node) { if (node) node.replaceChildren(); }
  function textNode(tag, text, className = '') {
    const node = document.createElement(tag); node.textContent = String(text == null ? '' : text);
    if (className) node.className = className; return node;
  }
  function safeHref(value, { doi = false } = {}) {
    try {
      const url = doi
        ? new URL(`https://doi.org/${encodeURIComponent(String(value || ''))}`)
        : new URL(String(value || ''));
      return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
    } catch (_) { return ''; }
  }
  function showAlert(message) {
    const node = $('rb-alert'); if (!node) return;
    const text = String(message || '').trim(); node.hidden = !text; node.textContent = text;
  }
  function operation(message, tone = '') {
    const node = $('rb-operation'); if (!node) return;
    node.textContent = String(message || ''); node.className = `rb-operation${tone ? ` ${tone}` : ''}`;
  }
  function errorMessage(result, fallback) {
    const error = plain(result && result.error); const code = safeId(error.code);
    return tr(`reference.error.${code || 'unavailable'}`, String(error.message || fallback || tr(
      'reference.error.unavailable', '外部参考暂不可用')));
  }
  function setBusy(busy) {
    State.busy = busy === true;
    ['rb-network-toggle', 'rb-key-save', 'rb-key-delete', 'rb-search', 'rb-compare',
    ].forEach(id => { const node = $(id); if (node) node.disabled = State.busy || (id === 'rb-compare' && !State.selected.size); });
    const form = $('rb-search-form'); if (form) form.setAttribute('aria-busy', State.busy ? 'true' : 'false');
    if (State.catalog) renderCredential();
  }

  function renderNetwork() {
    const enabled = plain(State.catalog).network_enabled === true;
    setText('rb-network-state', enabled
      ? tr('reference.network.enabled', '已启用（仅本会话）')
      : tr('reference.network.disabled', '已禁用'));
    const button = $('rb-network-toggle'); if (button) {
      button.textContent = enabled
        ? tr('reference.network.disable', '禁用外部网络')
        : tr('reference.network.enable', '本会话启用');
      button.disabled = State.busy;
    }
  }

  function renderProviders() {
    const list = $('rb-provider-list'); const select = $('rb-provider');
    const providers = Array.isArray(plain(State.catalog).providers) ? State.catalog.providers : [];
    if (!State.providerId || !providers.some(item => item.id === State.providerId)) {
      State.providerId = String(providers[0] && providers[0].id || '');
    }
    clear(list); clear(select);
    providers.forEach(provider => {
      const id = safeId(provider.id); if (!id) return;
      const item = document.createElement('li');
      const button = document.createElement('button'); button.type = 'button';
      button.className = 'rb-provider-card'; button.dataset.providerId = id;
      button.dataset.selected = id === State.providerId ? 'true' : 'false';
      button.append(
        textNode('b', localized(provider, 'label', id)),
        textNode('small', localized(provider, 'description')),
        textNode('span', tr(`reference.provider.status.${provider.status}`,
          String(provider.status || 'unavailable')), 'rb-provider-status'));
      button.lastChild.dataset.status = String(provider.status || 'unavailable');
      item.appendChild(button); list.appendChild(item);
      const option = document.createElement('option'); option.value = id;
      option.textContent = localized(provider, 'label', id); option.selected = id === State.providerId;
      select.appendChild(option);
    });
    if (!providers.length && list) list.appendChild(textNode(
      'li', tr('reference.providers.empty', '没有可用 provider。')));
    if (list) list.setAttribute('aria-busy', State.catalog ? 'false' : 'true');
    renderCredential(); renderFilterFields(); renderNetwork();
  }

  function renderCredential() {
    const provider = providerRecord(); const group = $('rb-key-group');
    if (!group) return; group.hidden = !(provider && provider.requires_api_key === true);
    const label = $('rb-key-label'); if (label && provider) label.textContent = tr(
      'reference.key.provider_label', '{provider} API key', {
        provider: localized(provider, 'label', safeId(provider.id)),
      });
    const remove = $('rb-key-delete'); if (remove) remove.disabled = State.busy || !(provider && provider.credential_available === true);
  }

  function resetSearchState() {
    State.generation += 1;
    State.result = null; State.comparison = null; State.selected.clear(); State.importConsumed = false;
    setBusy(false); renderResults(); renderComparison();
  }

  function renderFilterFields({ clearValues = false } = {}) {
    const fields = FILTERS[State.providerId] || [];
    for (let index = 0; index < 5; index += 1) {
      const definition = fields[index]; const group = $(`rb-filter-group-${index + 1}`);
      const input = $(`rb-filter-${index + 1}`); const label = $(`rb-filter-label-${index + 1}`);
      if (!group || !input || !label) continue;
      group.hidden = !definition;
      if (clearValues) input.value = '';
      if (!definition) continue;
      label.textContent = tr(definition.label, definition.fallback);
      input.inputMode = definition.kind === 'integer' ? 'numeric' : 'text';
      input.setAttribute('aria-label', label.textContent);
    }
  }

  function buildFilters() {
    const definitions = FILTERS[State.providerId] || [];
    const filters = {
      page: Number($('rb-page') && $('rb-page').value),
      limit: Number($('rb-limit') && $('rb-limit').value),
    };
    if (!Number.isInteger(filters.page) || filters.page < 1 || filters.page > 4) {
      throw new Error(tr('reference.error.page', '页码必须为 1–4 的整数。'));
    }
    if (!Number.isInteger(filters.limit) || ![5, 10, 20, 25].includes(filters.limit)) {
      throw new Error(tr('reference.error.limit', '每页数量无效。'));
    }
    let scientificFilters = 0;
    definitions.forEach((definition, index) => {
      const raw = String($(`rb-filter-${index + 1}`) && $(`rb-filter-${index + 1}`).value || '').trim();
      if (!raw) return;
      if (definition.kind === 'list') {
        const values = raw.split(',').map(item => item.trim()).filter(Boolean);
        if (!values.length) throw new Error(tr('reference.error.filter', '搜索条件无效。'));
        filters[definition.key] = values;
      } else if (definition.kind === 'integer') {
        if (!/^[0-9]+$/.test(raw)) throw new Error(tr(
          'reference.error.integer', '元素数量必须为整数。'));
        filters[definition.key] = Number(raw);
      } else {
        filters[definition.key] = raw;
      }
      scientificFilters += 1;
    });
    if (!scientificFilters) throw new Error(tr(
      'reference.error.filter_required', '请至少填写一个科学搜索条件。'));
    return filters;
  }

  function appendMeta(list, label, value) {
    if (value === null || value === undefined || value === '') return;
    list.append(textNode('dt', label), textNode('dd', value));
  }

  function renderResult(item) {
    const card = document.createElement('article'); card.className = 'rb-result';
    const head = document.createElement('div'); head.className = 'rb-result-head';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    checkbox.value = safeId(item.item_id); checkbox.checked = State.selected.has(checkbox.value);
    checkbox.disabled = State.busy || !checkbox.value;
    checkbox.setAttribute('aria-label', tr('reference.result.select', '选择 {title}', { title: item.title || item.source_id || '' }));
    const title = document.createElement('div'); title.className = 'rb-result-title';
    title.append(textNode('b', item.title || item.source_id || tr('reference.result.untitled', '未命名结果')),
      textNode('small', item.source_id || ''));
    head.append(checkbox, title); card.appendChild(head);
    const meta = document.createElement('dl'); meta.className = 'rb-result-meta';
    appendMeta(meta, tr('reference.result.kind', '类型'), item.kind);
    appendMeta(meta, tr('reference.result.formula', '组成'), item.formula || item.chemical_system);
    appendMeta(meta, tr('reference.result.surface', '表面'), [item.surface, item.facet].filter(Boolean).join(' / '));
    const method = plain(item.method);
    appendMeta(meta, tr('reference.result.method', '方法'), `${method.label || ''} · ${method.status || 'unknown'}`);
    appendMeta(meta, tr('reference.result.license', '许可'), plain(item.license).id || 'unknown');
    card.appendChild(meta);
    const properties = document.createElement('div'); properties.className = 'rb-properties';
    (Array.isArray(item.properties) ? item.properties : []).forEach(property => {
      const value = property.value === null || property.value === undefined ? '—' : String(property.value);
      properties.appendChild(textNode('span', `${property.key}: ${value}${property.unit ? ` ${property.unit}` : ''}`, 'rb-property'));
    });
    if (properties.childNodes.length) card.appendChild(properties);
    const links = document.createElement('div'); links.className = 'rb-result-links';
    const licenseHref = safeHref(plain(item.license).url);
    if (licenseHref) { const link = textNode('a', tr('reference.result.license_link', '许可')); link.href = licenseHref; link.target = '_blank'; link.rel = 'noreferrer'; links.appendChild(link); }
    (plain(item.citation).dois || []).forEach(doi => {
      const href = safeHref(doi, { doi: true }); if (!href) return;
      const link = textNode('a', `DOI ${doi}`); link.href = href; link.target = '_blank'; link.rel = 'noreferrer'; links.appendChild(link);
    });
    if (links.childNodes.length) card.appendChild(links);
    const actions = document.createElement('div'); actions.className = 'actions';
    const importButton = document.createElement('button'); importButton.type = 'button'; importButton.className = 'btn';
    importButton.dataset.importItem = safeId(item.item_id);
    importButton.textContent = item.structure_available
      ? tr('reference.import.button', '导入候选 provenance')
      : tr('reference.import.unavailable', '无可导入结构');
    importButton.disabled = State.busy || State.importConsumed || item.structure_available !== true;
    actions.appendChild(importButton); card.appendChild(actions);
    return card;
  }

  function renderResults() {
    const box = $('rb-results'); if (!box) return; clear(box);
    const items = Array.isArray(plain(State.result).items) ? State.result.items : [];
    if (!items.length) box.appendChild(textNode('div', tr(
      'reference.results.empty', '尚未搜索。'), 'rb-empty'));
    else items.forEach(item => box.appendChild(renderResult(plain(item))));
    const total = plain(State.result).total_count;
    setText('rb-results-summary', items.length
      ? tr('reference.results.count', '本页 {count} 条；provider 总数 {total}', {
        count: items.length, total: total == null ? tr('common.unknown', '未知') : total,
      })
      : tr('reference.results.empty', '尚未搜索。'));
    const compare = $('rb-compare'); if (compare) compare.disabled = State.busy || !State.selected.size;
  }

  function comparisonTable(title, rows, columns) {
    const section = document.createElement('section'); section.className = 'rb-compare-column';
    section.appendChild(textNode('h3', title));
    const table = document.createElement('table'); table.className = 'rb-compare-table';
    const thead = document.createElement('thead'); const header = document.createElement('tr');
    columns.forEach(column => header.appendChild(textNode('th', column.label)));
    thead.appendChild(header); table.appendChild(thead);
    const body = document.createElement('tbody');
    rows.forEach(row => {
      const trNode = document.createElement('tr');
      columns.forEach(column => trNode.appendChild(textNode('td', column.value(row))));
      body.appendChild(trNode);
    });
    if (!rows.length) { const empty = document.createElement('tr'); const cell = textNode(
      'td', tr('reference.comparison.no_rows', '没有可显示行')); cell.colSpan = columns.length; empty.appendChild(cell); body.appendChild(empty); }
    table.appendChild(body); section.appendChild(table); return section;
  }

  function renderComparison() {
    const box = $('rb-comparison'); if (!box) return; clear(box);
    const comparison = plain(State.comparison);
    if (!comparison.ok) { box.appendChild(textNode('div', tr(
      'reference.comparison.empty', '选择一个或多个结果后比较。'), 'rb-empty')); return; }
    const grid = document.createElement('div'); grid.className = 'rb-compare-grid';
    const local = Array.isArray(comparison.local_items) ? comparison.local_items : [];
    const external = [];
    (Array.isArray(comparison.external_items) ? comparison.external_items : []).forEach(item => {
      const properties = Array.isArray(item.properties) && item.properties.length ? item.properties : [{}];
      properties.forEach(property => external.push({ item, property }));
    });
    grid.append(
      comparisonTable(tr('reference.comparison.local', '本地结果'), local, [
        { label: tr('reference.table.name', '名称'), value: row => row.name || '—' },
        { label: tr('reference.table.quantity', '量'), value: row => row.quantity || '—' },
        { label: tr('reference.table.value', '值'), value: row => row.value == null ? '—' : `${row.value} ${row.unit || ''}` },
        { label: tr('reference.table.method', '方法状态'), value: row => row.method_status || 'unknown' },
      ]),
      comparisonTable(tr('reference.comparison.external', '外部参考'), external, [
        { label: tr('reference.table.name', '名称'), value: row => row.item.title || row.item.source_id || '—' },
        { label: tr('reference.table.quantity', '量'), value: row => row.property.quantity || row.property.key || '—' },
        { label: tr('reference.table.value', '值'), value: row => row.property.value == null ? '—' : `${row.property.value} ${row.property.unit || ''}` },
        { label: tr('reference.table.method', '方法状态'), value: row => plain(row.item.method).status || 'unknown' },
      ]));
    grid.appendChild(textNode('p', tr('reference.comparison.boundary',
      '未生成 aggregate：外部值没有默认方法兼容性，也不能进入 ValidationResult 或 final claims。'), 'rb-compare-boundary'));
    box.appendChild(grid);
  }

  async function loadCatalog() {
    const generation = ++State.generation; setBusy(true); showAlert('');
    operation(tr('reference.operation.loading', '正在读取 provider registry…'), 'busy');
    try {
      const result = await VCS.call('external_reference_catalog');
      if (generation !== State.generation) return false;
      if (!result || result.ok === false) throw new Error(errorMessage(result, tr(
        'reference.error.catalog', 'Provider registry 不可用。')));
      State.catalog = result; renderProviders();
      operation(tr('reference.operation.ready', '等待受限搜索。'), 'ok'); return true;
    } catch (error) {
      if (generation !== State.generation) return false;
      showAlert(error && error.message || String(error)); operation(tr(
        'reference.operation.failed', '外部参考不可用；本地工作流未受影响。'), 'bad'); return false;
    } finally { if (generation === State.generation) setBusy(false); }
  }

  async function search(event) {
    if (event) event.preventDefault(); if (State.busy) return false;
    let filters;
    try { filters = buildFilters(); } catch (error) { showAlert(error.message || String(error)); return false; }
    const providerId = safeId(State.providerId); const generation = ++State.generation;
    setBusy(true); showAlert(''); operation(tr('reference.operation.searching', '正在执行有界 provider 查询…'), 'busy');
    try {
      const result = await VCS.call('external_reference_search', providerId, filters);
      if (generation !== State.generation || providerId !== State.providerId) return false;
      if (!result || result.ok === false || result.status !== 'available') throw new Error(errorMessage(
        result, tr('reference.error.search', '外部搜索不可用。')));
      if (safeId(result.provider) !== providerId || !safeId(result.result_token)) throw new Error(tr(
        'reference.error.identity', '外部结果身份无效。'));
      State.result = result; State.selected.clear(); State.comparison = null; State.importConsumed = false;
      renderResults(); renderComparison(); operation(tr(
        'reference.operation.complete', '搜索完成；结果仍是 external_reference。'), 'ok'); return true;
    } catch (error) {
      if (generation !== State.generation) return false;
      State.result = null; State.selected.clear(); State.comparison = null; renderResults(); renderComparison();
      showAlert(error && error.message || String(error)); operation(tr(
        'reference.operation.failed', '外部参考不可用；本地工作流未受影响。'), 'bad'); return false;
    } finally { if (generation === State.generation) setBusy(false); }
  }

  async function compareSelected() {
    if (State.busy || !State.result || !State.selected.size || !State.projectId) return false;
    const generation = ++State.generation; const selected = Array.from(State.selected);
    setBusy(true); showAlert(''); operation(tr('reference.operation.comparing', '正在构建只读并排比较…'), 'busy');
    try {
      const result = await VCS.call('external_reference_compare', State.projectId,
        State.result.result_token, selected);
      if (generation !== State.generation) return false;
      if (!result || result.ok === false) throw new Error(errorMessage(result, tr(
        'reference.error.compare', '并排比较不可用。')));
      State.comparison = result; renderComparison(); operation(tr(
        'reference.operation.compared', '并排比较已生成；未创建 aggregate。'), 'ok'); return true;
    } catch (error) {
      if (generation !== State.generation) return false;
      showAlert(error && error.message || String(error)); return false;
    } finally { if (generation === State.generation) setBusy(false); }
  }

  async function importCandidate(itemId) {
    if (State.busy || State.importConsumed || !State.result || !State.projectId) return false;
    const generation = ++State.generation; const projectId = State.projectId;
    const resultToken = safeId(State.result.result_token); const selectedItem = safeId(itemId);
    setBusy(true); showAlert('');
    operation(tr('reference.operation.previewing', '正在生成项目绑定的结构预览…'), 'busy');
    try {
      const preview = await VCS.call('external_reference_import_preview', projectId,
        resultToken, selectedItem);
      if (generation !== State.generation || projectId !== State.projectId
        || resultToken !== safeId(plain(State.result).result_token)) return false;
      if (!preview || preview.ok === false || preview.status !== 'preview'
        || safeId(preview.project_id) !== projectId || !safeId(preview.preview_token)
        || !/^[a-f0-9]{64}$/.test(String(preview.content_sha256 || ''))) throw new Error(errorMessage(
        preview, tr('reference.error.preview', '结构预览不可用。')));
      const confirmed = window.confirm(tr('reference.import.confirm_preview',
        '确认导入 {formula}（{sites} sites，内容 {hash}…）为 candidate provenance？它不会成为 accepted member 或报告结论。', {
          formula: String(preview.formula || '—'), sites: Number(preview.site_count || 0),
          hash: String(preview.content_sha256).slice(0, 12),
        }));
      if (!confirmed) {
        operation(tr('reference.operation.preview_cancelled', '结构预览已取消；未写入项目。'));
        return false;
      }
      if (generation !== State.generation || projectId !== State.projectId) return false;
      operation(tr('reference.operation.importing', '正在写入 candidate-only provenance…'), 'busy');
      const result = await VCS.call('external_reference_import', projectId,
        preview.preview_token,
        { confirmed: true, scope: 'candidate_provenance' });
      if (generation !== State.generation || projectId !== State.projectId) return false;
      if (!result || result.ok === false || result.status !== 'candidate_only') throw new Error(errorMessage(
        result, tr('reference.error.import', '候选 provenance 导入失败。')));
      State.importConsumed = true; renderResults(); operation(tr(
        'reference.operation.imported', '已导入 candidate-only provenance；未改变科学门禁。'), 'ok'); return true;
    } catch (error) {
      if (generation !== State.generation) return false;
      showAlert(error && error.message || String(error)); return false;
    } finally { if (generation === State.generation) setBusy(false); }
  }

  async function toggleNetwork() {
    if (State.busy) return; const enabled = plain(State.catalog).network_enabled === true;
    if (!enabled && !window.confirm(tr('reference.network.confirm',
      '本会话将访问已注册的外部科学数据库。继续？'))) return;
    setBusy(true); showAlert('');
    try {
      const result = await VCS.call('external_reference_set_network', !enabled,
        enabled ? null : 'enable-external-reference-network');
      if (!result || result.ok === false) throw new Error(errorMessage(result));
      await loadCatalog();
    } catch (error) { showAlert(error && error.message || String(error)); }
    finally { setBusy(false); }
  }

  async function saveKey() {
    const input = $('rb-api-key'); const key = String(input && input.value || '');
    if (!key) { showAlert(tr('reference.error.key_required', '请输入 API key。')); return; }
    setBusy(true); showAlert('');
    try {
      const result = await VCS.call('external_reference_store_api_key', State.providerId, key);
      if (!result || result.ok === false) throw new Error(errorMessage(result));
      if (input) input.value = ''; await loadCatalog();
    } catch (error) { showAlert(error && error.message || String(error)); }
    finally { if (input) input.value = ''; setBusy(false); }
  }

  async function deleteKey() {
    setBusy(true); showAlert('');
    try {
      const result = await VCS.call('external_reference_delete_api_key', State.providerId);
      if (!result || result.ok === false) throw new Error(errorMessage(result));
      await loadCatalog();
    } catch (error) { showAlert(error && error.message || String(error)); }
    finally { setBusy(false); }
  }

  async function enter() {
    const project = currentProject(); const projectId = safeId(project && project.project_id);
    State.projectId = projectId; State.projectName = String(project && project.name || '');
    setText('rb-project-name', projectId
      ? tr('reference.project.current', '当前项目：{name}', { name: State.projectName || projectId })
      : tr('reference.project.missing', '请先选择一个已登记项目。'));
    resetSearchState(); if (!State.catalog) await loadCatalog(); else renderProviders();
  }

  function selectProvider(providerId) {
    const id = safeId(providerId); if (!providerRecord(id) || id === State.providerId) return;
    State.providerId = id; resetSearchState(); renderProviders(); renderFilterFields({ clearValues: true });
  }

  function wire() {
    const form = $('rb-search-form'); if (form) form.addEventListener('submit', search);
    const provider = $('rb-provider'); if (provider) provider.addEventListener('change', () => selectProvider(provider.value));
    const list = $('rb-provider-list'); if (list) list.addEventListener('click', event => {
      const button = event.target.closest('[data-provider-id]'); if (button) selectProvider(button.dataset.providerId);
    });
    const results = $('rb-results'); if (results) {
      results.addEventListener('change', event => {
        if (event.target.type !== 'checkbox') return; const id = safeId(event.target.value); if (!id) return;
        if (event.target.checked) State.selected.add(id); else State.selected.delete(id); renderResults();
      });
      results.addEventListener('click', event => {
        const button = event.target.closest('[data-import-item]'); if (button) importCandidate(safeId(button.dataset.importItem));
      });
    }
    if ($('rb-compare')) $('rb-compare').addEventListener('click', compareSelected);
    if ($('rb-network-toggle')) $('rb-network-toggle').addEventListener('click', toggleNetwork);
    if ($('rb-key-save')) $('rb-key-save').addEventListener('click', saveKey);
    if ($('rb-key-delete')) $('rb-key-delete').addEventListener('click', deleteKey);
    document.addEventListener('vcs:route', event => {
      if (event.detail && event.detail.page === 'reference-browser') enter();
    });
    document.addEventListener('vcs:page', event => {
      if (!VCS.workspace && event.detail && event.detail.page === 'reference-browser') enter();
    });
    document.addEventListener('vcs:workspace-project', () => {
      const page = $('page-reference-browser'); if (page && !page.hidden) enter();
    });
    document.addEventListener('vcs:language', () => {
      if (State.catalog) { renderProviders(); renderResults(); renderComparison(); }
    });
  }

  if (window.__VCS_TEST__ === true) {
    window.__VCS_REFERENCE_TEST__ = Object.freeze({
      buildFilters,
      configure(providerId) { State.providerId = safeId(providerId); },
      snapshot() { return { provider_id: State.providerId, selected: Array.from(State.selected), has_result: !!State.result }; },
    });
  }
  window.ReferenceBrowser = { open: enter, refresh: loadCatalog };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true });
  else wire();
})();
