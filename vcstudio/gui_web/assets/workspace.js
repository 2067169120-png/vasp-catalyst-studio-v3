// workspace.js — V4 项目上下文壳层：语义路由、跨会话恢复、全局上下文与抽屉。
// 旧业务模块仍使用 dashboard/structure/... 物理页；本文件只做稳定语义路由适配。
'use strict';

(function () {
  const VCS = window.VCS;
  if (!VCS) return;

  const SCHEMA = 'vcstudio.workspace-context/v1';
  const STORAGE_KEY = 'vcs.workspace.context.v1';
  const OPERATION_STORAGE_KEY = 'vcs.workspace.operations.v1';
  const OPERATION_SCHEMA = 'vcstudio.operation-queue/v1';
  const MAX_OPERATION_RECORDS = 80;
  const LEGACY_PROJECT_STORAGE_KEYS = [
    'vcs.adsorption.current_project', 'vcs.adsorption.compare_projects',
  ];
  const DRAFT_PREFIX = 'vcs.workspace.draft.v1.';
  const MAX_DRAFT_CHARS = 65536;
  const PROJECT_TOKEN = /^[A-Za-z0-9._~-]{1,160}$/;
  const JOB_TOKEN = /^[A-Za-z0-9._~:-]{1,160}$/;
  const ROUTE_TOKEN = /^[a-z0-9][a-z0-9._-]{0,79}$/;
  const AUTHORITY_TOKEN = /^[a-f0-9]{32}$/;
  const JOB_STATUS_FILTERS = new Set(['queue', 'run', 'need', 'done', 'fail']);
  const REPORT_QUERY_KEYS = new Set(['project', 'spec', 'revision']);
  const ROUTE_QUERY_KEYS = Object.freeze({
    'run-jobs': new Set(['status', 'cluster']),
    'publish-figures': new Set(['project']),
    'publish-report': REPORT_QUERY_KEYS,
    'publish-si': REPORT_QUERY_KEYS,
    'publish-draftpack': REPORT_QUERY_KEYS,
    'publish-versions': REPORT_QUERY_KEYS,
    'publish-export': REPORT_QUERY_KEYS,
  });

  function tr(key, fallback, params) {
    return typeof VCS.t === 'function' ? VCS.t(key, params || {}, fallback) : fallback;
  }

  const ROUTES = Object.freeze({
    home: { area: 'home', page: 'dashboard', label: '首页', labelKey: 'workspace.route.home', path: () => '/home' },
    'project-overview': { area: 'project', page: 'project', label: '项目概览', labelKey: 'workspace.route.project_overview',
      path: c => `/projects/${projectToken(c)}/overview` },
    'project-members': { area: 'project', page: 'project', label: '成员与数据', labelKey: 'workspace.route.project_members',
      path: c => `/projects/${projectToken(c)}/members`, focus: '#pj-select' },
    'project-workflow': { area: 'project', page: 'dashboard', label: '项目工作流', labelKey: 'workspace.route.project_workflow',
      path: c => `/projects/${projectToken(c)}/workflow`, focus: '#db-pipeline' },
    'project-runs': { area: 'project', page: 'jobs', label: '项目运行', labelKey: 'workspace.route.project_runs',
      path: c => `/projects/${projectToken(c)}/runs` },
    'project-activity': { area: 'project', page: 'dashboard', label: '活动与版本', labelKey: 'workspace.route.project_activity',
      path: c => `/projects/${projectToken(c)}/activity`, focus: '#db-feed' },

    'prepare-structure': { area: 'prepare', page: 'structure', label: '结构', labelKey: 'workspace.route.prepare_structure',
      path: () => '/prepare/structure' },
    'prepare-input': { area: 'prepare', page: 'generate', label: '输入', labelKey: 'workspace.route.prepare_input',
      path: () => '/prepare/input' },
    'prepare-batch': { area: 'prepare', page: 'generate', label: '批量', labelKey: 'workspace.route.prepare_batch',
      path: () => '/prepare/batch' },
    'prepare-templates': { area: 'prepare', page: 'generate', label: '模板', labelKey: 'workspace.route.prepare_templates',
      path: () => '/prepare/templates', focus: '#research-recipes-card' },
    'prepare-preflight': { area: 'prepare', page: 'generate', label: '预检', labelKey: 'workspace.route.prepare_preflight',
      path: () => '/prepare/preflight' },

    'run-jobs': { area: 'run', page: 'jobs', label: '作业', labelKey: 'workspace.route.run_jobs', path: () => '/jobs' },
    'run-remote': { area: 'run', page: 'cluster', label: '远程', labelKey: 'workspace.route.run_remote', path: () => '/run/remote' },

    'analyze-energy': { area: 'analyze', page: 'analysis-workbench', label: '能量与稳定性', labelKey: 'workspace.route.analyze_energy',
      path: c => `/projects/${projectToken(c)}/analysis/adsorption`,
      analysisId: 'adsorption-energy', scenePage: 'project', focus: '#aw-title' },
    'analyze-thermo': { area: 'analyze', page: 'analysis-workbench', label: '热力学与动力学', labelKey: 'workspace.route.analyze_thermo',
      path: c => `/projects/${projectToken(c)}/analysis/thermo`,
      analysisId: 'free-energy-path', scenePage: 'project', focus: '#aw-title' },
    'analyze-electronic': { area: 'analyze', page: 'analysis-workbench', label: '电子结构', labelKey: 'workspace.route.analyze_electronic',
      path: c => `/projects/${projectToken(c)}/analysis/electronic`,
      analysisId: 'electronic-structure', scenePage: 'wavefunction', focus: '#aw-title' },
    'analyze-charge': { area: 'analyze', page: 'analysis-workbench', label: '电荷与波函数', labelKey: 'workspace.route.analyze_charge',
      path: c => `/projects/${projectToken(c)}/analysis/charge`,
      analysisId: 'charge-wavefunction', scenePage: 'wavefunction', focus: '#aw-title' },
    'analyze-comparison': { area: 'analyze', page: 'analysis-workbench', label: '比较', labelKey: 'workspace.route.analyze_comparison',
      path: c => `/projects/${projectToken(c)}/analysis/comparison`,
      analysisId: 'multi-project-comparison', scenePage: 'project', focus: '#aw-title' },
    'analyze-custom': { area: 'analyze', page: 'analysis-workbench', label: '自定义', labelKey: 'workspace.route.analyze_custom',
      path: c => `/projects/${projectToken(c)}/analysis/custom`,
      analysisId: 'task-results', scenePage: 'project', focus: '#aw-title' },
    'analyze-properties': { area: 'analyze', page: 'analysis-workbench', label: '性质计算器', labelKey: 'workspace.route.analyze_properties',
      path: c => `/projects/${projectToken(c)}/analysis/properties`,
      analysisId: 'property-calculators', scenePage: 'project', focus: '#aw-title' },

    'publish-figures': { area: 'publish', page: 'figures', label: '图表', labelKey: 'workspace.route.publish_figures',
      path: c => `/publish/figures?project=${encodeURIComponent(projectToken(c))}` },
    'publish-report': { area: 'publish', page: 'report-workbench', label: '报告', labelKey: 'workspace.route.publish_report',
      path: c => `/publish/report?project=${encodeURIComponent(projectToken(c))}`,
      reportMode: 'report', focus: '#rw-title' },
    'publish-si': { area: 'publish', page: 'report-workbench', label: '补充信息', labelKey: 'workspace.route.publish_si',
      path: c => `/publish/si?project=${encodeURIComponent(projectToken(c))}`,
      reportMode: 'si', focus: '#rw-step-button-outline' },
    'publish-draftpack': { area: 'publish', page: 'report-workbench', label: '草稿包', labelKey: 'workspace.route.publish_draftpack',
      path: c => `/publish/draftpack?project=${encodeURIComponent(projectToken(c))}`,
      reportMode: 'draftpack', focus: '#rw-step-button-export' },
    'publish-versions': { area: 'publish', page: 'report-workbench', label: '版本', labelKey: 'workspace.route.publish_versions',
      path: c => `/publish/versions?project=${encodeURIComponent(projectToken(c))}`,
      reportMode: 'versions', focus: '#rw-history-heading' },
    'publish-export': { area: 'publish', page: 'report-workbench', label: '导出与归档', labelKey: 'workspace.route.publish_export',
      path: c => `/publish/export?project=${encodeURIComponent(projectToken(c))}`,
      reportMode: 'export', focus: '#rw-step-button-export' },

    'environment-cluster': { area: 'environment', page: 'cluster', label: '集群', labelKey: 'workspace.route.environment_cluster',
      path: () => '/environment/cluster' },
    'environment-local': { area: 'environment', page: 'jobs', label: '本地运行器', labelKey: 'workspace.route.environment_local',
      path: () => '/environment/local-runner' },
    'environment-dependencies': { area: 'environment', page: 'settings', label: '依赖', labelKey: 'workspace.route.environment_dependencies',
      path: () => '/environment/dependencies', focus: '#deps-panel' },
    'environment-paths': { area: 'environment', page: 'settings', label: '数据路径', labelKey: 'workspace.route.environment_paths',
      path: () => '/environment/data-paths' },
    'environment-templates': { area: 'environment', page: 'settings', label: '模板', labelKey: 'workspace.route.environment_templates',
      path: () => '/environment/templates' },
    'environment-settings': { area: 'environment', page: 'settings', label: '设置', labelKey: 'workspace.route.environment_settings',
      path: () => '/environment/settings' },
  });

  const AREA_LABELS = Object.freeze({
    home: 'Home', project: 'Project', prepare: 'Prepare', run: 'Run',
    analyze: 'Analyze', publish: 'Publish', environment: 'Environment',
  });

  const SUBNAV = Object.freeze({
    home: [{ route: 'home', label: '首页', labelKey: 'workspace.route.home' }],
    project: [
      { route: 'project-overview', label: '概览' },
      { route: 'project-members', label: '成员与数据' },
      { route: 'project-workflow', label: '工作流' },
      { route: 'project-runs', label: '运行' },
      { route: 'project-activity', label: '活动与版本' },
    ],
    prepare: [
      { route: 'prepare-structure', label: '结构' },
      { route: 'prepare-input', label: '输入' },
      { route: 'prepare-batch', label: '批量' },
      { route: 'prepare-templates', label: '模板' },
      { route: 'prepare-preflight', label: '预检' },
    ],
    run: [
      { route: 'run-jobs', label: '待处理', labelKey: 'workspace.jobs.queue', query: { status: 'queue' } },
      { route: 'run-jobs', label: '运行中', labelKey: 'workspace.jobs.run', query: { status: 'run' } },
      { route: 'run-jobs', label: '需关注', labelKey: 'workspace.jobs.need', query: { status: 'need' } },
      { route: 'run-jobs', label: '已完成', labelKey: 'workspace.jobs.done', query: { status: 'done' } },
      { route: 'run-remote', label: '远程' },
    ],
    analyze: [
      { route: 'analyze-energy', label: '能量与稳定性' },
      { route: 'analyze-thermo', label: '热力学与动力学' },
      { route: 'analyze-electronic', label: '电子结构' },
      { route: 'analyze-charge', label: '电荷与波函数' },
      { route: 'analyze-comparison', label: '比较' },
      { route: 'analyze-custom', label: '自定义' },
    ],
    publish: [
      { route: 'publish-figures', label: '图表' },
      { route: 'publish-report', label: '报告' },
      { route: 'publish-si', label: '补充信息' },
      { route: 'publish-draftpack', label: '草稿包' },
      { route: 'publish-versions', label: '版本' },
      { route: 'publish-export', label: '导出与归档' },
    ],
    environment: [
      { route: 'environment-cluster', label: '集群' },
      { route: 'environment-local', label: '本地运行器' },
      { route: 'environment-dependencies', label: '依赖' },
      { route: 'environment-paths', label: '数据路径' },
      { route: 'environment-templates', label: '模板' },
      { route: 'environment-settings', label: '设置' },
    ],
  });

  const LEGACY_ROUTE = Object.freeze({
    dashboard: 'home', structure: 'prepare-structure', generate: 'prepare-input',
    jobs: 'run-jobs', project: 'project-overview', wavefunction: 'analyze-electronic',
    'analysis-workbench': 'analyze-energy',
    figures: 'publish-figures', cluster: 'environment-cluster',
    settings: 'environment-settings', ai: 'assistant',
  });

  function blankState() {
    return {
      schema: SCHEMA,
      route: '#/home',
      project_id: '',
      analysis_id: '',
      selected_job_id: '',
      panels: {},
      filters: {},
      sort: {},
      scroll: {},
      draft_refs: {},
      server_authority_id: '',
      server_revision: 0,
      updated_at_ms: 0,
    };
  }

  function plainObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function safeToken(value, pattern = ROUTE_TOKEN) {
    const out = String(value || '').trim();
    return pattern.test(out) ? out : '';
  }

  function safeAuthorityId(value) {
    return safeToken(value, AUTHORITY_TOKEN);
  }

  function safeOperationText(value, limit) {
    let out = String(value || '');
    // Keep a useful public error while stripping credentials before paths.  In
    // particular, do not leave user/password pairs in otherwise valid URLs.
    out = out.replace(/\b([A-Za-z][A-Za-z0-9+.-]{1,31}:\/\/)[^\s/@:]+:[^\s/@]+@/g,
      '$1[redacted]@');
    out = out.replace(
      /\b((?:proxy[-_ ]?)?authorization\s*:\s*)(?:Basic|Bearer)\s+[^\s,;&]+/gi,
      '$1[redacted]');
    out = out.replace(
      /\b((?:password|passwd|pwd|token|secret|client[_-]?secret|authorization|cookie|api[_-]?key|access[_-]?key|aws_access_key_id|aws_secret_access_key|aws_session_token)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&]+)/gi,
      '$1[redacted]');
    out = out.replace(/(?:\bBearer\s+|github_pat_|gh[pousr]_|sk-)[A-Za-z0-9_.-]{8,}/gi,
      '[redacted]');
    out = out.replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, '[redacted]');
    out = out.replace(/-----BEGIN[^\r\n]*PRIVATE KEY-----[^\r\n]*/gi, '[redacted]');
    out = out.replace(/(?:[A-Za-z]:[\\/]|\\\\)[^\s<>"'`]+/g, '[redacted-path]');
    // A POSIX absolute path may live under any mount point (for example
    // /scratch or /work), so a directory-name allowlist is not sufficient.
    // Requiring a boundary and rejecting // avoids treating URL separators as
    // local paths.
    out = out.replace(/(^|[\s("'`=:,;\[])\/(?!\/)[^\s<>"'`]+/g,
      (_match, prefix) => `${prefix}[redacted-path]`);
    return out.slice(0, limit);
  }

  function sanitizeOperationValue(value, depth = 0) {
    if (typeof value === 'string') return safeOperationText(value, 1000);
    if (typeof value === 'number') return Number.isFinite(value) ? value : 0;
    if (typeof value === 'boolean' || value === null) return value;
    if (depth >= 5) return null;
    if (Array.isArray(value)) {
      return value.slice(0, 100).map(item => sanitizeOperationValue(item, depth + 1));
    }
    if (!value || typeof value !== 'object') return null;
    const out = {};
    Object.entries(value).slice(0, 100).forEach(([key, raw]) => {
      const safeKey = safeToken(key, /^[A-Za-z][A-Za-z0-9._-]{0,79}$/);
      if (safeKey) out[safeKey] = sanitizeOperationValue(raw, depth + 1);
    });
    return out;
  }

  function normalizedOperation(record) {
    const source = plainObject(sanitizeOperationValue(record));
    const id = safeToken(source.id, JOB_TOKEN);
    if (!id) return null;
    const status = safeToken(source.status, /^[a-z][a-z0-9._-]{0,39}$/) || 'pending';
    return {
      id,
      kind: safeToken(source.kind, /^[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$/) || 'operation',
      label: safeOperationText(source.label || 'Operation', 160),
      status,
      count: Math.max(0, Number(source.count || 0) || 0),
      error: safeOperationText(source.error, 500),
      route: safeToken(source.route, ROUTE_TOKEN),
      started_at: String(source.started_at || new Date().toISOString()).slice(0, 40),
      updated_at: String(source.updated_at || new Date().toISOString()).slice(0, 40),
    };
  }

  function loadOperationRecords() {
    const records = new Map();
    try {
      const parsed = JSON.parse(localStorage.getItem(OPERATION_STORAGE_KEY) || '{}');
      if (parsed.schema !== OPERATION_SCHEMA || !Array.isArray(parsed.records)) return records;
      parsed.records.slice(-MAX_OPERATION_RECORDS).forEach(raw => {
        const item = normalizedOperation(raw);
        if (item) records.set(item.id, item);
      });
      // Rewrite even a previously stored v1 record through today's sanitizer;
      // local activity is a cache, never an archive for raw backend payloads.
      writeOperationRecords(records);
    } catch (_) { /* malformed local activity is non-authoritative */ }
    return records;
  }

  function writeOperationRecords(records) {
    const safeRecords = Array.from(records.values()).slice(-MAX_OPERATION_RECORDS)
      .map(item => normalizedOperation(item)).filter(Boolean);
    localStorage.setItem(OPERATION_STORAGE_KEY, JSON.stringify({
      schema: OPERATION_SCHEMA,
      records: sanitizeOperationValue(safeRecords),
    }));
  }

  function persistOperationRecords() {
    try {
      writeOperationRecords(operationRecords);
    } catch (_) { /* the queue remains usable in memory when storage is unavailable */ }
  }

  function safeQueryValue(key, value) {
    const name = String(key || '');
    if (name === 'project') return safeToken(value, PROJECT_TOKEN);
    if (name === 'spec') return safeToken(value, PROJECT_TOKEN);
    if (name === 'revision') {
      const out = String(value || '').trim();
      return /^[1-9][0-9]{0,8}$/.test(out) ? out : '';
    }
    if (name === 'status') {
      const status = safeToken(value, JOB_TOKEN);
      return JOB_STATUS_FILTERS.has(status) ? status : '';
    }
    if (name !== 'cluster') return safeToken(value, JOB_TOKEN);
    const out = String(value || '').trim();
    if (!out || out.length > 128 || /[\x00-\x1f\x7f\\/?#&=]/.test(out)) return '';
    if (/(?:\bBearer\s+|gh[pousr]_|sk-[A-Za-z0-9_-]{12,}|-----BEGIN\s)/i.test(out)) return '';
    return out;
  }

  function safeRoute(value) {
    const out = String(value || '').trim();
    if (!out.startsWith('#/') || out.length > 512 || /[\\\r\n]/.test(out)) return '';
    if (/(?:^|[/?=&])(?:[A-Za-z]:|file:|\.\.)/i.test(decodeURIComponentSafe(out))) return '';
    return out;
  }

  function decodeURIComponentSafe(value) {
    try { return decodeURIComponent(value); } catch (_) { return String(value || ''); }
  }

  function finiteScrollMap(value) {
    const out = {};
    Object.entries(plainObject(value)).slice(-40).forEach(([key, raw]) => {
      const n = Number(raw);
      if (safeRoute(key) && Number.isFinite(n) && n >= 0) out[key] = Math.min(10000000, Math.round(n));
    });
    return out;
  }

  function smallRecord(value, maxKeys = 40) {
    const out = {};
    Object.entries(plainObject(value)).slice(-maxKeys).forEach(([key, raw]) => {
      const cleanKey = safeToken(key, /^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$/);
      if (!cleanKey) return;
      if (typeof raw === 'string') out[cleanKey] = raw.slice(0, 512);
      else if (typeof raw === 'number' && Number.isFinite(raw)) out[cleanKey] = raw;
      else if (typeof raw === 'boolean' || raw === null) out[cleanKey] = raw;
      else if (Array.isArray(raw)) out[cleanKey] = raw.slice(0, 40)
        .map(item => String(item).slice(0, 160));
    });
    return out;
  }

  function normalizeState(raw) {
    const src = plainObject(raw);
    const state = blankState();
    const route = safeRoute(src.route);
    if (route) state.route = route;
    state.project_id = safeToken(src.project_id || src.current_project_id, PROJECT_TOKEN);
    state.analysis_id = safeToken(src.analysis_id || src.current_analysis_id);
    state.selected_job_id = safeToken(src.selected_job_id, JOB_TOKEN);
    state.panels = smallRecord(src.panels);
    state.filters = smallRecord(src.filters);
    state.sort = smallRecord(src.sort);
    state.scroll = finiteScrollMap(src.scroll);
    state.draft_refs = plainObject(src.draft_refs);
    state.server_authority_id = safeAuthorityId(src.server_authority_id);
    state.server_revision = Number.isSafeInteger(Number(src.server_revision))
      ? Math.max(0, Number(src.server_revision)) : 0;
    state.updated_at_ms = Number.isFinite(Number(src.updated_at_ms))
      ? Math.max(0, Math.round(Number(src.updated_at_ms))) : 0;
    return state;
  }

  function loadLocalState() {
    try {
      LEGACY_PROJECT_STORAGE_KEYS.forEach(key => localStorage.removeItem(key));
      return normalizeState(JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'));
    } catch (_) {
      return blankState();
    }
  }

  let state = loadLocalState();
  let authorityId = state.server_authority_id;
  let revision = state.server_revision;
  let remoteSaveBlocked = false;
  let remoteDirtyHold = false;
  let currentRoute = null;
  let projects = [];
  let selectedProject = null;
  let remoteSaveTimer = null;
  let remoteSaveInFlight = null;
  let remoteSaveRequested = false;
  let localStateGeneration = 0;
  let routeGeneration = 0;
  let navigationTail = Promise.resolve();
  let routeApplyTail = Promise.resolve();
  let externalNavigationPromise = null;
  let externalNavigationHash = '';
  const initialHistoryState = history.state && typeof history.state === 'object'
    ? history.state : null;
  let historyIndex = initialHistoryState &&
    Number.isSafeInteger(Number(initialHistoryState.vcsIndex))
    ? Number(initialHistoryState.vcsIndex) : 0;
  let historyCompensation = null;
  let projectSwitchGeneration = 0;
  let locationIntentGeneration = 0;
  let jobContextGeneration = 0;
  let pendingJobProjectSwitch = null;
  let navReturnFocus = null;
  let activityReturnFocus = null;
  let assistantReturnFocus = null;
  let scrollTimer = null;
  let contextRefreshTimer = null;
  let contextRefreshGeneration = 0;
  let lastContextRefreshAt = 0;
  let initialHashWasExplicit = !!safeRoute(window.location.hash);
  const dirtyScopes = new Map();
  const operationRecords = loadOperationRecords();

  function publishOperation(record = {}) {
    const id = safeToken(record.id, JOB_TOKEN);
    if (!id) return false;
    const previous = operationRecords.get(id) || {};
    const status = String(record.status || previous.status || 'pending').slice(0, 40);
    const normalized = normalizedOperation({
      id,
      kind: record.kind || previous.kind,
      label: record.label || previous.label,
      status,
      count: record.count != null ? record.count : previous.count,
      error: record.error,
      route: record.route || previous.route,
      started_at: record.started_at || previous.started_at,
      updated_at: record.updated_at || new Date().toISOString(),
    });
    operationRecords.delete(id);
    operationRecords.set(id, normalized);
    if (operationRecords.size > MAX_OPERATION_RECORDS) {
      const oldest = operationRecords.keys().next().value;
      operationRecords.delete(oldest);
    }
    persistOperationRecords();
    renderActivity();
    return true;
  }

  function retryOperation(id) {
    const key = safeToken(id, JOB_TOKEN);
    const operation = key && operationRecords.get(key);
    if (!operation || operation.status !== 'failed' || !operation.route) return false;
    return navigateRoute(operation.route, { source: 'operation-retry' });
  }

  function clearCompletedOperations() {
    Array.from(operationRecords.entries()).forEach(([id, operation]) => {
      if (['succeeded', 'cancelled'].includes(operation.status)) operationRecords.delete(id);
    });
    persistOperationRecords();
    renderActivity();
  }

  VCS.operations = {
    publish: publishOperation,
    snapshot() { return Array.from(operationRecords.values()).map(item => Object.assign({}, item)); },
    retry: retryOperation,
    clearCompleted: clearCompletedOperations,
  };

  function projectToken(context) {
    const raw = context && (context.projectId || context.project_id) || state.project_id;
    return safeToken(raw, PROJECT_TOKEN) || 'current';
  }

  function routeHash(id, options = {}) {
    const def = ROUTES[id] || ROUTES.home;
    const path = def.path({ projectId: options.projectId || state.project_id });
    const url = new URL(path, 'https://vcstudio.local');
    const allowed = ROUTE_QUERY_KEYS[id] || new Set();
    Object.entries(plainObject(options.query)).forEach(([key, value]) => {
      const cleanKey = safeToken(key);
      const cleanValue = safeQueryValue(cleanKey, value);
      if (allowed.has(cleanKey) && cleanValue) {
        url.searchParams.set(cleanKey, cleanValue);
      }
    });
    return '#' + url.pathname + (url.search ? url.search : '');
  }

  function parseRoute(hash) {
    const safe = safeRoute(hash);
    if (!safe) return null;
    let url;
    try { url = new URL(safe.slice(1), 'https://vcstudio.local'); } catch (_) { return null; }
    const path = url.pathname.replace(/\/+$/, '') || '/';
    const query = Object.fromEntries(url.searchParams.entries());
    const queryPairs = Array.from(url.searchParams.entries());
    if (new Set(queryPairs.map(pair => pair[0])).size !== queryPairs.length) return null;
    const result = (id, params = {}) => {
      const allowed = ROUTE_QUERY_KEYS[id] || new Set();
      if (queryPairs.some(([key, value]) => !allowed.has(key) ||
          !safeQueryValue(key, value))) return null;
      return { id, def: ROUTES[id], params, query, hash: safe };
    };
    if (path === '/home') return result('home');

    let match = path.match(/^\/projects\/([^/]+)\/(overview|members|workflow|runs|activity)$/);
    if (match) {
      const byView = { overview: 'project-overview', members: 'project-members',
        workflow: 'project-workflow', runs: 'project-runs', activity: 'project-activity' };
      const projectId = safeToken(decodeURIComponentSafe(match[1]), PROJECT_TOKEN);
      return projectId ? result(byView[match[2]], { projectId }) : null;
    }
    match = path.match(/^\/projects\/([^/]+)\/analysis\/(adsorption|thermo|electronic|charge|comparison|custom|properties)$/);
    if (match) {
      const byAnalysis = { adsorption: 'analyze-energy', thermo: 'analyze-thermo',
        electronic: 'analyze-electronic', charge: 'analyze-charge',
        comparison: 'analyze-comparison', custom: 'analyze-custom',
        properties: 'analyze-properties' };
      const projectId = safeToken(decodeURIComponentSafe(match[1]), PROJECT_TOKEN);
      return projectId ? result(byAnalysis[match[2]], { projectId }) : null;
    }
    match = path.match(/^\/prepare\/(structure|input|batch|templates|preflight)$/);
    if (match) return result(`prepare-${match[1]}`);
    if (path === '/jobs') return result('run-jobs');
    if (path === '/run/remote') return result('run-remote');
    match = path.match(/^\/publish\/(figures|report|si|draftpack|versions|export)$/);
    if (match) {
      const id = `publish-${match[1]}`;
      const projectId = safeToken(query.project, PROJECT_TOKEN);
      return result(id, projectId ? { projectId } : {});
    }
    match = path.match(/^\/environment\/(cluster|local-runner|dependencies|data-paths|templates|settings)$/);
    if (match) {
      const byView = { cluster: 'environment-cluster', 'local-runner': 'environment-local',
        dependencies: 'environment-dependencies', 'data-paths': 'environment-paths',
        templates: 'environment-templates', settings: 'environment-settings' };
      return result(byView[match[1]]);
    }
    return null;
  }

  function statePayload() {
    const route = currentRoute || parseRoute(state.route) || parseRoute('#/home');
    const draftRefs = {};
    Object.entries(plainObject(state.draft_refs)).forEach(([id, raw]) => {
      const cleanId = safeToken(id);
      if (!cleanId || !raw || typeof raw !== 'object' || Array.isArray(raw)) return;
      const ref = plainObject(raw);
      let updatedAtMs = Number(ref.updated_at_ms);
      if (!Number.isFinite(updatedAtMs) || updatedAtMs <= 0) {
        updatedAtMs = Date.parse(String(ref.updated_at || ''));
      }
      if (!Number.isFinite(updatedAtMs) || updatedAtMs <= 0) updatedAtMs = Date.now();
      const rawSize = Number(ref.size);
      const size = Number.isFinite(rawSize) && rawSize >= 0
        ? Math.min(MAX_DRAFT_CHARS, Math.round(rawSize)) : 0;
      draftRefs[cleanId] = {
        project_id: safeToken(ref.project_id, PROJECT_TOKEN) || null,
        route: safeRoute(ref.route) || state.route,
        kind: 'unverified_draft',
        dirty: ref.dirty !== false,
        updated_at: new Date(updatedAtMs).toISOString(),
        size,
      };
    });
    return {
      route: { hash: route.hash, area: route.def.area, view: route.id },
      current_project_id: state.project_id || null,
      current_analysis_id: state.analysis_id || null,
      selected_job_id: state.selected_job_id || null,
      panels: state.panels,
      filters: state.filters,
      sort: state.sort,
      scroll: state.scroll,
      draft_refs: draftRefs,
    };
  }

  function persistLocal({ remote = true, touch = true } = {}) {
    state.schema = SCHEMA;
    if (touch) {
      state.updated_at_ms = Date.now();
      localStateGeneration += 1;
    }
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) { /* 本地存储不可用不阻断 */ }
    if (remote) scheduleRemoteSave();
  }

  function scheduleRemoteSave() {
    if (remoteSaveTimer) clearTimeout(remoteSaveTimer);
    remoteSaveTimer = setTimeout(saveRemoteState, 900);
  }

  async function saveRemoteState() {
    if (remoteSaveTimer) clearTimeout(remoteSaveTimer);
    remoteSaveTimer = null;
    remoteSaveRequested = true;
    if (remoteSaveBlocked) return;
    if (!authorityId) {
      remoteSaveBlocked = true;
      setTimeout(() => refreshWorkspaceContext(), 0);
      return;
    }
    if (remoteSaveInFlight) return remoteSaveInFlight;
    remoteSaveInFlight = (async () => {
      while (remoteSaveRequested && !remoteSaveBlocked) {
        remoteSaveRequested = false;
        const savedGeneration = localStateGeneration;
        try {
          const out = await VCS.call('workspace_preferences_update',
            { set: statePayload(), remove: [] }, revision, authorityId);
          if (!out || out.error === '桥方法不存在:workspace_preferences_update') return;
          if (out.ok) {
            const returnedAuthorityId = safeAuthorityId(out.authority_id);
            if (!returnedAuthorityId || returnedAuthorityId !== authorityId) {
              remoteSaveBlocked = true;
              setTimeout(() => refreshWorkspaceContext(), 0);
              return;
            }
            revision = Number(out.state_revision || out.revision || revision);
            authorityId = returnedAuthorityId;
            state.server_authority_id = authorityId;
            state.server_revision = revision;
            remoteSaveBlocked = false;
            persistLocal({ remote: false, touch: false });
            if (localStateGeneration !== savedGeneration) remoteSaveRequested = true;
          } else if (out.conflict) {
            remoteSaveBlocked = true;
            document.dispatchEvent(new CustomEvent('vcs:workspace-conflict', { detail: out }));
            VCS.toast('工作区状态已在另一个窗口更新；正在采用较新的状态', 'fail');
            setTimeout(() => refreshWorkspaceContext(), 0);
          }
        } catch (_) { return; /* UI 恢复状态失败不影响科学文件 */ }
      }
    })().finally(() => { remoteSaveInFlight = null; });
    return remoteSaveInFlight;
  }

  function saveCurrentScroll() {
    if (!currentRoute) return;
    state.scroll[currentRoute.hash] = Math.max(0, Math.round(window.scrollY || 0));
    const entries = Object.entries(state.scroll);
    if (entries.length > 40) state.scroll = Object.fromEntries(entries.slice(-40));
    persistLocal();
  }

  function isProjectRoute(route) {
    return !!(route && (route.params.projectId || route.def.area === 'project' ||
      route.def.area === 'analyze' || route.def.area === 'publish'));
  }

  function routeProject(route) {
    return safeToken(route && route.params && route.params.projectId, PROJECT_TOKEN) ||
      safeToken(route && route.query && route.query.project, PROJECT_TOKEN) || '';
  }

  function resolveRemoteDirtyHold() {
    if (!remoteDirtyHold || dirtyScopes.size) return;
    remoteDirtyHold = false;
    // Keep saves blocked until a fresh server snapshot has been adopted.
    setTimeout(() => refreshWorkspaceContext(), 0);
  }

  async function guardUnsaved(reason) {
    if (!dirtyScopes.size) return true;
    const pending = Array.from(dirtyScopes.entries());
    const details = pending.map(([, item]) => item.label).filter(Boolean);
    const detailText = details.length ? tr('workspace.unsaved.details',
      '\n\n未保存：{items}', { items: details.join('、') }) : '';
    const message = tr('workspace.unsaved.confirm',
      '{reason}会丢弃尚未保存的修改。{details}\n\n是否丢弃并继续？', {
        reason: reason || tr('workspace.unsaved.leave_current', '离开当前内容'),
        details: detailText,
      });
    const ok = await VCS.confirm(message);
    if (!ok) return false;
    try {
      for (const [, item] of pending) {
        if (typeof item.canDiscard === 'function' && await item.canDiscard() === false) return false;
      }
      for (const [, item] of pending) {
        if (typeof item.discard === 'function' && await item.discard() === false) return false;
      }
    } catch (error) {
      VCS.log(`discard failed: ${error && error.message || error}`, 'failc');
      VCS.toast(tr('workspace.unsaved.discard_failed',
        '未能丢弃当前修改，请处理后重试。'));
      return false;
    }
    dirtyScopes.clear();
    renderDirty();
    resolveRemoteDirtyHold();
    return true;
  }

  function findPrimaryRoute(area) {
    const anchor = document.querySelector(`#shell-nav a[data-area="${area}"][data-route]`);
    return anchor && anchor.dataset.route;
  }

  function routeIsAvailable(def) {
    if (!def) return false;
    if (typeof VCS.canActivatePage === 'function') {
      if (!VCS.canActivatePage(def.page)) return false;
      return !def.scenePage || VCS.canActivatePage(def.scenePage);
    }
    if (VCS.scenario && typeof VCS.sceneVisible === 'function') {
      if (!VCS.sceneVisible(VCS.scenario, 'pages.' + def.page)) return false;
      if (def.scenePage && !VCS.sceneVisible(
          VCS.scenario, 'pages.' + def.scenePage)) return false;
    }
    return true;
  }

  function syncPrimaryAreaRoutes(route) {
    document.querySelectorAll('#shell-nav a[data-area][data-route]').forEach(anchor => {
      const area = anchor.dataset.area;
      if (!anchor.dataset.primaryRoute) anchor.dataset.primaryRoute = anchor.dataset.route;
      const items = SUBNAV[area] || [];
      const candidates = items.map(item => item.route).filter(id =>
        ROUTES[id] && routeIsAvailable(ROUTES[id]));
      let preferred = route && route.def.area === area && candidates.includes(route.id)
        ? route.id : anchor.dataset.primaryRoute;
      if (!candidates.includes(preferred)) preferred = candidates[0] || '';
      if (!preferred) {
        anchor.setAttribute('data-scene-hidden', '');
        anchor.removeAttribute('aria-current');
        return;
      }
      const def = ROUTES[preferred];
      anchor.dataset.route = preferred;
      anchor.dataset.page = def.page;
      anchor.dataset.scene = 'pages.' + def.page;
      anchor.removeAttribute('data-scene-hidden');
      anchor.hidden = false;
    });
  }

  function updateRouteLinks() {
    document.querySelectorAll('[data-route]').forEach(anchor => {
      const id = anchor.dataset.route;
      if (!ROUTES[id]) return;
      anchor.setAttribute('href', routeHash(id));
    });
  }

  function routeSubnavKey(route) {
    if (!route) return '';
    if (route.id === 'run-jobs') return `run-jobs:${route.query.status || ''}`;
    return route.id;
  }

  function renderSubnav(route) {
    const box = document.getElementById('workspace-subnav');
    if (!box || !route) return;
    const items = SUBNAV[route.def.area] || [];
    const activeKey = routeSubnavKey(route);
    box.innerHTML = '';
    items.forEach(item => {
      const def = ROUTES[item.route];
      if (!def) return;
      if (!routeIsAvailable(def)) return;
      const anchor = document.createElement('a');
      anchor.dataset.route = item.route;
      anchor.dataset.area = def.area;
      anchor.dataset.page = def.page;
      anchor.dataset.scene = 'pages.' + def.page;
      anchor.href = routeHash(item.route, { query: item.query });
      anchor.textContent = tr(item.labelKey || def.labelKey, item.label || def.label);
      const key = item.route === 'run-jobs'
        ? `run-jobs:${(item.query && item.query.status) || ''}` : item.route;
      if (key === activeKey) {
        anchor.classList.add('on');
        anchor.setAttribute('aria-current', 'page');
      }
      box.appendChild(anchor);
    });
    box.hidden = !items.length;
  }

  function renderRoute(route) {
    if (!route) return;
    const area = route.def.area;
    syncPrimaryAreaRoutes(route);
    document.querySelectorAll('#shell-nav a[data-area]').forEach(anchor => {
      const active = anchor.dataset.area === area;
      anchor.classList.toggle('on', active);
      if (active) anchor.setAttribute('aria-current', 'page');
      else anchor.removeAttribute('aria-current');
    });
    renderSubnav(route);
    updateRouteLinks();
    document.body.dataset.workspaceArea = area;
    document.body.dataset.activeArea = area;
    document.body.dataset.workspacePage = route.def.page;
    document.title = `${tr(route.def.labelKey, route.def.label)} · VASP Catalyst Studio`;
  }

  function applyRouteControls(route) {
    const analysisId = route.def.analysisId || route.def.analysis || '';
    const analysis = document.getElementById('analysis-type');
    if (route.def.page !== 'analysis-workbench' && analysis && route.def.analysis &&
        analysis.value !== route.def.analysis &&
        Array.from(analysis.options || []).some(option => option.value === route.def.analysis)) {
      analysis.value = route.def.analysis;
      analysis.dispatchEvent(new Event('change', { bubbles: true }));
    }
    if (analysisId) state.analysis_id = analysisId;

    const filterStatus = route.query.status || state.filters.job_status || '';
    const status = document.getElementById('jf-status');
    if (status && Array.from(status.options || []).some(option => option.value === filterStatus)) {
      status.value = filterStatus;
      status.dispatchEvent(new Event('change', { bubbles: true }));
    }
    const cluster = document.getElementById('jf-cluster');
    const clusterValue = route.query.cluster || state.filters.job_cluster || '';
    if (cluster && Array.from(cluster.options || []).some(option => option.value === clusterValue)) {
      cluster.value = clusterValue;
      cluster.dispatchEvent(new Event('change', { bubbles: true }));
    }
    const sort = document.getElementById('jf-sort');
    if (sort && state.sort.jobs && Array.from(sort.options || []).some(
      option => option.value === state.sort.jobs)) {
      sort.value = state.sort.jobs;
      sort.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  function restorePanels() {
    document.querySelectorAll('[data-acc]').forEach(card => {
      const key = safeToken(card.getAttribute('data-acc'));
      if (key && Object.prototype.hasOwnProperty.call(state.panels, key)) {
        if (VCS.ui && typeof VCS.ui.setAccordionOpen === 'function') {
          VCS.ui.setAccordionOpen(card, !!state.panels[key], true);
        } else {
          card.setAttribute('data-open', state.panels[key] ? '1' : '0');
          const toggle = card.querySelector(':scope > .acc-h > .acc-toggle');
          if (toggle) toggle.setAttribute('aria-expanded', state.panels[key] ? 'true' : 'false');
        }
      }
    });
  }

  function focusPageHeading(route, source) {
    if (source === 'history' || source === 'restore' || source === 'initial') return;
    const section = document.querySelector(
      `main section[data-page="${route.def.page}"]:not([data-shell-assistant])`);
    const heading = section && section.querySelector('h1,.pagebar h1');
    if (!heading) return;
    if (!heading.hasAttribute('tabindex')) heading.tabIndex = -1;
    try { heading.focus({ preventScroll: true }); } catch (_) { heading.focus(); }
  }

  async function selectProjectById(projectId, { syncPage = true } = {}) {
    const id = safeToken(projectId, PROJECT_TOKEN);
    const hit = projects.find(item => item.id === id) || null;
    if (!hit) return false;
    const previousProjectId = state.project_id;
    if (syncPage && window.Project && typeof window.Project.selectById === 'function') {
      const applied = await window.Project.selectById(hit.id);
      if (applied === false) return false;
    }
    selectedProject = hit;
    state.project_id = hit.id;
    if (previousProjectId !== hit.id) state.selected_job_id = '';
    renderContext();
    const select = document.getElementById('workspace-project');
    if (select && select.value !== hit.id) select.value = hit.id;
    document.dispatchEvent(new CustomEvent('vcs:workspace-project', {
      detail: {
        project_id: hit.id,
        name: hit.name,
        counts: { members: hit.n_members, done: hit.n_done,
          pending: Math.max(0, hit.n_members - hit.n_done) },
      },
    }));
    return true;
  }

  function routeCanApply(route, options = {}) {
    if (!route || !routeIsAvailable(route.def)) return false;
    const wantedProject = routeProject(route);
    if (!wantedProject || wantedProject === 'current') return true;
    if (projects.some(project => project.id === wantedProject)) return true;
    return !!(options.allowUnresolvedProject && !projects.length);
  }

  async function applyRouteNow(route, options = {}) {
    if (!route) return { ok: false, focused: false };
    const generation = options.generation;
    try {
      if (generation !== routeGeneration || !routeCanApply(route, options)) {
        return { ok: false, focused: false,
          superseded: generation !== routeGeneration, blocked: generation === routeGeneration };
      }
      const wantedProject = routeProject(route);
      if (wantedProject && wantedProject !== 'current' &&
          (wantedProject !== state.project_id || !selectedProject)) {
        if (!projects.length && options.allowUnresolvedProject) {
          state.project_id = wantedProject;
        } else {
          const selected = await selectProjectById(wantedProject, { syncPage: true });
          if (!selected) return { ok: false, focused: false, missingProject: true };
        }
      }
      if (generation !== routeGeneration) {
        return { ok: false, focused: false, superseded: true };
      }
      const ok = VCS.activatePage(route.def.page, null, {
        source: options.source || 'route', route: route.id, area: route.def.area,
      });
      if (!ok) return { ok: false, focused: false };
      currentRoute = route;
      state.route = route.hash;
      const analysisId = route.def.analysisId || route.def.analysis || '';
      if (analysisId) state.analysis_id = analysisId;
      renderRoute(route);
      renderContext();
      applyRouteControls(route);
      restorePanels();
      if (route.def.page === 'jobs' && state.selected_job_id && window.Jobs &&
          typeof window.Jobs.selectById === 'function') {
        const selectedJob = await window.Jobs.selectById(state.selected_job_id);
        if (selectedJob === false) state.selected_job_id = '';
        if (generation !== routeGeneration) {
          return { ok: false, focused: false, superseded: true };
        }
      }
      closeNav();
      const restoreY = options.restoreScroll ? Number(state.scroll[route.hash] || 0) : 0;
      const hasExplicitFocus = !!(options.focusJobDir || options.focusSelector || route.def.focus);
      // 等页面显示与滚动提交到下一帧后再做显式聚焦；否则随后运行的标题聚焦会
      // 抢走深链目标（例如 Templates 卡片或指定 Job 行）的键盘焦点。
      await new Promise(resolve => requestAnimationFrame(() => {
        window.scrollTo({ top: restoreY, left: 0, behavior: 'auto' });
        if (!hasExplicitFocus) focusPageHeading(route, options.source || 'route');
        renderCompactActions();
        resolve();
      }));
      if (generation !== routeGeneration) {
        return { ok: false, focused: false, superseded: true };
      }
      const focused = await VCS.focusNavigationTarget({
        focusJobDir: options.focusJobDir,
        focusSelector: options.focusSelector || route.def.focus,
      });
      if (generation !== routeGeneration) {
        return { ok: false, focused: false, superseded: true };
      }
      persistLocal({ remote: options.persist !== false });
      document.dispatchEvent(new CustomEvent('vcs:route', {
        detail: { id: route.id, area: route.def.area, page: route.def.page,
          hash: route.hash, params: route.params, query: route.query },
      }));
      return { ok: true, focused };
    } finally { /* generation 让最后一次导航获胜，无需全局互斥锁 */ }
  }

  function applyRoute(route, options = {}) {
    const generation = ++routeGeneration;
    const run = () => applyRouteNow(route, Object.assign({}, options, { generation }));
    const task = routeApplyTail.then(run, run);
    routeApplyTail = task.catch(() => undefined);
    return task;
  }

  function queueNavigation(operation) {
    const task = navigationTail.then(operation, operation);
    navigationTail = task.catch(() => undefined);
    return task;
  }

  async function performNavigateRoute(id, options = {}) {
    const locationIntent = Number.isSafeInteger(options.locationIntent)
      ? options.locationIntent : locationIntentGeneration;
    const def = ROUTES[id];
    if (!def) return { ok: false, focused: false };
    const hash = routeHash(id, options);
    const route = parseRoute(hash);
    if (!route) return { ok: false, focused: false };
    if (!routeCanApply(route)) {
      VCS.toast('当前模式或项目上下文无法打开此页面', 'fail');
      return { ok: false, focused: false, blocked: true };
    }
    if (!options.force && currentRoute && route.hash !== currentRoute.hash) {
      const allowed = await guardUnsaved(options.reason || '切换页面');
      if (!allowed) return { ok: false, focused: false, cancelled: true };
      if (locationIntent !== locationIntentGeneration) {
        return { ok: false, focused: false, superseded: true };
      }
    }
    saveCurrentScroll();
    const out = await applyRoute(route, Object.assign({}, options, {
      source: options.source || 'navigate',
    }));
    if (locationIntent !== locationIntentGeneration) {
      return { ok: false, focused: false, superseded: true };
    }
    if (!out || !out.ok) return out || { ok: false, focused: false };
    if (options.replace) {
      history.replaceState({ vcsRoute: route.id, vcsIndex: historyIndex }, '', route.hash);
    }
    else if (window.location.hash !== route.hash) {
      historyIndex += 1;
      history.pushState({ vcsRoute: route.id, vcsIndex: historyIndex }, '', route.hash);
    }
    return out;
  }

  function navigateRoute(id, options = {}) {
    const locationIntent = locationIntentGeneration;
    return queueNavigation(() => performNavigateRoute(
      id, Object.assign({}, options, { locationIntent })));
  }

  async function navigateLegacy(page, options = {}) {
    const name = String(page || '');
    const routeId = LEGACY_ROUTE[name];
    if (routeId === 'assistant') {
      const opened = workspace.assistant.open(null);
      return { ok: !!opened, focused: !!opened };
    }
    if (!routeId) return { ok: false, focused: false };
    return navigateRoute(routeId, options);
  }

  function renderDirty() {
    const chip = document.getElementById('workspace-dirty');
    if (!chip) return;
    const count = dirtyScopes.size;
    chip.hidden = !count;
    chip.textContent = count ? tr('workspace.unsaved.count', '未保存 {count}', { count }) : '';
    chip.setAttribute('aria-label', count
      ? tr('workspace.unsaved.count_aria', '{count} 处内容尚未保存', { count })
      : tr('workspace.unsaved.none', '没有未保存内容'));
  }

  VCS.unsaved = {
    mark(scope, label = '当前编辑', lifecycle = null) {
      const key = safeToken(scope) || 'workspace';
      const previous = dirtyScopes.get(key) || {};
      const hooks = lifecycle && typeof lifecycle === 'object' ? lifecycle : {};
      dirtyScopes.set(key, {
        label: String(label || previous.label || '当前编辑').slice(0, 120),
        at: Date.now(),
        canDiscard: typeof hooks.canDiscard === 'function' ? hooks.canDiscard : previous.canDiscard,
        discard: typeof hooks.discard === 'function' ? hooks.discard : previous.discard,
        save: typeof hooks.save === 'function' ? hooks.save : previous.save,
      });
      renderDirty();
      document.dispatchEvent(new CustomEvent('vcs:unsaved', {
        detail: { dirty: true, count: dirtyScopes.size },
      }));
    },
    clear(scope) {
      if (scope) dirtyScopes.delete(safeToken(scope)); else dirtyScopes.clear();
      renderDirty();
      document.dispatchEvent(new CustomEvent('vcs:unsaved', {
        detail: { dirty: dirtyScopes.size > 0, count: dirtyScopes.size },
      }));
      resolveRemoteDirtyHold();
    },
    isDirty() { return dirtyScopes.size > 0; },
    scopes() { return Array.from(dirtyScopes.keys()); },
    lifecycle(scope, lifecycle = {}) {
      const key = safeToken(scope) || 'workspace';
      const previous = dirtyScopes.get(key);
      if (!previous) return false;
      this.mark(key, previous.label, lifecycle);
      return true;
    },
    confirm: guardUnsaved,
  };

  function renderAssistantContext() {
    const set = (id, value) => {
      const element = document.getElementById(id);
      if (element) element.textContent = String(value == null || value === '' ? '—' : value);
    };
    set('assistant-context-project', selectedProject
      ? selectedProject.name || selectedProject.id
      : tr('ai.context.no_project', '未选择项目'));
    set('assistant-context-task', VCS.activeCalculation ||
      tr('ai.context.no_task', '未选择任务'));
    set('assistant-context-stage', selectedProject && selectedProject.stage || '—');
    const reportState = selectedProject && (
      selectedProject.scientific_status || selectedProject.artifact_status ||
      selectedProject.report_status);
    set('assistant-context-report', reportState ||
      tr('ai.context.report_unchecked', '尚未检查'));
    document.querySelectorAll('[data-assistant-route][data-project-required]').forEach(button => {
      button.disabled = !selectedProject;
      button.title = selectedProject ? '' : tr(
        'ai.context.project_required', '请先选择项目');
    });
  }

  function renderContext() {
    const projectSelect = document.getElementById('workspace-project');
    if (projectSelect) {
      const previous = projectSelect.value;
      const signature = projects.map(project =>
        `${project.id}\u0000${project.name}\u0000${project.n_done}/${project.n_members}`).join('\u0001');
      if (projectSelect.dataset.optionsSignature !== signature) {
        projectSelect.innerHTML = '<option value="">未选择项目</option>' + projects.map(project =>
          `<option value="${VCS.esc(project.id)}">${VCS.esc(project.name || '未命名项目')} · ` +
          `${VCS.esc(project.n_done)}/${VCS.esc(project.n_members)}</option>`).join('');
        projectSelect.dataset.optionsSignature = signature;
      }
      const wanted = selectedProject && selectedProject.id || state.project_id || previous;
      if (projects.some(project => project.id === wanted)) projectSelect.value = wanted;
      else projectSelect.value = '';
    }
    const engine = document.getElementById('workspace-engine');
    if (engine) {
      const key = String(VCS.activeEngine || 'vasp').toUpperCase();
      engine.textContent = key;
      engine.title = '当前生成意图的计算引擎；不改写已有作业事实';
    }
    const task = document.getElementById('workspace-task');
    if (task) {
      task.textContent = VCS.activeCalculation || '未选择任务';
      task.title = '当前生成意图；项目成员可以包含不同实际任务类型';
    }
    const stage = document.getElementById('workspace-stage');
    if (stage) {
      const value = selectedProject && selectedProject.stage;
      stage.textContent = value
        ? tr('workspace.context.stage_value', '阶段 {stage}', { stage: value })
        : (selectedProject
          ? tr('workspace.context.stage_unknown', '阶段未知')
          : tr('workspace.context.no_project', '未选择项目'));
      stage.classList.toggle('warn', !!(selectedProject && selectedProject.needs_human));
    }
    renderAssistantContext();
    renderActivity();
    renderDirty();
  }

  function normalizeProjectRows(rows, pipelineRows) {
    const pipelineById = new Map((pipelineRows || []).map(row => [
      safeToken(row.project_id, PROJECT_TOKEN), row,
    ]).filter(entry => entry[0]));
    return (rows || []).map(row => {
      const id = safeToken(row.project_id, PROJECT_TOKEN);
      if (!id) return null;
      const progress = pipelineById.get(id) || {};
      const counts = plainObject(row.counts);
      const progressCounts = plainObject(progress.counts);
      return {
        id,
        project_id: id,
        name: String(row.name || ''),
        n_members: Number(progressCounts.members != null ? progressCounts.members
          : progress.total != null ? progress.total
            : counts.members != null ? counts.members : row.n_members || 0),
        n_done: Number(progressCounts.done != null ? progressCounts.done
          : progress.done != null ? progress.done
            : counts.done != null ? counts.done : row.n_done || 0),
        stage: String(progress.stage || row.stage || ''),
        needs_human: !!(progress.needs_human || row.needs_human),
        artifact_status: String(progress.artifact_status || row.artifact_status || ''),
        scientific_status: String(progress.scientific_status || row.scientific_status || ''),
        scientific_qualification: String(
          progress.scientific_qualification || row.scientific_qualification || ''),
        report_status: String(progress.report_status || row.report_status || ''),
      };
    }).filter(Boolean);
  }

  async function syncLegacyProjectSelection() {
    if (!selectedProject || !window.Project) return true;
    const current = typeof window.Project.current === 'function'
      ? window.Project.current() : null;
    const currentId = safeToken(current && current.project_id, PROJECT_TOKEN);
    if (currentId === selectedProject.id) return true;
    try {
      if (typeof window.Project.selectById === 'function') {
        return !!(await window.Project.selectById(selectedProject.id));
      }
    } catch (_) { return false; }
    return false;
  }

  function remotePreferences(out) {
    const selection = plainObject(out && out.selection);
    const restore = plainObject(out && out.restore);
    const preferences = plainObject(out && out.preferences);
    const storedRoute = plainObject(preferences.route);
    return normalizeState({
      route: selection.route_hash || storedRoute.hash || '#/home',
      current_project_id: selection.project_id || preferences.current_project_id,
      current_analysis_id: selection.analysis_id || preferences.current_analysis_id,
      selected_job_id: selection.job_id || preferences.selected_job_id,
      panels: restore.panels || preferences.panels,
      filters: restore.filters || preferences.filters,
      sort: restore.sort || preferences.sort,
      scroll: restore.scroll || preferences.scroll,
      draft_refs: restore.draft_refs || preferences.draft_refs,
      updated_at_ms: 0,
    });
  }

  function completeWorkspaceSnapshot(out) {
    if (!out || out.ok !== true || out.schema !== SCHEMA ||
        !safeAuthorityId(out.authority_id)) return false;
    const serverRevision = Number(out.state_revision);
    return Number.isSafeInteger(serverRevision) && serverRevision >= 0 &&
      !!out.selection && typeof out.selection === 'object' && !Array.isArray(out.selection) &&
      !!out.restore && typeof out.restore === 'object' && !Array.isArray(out.restore) &&
      Array.isArray(out.projects);
  }

  async function refreshWorkspaceContext() {
    const refreshGeneration = ++contextRefreshGeneration;
    let adoptedRemoteRoute = false;
    lastContextRefreshAt = Date.now();
    if (remoteSaveInFlight) await remoteSaveInFlight;
    if (refreshGeneration !== contextRefreshGeneration) return;
    let aggregated = null;
    try { aggregated = await VCS.call('workspace_context'); } catch (_) { aggregated = null; }
    if (!aggregated || aggregated.error === '桥方法不存在:workspace_context') {
      try { aggregated = await VCS.call('workspace_context_get'); } catch (_) { aggregated = null; }
    }
    if (refreshGeneration !== contextRefreshGeneration) return;
    if (aggregated && aggregated.ok && !completeWorkspaceSnapshot(aggregated)) {
      remoteSaveBlocked = true;
      return;
    }
    if (completeWorkspaceSnapshot(aggregated)) {
      const serverAuthorityId = safeAuthorityId(aggregated.authority_id);
      const serverRevision = Math.max(0,
        Number(aggregated.state_revision || aggregated.revision || 0) || 0);
      const sameAuthority = authorityId && serverAuthorityId === authorityId;
      if (sameAuthority && serverRevision < revision) return;
      const authorityReset = !!authorityId && serverAuthorityId !== authorityId;
      const remote = remotePreferences(aggregated);
      const localBaseRevision = Math.max(0, Number(state.server_revision || 0) || 0);
      const shouldAdoptRemote = authorityReset || !authorityId || remoteSaveBlocked ||
        state.updated_at_ms <= 0 ||
        localBaseRevision !== serverRevision;
      if (shouldAdoptRemote) {
        const localInteraction = state;
        const preserveDirtyInteraction = dirtyScopes.size > 0;
        const localDraftRefs = plainObject(state.draft_refs);
        state = remote;
        // Draft bodies remain local-only.  Keep their references while adopting
        // the server snapshot so CAS never pairs a fresh revision with stale UI state.
        state.draft_refs = Object.assign({}, plainObject(remote.draft_refs), localDraftRefs);
        if (preserveDirtyInteraction) {
          state.route = currentRoute ? currentRoute.hash : localInteraction.route;
          state.project_id = localInteraction.project_id;
          state.analysis_id = localInteraction.analysis_id;
          state.selected_job_id = localInteraction.selected_job_id;
          state.panels = localInteraction.panels;
          state.filters = localInteraction.filters;
          state.sort = localInteraction.sort;
          state.scroll = localInteraction.scroll;
        } else if (initialHashWasExplicit && currentRoute) {
          state.route = currentRoute.hash;
          const explicitProject = routeProject(currentRoute);
          if (explicitProject && explicitProject !== 'current') state.project_id = explicitProject;
        }
        state.server_authority_id = serverAuthorityId;
        state.server_revision = serverRevision;
        state.updated_at_ms = Date.now();
        remoteSaveRequested = false;
        persistLocal({ remote: false, touch: false });
        remoteDirtyHold = preserveDirtyInteraction;
        adoptedRemoteRoute = !preserveDirtyInteraction && !initialHashWasExplicit;
      } else {
        state.server_authority_id = serverAuthorityId;
        state.server_revision = serverRevision;
      }
      authorityId = serverAuthorityId;
      revision = serverRevision;
      remoteSaveBlocked = remoteDirtyHold;
      projects = normalizeProjectRows(aggregated.projects || [], []);
    } else {
      const [listed, pipeline] = await Promise.all([
        VCS.call('proj_list'), VCS.call('pipeline_status'),
      ]);
      if (refreshGeneration !== contextRefreshGeneration) return;
      projects = normalizeProjectRows((listed && listed.projects) || [],
        (pipeline && pipeline.projects) || []);
    }

    const selectionRoute = adoptedRemoteRoute ? parseRoute(state.route) : currentRoute;
    const targetId = routeProject(selectionRoute) || state.project_id;
    if (targetId && targetId !== 'current') {
      selectedProject = projects.find(project => project.id === targetId) || null;
      if (!selectedProject && projects.length && isProjectRoute(selectionRoute)) {
        if (dirtyScopes.size) {
          VCS.toast('当前项目已移动、删除或未注册；请先处理未保存内容', 'fail');
        } else {
          VCS.toast('深链接中的项目已移动、删除或未注册，已返回首页', 'fail');
          state.project_id = '';
          await navigateRoute('home', { replace: true, force: true, source: 'missing-project' });
        }
      }
    } else if (state.project_id) {
      selectedProject = projects.find(project => project.id === state.project_id) || null;
    }
    if (!selectedProject && !state.project_id && projects.length) {
      // 首次进入不替用户暗选最后项目；上下文栏保持明确“未选择”。
      selectedProject = null;
    }
    if (selectedProject) await syncLegacyProjectSelection();
    if (refreshGeneration !== contextRefreshGeneration) return;
    renderContext();
    updateRouteLinks();
    if (!initialHashWasExplicit && state.route && state.route !== window.location.hash) {
      const restored = parseRoute(state.route);
      if (restored) {
        const applied = await applyRoute(restored, {
          source: 'restore', restoreScroll: true, persist: false,
        });
        if (applied && applied.ok) {
          history.replaceState(
            { vcsRoute: restored.id, vcsIndex: historyIndex }, '', restored.hash);
        }
      }
    }
    if (initialHashWasExplicit) {
      initialHashWasExplicit = false;
      persistLocal();
    }
  }

  function scheduleContextRefresh() {
    if (contextRefreshTimer) return;
    const remaining = Math.max(0, 15000 - (Date.now() - lastContextRefreshAt));
    contextRefreshTimer = setTimeout(async () => {
      contextRefreshTimer = null;
      await refreshWorkspaceContext();
    }, remaining);
  }

  function renderActivity() {
    const list = document.getElementById('activity-list');
    const toggle = document.getElementById('activity-toggle');
    const pipelineEvents = VCS.pipeline && Array.isArray(VCS.pipeline.events) ? VCS.pipeline.events : [];
    const operationEvents = Array.from(operationRecords.values()).reverse().map(operation => ({
      kind: 'operation', source: 'operation', operation,
      project: operation.label,
      text: tr('workspace.operation.summary', '{count} 项 · {status}{error}', {
        count: operation.count,
        status: operation.status,
        error: operation.error ? ` · ${operation.error}` : '',
      }),
      time: operation.updated_at,
    }));
    const events = operationEvents.concat(pipelineEvents);
    const runtime = VCS.pipeline && VCS.pipeline.runtime || {};
    const blockers = projects.filter(project => project.needs_human).length;
    const errors = events.filter(event => event.kind === 'error').length;
    const activeOperations = operationEvents.filter(event =>
      ['pending', 'confirming', 'running'].includes(event.operation.status)).length;
    const count = blockers + errors + activeOperations + (runtime.tick_running ? 1 : 0);
    if (toggle) {
      const badge = toggle.querySelector('[data-activity-count]');
      if (badge) { badge.textContent = String(count); badge.hidden = !count; }
      toggle.setAttribute('aria-label', count
        ? tr('workspace.activity.attention', `活动中心，${count} 项需关注`, { count })
        : tr('workspace.activity.clear', '活动中心，没有待处理提醒'));
    }
    if (!list) return;
    if (!events.length) {
      const empty = document.createElement('div'); empty.className = 'workspace-empty';
      empty.textContent = tr('workspace.activity.empty',
        '本次会话暂无后台活动。作业状态史与报告证据仍保存在各自项目中。');
      list.replaceChildren(empty);
      return;
    }
    const labels = { refresh: ['workspace.activity.refresh', '同步'],
      continue: ['workspace.activity.continue', '续算'], fetch: ['workspace.activity.fetch', '下载'],
      report_done: ['workspace.activity.report_done', '报告'],
      report_blocked: ['workspace.activity.report_blocked', '报告阻断'],
      skip: ['workspace.activity.skip', '跳过'], error: ['workspace.activity.error', '错误'] };
    list.innerHTML = events.slice(0, 40).map(event => {
      const severity = event.kind === 'error' || event.operation && event.operation.status === 'failed'
        ? 'error' : event.kind === 'report_done' || event.operation && event.operation.status === 'succeeded'
          ? 'success' : 'info';
      const label = labels[event.kind];
      const route = event.operation && event.operation.route;
      const action = event.operation && route
        ? (event.operation.status === 'failed'
          ? tr('workspace.operation.retry', '打开并重试')
          : tr('workspace.operation.open', '打开')) : '';
      return `<div class="activity-item ${severity}"${route ? ` data-operation-route="${VCS.esc(route)}" role="link" tabindex="0"` : ''}><span class="activity-kind">${VCS.esc(event.operation
        ? tr('workspace.activity.operation', '操作')
        : label ? tr(label[0], label[1]) : event.kind || tr('workspace.activity.item', '活动'))}</span>` +
        `<div><b>${VCS.esc(event.project || event.cluster || tr('workspace.context.workspace', '工作区'))}</b>` +
        `<p>${VCS.esc(event.text || '')}</p>${action ? `<small>${VCS.esc(action)}</small>` : ''}</div>` +
        `<time>${VCS.esc(event.time || '')}</time></div>`;
    }).join('');
  }

  function focusableIn(container) {
    return Array.from(container.querySelectorAll(
      'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
      'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'))
      .filter(el => {
        if (el.closest('[hidden],[inert],[aria-hidden="true"],fieldset[disabled]')) return false;
        const style = typeof window.getComputedStyle === 'function'
          ? window.getComputedStyle(el) : null;
        if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
        return !el.getClientRects || el.getClientRects().length > 0;
      });
  }

  const drawerIsolation = new Map();

  function restoreDrawerIsolation() {
    drawerIsolation.forEach((saved, element) => {
      if (!element || !element.isConnected) return;
      if (saved.inert) element.setAttribute('inert', ''); else element.removeAttribute('inert');
      if (saved.ariaHidden == null) element.removeAttribute('aria-hidden');
      else element.setAttribute('aria-hidden', saved.ariaHidden);
    });
    drawerIsolation.clear();
  }

  function isolateDrawer(drawer, backdrop) {
    restoreDrawerIsolation();
    if (!drawer) return;
    const keep = new Set([drawer]);
    if (backdrop) keep.add(backdrop);
    let ancestor = drawer.parentElement;
    while (ancestor && ancestor !== document.body) {
      keep.add(ancestor); ancestor = ancestor.parentElement;
    }
    function save(element) {
      if (!drawerIsolation.has(element)) {
        drawerIsolation.set(element, {
          inert: element.hasAttribute('inert'),
          ariaHidden: element.getAttribute('aria-hidden'),
        });
      }
    }
    function visit(parent) {
      Array.from(parent.children).forEach(element => {
        if (keep.has(element)) {
          if (element === drawer) {
            element.removeAttribute('inert');
            element.setAttribute('aria-hidden', 'false');
          } else if (element !== backdrop) visit(element);
          return;
        }
        save(element);
        element.setAttribute('inert', '');
        element.setAttribute('aria-hidden', 'true');
      });
    }
    visit(document.body);
    if (backdrop) backdrop.setAttribute('aria-hidden', 'true');
  }

  function trapDrawerFocus(event, drawer, close) {
    if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    if (event.key !== 'Tab') return;
    const items = focusableIn(drawer);
    if (!items.length) { event.preventDefault(); drawer.focus(); return; }
    const first = items[0]; const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  }

  function openNav(trigger) {
    const nav = document.getElementById('shell-nav');
    const backdrop = document.getElementById('nav-backdrop');
    const button = document.getElementById('nav-toggle');
    if (!nav || window.matchMedia('(min-width: 960px)').matches) return false;
    navReturnFocus = trigger || document.activeElement;
    nav.classList.add('open');
    // WebView/background tabs may throttle CSS transitions indefinitely;
    // the inline target keeps the drawer immediately operable and is removed on close.
    nav.style.transition = 'none';
    nav.style.transform = 'translateX(0)';
    nav.removeAttribute('inert');
    nav.inert = false;
    nav.setAttribute('aria-hidden', 'false');
    if (backdrop) backdrop.hidden = false;
    if (button) button.setAttribute('aria-expanded', 'true');
    document.body.classList.add('drawer-open');
    document.body.classList.add('nav-open');
    const first = nav.querySelector('a.on') || nav.querySelector('a,button');
    if (first) first.focus();
    return true;
  }

  function closeNav() {
    const nav = document.getElementById('shell-nav');
    const backdrop = document.getElementById('nav-backdrop');
    const button = document.getElementById('nav-toggle');
    if (!nav) return;
    const wasOpen = nav.classList.contains('open');
    nav.classList.remove('open');
    if (window.matchMedia('(max-width: 959px)').matches) {
      nav.style.transition = 'none';
      nav.style.transform = 'translateX(-105%)';
      nav.setAttribute('aria-hidden', 'true');
      nav.setAttribute('inert', '');
      nav.inert = true;
    } else {
      nav.style.removeProperty('transition');
      nav.style.removeProperty('transform');
      nav.removeAttribute('aria-hidden');
      nav.removeAttribute('inert');
      nav.inert = false;
    }
    if (backdrop) backdrop.hidden = true;
    if (button) button.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('drawer-open');
    document.body.classList.remove('nav-open');
    if (wasOpen && navReturnFocus && navReturnFocus.isConnected) navReturnFocus.focus();
    navReturnFocus = null;
  }

  function openActivity(trigger) {
    const drawer = document.getElementById('activity-drawer');
    const backdrop = document.getElementById('activity-backdrop');
    if (!drawer) return false;
    closeAssistant(); closeNav();
    activityReturnFocus = trigger || document.activeElement;
    drawer.hidden = false;
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    drawer.setAttribute('aria-modal', 'true');
    if (backdrop) backdrop.hidden = false;
    isolateDrawer(drawer, backdrop);
    const button = document.getElementById('activity-toggle');
    if (button) button.setAttribute('aria-expanded', 'true');
    renderActivity();
    const close = document.getElementById('activity-close');
    (close || drawer).focus();
    return true;
  }

  function closeActivity() {
    const drawer = document.getElementById('activity-drawer');
    const backdrop = document.getElementById('activity-backdrop');
    if (!drawer || drawer.hidden) return;
    drawer.classList.remove('open'); drawer.hidden = true;
    drawer.setAttribute('aria-hidden', 'true');
    drawer.setAttribute('aria-modal', 'false');
    if (backdrop) backdrop.hidden = true;
    restoreDrawerIsolation();
    const button = document.getElementById('activity-toggle');
    if (button) button.setAttribute('aria-expanded', 'false');
    if (activityReturnFocus && activityReturnFocus.isConnected) activityReturnFocus.focus();
    activityReturnFocus = null;
  }

  function openAssistant(trigger) {
    const drawer = document.getElementById('page-ai');
    const backdrop = document.getElementById('assistant-backdrop');
    const gate = document.getElementById('assistant-toggle');
    if ((gate && gate.hasAttribute('data-scene-hidden')) ||
        (VCS.scenario && typeof VCS.sceneVisible === 'function' &&
          !VCS.sceneVisible(VCS.scenario, 'pages.ai'))) {
      VCS.toast('当前工作模式未启用上下文助手', 'fail');
      return false;
    }
    if (!drawer) return false;
    closeActivity(); closeNav();
    assistantReturnFocus = trigger || document.activeElement;
    drawer.hidden = false;
    drawer.classList.add('open');
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-modal', 'true');
    drawer.setAttribute('aria-hidden', 'false');
    drawer.setAttribute('aria-label', '项目上下文助手');
    if (backdrop) backdrop.hidden = false;
    isolateDrawer(drawer, backdrop);
    const button = document.getElementById('assistant-toggle');
    if (button) button.setAttribute('aria-expanded', 'true');
    document.body.classList.add('assistant-open');
    renderAssistantContext();
    document.dispatchEvent(new CustomEvent('vcs:page', {
      detail: { page: 'ai', overlay: true, source: 'assistant-drawer' },
    }));
    let close = drawer.querySelector('[data-assistant-close]');
    if (!close) {
      close = document.createElement('button');
      close.type = 'button'; close.className = 'btn quiet assistant-close';
      close.dataset.assistantClose = '1'; close.textContent = '关闭助手';
      drawer.insertBefore(close, drawer.firstChild);
      close.addEventListener('click', closeAssistant);
    }
    close.focus();
    return true;
  }

  function closeAssistant() {
    const drawer = document.getElementById('page-ai');
    const backdrop = document.getElementById('assistant-backdrop');
    if (!drawer || drawer.hidden) return;
    drawer.classList.remove('open'); drawer.hidden = true;
    drawer.setAttribute('aria-modal', 'false');
    drawer.setAttribute('aria-hidden', 'true');
    if (backdrop) backdrop.hidden = true;
    restoreDrawerIsolation();
    const button = document.getElementById('assistant-toggle');
    if (button) button.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('assistant-open');
    if (assistantReturnFocus && assistantReturnFocus.isConnected) assistantReturnFocus.focus();
    assistantReturnFocus = null;
  }

  function renderCompactActions() {
    const bar = document.getElementById('compact-actions');
    if (!bar) return;
    bar.innerHTML = '';
    const section = document.querySelector(
      'main section[data-page]:not([data-shell-assistant]):not([hidden])');
    if (!section) return;
    // 首页四种起点同等重要，不用“前三个 primary”在窄屏暗中替用户排序。
    // 其它页面优先采用显式 data-compact-primary；没有声明时只镜像一个主动作。
    if (section.dataset.page === 'dashboard') { bar.hidden = true; return; }
    const declared = Array.from(section.querySelectorAll('[data-compact-primary]:not([disabled])'));
    const candidates = declared.length ? declared : Array.from(section.querySelectorAll(
      '.actions .btn.primary:not([disabled]), .pagebar .btn.primary:not([disabled]), ' +
      '.start-actions .start-action.primary:not([disabled])'));
    const actions = candidates
      .filter(button => {
        if (button.closest('#compact-actions')) return false;
        if (button.closest('[hidden],[aria-hidden="true"],[data-scene-hidden],'
          + '[data-engine-hidden],[data-task-hidden],.acc[data-open="0"]')) return false;
        const style = typeof window.getComputedStyle === 'function'
          ? window.getComputedStyle(button) : null;
        return !style || (style.display !== 'none' && style.visibility !== 'hidden');
      }).slice(0, 1);
    actions.forEach(original => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'btn primary';
      button.textContent = (original.textContent || original.getAttribute('aria-label') || '执行').trim();
      button.addEventListener('click', () => original.click());
      bar.appendChild(button);
    });
    bar.hidden = !actions.length;
  }

  async function requestProjectSwitch(projectId, apply) {
    const generation = ++projectSwitchGeneration;
    const id = safeToken(projectId, PROJECT_TOKEN);
    if (id === state.project_id) {
      if (typeof apply === 'function') {
        const applied = await apply();
        if (applied === false || generation !== projectSwitchGeneration) return false;
      }
      return true;
    }
    const allowed = await guardUnsaved('切换项目');
    if (!allowed || generation !== projectSwitchGeneration) return false;
    if (!id) {
      if (typeof apply === 'function') {
        const applied = await apply(null);
        if (applied === false || generation !== projectSwitchGeneration) return false;
      }
      selectedProject = null;
      state.project_id = '';
      state.selected_job_id = '';
      state.analysis_id = '';
      state.filters = {};
      state.sort = {};
      persistLocal(); renderContext(); updateRouteLinks();
      document.dispatchEvent(new CustomEvent('vcs:workspace-project', {
        detail: { project_id: '', cleared: true },
      }));
      if (currentRoute && isProjectRoute(currentRoute)) {
        await navigateRoute('home', { replace: true, force: true, source: 'project-clear' });
      }
      return true;
    }
    const hit = projects.find(project => project.id === id);
    if (!hit) return false;
    if (typeof apply === 'function') {
      const applied = await apply(hit);
      if (applied === false || generation !== projectSwitchGeneration) return false;
    }
    selectedProject = hit; state.project_id = id;
    state.selected_job_id = ''; state.analysis_id = '';
    state.filters = {}; state.sort = {};
    persistLocal(); renderContext(); updateRouteLinks();
    document.dispatchEvent(new CustomEvent('vcs:workspace-project', {
      detail: {
        project_id: hit.id,
        name: hit.name,
        counts: { members: hit.n_members, done: hit.n_done,
          pending: Math.max(0, hit.n_members - hit.n_done) },
      },
    }));
    if (currentRoute && isProjectRoute(currentRoute)) {
      await navigateRoute(currentRoute.id, { replace: true, force: true, projectId: id,
        query: currentRoute.query, source: 'project-switch' });
    }
    return true;
  }

  const drafts = {
    save(id, text, metadata = {}) {
      const key = safeToken(id);
      if (!key) throw new Error('草稿标识无效');
      const body = String(text || '');
      if (body.length > MAX_DRAFT_CHARS) throw new Error('草稿超过 64 KiB，请先保存到项目文件');
      const record = { schema: 'vcstudio.unverified-draft/v1', id: key, text: body,
        project_id: state.project_id || null, route: state.route,
        updated_at_ms: Date.now(), metadata: smallRecord(metadata, 20) };
      localStorage.setItem(DRAFT_PREFIX + key, JSON.stringify(record));
      state.draft_refs[key] = { project_id: record.project_id, route: record.route,
        updated_at_ms: record.updated_at_ms, dirty: true, status: 'unverified_draft',
        size: body.length };
      VCS.unsaved.mark('draft-' + key, metadata.label || '未完成草稿');
      persistLocal();
      return record;
    },
    load(id) {
      const key = safeToken(id);
      if (!key) return null;
      try { return JSON.parse(localStorage.getItem(DRAFT_PREFIX + key) || 'null'); }
      catch (_) { return null; }
    },
    remove(id) {
      const key = safeToken(id); if (!key) return;
      localStorage.removeItem(DRAFT_PREFIX + key);
      delete state.draft_refs[key];
      VCS.unsaved.clear('draft-' + key);
      persistLocal();
    },
  };

  function restoreCommittedLocation(route = currentRoute) {
    const fallback = route || parseRoute(state.route) || parseRoute('#/home');
    if (fallback) history.replaceState(
      { vcsRoute: fallback.id, vcsIndex: historyIndex }, '', fallback.hash);
  }

  function restoreExternalLocation(source, targetIndex, route = currentRoute) {
    if (source === 'popstate' && Number.isSafeInteger(targetIndex) &&
        targetIndex !== historyIndex) {
      historyCompensation = { hash: route && route.hash, index: historyIndex };
      history.go(historyIndex - targetIndex);
      return;
    }
    if ((source === 'hashchange' || source === 'popstate') && targetIndex === null && route) {
      // A manually assigned hash creates a new, unindexed entry.  Remove that
      // entry instead of replacing it with a duplicate of the current route.
      historyCompensation = { hash: route.hash, index: historyIndex };
      history.back();
      return;
    }
    restoreCommittedLocation(route);
  }

  function handleExternalNavigation(source, event) {
    const requestedHash = window.location.hash;
    const eventIndex = Number(event && event.state && event.state.vcsIndex);
    const targetIndex = Number.isSafeInteger(eventIndex) ? eventIndex : null;
    if (historyCompensation && requestedHash === historyCompensation.hash &&
        (targetIndex === null || targetIndex === historyCompensation.index)) {
      locationIntentGeneration += 1;
      historyCompensation = null;
      return Promise.resolve({ ok: false, compensated: true });
    }
    if (externalNavigationPromise && externalNavigationHash === requestedHash) {
      return externalNavigationPromise;
    }
    const locationIntent = ++locationIntentGeneration;
    externalNavigationHash = requestedHash;
    const task = queueNavigation(async () => {
      if (locationIntent !== locationIntentGeneration || window.location.hash !== requestedHash) {
        return { ok: false, superseded: true };
      }
      const route = parseRoute(requestedHash);
      if (!route) {
        restoreExternalLocation(source, targetIndex);
        VCS.toast('链接无效，已保留当前页面', 'fail');
        return { ok: false, blocked: true };
      }
      if (currentRoute && route.hash === currentRoute.hash) {
        if (targetIndex !== null) historyIndex = targetIndex;
        history.replaceState(
          { vcsRoute: route.id, vcsIndex: historyIndex }, '', route.hash);
        return { ok: true, unchanged: true };
      }
      if (!routeCanApply(route)) {
        restoreExternalLocation(source, targetIndex);
        VCS.toast('链接指向的页面在当前模式或项目上下文中不可用', 'fail');
        return { ok: false, blocked: true };
      }
      const allowed = await guardUnsaved('返回到其他页面');
      if (locationIntent !== locationIntentGeneration || window.location.hash !== requestedHash) {
        return { ok: false, superseded: true };
      }
      if (!allowed) {
        restoreExternalLocation(source, targetIndex);
        return { ok: false, cancelled: true };
      }
      const previous = currentRoute;
      saveCurrentScroll();
      const out = await applyRoute(route, { source: source || 'history', restoreScroll: true });
      if (locationIntent !== locationIntentGeneration || window.location.hash !== requestedHash) {
        return { ok: false, superseded: true };
      }
      if (!out || !out.ok) {
        restoreExternalLocation(source, targetIndex, previous);
        return out || { ok: false };
      }
      // The browser already moved to this history entry.  Only attach canonical
      // state after the route itself succeeds; never leave a new hash on old UI.
      if (targetIndex !== null) historyIndex = targetIndex;
      else historyIndex += 1;
      history.replaceState(
        { vcsRoute: route.id, vcsIndex: historyIndex }, '', route.hash);
      return out;
    });
    externalNavigationPromise = task.catch(() => ({ ok: false })).finally(() => {
      if (externalNavigationHash === requestedHash) {
        externalNavigationPromise = null;
        externalNavigationHash = '';
      }
    });
    return externalNavigationPromise;
  }

  function wireShell() {
    document.addEventListener('click', async event => {
      const skip = event.target.closest && event.target.closest('.skip-link[href="#workspace-main"]');
      if (skip) {
        event.preventDefault();
        const main = document.getElementById('workspace-main');
        if (main) {
          if (typeof main.scrollIntoView === 'function') main.scrollIntoView({ block: 'start' });
          main.focus();
        }
        return;
      }
      const routeLink = event.target.closest && event.target.closest('a[data-route]');
      if (routeLink) {
        event.preventDefault();
        const linkedRoute = parseRoute(routeLink.hash || routeLink.getAttribute('href'));
        const query = linkedRoute ? linkedRoute.query : {};
        await navigateRoute(routeLink.dataset.route, { query, source: 'navigation' });
        return;
      }
      const toggle = event.target.closest && event.target.closest('#nav-toggle');
      if (toggle) { event.preventDefault(); openNav(toggle); return; }
      if (event.target.closest && event.target.closest('#nav-close')) { closeNav(); return; }
      if (event.target.closest && event.target.closest('#nav-backdrop')) { closeNav(); return; }
      const activity = event.target.closest && event.target.closest('#activity-toggle');
      if (activity) { event.preventDefault(); openActivity(activity); return; }
      if (event.target.closest && event.target.closest('#activity-close')) { closeActivity(); return; }
      if (event.target.closest && event.target.closest('#activity-backdrop')) { closeActivity(); return; }
      const operationRoute = event.target.closest && event.target.closest('[data-operation-route]');
      if (operationRoute) {
        event.preventDefault(); closeActivity();
        await navigateRoute(operationRoute.dataset.operationRoute, { source: 'operation-queue' });
        return;
      }
      const assistant = event.target.closest && event.target.closest('#assistant-toggle');
      if (assistant) { event.preventDefault(); openAssistant(assistant); return; }
      const assistantAction = event.target.closest && event.target.closest('[data-assistant-route]');
      if (assistantAction) {
        event.preventDefault();
        if (assistantAction.hasAttribute('data-project-required') && !selectedProject) {
          VCS.toast(tr('ai.context.project_required', '请先选择项目'), 'fail');
          return;
        }
        const routeId = safeToken(assistantAction.dataset.assistantRoute, ROUTE_TOKEN);
        closeAssistant();
        await navigateRoute(routeId, {
          projectId: selectedProject && selectedProject.id || undefined,
          source: 'assistant-context',
        });
        return;
      }
      if (event.target.closest && event.target.closest('#assistant-backdrop')) closeAssistant();
    });

    const project = document.getElementById('workspace-project');
    if (project) project.addEventListener('change', async () => {
      const previous = state.project_id;
      const wanted = project.value;
      const ok = await requestProjectSwitch(wanted, async hit => {
        if (hit && window.Project && typeof window.Project.selectById === 'function') {
          return window.Project.selectById(hit.id);
        }
        return true;
      });
      if (!ok) project.value = previous;
    });

    const nav = document.getElementById('shell-nav');
    if (nav) nav.addEventListener('keydown', event => {
      if (nav.classList.contains('open')) trapDrawerFocus(event, nav, closeNav);
    });
    const activityDrawer = document.getElementById('activity-drawer');
    if (activityDrawer) activityDrawer.addEventListener('keydown', event => {
      const route = event.target.closest && event.target.closest('[data-operation-route]');
      if (route && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault(); route.click(); return;
      }
      trapDrawerFocus(event, activityDrawer, closeActivity);
    });
    const assistantDrawer = document.getElementById('page-ai');
    if (assistantDrawer) assistantDrawer.addEventListener('keydown', event =>
      trapDrawerFocus(event, assistantDrawer, closeAssistant));

    window.addEventListener('beforeunload', event => {
      if (!dirtyScopes.size) return;
      event.preventDefault(); event.returnValue = '';
    });
    window.addEventListener('scroll', () => {
      if (scrollTimer) clearTimeout(scrollTimer);
      scrollTimer = setTimeout(saveCurrentScroll, 300);
    }, { passive: true });
    window.addEventListener('resize', () => {
      const nav = document.getElementById('shell-nav');
      if (window.matchMedia('(min-width: 960px)').matches ||
          (nav && !nav.classList.contains('open'))) closeNav();
      renderCompactActions();
    });
    window.addEventListener('popstate', event => handleExternalNavigation('popstate', event));
    window.addEventListener('hashchange', event => handleExternalNavigation('hashchange', event));

    document.addEventListener('change', event => {
      const target = event.target;
      if (!target || !target.id) return;
      if (target.id === 'analysis-type') {
        state.analysis_id = safeToken(target.value);
      } else if (target.id === 'jf-cluster') {
        state.filters.job_cluster = String(target.value || '').slice(0, 160);
      } else if (target.id === 'jf-status') {
        state.filters.job_status = String(target.value || '').slice(0, 80);
      } else if (target.id === 'jf-sort') {
        state.sort.jobs = String(target.value || '').slice(0, 80);
      } else return;
      persistLocal();
    });
    document.addEventListener('input', event => {
      const target = event.target;
      if (!target || !target.matches || !target.matches('[data-workspace-draft]')) return;
      VCS.unsaved.mark(target.getAttribute('data-workspace-draft') || target.id || 'draft',
        target.getAttribute('data-dirty-label') || '当前草稿');
    });
    document.addEventListener('click', event => {
      const header = event.target.closest && event.target.closest('[data-acc]>.acc-h');
      if (!header) return;
      requestAnimationFrame(() => {
        const card = header.closest('[data-acc]');
        const key = safeToken(card && card.getAttribute('data-acc'));
        if (!key) return;
        state.panels[key] = card.getAttribute('data-open') === '1';
        persistLocal();
      });
    });

    document.addEventListener('vcs:scenario', () => {
      if (currentRoute) renderRoute(currentRoute);
      renderContext();
    });
    document.addEventListener('vcs:language', () => {
      if (currentRoute) renderRoute(currentRoute);
      renderContext(); renderActivity();
    });
    document.addEventListener('vcs:engine', renderContext);
    document.addEventListener('vcs:calculation', renderContext);
    document.addEventListener('vcs:pipeline-runtime', () => {
      renderContext(); renderActivity(); scheduleContextRefresh();
    });
    document.addEventListener('vcs:pipeline-events', () => {
      renderActivity(); scheduleContextRefresh();
    });
    document.addEventListener('vcs:project-context', event => {
      const detail = plainObject(event.detail);
      const id = safeToken(detail.project_id, PROJECT_TOKEN);
      if (!id) return;
      const hit = projects.find(project => project.id === id);
      if (hit) {
        if (state.project_id !== id) state.selected_job_id = '';
        const counts = plainObject(detail.counts);
        hit.name = String(detail.name || hit.name || '');
        hit.n_members = Number(counts.members != null ? counts.members : hit.n_members || 0);
        hit.n_done = Number(counts.done != null ? counts.done : hit.n_done || 0);
        selectedProject = hit; state.project_id = id; persistLocal(); renderContext(); updateRouteLinks();
      }
    });
    document.addEventListener('vcs:job-context', async event => {
      const detail = plainObject(event.detail);
      const id = safeToken(detail.id || detail.job_id || detail.job_uuid, JOB_TOKEN);
      if (!id) {
        if (detail.clear === true || detail.cleared === true || detail.id === null) {
          if (!pendingJobProjectSwitch) jobContextGeneration += 1;
          state.selected_job_id = '';
          persistLocal();
          renderContext();
        }
        return;
      }
      const generation = ++jobContextGeneration;
      const jobProjectId = safeToken(detail.project_id, PROJECT_TOKEN);
      if (jobProjectId && jobProjectId !== state.project_id) {
        const pending = { generation, id, project_id: jobProjectId };
        pendingJobProjectSwitch = pending;
        const switched = await requestProjectSwitch(jobProjectId, async hit => {
          if (!hit || !window.Project || typeof window.Project.selectById !== 'function') return true;
          return window.Project.selectById(hit.id);
        });
        if (generation !== jobContextGeneration || pendingJobProjectSwitch !== pending) return;
        if (!switched) {
          pendingJobProjectSwitch = null;
          if (window.Jobs && typeof window.Jobs.clearSelection === 'function') {
            window.Jobs.clearSelection();
          }
          state.selected_job_id = '';
          persistLocal(); renderContext();
          return;
        }
        if (window.Jobs && typeof window.Jobs.selectById === 'function') {
          const selected = await window.Jobs.selectById(id);
          if (selected === false && generation === jobContextGeneration) {
            if (typeof window.Jobs.clearSelection === 'function') window.Jobs.clearSelection();
            state.selected_job_id = '';
            persistLocal(); renderContext();
          }
        }
        if (pendingJobProjectSwitch === pending) pendingJobProjectSwitch = null;
        return;
      }
      state.selected_job_id = id;
      persistLocal();
      renderContext();
    });

    const observer = new MutationObserver(() => {
      clearTimeout(observer._timer);
      observer._timer = setTimeout(renderCompactActions, 80);
    });
    const main = document.querySelector('main');
    if (main) observer.observe(main, { subtree: true, childList: true, attributes: true,
      attributeFilter: ['hidden', 'disabled', 'class', 'style', 'aria-hidden', 'data-open',
        'data-scene-hidden', 'data-engine-hidden', 'data-task-hidden'] });
  }

  const workspace = {
    schema: SCHEMA,
    routes: ROUTES,
    get current() { return currentRoute; },
    get state() { return Object.assign({}, state); },
    get projects() { return projects.slice(); },
    navigateRoute,
    navigateLegacy,
    parseRoute,
    routeHash,
    refresh: refreshWorkspaceContext,
    requestProjectSwitch,
    drafts,
    assistant: { open: openAssistant, close: closeAssistant },
    activity: { open: openActivity, close: closeActivity, render: renderActivity },
    navigation: { open: openNav, close: closeNav },
  };
  VCS.workspace = workspace;

  if (window.__VCS_TEST__ === true) {
    window.__VCS_WORKSPACE_TEST__ = Object.freeze({
      requestProjectSwitch,
      guardUnsaved,
      parseRoute,
      applyRoute,
      refreshWorkspaceContext,
      saveRemoteState,
      configure({ projectRows = [], projectId = '', route = null } = {}) {
        projects = Array.isArray(projectRows)
          ? projectRows.map(item => Object.assign({}, item)) : [];
        state.project_id = safeToken(projectId, PROJECT_TOKEN);
        selectedProject = projects.find(item => item.id === state.project_id) || null;
        currentRoute = route || null;
      },
      snapshot() {
        return {
          state: Object.assign({}, state),
          authority_id: authorityId,
          revision,
          remote_save_blocked: remoteSaveBlocked,
          selected_project: selectedProject && Object.assign({}, selectedProject),
          current_route: currentRoute,
          dirty_scopes: Array.from(dirtyScopes.keys()),
        };
      },
    });
  }

  async function init() {
    wireShell();
    closeNav();
    renderDirty();
    const initial = parseRoute(window.location.hash) || parseRoute(state.route) || parseRoute('#/home');
    if (initial) {
      let applied = await applyRoute(initial, {
        source: 'initial', restoreScroll: true, allowUnresolvedProject: true, persist: false,
      });
      let committed = initial;
      if (!applied || !applied.ok) {
        committed = parseRoute('#/home');
        applied = await applyRoute(committed, {
          source: 'initial-fallback', allowUnresolvedProject: true, persist: false,
        });
      }
      if (applied && applied.ok) {
        history.replaceState(
          { vcsRoute: committed.id, vcsIndex: historyIndex }, '', committed.hash);
      }
    }
    try { await VCS.ready; await refreshWorkspaceContext(); }
    catch (_) { renderContext(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { init(); }, { once: true });
  } else { init(); }
})();
