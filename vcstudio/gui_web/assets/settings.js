// settings.js — 设置页:LLM 智能分析 / 报告提示词 / 数据路径 / 外观与自动化。
// 只依赖 app.js 暴露的 VCS.*(settings_get/llm_*/prompt_*/paths_save/theme_set/autopilot_save)。
// 全部插值走 VCS.esc(textarea/输入框用 .value 天然安全);零 emoji;动态文案双语。密钥绝不回显。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = (v == null ? '' : v); };
  const setSel = (id, v) => { const el = $(id); if (el) el.value = String(v); };
  const State = {
    scenarios: [], engines: [], tasks: [], keySaved: false,
    scenarioKey: 'full', engineKey: 'vasp', calculationKey: '',
    workspaceBusy: false,
    labPolicies: [], labPolicyRevision: 0, labPolicyPreview: null,
    labPolicySelection: null, labPolicyBusy: false,
  };
  let workspaceIntentGeneration = 0;
  let labPolicyIntentGeneration = 0;
  const draftSnapshots = new Map();
  const dirtyDraftScopes = new Set();
  const DRAFT_FIELDS = Object.freeze({
    'settings-llm': ['set-llm-provider', 'set-llm-baseurl', 'set-llm-model', 'set-llm-external'],
    'settings-llm-key': ['set-llm-key'],
    'settings-prompt': ['set-prompt'],
    'settings-paths': ['set-potcar', 'set-molecules', 'set-idw-lo', 'set-idw-hi'],
    'settings-autopilot': ['set-ap-on', 'set-ap-interval', 'set-ap-continue', 'set-ap-fetch', 'set-ap-report'],
    'settings-figures': ['set-fig-journal', 'set-fig-auto', 'set-fig-panel'],
    'settings-lab-policy': [
      'set-lab-policy', 'set-lab-applicability', 'set-lab-cores',
      'set-lab-walltime', 'set-lab-encut', 'set-lab-force',
    ],
  });
  const DRAFT_LABELS = Object.freeze({
    'settings-llm': ['settings.draft.llm', 'LLM 配置', 'LLM configuration'],
    'settings-llm-key': ['settings.draft.llm_key', '尚未保存的 API 密钥', 'Unsaved API key'],
    'settings-prompt': ['settings.draft.prompt', '报告分析提示词', 'Report-analysis prompt'],
    'settings-paths': ['settings.draft.paths', '数据路径', 'Data paths'],
    'settings-autopilot': ['settings.draft.autopilot', '自动托管设置', 'Managed-workflow settings'],
    'settings-figures': ['settings.draft.figures', '出图偏好', 'Figure preferences'],
    'settings-lab-policy': [
      'settings.draft.lab_policy', '实验室推荐策略', 'Laboratory recommendation policy',
    ],
  });
  const english = () => !!(VCS.i18n && VCS.i18n.lang === 'en');
  const tr = (key, params, zh, en) => {
    const fallback = english() ? (en || key) : (zh || key);
    return typeof VCS.t === 'function'
      ? VCS.t(key, params || {}, fallback)
      : String(fallback).replace(/\{([^{}]+)\}/g,
        (match, name) => Object.prototype.hasOwnProperty.call(params || {}, name)
          ? String(params[name]) : match);
  };
  function draftLabel(scope) {
    const record = DRAFT_LABELS[scope] || [
      'settings.draft.generic', '设置草稿', 'Settings draft',
    ];
    return tr(record[0], {}, record[1], record[2]);
  }
  function fieldSnapshot(id) {
    const element = $(id);
    if (!element) return null;
    if (element.type === 'checkbox' || element.type === 'radio') return !!element.checked;
    return String(element.value == null ? '' : element.value);
  }
  function applyFieldSnapshot(id, value) {
    const element = $(id);
    if (!element) return;
    if (element.type === 'checkbox' || element.type === 'radio') element.checked = !!value;
    else element.value = value == null ? '' : String(value);
  }
  function scopeSnapshot(scope) {
    return (DRAFT_FIELDS[scope] || []).map(id => [id, fieldSnapshot(id)]);
  }
  function captureDraftScope(scope) {
    draftSnapshots.set(scope, scopeSnapshot(scope));
    dirtyDraftScopes.delete(scope);
    if (VCS.unsaved && typeof VCS.unsaved.clear === 'function') VCS.unsaved.clear(scope);
  }
  function restoreDraftScope(scope) {
    const snapshot = draftSnapshots.get(scope) || [];
    snapshot.forEach(([id, value]) => applyFieldSnapshot(id, value));
    dirtyDraftScopes.delete(scope);
    if (VCS.unsaved && typeof VCS.unsaved.clear === 'function') VCS.unsaved.clear(scope);
    return true;
  }
  function updateDraftScope(scope) {
    const baseline = JSON.stringify(draftSnapshots.get(scope) || []);
    const current = JSON.stringify(scopeSnapshot(scope));
    if (baseline === current) {
      dirtyDraftScopes.delete(scope);
      if (VCS.unsaved && typeof VCS.unsaved.clear === 'function') VCS.unsaved.clear(scope);
      return;
    }
    dirtyDraftScopes.add(scope);
    if (VCS.unsaved && typeof VCS.unsaved.mark === 'function') {
      VCS.unsaved.mark(scope, draftLabel(scope), {
        discard: () => restoreDraftScope(scope),
      });
    }
  }
  function captureAllDraftScopes() {
    Object.keys(DRAFT_FIELDS).forEach(captureDraftScope);
  }
  function wireDraftTracking() {
    Object.entries(DRAFT_FIELDS).forEach(([scope, ids]) => {
      ids.forEach(id => {
        const element = $(id);
        if (!element) return;
        const eventName = element.tagName === 'SELECT' || element.type === 'checkbox'
          || element.type === 'radio' ? 'change' : 'input';
        element.addEventListener(eventName, () => updateDraftScope(scope));
      });
    });
  }
  function setWorkspaceControlsBusy(busy) {
    State.workspaceBusy = !!busy;
    const scenario = $('set-scenario');
    const engine = $('set-engine');
    const calculation = $('set-calculation');
    if (scenario) scenario.disabled = State.workspaceBusy;
    if (engine) engine.disabled = State.workspaceBusy || engine.options.length < 2;
    if (calculation) calculation.disabled = State.workspaceBusy || !State.tasks.length;
    const region = $('set-workflow-card');
    if (region) region.setAttribute('aria-busy', State.workspaceBusy ? 'true' : 'false');
  }
  function beginWorkspaceIntent() {
    const generation = ++workspaceIntentGeneration;
    setWorkspaceControlsBusy(true);
    return generation;
  }
  function currentWorkspaceIntent(generation) {
    return generation === workspaceIntentGeneration;
  }
  function endWorkspaceIntent(generation) {
    if (currentWorkspaceIntent(generation)) setWorkspaceControlsBusy(false);
  }
  const rawError = (result, key, zh, en) =>
    (result && result.error) || tr(key, {}, zh, en);
  const localField = (row, zhField, enField, enFallback) => {
    const item = row || {};
    if (english()) return item[enField] || enFallback || '';
    return item[zhField] || '';
  };
  function appendSettingLog(line, cls) {
    const box = $('settings-log');
    if (!box) { VCS.log(line, cls || ''); return null; }
    const now = new Date();
    const time = document.createElement('span');
    time.className = 't';
    time.textContent = [now.getHours(), now.getMinutes(), now.getSeconds()]
      .map(value => String(value).padStart(2, '0')).join(':');
    const message = document.createElement('span');
    if (cls) message.className = cls;
    message.textContent = String(line);
    const row = document.createElement('div');
    row.append(time, message);
    const head = box.querySelector(':scope > .head');
    box.insertBefore(row, head ? head.nextSibling : box.firstChild);
    const rows = box.querySelectorAll(':scope > div:not(.head)');
    for (let index = rows.length - 1; index >= 200; index--) rows[index].remove();
    return { row, message };
  }

  function logLocalizedSetting(key, params, zh, en, cls) {
    const entry = appendSettingLog(tr(key, params || {}, zh, en), cls || '');
    if (!entry) return;
    const { row, message } = entry;
    row.dataset.i18nLogKey = key;
    row.dataset.i18nLogParams = JSON.stringify(params || {});
    row.dataset.i18nLogFallbackZh = zh || key;
    row.dataset.i18nLogFallbackEn = en || key;
    message.dataset.i18nLogMessage = '';
  }

  function redrawLocalizedSettingLogs() {
    const box = $('settings-log');
    if (!box) return;
    box.querySelectorAll('[data-i18n-log-key]').forEach(row => {
      const message = row.querySelector('[data-i18n-log-message]');
      if (!message) return;
      let params = {};
      try { params = JSON.parse(row.dataset.i18nLogParams || '{}'); } catch (_) { params = {}; }
      message.textContent = tr(row.dataset.i18nLogKey, params,
        row.dataset.i18nLogFallbackZh, row.dataset.i18nLogFallbackEn);
    });
  }

  // 引擎能力 DTO 目前也供生成页使用，不能为翻译改写科学配置。
  // 设置页只按 engine key 提供展示层英文，边界/任务白名单仍以服务端为准。
  const ENGINE_EN = Object.freeze({
    vasp: {
      support_label: 'Full workflow',
      summary: 'Primary engine with the complete VASP task catalog, four-file input set, managed workflow, and result tools.',
      limitations: [],
    },
    cp2k: {
      support_label: 'File-level workflow',
      summary: 'Generates cp2k.inp, registers and submits the job, and parses cp2k.out; CP2K and GTH data files must be supplied by the user.',
      limitations: [
        'CUTOFF is the GTH density-grid cutoff in Ry and cannot be converted from VASP ENCUT.',
        'The generic adapter currently supports geometry optimization, single-point energy, and frequencies only; absolute energies cannot be compared across engines.',
      ],
    },
    gaussian: {
      support_label: 'Molecular file-level workflow',
      summary: 'For isolated molecules: generates .gjf, registers and submits the job, and parses .log.',
      limitations: [
        'Only isolated molecules and clusters are supported; periodic slabs or bulk systems require VASP, CP2K, or CASTEP.',
        'Absolute energies from VASP plane-wave/PAW and Gaussian basis-set calculations are not directly comparable.',
      ],
    },
    castep: {
      support_label: 'File-level workflow',
      summary: 'Generates .cell/.param, registers and submits the job, and parses .castep; a CASTEP license and pseudopotentials must be supplied by the user.',
      limitations: [
        'The generic adapter currently supports geometry optimization, single-point energy, and frequencies only.',
        'CASTEP pseudopotentials differ from VASP PAW datasets; converge the cutoff independently and do not compare absolute energies across engines.',
      ],
    },
  });
  const GENERIC_TASK_EN = Object.freeze({
    relax: {
      name: 'Geometry Optimization',
      description: 'Optimize the geometry with {engine} using its engine-specific fields and generated input contract.',
    },
    static: {
      name: 'Single-Point Energy',
      description: 'Run a fixed-geometry single-point energy calculation with {engine}.',
    },
    freq: {
      name: 'Vibrational Frequency Analysis',
      description: 'Calculate vibrational frequencies with {engine}, then inspect imaginary modes and the available thermochemistry evidence.',
    },
  });

  // 提供商预设 → [base_url, model](自定义为空,不覆盖用户已填)
  const PROVIDERS = {
    openai: ['https://api.openai.com/v1/chat/completions', 'gpt-4o'],
    deepseek: ['https://api.deepseek.com/v1/chat/completions', 'deepseek-chat'],
    glm: ['https://open.bigmodel.cn/api/paas/v4/chat/completions', 'glm-4-plus'],
    kimi: ['https://api.moonshot.cn/v1/chat/completions', 'moonshot-v1-8k'],
    custom: ['', ''],
  };

  // ── 实验室策略:只预览/确认用户级推荐，不改作业或触发提交 ───────────────
  function setLabPolicyBusy(busy) {
    State.labPolicyBusy = !!busy;
    ['set-lab-preview', 'set-lab-confirm'].forEach(id => {
      const button = $(id);
      if (button) button.disabled = !!busy || !State.labPolicies.length;
    });
    const card = $('set-lab-policy-card');
    if (card) card.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function selectedLabPolicy() {
    const id = val('set-lab-policy');
    return State.labPolicies.find(item => item.id === id) || null;
  }

  function renderLabPolicyOptions(selectedId) {
    const policy = $('set-lab-policy');
    if (!policy) return;
    const wanted = selectedId || policy.value;
    policy.innerHTML = State.labPolicies.map(item => {
      const label = english() ? item.label_en : item.label_zh;
      return `<option value="${VCS.esc(item.id)}">${VCS.esc(label || item.id)}</option>`;
    }).join('');
    if (State.labPolicies.some(item => item.id === wanted)) policy.value = wanted;
    renderLabApplicability();
  }

  function renderLabApplicability(selectedValue) {
    const element = $('set-lab-applicability');
    const policy = selectedLabPolicy();
    if (!element) return;
    const wanted = selectedValue || element.value;
    const labels = {
      bulk: tr('settings.lab.applicability.bulk', {}, '体相', 'Bulk'),
      slab: tr('settings.lab.applicability.slab', {}, '表面', 'Slab'),
      adsorption: tr('settings.lab.applicability.adsorption', {}, '吸附', 'Adsorption'),
      molecule: tr('settings.lab.applicability.molecule', {}, '分子', 'Molecule'),
    };
    const values = (policy && policy.applicability) || [];
    element.innerHTML = values.map(value =>
      `<option value="${VCS.esc(value)}">${VCS.esc(labels[value] || value)}</option>`).join('');
    if (values.indexOf(wanted) >= 0) element.value = wanted;
  }

  function labOverrides() {
    const fields = [
      ['set-lab-cores', 'cores', value => Number.parseInt(value, 10)],
      ['set-lab-walltime', 'walltime', value => value],
      ['set-lab-encut', 'encut_enmax_multiplier', value => Number(value)],
      ['set-lab-force', 'force_tolerance_eV_A', value => Number(value)],
    ];
    const overrides = {};
    fields.forEach(([id, key, parse]) => {
      const raw = val(id);
      if (raw !== '') overrides[key] = parse(raw);
    });
    return overrides;
  }

  function labPolicyRequest() {
    return {
      policy_id: val('set-lab-policy'),
      applicability: val('set-lab-applicability') || null,
      overrides: labOverrides(),
    };
  }

  function renderLabPolicySummary() {
    const current = $('set-lab-current');
    const revision = $('set-lab-revision');
    if (revision) revision.textContent = tr(
      'settings.lab.revision', { revision: State.labPolicyRevision },
      '当前修订：{revision}', 'Current revision: {revision}');
    if (!current) return;
    const selection = State.labPolicySelection;
    current.textContent = selection
      ? tr('settings.lab.current', {
        policy: selection.policy_id, actor: selection.actor,
        confirmed_at: selection.confirmed_at,
      }, '已确认：{policy}；确认人 {actor}；{confirmed_at}',
      'Confirmed: {policy}; actor {actor}; {confirmed_at}')
      : tr('settings.lab.current_none', {}, '尚未确认策略', 'No policy has been confirmed');
  }

  function renderLabPolicyPreview() {
    const box = $('set-lab-preview-out');
    if (!box) return;
    const preview = State.labPolicyPreview;
    box.hidden = !preview;
    box.replaceChildren();
    if (!preview) return;
    const rows = [
      [tr('settings.lab.preview.policy', {}, '策略', 'Policy'),
        `${preview.policy_id} v${preview.policy_version}`],
      [tr('settings.lab.preview.applicability', {}, '适用对象', 'Applicability'),
        preview.applicability || '—'],
      [tr('settings.lab.preview.method', {}, '方法推荐', 'Method recommendation'),
        JSON.stringify(preview.method || {})],
      [tr('settings.lab.preview.resources', {}, '资源推荐', 'Resource recommendation'),
        JSON.stringify(preview.resources || {})],
      [tr('settings.lab.preview.checks', {}, '必做检查', 'Required checks'),
        (preview.required_checks || []).join(', ')],
      [tr('settings.lab.preview.hash', {}, '语义哈希', 'Semantic hash'),
        preview.semantic_sha256 || '—'],
    ];
    rows.forEach(([term, value]) => {
      const row = document.createElement('div');
      const label = document.createElement('b');
      const output = document.createElement('span');
      label.textContent = term;
      output.textContent = String(value);
      row.append(label, output);
      box.append(row);
    });
  }

  async function loadLabPolicies() {
    const generation = ++labPolicyIntentGeneration;
    setLabPolicyBusy(true);
    try {
      const [catalog, snapshot] = await Promise.all([
        VCS.call('lab_policy_catalog'), VCS.call('lab_policy_read'),
      ]);
      if (generation !== labPolicyIntentGeneration) return false;
      if (!(catalog && catalog.ok) || !(snapshot && snapshot.ok)) {
        const failed = !(catalog && catalog.ok) ? catalog : snapshot;
        throw new Error(rawError(failed, 'common.unknown_error', '未知错误', 'Unknown error'));
      }
      State.labPolicies = (catalog.catalog && catalog.catalog.policies) || [];
      State.labPolicyRevision = Number(snapshot.revision || 0);
      State.labPolicySelection = snapshot.selection || null;
      const selected = State.labPolicySelection;
      renderLabPolicyOptions(selected && selected.policy_id);
      if (selected) {
        renderLabApplicability(selected.applicability);
        const overrides = selected.overrides || {};
        setVal('set-lab-cores', overrides.cores);
        setVal('set-lab-walltime', overrides.walltime);
        setVal('set-lab-encut', overrides.encut_enmax_multiplier);
        setVal('set-lab-force', overrides.force_tolerance_eV_A);
      }
      renderLabPolicySummary();
      return true;
    } catch (error) {
      if (generation !== labPolicyIntentGeneration) return false;
      State.labPolicies = [];
      logLocalizedSetting('settings.lab.load_failed', { error: String(error.message || error) },
        '读取实验室策略失败：{error}', 'Failed to load laboratory policies: {error}', 'failc');
      return false;
    } finally {
      if (generation === labPolicyIntentGeneration) setLabPolicyBusy(false);
    }
  }

  async function previewLabPolicy() {
    const generation = ++labPolicyIntentGeneration;
    setLabPolicyBusy(true);
    try {
      const result = await VCS.call('lab_policy_preview', labPolicyRequest());
      if (generation !== labPolicyIntentGeneration) return false;
      if (!(result && result.ok)) throw new Error(rawError(
        result, 'common.unknown_error', '未知错误', 'Unknown error'));
      State.labPolicyPreview = result.preview;
      State.labPolicyRevision = Number(result.revision || 0);
      State.labPolicySelection = result.current_selection || null;
      renderLabPolicyPreview();
      renderLabPolicySummary();
      logLocalizedSetting('settings.lab.preview_ready', {},
        '实验室策略预览已更新；尚未确认，也不会修改作业',
        'Laboratory policy preview updated; it is unconfirmed and no job was changed', 'okc');
      return true;
    } catch (error) {
      if (generation !== labPolicyIntentGeneration) return false;
      State.labPolicyPreview = null;
      renderLabPolicyPreview();
      logLocalizedSetting('settings.lab.preview_failed', { error: String(error.message || error) },
        '策略预览失败：{error}', 'Policy preview failed: {error}', 'failc');
      return false;
    } finally {
      if (generation === labPolicyIntentGeneration) setLabPolicyBusy(false);
    }
  }

  async function confirmLabPolicy() {
    if (typeof window.confirm === 'function' && !window.confirm(tr(
      'settings.lab.confirm_prompt', {},
      '确认保存这份“仅推荐”策略？它不会修改或提交任何作业。',
      'Save this recommendation-only policy? It will not modify or submit any job.'))) return false;
    const generation = ++labPolicyIntentGeneration;
    setLabPolicyBusy(true);
    try {
      const result = await VCS.call(
        'lab_policy_confirm', labPolicyRequest(), State.labPolicyRevision, true);
      if (generation !== labPolicyIntentGeneration) return false;
      if (!(result && result.ok)) {
        if (result && result.conflict) {
          State.labPolicyRevision = Number(result.revision || 0);
          State.labPolicySelection = result.selection || null;
          renderLabPolicySummary();
          logLocalizedSetting('settings.lab.conflict', {},
            '策略已被另一会话更新；已显示权威修订，请检查草稿后重试',
            'Another session updated the policy; the authoritative revision is shown. Review the draft and retry', 'warnc');
          return false;
        }
        throw new Error(rawError(result, 'common.unknown_error', '未知错误', 'Unknown error'));
      }
      State.labPolicyRevision = Number(result.revision || 0);
      State.labPolicySelection = result.selection || null;
      renderLabPolicySummary();
      captureDraftScope('settings-lab-policy');
      logLocalizedSetting('settings.lab.confirmed', {},
        '已确认用户级实验室推荐策略；既有作业未改变，未触发提交',
        'User-level laboratory recommendation confirmed; existing jobs are unchanged and no submission was triggered', 'okc');
      return true;
    } catch (error) {
      if (generation !== labPolicyIntentGeneration) return false;
      logLocalizedSetting('settings.lab.confirm_failed', { error: String(error.message || error) },
        '策略确认失败：{error}', 'Policy confirmation failed: {error}', 'failc');
      return false;
    } finally {
      if (generation === labPolicyIntentGeneration) setLabPolicyBusy(false);
    }
  }

  // ── 载入 / 回填 ──────────────────────────────────────────────────────────────
  async function load() {
    const s = await VCS.call('settings_get');
    if (!s || s.ok === false || s.error) {
      logLocalizedSetting('settings.status.load_failed',
        { error: rawError(s, 'common.unknown_error', '未知错误', 'Unknown error') },
        '读取设置失败：{error}', 'Failed to load settings: {error}', 'failc');
      return;
    }
    const llm = s.llm || {};
    setVal('set-llm-baseurl', llm.base_url);
    setVal('set-llm-model', llm.model);
    if ($('set-llm-external')) $('set-llm-external').checked = !!llm.allow_external;
    State.keySaved = !!llm.key_saved;
    renderKeyState(State.keySaved);
    guessProvider(llm.base_url, llm.model);

    const prompt = s.prompt || {};
    setVal('set-prompt', prompt.text);

    const paths = s.paths || {};
    setVal('set-potcar', paths.potcar_lib_root);
    setVal('set-molecules', paths.lis_molecules_dir);
    const iw = paths.ideal_window || [];
    setVal('set-idw-lo', iw.length ? iw[0] : '');
    setVal('set-idw-hi', iw.length > 1 ? iw[1] : '');

    const ui = s.ui || {};
    selectTheme(ui.theme || 'classic', false);
    selectDensity(ui.density || 'standard', false);
    // 自动托管必须显式开启；旧配置缺少该键时保持关闭，避免误操作台账中的其它任务。
    if ($('set-ap-on')) $('set-ap-on').checked = ui.autopilot === true;
    setSel('set-ap-interval', ui.poll_interval || 10);
    if ($('set-ap-continue')) $('set-ap-continue').checked = ui.autopilot_continue !== false;
    if ($('set-ap-fetch')) $('set-ap-fetch').checked = ui.autopilot_fetch !== false;
    if ($('set-ap-report')) $('set-ap-report').checked = ui.autopilot_report !== false;

    const fig = s.figures || {};
    setSel('set-fig-journal', fig.journal_style || 'nature');
    if ($('set-fig-auto')) $('set-fig-auto').checked = fig.auto_figures !== false;
    if ($('set-fig-panel')) $('set-fig-panel').checked = fig.multi_panel !== false;

    await loadLabPolicies();
    captureAllDraftScopes();
    loadWorkspace();          // 界面语言 + 工作模式 + 本次计算类型
  }

  async function saveFigPrefs() {
    const r = await VCS.call('figure_prefs_save',
      ($('set-fig-journal') && $('set-fig-journal').value) || 'nature',
      $('set-fig-auto') ? $('set-fig-auto').checked : true,
      $('set-fig-panel') ? $('set-fig-panel').checked : true);
    if (r && r.ok) {
      captureDraftScope('settings-figures');
      logLocalizedSetting('settings.status.figure_saved', {},
        '已保存出图偏好', 'Figure preferences saved', 'okc');
      VCS.toast(tr('settings.status.figure_saved', {},
        '已保存出图偏好', 'Figure preferences saved'));
    } else {
      logLocalizedSetting('settings.status.figure_save_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '保存出图偏好失败：{error}', 'Failed to save figure preferences: {error}', 'failc');
    }
  }

  function renderKeyState(saved) {
    const el = $('set-llm-keystate');
    if (!el) return;
    el.className = 'keystate ' + (saved ? 'saved' : 'unset');
    el.textContent = saved
      ? tr('common.saved', {}, '已保存', 'Saved')
      : tr('settings.llm.key_unset', {}, '未设置', 'Not set');
  }

  // base_url/model 与某预设完全一致 → 选中该预设,否则「自定义」
  function guessProvider(baseUrl, model) {
    const sel = $('set-llm-provider');
    if (!sel) return;
    let hit = 'custom';
    Object.keys(PROVIDERS).forEach(k => {
      if (k !== 'custom' && PROVIDERS[k][0] === (baseUrl || '') && PROVIDERS[k][1] === (model || '')) hit = k;
    });
    sel.value = hit;
  }

  // ── LLM 卡片 ────────────────────────────────────────────────────────────────
  function onProviderChange() {
    const sel = $('set-llm-provider');
    if (!sel) return;
    const preset = PROVIDERS[sel.value];
    if (!preset || sel.value === 'custom') return;   // 自定义:不动用户已填
    setVal('set-llm-baseurl', preset[0]);
    setVal('set-llm-model', preset[1]);
  }

  async function saveLlm() {
    const r = await VCS.call('llm_save', val('set-llm-baseurl'), val('set-llm-model'),
      $('set-llm-external') ? $('set-llm-external').checked : false);
    if (!(r && r.ok)) {
      logLocalizedSetting('settings.status.llm_save_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '保存 LLM 配置失败：{error}', 'Failed to save LLM settings: {error}', 'failc');
      return;
    }
    const external = $('set-llm-external') && $('set-llm-external').checked;
    logLocalizedSetting(
      external ? 'settings.status.llm_saved_external' : 'settings.status.llm_saved_local', {},
      external ? '已保存 LLM 端点/模型（已允许项目数据外发）'
        : '已保存 LLM 端点/模型（项目数据不外发）',
      external ? 'LLM endpoint/model saved (project-data transfer is allowed)'
        : 'LLM endpoint/model saved (project data stays local)', 'okc');
    captureDraftScope('settings-llm');
    VCS.toast(tr('common.saved', {}, '已保存', 'Saved'));
  }

  async function saveKey() {
    const key = val('set-llm-key');
    if (!key) {
      VCS.toast(tr('settings.status.key_required', {}, '请先粘贴密钥', 'Paste an API key first'), 'fail');
      return;
    }
    const r = await VCS.call('llm_key_save', key);
    if (!(r && r.ok)) {
      logLocalizedSetting('settings.status.key_save_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '保存密钥失败：{error}', 'Failed to save API key: {error}', 'failc');
      return;
    }
    setVal('set-llm-key', '');            // 存后立即清空输入,绝不回显
    captureDraftScope('settings-llm-key');
    State.keySaved = true;
    renderKeyState(true);
    logLocalizedSetting('settings.status.key_saved_securely', {},
      'API 密钥已存入系统凭据库',
      'API key saved to the system credential store', 'okc');
    VCS.toast(tr('settings.status.key_saved', {}, '密钥已保存', 'API key saved'));
  }

  async function testLlm() {
    const btn = $('set-llm-test');
    if (btn) btn.disabled = true;
    logLocalizedSetting('settings.status.llm_testing', {},
      '测试 LLM 连接（极小请求）…',
      'Testing the LLM connection with a minimal request…');
    try {
      const r = await VCS.call('llm_test', val('set-llm-baseurl'), val('set-llm-model'));
      if (r && r.ok) {
        logLocalizedSetting('settings.status.llm_ok', {},
          'LLM 连接正常', 'LLM connection succeeded', 'okc');
        VCS.toast(tr('settings.status.llm_ok', {},
          'LLM 连接正常', 'LLM connection succeeded'));
      } else {
        logLocalizedSetting('settings.status.llm_failed',
          { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
          'LLM 连接失败：{error}', 'LLM connection failed: {error}', 'failc');
        VCS.toast(tr('settings.status.connection_failed', {}, '连接失败', 'Connection failed'), 'fail');
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 提示词卡片 ──────────────────────────────────────────────────────────────
  async function savePrompt() {
    const t = $('set-prompt') ? $('set-prompt').value : '';
    if (!t.trim()) {
      VCS.toast(tr('settings.status.prompt_required', {},
        '提示词不能为空', 'The prompt cannot be empty'), 'fail');
      return;
    }
    const r = await VCS.call('prompt_save', t);
    if (!(r && r.ok)) {
      logLocalizedSetting('settings.status.prompt_save_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '保存提示词失败：{error}', 'Failed to save prompt: {error}', 'failc');
      return;
    }
    logLocalizedSetting('settings.status.prompt_saved', {},
      '已保存自定义报告分析提示词',
      'Custom report-analysis prompt saved', 'okc');
    captureDraftScope('settings-prompt');
    VCS.toast(tr('common.saved', {}, '已保存', 'Saved'));
  }

  async function resetPrompt() {
    const ok = await VCS.confirm(tr('settings.status.prompt_reset_confirm', {},
      '恢复内置发刊级默认提示词？（将丢弃当前自定义）',
      'Restore the built-in publication-grade prompt? Your custom prompt will be discarded.'));
    if (!ok) return;
    const r = await VCS.call('prompt_reset');
    if (!(r && r.ok)) {
      logLocalizedSetting('settings.status.prompt_reset_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '恢复默认失败：{error}', 'Failed to restore the default prompt: {error}', 'failc');
      return;
    }
    setVal('set-prompt', r.text);
    captureDraftScope('settings-prompt');
    logLocalizedSetting('settings.status.prompt_reset', {},
      '已恢复默认提示词', 'Default prompt restored', 'okc');
    VCS.toast(tr('settings.status.default_restored', {}, '已恢复默认', 'Defaults restored'));
  }

  // ── 数据路径卡片 ────────────────────────────────────────────────────────────
  async function pickDirInto(id) {
    const r = await VCS.call('pick_dir');
    if (r && r.error) {
      logLocalizedSetting('settings.status.directory_failed', { error: r.error },
        '选择目录失败：{error}', 'Failed to select directory: {error}', 'failc');
      return;
    }
    if (r && r.path) {
      setVal(id, r.path);
      updateDraftScope('settings-paths');
    }
  }

  async function savePaths() {
    const r = await VCS.call('paths_save', val('set-potcar'), val('set-molecules'),
      val('set-idw-lo'), val('set-idw-hi'));
    if (!(r && r.ok)) {
      logLocalizedSetting('settings.status.paths_save_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '保存数据路径失败：{error}', 'Failed to save data paths: {error}', 'failc');
      return;
    }
    logLocalizedSetting('settings.status.paths_saved', {},
      '已保存数据路径（赝势库 / 分子库 / 理想窗口）',
      'Data paths saved (pseudopotential library / molecule library / ideal window)', 'okc');
    captureDraftScope('settings-paths');
    VCS.toast(tr('common.saved', {}, '已保存', 'Saved'));
  }

  // ── 外观与自动化卡片 ────────────────────────────────────────────────────────
  function selectTheme(name, persist) {
    const row = $('set-theme-row');
    if (row) row.querySelectorAll('.theme-opt').forEach(
      o => o.classList.toggle('sel', o.dataset.theme === name));
    if (persist) {
      VCS.themeApply(name);                       // 即时生效 + 记忆(防闪烁)
      VCS.call('theme_set', name).then(r => {
        if (r && r.ok === false) {
          logLocalizedSetting('settings.status.theme_failed',
            { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
            '切换主题失败：{error}', 'Failed to change theme: {error}', 'failc');
          return;
        }
        logLocalizedSetting('settings.status.theme_changed', { name },
          '主题已切换：{name}', 'Theme changed: {name}', 'okc');
      });
    }
  }

  function selectDensity(name, persist) {
    const density = VCS.densityApply ? VCS.densityApply(name) : 'standard';
    document.querySelectorAll('input[name="set-density"]').forEach(input => {
      input.checked = input.value === density;
    });
    if (!persist) return;
    VCS.call('density_set', density).then(r => {
      if (r && r.ok) {
        logLocalizedSetting('settings.status.density_changed', { density },
          '视觉密度已切换：{density}', 'Visual density changed: {density}', 'okc');
        return;
      }
      logLocalizedSetting('settings.status.density_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '保存视觉密度失败：{error}', 'Failed to save visual density: {error}', 'failc');
      // 服务端拒绝时重新读取权威设置，避免本地首帧缓存长期分叉。
      VCS.call('settings_get').then(s => {
        const ui = (s && s.ui) || {};
        selectDensity(ui.density || 'standard', false);
      });
    });
  }

  async function saveAutopilot() {
    const r = await VCS.call('autopilot_save',
      $('set-ap-on') ? $('set-ap-on').checked : true,
      parseInt(val('set-ap-interval'), 10) || 10,
      $('set-ap-continue') ? $('set-ap-continue').checked : true,
      $('set-ap-fetch') ? $('set-ap-fetch').checked : true,
      $('set-ap-report') ? $('set-ap-report').checked : true);
    if (!(r && r.ok)) {
      logLocalizedSetting('settings.status.managed_save_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '保存自动托管设置失败：{error}', 'Failed to save managed-workflow settings: {error}', 'failc');
      return;
    }
    logLocalizedSetting('settings.status.managed_saved', {},
      '已保存自动托管设置', 'Managed-workflow settings saved', 'okc');
    captureDraftScope('settings-autopilot');
    VCS.toast(tr('common.saved', {}, '已保存', 'Saved'));
    if (VCS.pipeline && typeof VCS.pipeline.reconfigure === 'function') VCS.pipeline.reconfigure();
  }

  // ── 界面语言 + 工作模式 + 本次计算类型 ──────────────────────────────────────
  const languageName = lang => lang === 'zh'
    ? tr('settings.lang.zh_option', {}, '中文（简体）', 'Simplified Chinese')
    : lang === 'en'
      ? tr('settings.lang.en_option', {}, '英语', 'English') : lang;
  const scenarioName = scenario => localField(
    scenario, 'name', 'name_en', (scenario && scenario.key) || '');
  const scenarioDescription = scenario => localField(
    scenario, 'description', 'description_en', '');
  const taskName = task => {
    const row = task || {};
    const generic = english() && row.engine !== 'vasp' && GENERIC_TASK_EN[row.key];
    return generic ? generic.name : localField(row, 'name_zh', 'name_en', row.key || '');
  };
  const taskDescription = task => {
    const row = task || {};
    const generic = english() && row.engine !== 'vasp' && GENERIC_TASK_EN[row.key];
    if (generic) return generic.description.replace(
      '{engine}', String(row.engine || '').toUpperCase());
    return localField(row, 'description', 'description_en', '');
  };
  const taskRequires = task => {
    const fallback = tr('settings.calculation.requires_fallback', {},
      '按页面提示准备', 'Follow the on-screen prerequisites');
    return english() ? ((task && task.requires_en) || fallback)
      : ((task && task.requires) || fallback);
  };
  const taskOutputs = task => {
    const fallback = tr('settings.calculation.outputs_fallback', {},
      '按任务生成', 'Generated according to the task');
    return english() ? ((task && task.outputs_en) || fallback)
      : ((task && task.outputs) || fallback);
  };
  const taskNextAction = task => {
    const fallback = tr('settings.calculation.next_fallback', {},
      '在任务页监控、下载并生成报告。',
      'Monitor the calculation on Jobs, download its results, and open the matching result or report tool.');
    if (!english()) {
      return (task && task.next_action) || fallback;
    }
    if (task && task.next_action_en) return task.next_action_en;
    if (task && task.engine && task.engine !== 'vasp') {
      return tr('settings.calculation.next_non_vasp',
        { engine: String(task.engine).toUpperCase() },
        '在生成输入页填写 {engine} 专属字段，生成并纳管后到任务页提交。',
        'Fill the {engine}-specific fields on Generate Inputs, register the generated job, then submit it from Jobs.');
    }
    return fallback;
  };

  function renderScenarioOptions(selectedKey) {
    const sel = $('set-scenario');
    if (!sel) return;
    const primary = State.scenarios.filter(s => s.primary);
    const advanced = State.scenarios.filter(s => !s.primary);
    const opts = rows => rows.map(s =>
      `<option value="${VCS.esc(s.key)}">${VCS.esc(scenarioName(s))}</option>`).join('');
    const primaryLabel = tr('settings.scenario.primary_group', {}, '常用模式', 'Common workflows');
    const advancedLabel = tr('settings.scenario.advanced_group', {}, '更多专业预设', 'More specialist presets');
    sel.innerHTML = `<optgroup label="${VCS.esc(primaryLabel)}">${opts(primary)}</optgroup>` +
      (advanced.length
        ? `<optgroup label="${VCS.esc(advancedLabel)}">${opts(advanced)}</optgroup>` : '');
    if (State.scenarios.some(s => s.key === selectedKey)) sel.value = selectedKey;
    State.scenarioKey = sel.value || selectedKey || 'full';
  }

  function renderEngineOptions(selectedKey) {
    const sel = $('set-engine');
    if (!sel) return;
    const recommended = tr('common.recommended_suffix', {}, '（推荐）', ' (recommended)');
    const adapter = tr('settings.engine.file_adapter_suffix', {},
      '（文件级适配）', ' (file-level adapter)');
    sel.innerHTML = State.engines.map(row =>
      `<option value="${VCS.esc(row.key)}">${VCS.esc(row.name || String(row.key).toUpperCase())}` +
      `${VCS.esc(row.key === 'vasp' ? recommended : adapter)}</option>`).join('') ||
      `<option value="vasp">VASP${VCS.esc(recommended)}</option>`;
    const active = State.engines.some(row => row.key === selectedKey)
      ? selectedKey : sel.options[0].value;
    sel.value = active;
    sel.disabled = State.workspaceBusy || sel.options.length < 2;
    State.engineKey = active;
  }

  function renderCalculationOptions(selectedKey) {
    const sel = $('set-calculation');
    if (!sel) return;
    const groups = [];
    State.tasks.forEach(task => {
      const identity = task.category || task.category_en || 'other';
      let group = groups.find(item => item.identity === identity);
      if (!group) {
        group = {
          identity,
          label: localField(task, 'category', 'category_en',
            tr('settings.calculation.other_category', {}, '其他', 'Other')),
          tasks: [],
        };
        groups.push(group);
      }
      group.tasks.push(task);
    });
    sel.innerHTML = groups.map(group =>
      `<optgroup label="${VCS.esc(group.label)}">` + group.tasks.map(task =>
        `<option value="${VCS.esc(task.key)}">${VCS.esc(taskName(task))}</option>`).join('') +
      '</optgroup>').join('') || `<option value="">${VCS.esc(tr(
        'settings.calculation.none', {}, '当前模式没有可选任务',
        'No calculations are available in this workflow'))}</option>`;
    if (State.tasks.some(task => task.key === selectedKey)) sel.value = selectedKey;
    else if (State.tasks.length) sel.value = State.tasks[0].key;
    State.calculationKey = sel.value || '';
    sel.disabled = State.workspaceBusy || !State.tasks.length;
  }

  async function loadWorkspace(intentGeneration = null) {
    const ownsIntent = intentGeneration == null;
    const generation = ownsIntent ? beginWorkspaceIntent() : intentGeneration;
    try {
      // 语言下拉
      const langSel = $('set-lang');
      if (langSel) {
        const g = await VCS.call('lang_get');
        if (!currentWorkspaceIntent(generation)) return false;
        const avail = (g && g.available) || ['zh', 'en'];
        langSel.innerHTML = avail.map(l =>
          `<option value="${l}">${VCS.esc(languageName(l))}</option>`).join('');
        langSel.value = (g && g.lang) || 'zh';
      }
      // 场景下拉
      const scSel = $('set-scenario');
      if (scSel) {
        const [list, cur] = await Promise.all([
          VCS.call('scenario_list'), VCS.call('scenario_get')]);
        if (!currentWorkspaceIntent(generation)) return false;
        State.scenarios = (list && list.scenarios) || [];
        const curKey = (cur && cur.scenario && cur.scenario.key) || 'full';
        renderScenarioOptions(curKey);
        renderScenarioDesc(curKey);
        return await loadEngines(curKey, generation);
      }
      return true;
    } finally {
      if (ownsIntent) endWorkspaceIntent(generation);
    }
  }

  async function loadEngines(scenarioKey, generation = workspaceIntentGeneration) {
    const sel = $('set-engine');
    if (!sel) return loadCalculations(scenarioKey, 'vasp', generation);
    const [listed, current] = await Promise.all([
      VCS.call('engine_list', scenarioKey || null), VCS.call('engine_get')]);
    if (!currentWorkspaceIntent(generation)) return false;
    State.engines = ((listed && listed.engines) || []).filter(row => row.visible !== false);
    State.engines.sort((a, b) => (a.key === 'vasp' ? -1 : b.key === 'vasp' ? 1 : 0));
    const active = (current && current.engine) || (listed && listed.default) || 'vasp';
    renderEngineOptions(active);
    const capability = (current && current.engine === sel.value && current.capability) ||
      ((State.engines.find(row => row.key === sel.value) || {}));
    if (VCS.applyEngine) VCS.applyEngine(sel.value, capability);
    renderEngineDesc(sel.value);
    return loadCalculations(scenarioKey, sel.value, generation);
  }

  async function loadCalculations(scenarioKey, engineKey, generation = workspaceIntentGeneration) {
    const sel = $('set-calculation');
    if (!sel) return true;
    const [catalog, current] = await Promise.all([
      VCS.call('task_catalog', scenarioKey || null, null, engineKey || 'vasp'),
      VCS.call('calculation_get')]);
    if (!currentWorkspaceIntent(generation)) return false;
    State.tasks = (catalog && catalog.tasks) || [];
    const active = (current && current.active_calculation) ||
      ((VCS.scenario && VCS.scenario.defaults) || {}).active_calculation || '';
    renderCalculationOptions(active);
    if (sel.value && (!current || !current.configured)) {
      const configured = await VCS.call('calculation_set', sel.value);
      if (!currentWorkspaceIntent(generation)) return false;
      if (configured && configured.ok === false) return false;
    }
    if (VCS.applyCalculation) VCS.applyCalculation(sel.value || '');
    renderCalculationGuide(sel.value || '');
    return true;
  }

  function renderEngineDesc(key) {
    const out = $('set-engine-desc');
    if (!out) return;
    const row = State.engines.find(item => item.key === key) || {};
    const en = ENGINE_EN[key] || {};
    const supportLabel = english() ? (row.support_label_en || en.support_label || '')
      : (row.support_label || '');
    const summary = english() ? (row.summary_en || en.summary || '') : (row.summary || '');
    const limitations = english()
      ? (row.limitations_en || en.limitations || []) : (row.limitations || []);
    const separator = english() ? ': ' : '：';
    const joined = limitations.join(english() ? '; ' : '；');
    out.textContent = `${supportLabel}${summary ? separator + summary : ''}` +
      (joined ? tr('settings.engine.limitations', { limitations: joined },
        ' 注意：{limitations}', ' Note: {limitations}') : '');
  }

  function renderCalculationGuide(key) {
    const guide = $('set-calculation-guide');
    const task = State.tasks.find(t => t.key === key);
    if (!guide) return;
    guide.hidden = !task;
    if (!task) return;
    if ($('set-calculation-name')) $('set-calculation-name').textContent = taskName(task);
    if ($('set-calculation-desc')) $('set-calculation-desc').textContent = taskDescription(task);
    if ($('set-calculation-io')) $('set-calculation-io').textContent = tr(
      'settings.calculation.io', { requires: taskRequires(task), outputs: taskOutputs(task) },
      '需要：{requires}　产出：{outputs}', 'Requires: {requires}  Outputs: {outputs}');
    if ($('set-calculation-next')) $('set-calculation-next').textContent = tr(
      'settings.calculation.next', { action: taskNextAction(task) },
      '完成后：{action}', 'After completion: {action}');
    const start = $('set-calculation-start');
    const route = VCS.calculationRoute && VCS.calculationRoute(key, VCS.scenario);
    if (start) start.textContent = route && route.page === 'project'
      ? tr('settings.calculation.open_results', {}, '打开对应结果工具', 'Open the matching result tool')
      : route && route.page === 'structure'
        ? tr('settings.calculation.open_modeling', {}, '打开对应建模工具', 'Open the matching modeling tool')
        : tr('settings.calculation.open_inputs',
          { engine: String(VCS.activeEngine || 'vasp').toUpperCase() },
          '进入 {engine} 输入准备', 'Prepare {engine} inputs');
  }

  async function startCalculation() {
    const key = $('set-calculation') ? $('set-calculation').value : '';
    if (!key || !VCS.openCalculation) return;
    const out = await VCS.openCalculation(key, { source: 'settings-calculation' });
    if (!out.ok) logLocalizedSetting('settings.status.calculation_open_failed', {},
      '无法进入所选计算，请检查当前工作模式',
      'The selected calculation cannot be opened in the current workflow', 'failc');
  }
  function renderScenarioDesc(key) {
    const d = $('set-scenario-desc');
    const s = (State.scenarios || []).find(x => x.key === key);
    if (d) d.textContent = s ? scenarioDescription(s) : '';
  }
  function redrawLocalizedWorkspace() {
    // 语言只改显示层；不调用 scenario_set / engine_set / calculation_set，
    // 因此不会把科学工作配置与 UI 语言耦合。
    renderKeyState(State.keySaved);
    renderScenarioOptions(($('set-scenario') && $('set-scenario').value) || State.scenarioKey);
    renderScenarioDesc(State.scenarioKey);
    renderEngineOptions(($('set-engine') && $('set-engine').value) || State.engineKey);
    renderEngineDesc(State.engineKey);
    renderCalculationOptions(
      ($('set-calculation') && $('set-calculation').value) || State.calculationKey);
    renderCalculationGuide(State.calculationKey);
    renderLabPolicyOptions(val('set-lab-policy'));
    renderLabPolicySummary();
    renderLabPolicyPreview();
    redrawLocalizedSettingLogs();
  }
  async function onLangChange() {
    const lg = $('set-lang') ? $('set-lang').value : 'zh';
    const r = await VCS.call('lang_set', lg);
    if (!(r && r.ok)) {
      logLocalizedSetting('settings.status.language_failed',
        { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
        '切换语言失败：{error}', 'Failed to change interface language: {error}', 'failc');
      return;
    }
    try {
      if (VCS.loadLang) await VCS.loadLang(lg);      // 重拉词典并替换 data-i18n 文本
      logLocalizedSetting('settings.status.language_changed', { lang: lg },
        '界面语言已切换：{lang}', 'Interface language changed: {lang}', 'okc');
    } catch (error) {
      logLocalizedSetting('settings.status.language_bundle_failed', { error: String(error) },
        '界面语言已保存，但词典加载失败：{error}',
        'The interface language was saved, but its dictionary could not be loaded: {error}', 'failc');
    }
  }
  async function onScenarioChange() {
    const key = $('set-scenario') ? $('set-scenario').value : 'full';
    const generation = beginWorkspaceIntent();
    try {
      const r = await VCS.call('scenario_set', key);
      if (!currentWorkspaceIntent(generation)) return false;
      if (!(r && r.ok)) {
        logLocalizedSetting('settings.status.scenario_failed',
          { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
          '切换工作模式失败：{error}', 'Failed to change workflow: {error}', 'failc');
        await loadWorkspace(generation);
        return false;
      }
      State.scenarioKey = key;
      if (r.scenario) {
        const index = State.scenarios.findIndex(item => item.key === r.scenario.key);
        if (index >= 0) State.scenarios[index] = r.scenario;
      }
      renderScenarioDesc(key);
      if (VCS.applyScenario && r.scenario) VCS.applyScenario(r.scenario);
      const preferredEngine = r.scenario && r.scenario.defaults && r.scenario.defaults.engine;
      if (preferredEngine) {
        await VCS.call('engine_set', preferredEngine);
        if (!currentWorkspaceIntent(generation)) return false;
      }
      if (await loadEngines(key, generation) === false) return false;
      if (!currentWorkspaceIntent(generation)) return false;
      const name = r.scenario ? scenarioName(r.scenario) : key;
      logLocalizedSetting('settings.status.scenario_changed', { name },
        '工作模式已切换：{name}', 'Workflow changed: {name}', 'okc');
      VCS.toast(tr('settings.status.scenario_changed_short', {},
        '已切换工作模式', 'Workflow changed'));
      return true;
    } catch (error) {
      if (!currentWorkspaceIntent(generation)) return false;
      logLocalizedSetting('settings.status.scenario_failed', { error: String(error) },
        '切换工作模式失败：{error}', 'Failed to change workflow: {error}', 'failc');
      await loadWorkspace(generation);
      return false;
    } finally {
      endWorkspaceIntent(generation);
    }
  }
  async function onEngineChange() {
    const key = $('set-engine') ? $('set-engine').value : 'vasp';
    const generation = beginWorkspaceIntent();
    try {
      const r = await VCS.call('engine_set', key);
      if (!currentWorkspaceIntent(generation)) return false;
      if (!(r && r.ok)) {
        logLocalizedSetting('settings.status.engine_failed',
          { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
          '切换计算引擎失败：{error}', 'Failed to change compute engine: {error}', 'failc');
        await loadEngines(State.scenarioKey, generation);
        return false;
      }
      State.engineKey = key;
      const row = State.engines.find(item => item.key === key) || r.capability || {};
      if (VCS.applyEngine) VCS.applyEngine(key, Object.assign({}, row, r.capability || {}));
      renderEngineDesc(key);
      if (await loadCalculations(State.scenarioKey, key, generation) === false) return false;
      if (!currentWorkspaceIntent(generation)) return false;
      logLocalizedSetting('settings.status.engine_changed', { name: row.name || key.toUpperCase() },
        '本次计算引擎已切换：{name}', 'Compute engine changed: {name}', 'okc');
      VCS.toast(tr('settings.status.engine_filtered', {},
        '已按引擎收起不支持的任务和字段',
        'Tasks and fields unsupported by this engine are now hidden'));
      return true;
    } catch (error) {
      if (!currentWorkspaceIntent(generation)) return false;
      logLocalizedSetting('settings.status.engine_failed', { error: String(error) },
        '切换计算引擎失败：{error}', 'Failed to change compute engine: {error}', 'failc');
      await loadEngines(State.scenarioKey, generation);
      return false;
    } finally {
      endWorkspaceIntent(generation);
    }
  }
  async function onCalculationChange() {
    const key = $('set-calculation') ? $('set-calculation').value : '';
    if (!key) return;
    const generation = beginWorkspaceIntent();
    try {
      const r = await VCS.call('calculation_set', key);
      if (!currentWorkspaceIntent(generation)) return false;
      if (!(r && r.ok)) {
        logLocalizedSetting('settings.status.calculation_failed',
          { error: rawError(r, 'common.unknown_error', '未知错误', 'Unknown error') },
          '切换计算类型失败：{error}', 'Failed to change calculation type: {error}', 'failc');
        await loadCalculations(State.scenarioKey, State.engineKey, generation);
        return false;
      }
      State.calculationKey = key;
      if (VCS.applyCalculation) VCS.applyCalculation(key);
      const task = State.tasks.find(t => t.key === key);
      renderCalculationGuide(key);
      logLocalizedSetting('settings.status.calculation_changed', { name: task ? taskName(task) : key },
        '本次计算类型已切换：{name}', 'Calculation type changed: {name}', 'okc');
      VCS.toast(tr('settings.status.calculation_ready', {},
        '已切换本次计算类型；可直接进入对应步骤',
        'Calculation type changed; you can open its next step now'));
      return true;
    } catch (error) {
      if (!currentWorkspaceIntent(generation)) return false;
      logLocalizedSetting('settings.status.calculation_failed', { error: String(error) },
        '切换计算类型失败：{error}', 'Failed to change calculation type: {error}', 'failc');
      await loadCalculations(State.scenarioKey, State.engineKey, generation);
      return false;
    } finally {
      endWorkspaceIntent(generation);
    }
  }

  // ── 初始化 ──────────────────────────────────────────────────────────────────
  function wire(id, ev, fn) { const el = $(id); if (el) el.addEventListener(ev, fn); }

  function init() {
    wire('set-llm-provider', 'change', onProviderChange);
    wire('set-llm-save', 'click', saveLlm);
    wire('set-llm-keysave', 'click', saveKey);
    wire('set-llm-test', 'click', testLlm);
    wire('set-prompt-save', 'click', savePrompt);
    wire('set-prompt-reset', 'click', resetPrompt);
    wire('set-potcar-btn', 'click', () => pickDirInto('set-potcar'));
    wire('set-molecules-btn', 'click', () => pickDirInto('set-molecules'));
    wire('set-paths-save', 'click', savePaths);
    wire('set-ap-save', 'click', saveAutopilot);
    wire('set-fig-save', 'click', saveFigPrefs);
    wire('set-lang', 'change', onLangChange);
    wire('set-scenario', 'change', onScenarioChange);
    wire('set-engine', 'change', onEngineChange);
    wire('set-calculation', 'change', onCalculationChange);
    wire('set-calculation-start', 'click', startCalculation);
    wire('set-lab-policy', 'change', () => {
      renderLabApplicability();
      updateDraftScope('settings-lab-policy');
    });
    wire('set-lab-preview', 'click', previewLabPolicy);
    wire('set-lab-confirm', 'click', confirmLabPolicy);
    document.querySelectorAll('input[name="set-density"]').forEach(input => {
      input.addEventListener('change', e => {
        if (e.target.checked) selectDensity(e.target.value, true);
      });
    });
    const row = $('set-theme-row');
    if (row) row.addEventListener('click', e => {
      const o = e.target.closest('.theme-opt');
      if (o) selectTheme(o.dataset.theme, true);
    });
    wireDraftTracking();
    load();
  }

  // 切回设置页时刷新(密钥状态 / 其他会话可能改过 config)
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'settings' && !dirtyDraftScopes.size) load();
  });
  document.addEventListener('vcs:language', redrawLocalizedWorkspace);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Settings = {
    reload: load,
    discardDrafts() {
      Array.from(dirtyDraftScopes).forEach(restoreDraftScope);
      return true;
    },
  };
  if (window.__VCS_TEST__ === true) {
    window.__VCS_SETTINGS_TEST__ = Object.freeze({
      onScenarioChange,
      loadLabPolicies,
      previewLabPolicy,
      confirmLabPolicy,
      renderLabPolicyPreview,
      captureDraftScope,
      updateDraftScope,
      restoreDraftScope,
      snapshot() {
        return {
          state: Object.assign({}, State),
          dirty_scopes: Array.from(dirtyDraftScopes),
        };
      },
    });
  }
})();
