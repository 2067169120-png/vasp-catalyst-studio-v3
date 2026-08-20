// Versioned catalysis recipe gallery and read-only pre-execution DAG preview.
'use strict';

(function () {
  const VCS = window.VCS;
  if (!VCS) return;

  const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9._~:-]{0,159}$/;
  const State = {
    catalog: [], selectedKey: '', catalogBusy: false, loaded: false,
    preview: null, previewKey: '', pendingPreviewKeys: new Set(),
    parameterControls: [], evidenceControls: [], recipeButtons: [],
    selectionGeneration: 0, previewRequestGeneration: 0,
  };

  const el = id => document.getElementById(id);
  const english = () => String(VCS.i18n && VCS.i18n.lang || '').toLowerCase().startsWith('en');
  const tr = (key, fallback, params) => (
    typeof VCS.t === 'function' ? VCS.t(key, params || {}, fallback) : fallback
  );
  const localized = (row, stem) => String(
    row && row[`${stem}_${english() ? 'en' : 'zh'}`] || ''
  );
  const recipeKey = (recipeId, recipeVersion) => JSON.stringify([
    String(recipeId || ''), String(recipeVersion || ''),
  ]);
  const recipeDomId = (recipeId, recipeVersion) => (
    `rr-recipe-${encodeURIComponent(recipeKey(recipeId, recipeVersion))}`
  );

  function clear(node) {
    if (!node) return;
    if (typeof node.replaceChildren === 'function') node.replaceChildren();
    else node.children = [];
    node.textContent = '';
    node.innerHTML = '';
  }

  function textNode(tag, text, className = '') {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = String(text == null ? '' : text);
    return node;
  }

  function option(value, text) {
    const node = document.createElement('option');
    node.value = value;
    node.textContent = text;
    return node;
  }

  function setStatus(message, failed = false) {
    const node = el('rr-status');
    if (!node) return;
    node.textContent = message || '';
    node.classList.toggle('fail', !!failed);
  }

  function refreshBusyState() {
    const selectedPreviewBusy = State.pendingPreviewKeys.has(State.selectedKey);
    const button = el('rr-preview-button');
    if (button) button.disabled = State.catalogBusy || selectedPreviewBusy || !State.selectedKey;
    State.recipeButtons.forEach(item => { item.disabled = State.catalogBusy; });
    const list = el('rr-list');
    if (list) list.setAttribute('aria-busy', State.catalogBusy ? 'true' : 'false');
  }

  function setCatalogBusy(value) {
    State.catalogBusy = !!value;
    refreshBusyState();
  }

  function recipeByKey(key) {
    return State.catalog.find(item => recipeKey(
      item.recipe_id, item.recipe_version,
    ) === key) || null;
  }

  function renderGallery() {
    const host = el('rr-list');
    if (!host) return;
    clear(host);
    State.recipeButtons = [];
    State.catalog.forEach(recipe => {
      const key = recipeKey(recipe.recipe_id, recipe.recipe_version);
      const button = document.createElement('button');
      button.type = 'button';
      button.id = recipeDomId(recipe.recipe_id, recipe.recipe_version);
      button.className = 'rr-recipe';
      button.dataset.recipeId = recipe.recipe_id;
      button.dataset.recipeVersion = recipe.recipe_version;
      button.dataset.recipeKey = key;
      button.setAttribute('aria-pressed', key === State.selectedKey ? 'true' : 'false');
      button.disabled = State.catalogBusy;
      button.append(
        textNode('b', localized(recipe, 'label') || recipe.recipe_id),
        textNode('span', localized(recipe, 'summary')),
        textNode('span', `${tr('research_recipes.version', '版本', {})} ${recipe.recipe_version}`,
          'rr-version'),
      );
      button.addEventListener('click', () => selectRecipe(
        recipe.recipe_id, recipe.recipe_version, { focus: true },
      ));
      host.appendChild(button);
      State.recipeButtons.push(button);
    });
    host.setAttribute('aria-busy', 'false');
  }

  function parameterInput(definition) {
    let input = document.createElement('input');
    input.className = 'ipt';
    input.dataset.rrParam = definition.parameter_id;
    input.setAttribute('aria-label', localized(definition, 'label') || definition.parameter_id);
    const value = Array.isArray(definition.default)
      ? definition.default.join(', ') : String(definition.default);
    input.value = value;
    if (definition.value_kind === 'number' || definition.value_kind === 'integer') {
      input.type = 'number';
      input.inputMode = definition.value_kind === 'integer' ? 'numeric' : 'decimal';
      if (definition.minimum != null) input.min = String(definition.minimum);
      if (definition.maximum != null) input.max = String(definition.maximum);
      input.step = definition.value_kind === 'integer' ? '1' : 'any';
    } else if (definition.value_kind === 'boolean') {
      input = document.createElement('select');
      input.className = 'ipt';
      input.dataset.rrParam = definition.parameter_id;
      input.setAttribute('aria-label', localized(definition, 'label') || definition.parameter_id);
      input.append(
        option('true', tr('research_recipes.boolean_true', '是')),
        option('false', tr('research_recipes.boolean_false', '否')),
      );
      input.value = definition.default ? 'true' : 'false';
    }
    input.disabled = true;
    return input;
  }

  function renderParameters(recipe, draft) {
    const host = el('rr-parameters');
    if (!host) return;
    Array.from(host.children || []).forEach(child => {
      if (String(child.tagName || '').toLowerCase() !== 'legend') child.remove();
    });
    State.parameterControls = [];
    const draftOverrides = draft && draft.overrides || {};
    (recipe.parameters || []).forEach(definition => {
      const row = document.createElement('div');
      row.className = 'rr-field';
      const label = textNode('span', localized(definition, 'label'), 'rr-field-label');
      const details = textNode('small', [definition.parameter_id, definition.unit || ''].filter(Boolean).join(' · '));
      label.appendChild(details);
      const controls = document.createElement('div');
      controls.className = 'rr-parameter-control';
      const overrideLabel = document.createElement('label');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.dataset.rrOverride = definition.parameter_id;
      const input = parameterInput(definition);
      const hasDraft = Object.prototype.hasOwnProperty.call(draftOverrides, definition.parameter_id);
      checkbox.checked = hasDraft;
      input.disabled = !hasDraft;
      if (hasDraft) input.value = Array.isArray(draftOverrides[definition.parameter_id])
        ? draftOverrides[definition.parameter_id].join(', ') : String(draftOverrides[definition.parameter_id]);
      overrideLabel.append(checkbox, textNode('span', tr('research_recipes.override', '覆盖')));
      checkbox.addEventListener('change', () => { input.disabled = !checkbox.checked; });
      controls.append(overrideLabel, input);
      row.append(label, controls);
      host.appendChild(row);
      State.parameterControls.push({ definition, checkbox, input });
    });
  }

  function renderEvidence(recipe, draft) {
    const host = el('rr-evidence');
    if (!host) return;
    Array.from(host.children || []).forEach(child => {
      if (String(child.tagName || '').toLowerCase() !== 'legend') child.remove();
    });
    State.evidenceControls = [];
    const draftEvidence = draft && draft.evidence || {};
    (recipe.inputs || []).forEach(definition => {
      const row = document.createElement('div');
      row.className = 'rr-field';
      const label = textNode('span', localized(definition, 'label'), 'rr-field-label');
      label.appendChild(textNode('small', `${definition.input_id} · ${definition.evidence_type}`));
      const controls = document.createElement('div');
      controls.className = 'rr-evidence-control';
      const input = document.createElement('input');
      input.className = 'ipt';
      input.dataset.rrEvidence = definition.input_id;
      input.placeholder = tr('research_recipes.opaque_placeholder', '例如 evidence-001');
      input.setAttribute('aria-label', tr('research_recipes.evidence_id', '{label} opaque ID', {
        label: localized(definition, 'label'),
      }));
      const origin = document.createElement('select');
      origin.className = 'ipt';
      origin.dataset.rrOrigin = definition.input_id;
      origin.setAttribute('aria-label', tr('research_recipes.origin', '{label} 来源类型', {
        label: localized(definition, 'label'),
      }));
      origin.append(
        option('observed', tr('research_recipes.origin_observed', 'observed 观测')),
        option('imported', tr('research_recipes.origin_imported', 'imported 导入')),
        option('inferred', tr('research_recipes.origin_inferred', 'inferred 推断')),
      );
      const existing = draftEvidence[definition.input_id] || null;
      input.value = existing && existing.opaque_id || '';
      const defaultOrigin = ['publication_record', 'imported_record'].includes(
        definition.evidence_type) ? 'imported' : 'observed';
      origin.value = existing && existing.origin || defaultOrigin;
      controls.append(input, origin);
      row.append(label, controls);
      host.appendChild(row);
      State.evidenceControls.push({ definition, input, origin });
    });
  }

  function selectRecipe(recipeId, recipeVersion, options = {}) {
    if (State.catalogBusy && options.force !== true) return false;
    const key = recipeKey(recipeId, recipeVersion);
    const recipe = recipeByKey(key);
    if (!recipe) return false;
    if (key === State.selectedKey && options.focus && options.force !== true) {
      const currentButton = State.recipeButtons.find(
        item => item.dataset.recipeKey === key,
      );
      if (currentButton) currentButton.focus();
      return true;
    }
    State.selectionGeneration += 1;
    State.selectedKey = key;
    if (!options.preservePreview) {
      State.preview = null;
      State.previewKey = '';
      const previewPane = el('rr-preview');
      if (previewPane) previewPane.hidden = true;
    }
    const selection = el('rr-selection');
    if (selection) {
      clear(selection);
      selection.append(
        textNode('b', localized(recipe, 'label') || recipe.recipe_id),
        textNode('p', localized(recipe, 'summary')),
      );
    }
    renderParameters(recipe, options.draft || null);
    renderEvidence(recipe, options.draft || null);
    renderGallery();
    if (options.focus) {
      const selectedButton = State.recipeButtons.find(
        item => item.dataset.recipeKey === key,
      );
      if (selectedButton) selectedButton.focus();
    }
    refreshBusyState();
    return true;
  }

  function parseParameter(control) {
    const { definition, input } = control;
    if (definition.value_kind === 'boolean') return input.value === 'true';
    if (definition.value_kind === 'integer') return Number.parseInt(input.value, 10);
    if (definition.value_kind === 'number') return Number(input.value);
    if (definition.value_kind === 'number_list') {
      return String(input.value || '').split(',').map(item => Number(item.trim()));
    }
    return String(input.value || '').trim();
  }

  function readRequest() {
    const overrides = {};
    State.parameterControls.forEach(control => {
      if (control.checkbox.checked) overrides[control.definition.parameter_id] = parseParameter(control);
    });
    const evidence = {};
    State.evidenceControls.forEach(control => {
      const opaqueId = String(control.input.value || '').trim();
      if (!opaqueId) return;
      if (!OPAQUE_ID.test(opaqueId)) throw new Error('opaque_id_invalid');
      evidence[control.definition.input_id] = {
        ref_type: control.definition.evidence_type,
        opaque_id: opaqueId,
        origin: control.origin.value,
        revision_id: null,
      };
    });
    return { overrides, evidence };
  }

  function renderList(host, values, fallback) {
    if (!host) return;
    clear(host);
    const rows = values && values.length ? values : [fallback];
    rows.forEach(value => host.appendChild(textNode('li', value)));
  }

  function renderPreview(preview) {
    State.preview = preview || null;
    State.previewKey = preview
      ? recipeKey(preview.recipe_id, preview.recipe_version) : '';
    const pane = el('rr-preview');
    if (!pane || !preview) return;
    pane.hidden = false;
    const meta = el('rr-preview-meta');
    clear(meta);
    meta.append(
      textNode('span', `${preview.recipe_id}@${preview.recipe_version}`, 'rr-badge'),
      textNode('span', preview.status === 'preview_ready'
        ? tr('research_recipes.status_preview_ready', '预览就绪')
        : tr('research_recipes.status_blocked', '存在阻断'),
      `rr-badge ${preview.status === 'preview_ready' ? 'ready' : 'blocked'}`),
      textNode('span', tr('research_recipes.read_only', '只读'), 'rr-badge'),
    );
    const nodes = el('rr-node-list');
    clear(nodes);
    (preview.nodes || []).forEach(node => {
      const row = document.createElement('li');
      row.className = `rr-node ${node.status === 'blocked' ? 'blocked' : 'ready'}`;
      const heading = textNode('b', `${node.node_id} · ${node.task_kind}`);
      const detail = document.createElement('div');
      detail.className = 'rr-node-detail';
      detail.append(
        textNode('span', `${tr('research_recipes.depends', '依赖')}: ${(node.depends_on || []).join(', ') || '—'}`),
        textNode('span', `${tr('research_recipes.outputs', '输出')}: ${(node.outputs || []).join(', ')}`),
        textNode('span', `${tr('research_recipes.node_missing', '缺失')}: ${(node.missing_prerequisites || []).join(', ') || '—'}`),
      );
      row.append(heading, detail);
      nodes.appendChild(row);
    });
    renderList(el('rr-missing-list'), preview.missing_prerequisites,
      tr('research_recipes.none', '无'));
    renderList(el('rr-limits-list'), (preview.scientific_limits || []).map(item =>
      english() ? item.text_en : item.text_zh), tr('research_recipes.none', '无'));
    const references = el('rr-reference-list');
    clear(references);
    (preview.official_reference_urls || []).forEach(url => {
      const row = document.createElement('li');
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = url;
      row.appendChild(link);
      references.appendChild(row);
    });
    renderList(el('rr-source-list'), (preview.parameter_overrides || []).map(item =>
      `${item.parameter_id}: ${item.source === 'user_override'
        ? tr('research_recipes.source_user_override', '用户覆盖')
        : tr('research_recipes.source_recipe_default', '配方默认')}`),
    tr('research_recipes.none', '无'));
    const hash = el('rr-preview-hash');
    if (hash) hash.textContent = `${tr('research_recipes.hash', 'preview semantic hash')}: ${preview.preview_semantic_sha256}`;
  }

  async function requestPreview(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    const selectedKey = State.selectedKey;
    const recipe = recipeByKey(selectedKey);
    if (!recipe || State.catalogBusy || State.pendingPreviewKeys.has(selectedKey)) return false;
    const generation = State.selectionGeneration;
    const requestGeneration = ++State.previewRequestGeneration;
    let request;
    try {
      request = readRequest();
    } catch (_error) {
      setStatus(tr('research_recipes.invalid_opaque', '证据 ID 必须是 opaque ID，不能使用路径。'), true);
      return false;
    }
    State.pendingPreviewKeys.add(selectedKey);
    refreshBusyState();
    setStatus(tr('research_recipes.loading_preview', '正在解析版本与依赖…'));
    try {
      const result = await VCS.call(
        'research_recipe_preview', recipe.recipe_id, recipe.recipe_version, request,
      );
      const stillCurrent = () => (
        State.selectedKey === selectedKey
        && State.selectionGeneration === generation
        && State.previewRequestGeneration === requestGeneration
      );
      if (!stillCurrent()) return false;
      if (!result || result.ok !== true || !result.preview
          || recipeKey(result.preview.recipe_id, result.preview.recipe_version) !== selectedKey) {
        setStatus(tr('research_recipes.preview_failed', 'DAG 预览失败。'), true);
        return false;
      }
      renderPreview(result.preview);
      setStatus(result.preview.status === 'preview_ready'
        ? tr('research_recipes.ready_not_validated', '前置齐全；仍未经过科学 validated/accepted。')
        : tr('research_recipes.blocked', '预览已生成；缺失前置已标出。'));
      return true;
    } catch (_error) {
      if (State.selectedKey === selectedKey
          && State.selectionGeneration === generation
          && State.previewRequestGeneration === requestGeneration) {
        setStatus(tr('research_recipes.preview_failed', 'DAG 预览失败。'), true);
      }
      return false;
    } finally {
      State.pendingPreviewKeys.delete(selectedKey);
      refreshBusyState();
    }
  }

  async function load() {
    if (!el('rr-list') || State.catalogBusy) return false;
    setCatalogBusy(true);
    setStatus(tr('research_recipes.loading_catalog', '正在加载版本化配方…'));
    try {
      const result = await VCS.call('research_recipe_catalog');
      if (!result || result.ok !== true || !Array.isArray(result.recipes)) {
        setStatus(tr('research_recipes.catalog_failed', '研究配方目录不可用。'), true);
        return false;
      }
      State.catalog = result.recipes;
      State.loaded = true;
      renderGallery();
      if (!State.selectedKey && State.catalog.length) {
        selectRecipe(
          State.catalog[0].recipe_id, State.catalog[0].recipe_version, { force: true },
        );
      }
      setStatus(tr('research_recipes.catalog_ready', '配方目录已加载；尚未创建任何作业。'));
      return true;
    } catch (_error) {
      setStatus(tr('research_recipes.catalog_failed', '研究配方目录不可用。'), true);
      return false;
    } finally {
      setCatalogBusy(false);
    }
  }

  function redrawLanguage() {
    const draft = State.selectedKey ? readRequest() : null;
    const selectedRecipe = recipeByKey(State.selectedKey);
    renderGallery();
    if (selectedRecipe) selectRecipe(
      selectedRecipe.recipe_id, selectedRecipe.recipe_version,
      { draft, force: true, preservePreview: true },
    );
    if (State.preview) renderPreview(State.preview);
  }

  const form = el('rr-form');
  if (form) form.addEventListener('submit', requestPreview);
  document.addEventListener('vcs:route', event => {
    if (event.detail && event.detail.id === 'prepare-templates' && !State.loaded) load();
  });
  document.addEventListener('vcs:language', () => {
    try { redrawLanguage(); } catch (_error) { /* keep current scientific draft */ }
  });
  Promise.resolve(VCS.ready).then(load);

  window.ResearchRecipes = { load };
  if (window.__VCS_TEST__) {
    window.__VCS_RESEARCH_RECIPES_TEST__ = {
      State, load, selectRecipe, readRequest, renderGallery, renderPreview,
      requestPreview, recipeKey, recipeDomId,
      configure(value = {}) {
        if (Array.isArray(value.catalog)) State.catalog = value.catalog;
        if (value.selectedRecipe) State.selectedKey = recipeKey(
          value.selectedRecipe.recipe_id, value.selectedRecipe.recipe_version,
        );
      },
      snapshot() {
        const selectedRecipe = recipeByKey(State.selectedKey);
        return {
          selected_recipe: selectedRecipe ? {
            recipe_id: selectedRecipe.recipe_id,
            recipe_version: selectedRecipe.recipe_version,
          } : null,
          catalog_count: State.catalog.length,
          preview_hash: State.preview && State.preview.preview_semantic_sha256 || '',
          preview_recipe: State.preview ? {
            recipe_id: State.preview.recipe_id,
            recipe_version: State.preview.recipe_version,
          } : null,
          request: State.selectedKey ? readRequest() : { overrides: {}, evidence: {} },
        };
      },
    };
  }
})();
