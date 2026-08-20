// project.js — 吸附能项目页:新建项目(清洁表面 + 构型族 + 气相参考 → 批量生成)
// + 已有项目 ΔE 汇总 / 导出 CSV / 生成完整报告。行为对齐 vcstudio/gui/project_tab.py。
// 只依赖 app.js 暴露的 VCS.* 与 api 桥方法(proj_*/pick_file/pick_dir)。
// 全部插值走 VCS.esc;零 emoji;中文文案。数字列用 td.num 右对齐等宽。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  function tr(key, params = {}, zhFallback = '', enFallback = '') {
    const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
    const fallback = english
      ? (enFallback || `Translation unavailable (${key})`)
      : zhFallback;
    if (typeof VCS.t === 'function') return VCS.t(key, params, fallback);
    return String(fallback).replace(/\{([A-Za-z0-9_]+)\}/g,
      (match, name) => Object.prototype.hasOwnProperty.call(params, name)
        ? String(params[name]) : match);
  }
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = v || ''; };
  const LEGACY_CURRENT_PROJECT_KEY = 'vcs.adsorption.current_project';
  const LEGACY_COMPARE_PROJECTS_KEY = 'vcs.adsorption.compare_projects';
  const COMPARE_PROJECT_IDS_KEY = 'vcs.adsorption.compare_project_ids.v1';
  const PROJECT_ID_RE = /^[A-Za-z0-9._~-]{1,160}$/;
  const IMPORT_DRAFT_ID = 'project-import';
  // This marker deliberately contains no source/output path, project name,
  // candidate detail, confirmation rationale, or other form body.  The
  // workspace service projects only its opaque reference; reopening the entry
  // returns the user to the import flow and asks them to reselect local data.
  const IMPORT_DRAFT_MARKER = JSON.stringify({
    schema: 'vcstudio.safe-draft-ref/v1', kind: 'project-import',
  });
  const SINGLE_REPORT_FORMATS = Object.freeze([
    {
      id: 'pj-report-format-html', value: 'html', label: 'HTML',
      description: () => tr("runtime.project.tr.text_7a9ffb0460", {}, '浏览器预览（始终可用）', 'Browser preview (always available)'),
    },
    {
      id: 'pj-report-format-docx', value: 'docx', label: 'DOCX',
      description: () => tr("runtime.project.tr.text_5af8aeae5b", {}, '编辑与批注', 'Editing and annotation'),
    },
    {
      id: 'pj-report-format-pdf', value: 'pdf', label: 'PDF',
      description: () => tr("runtime.project.tr.text_b22000266d", {}, '打印与归档', 'Printing and archiving'),
    },
  ]);
  const reportCapabilityPendingReason = () =>
    tr("runtime.project.tr.text_34e6026ae2", {}, '正在等待后端明确确认；确认可用前不会提交此格式。', 'Waiting for explicit backend confirmation; this format will not be submitted until availability is confirmed.');

  function initialReportCapabilities() {
    return {
      html: { available: true, reason: '' },
      docx: { available: false, reason: reportCapabilityPendingReason() },
      pdf: { available: false, reason: reportCapabilityPendingReason() },
    };
  }

  const State = {
    configs: [],      // 构型 POSCAR 路径列表(逐个添加)
    configSpecies: Object.create(null), // path -> Li-S 物种
    configSpeciesMeta: Object.create(null), // path -> 物种来源/置信度/逐目录四件套/是否已确认
    cleanIncar: {
      path: '', sha256: '', status: 'missing', source: '', issues: [],
      quartet: null, inputMode: '', quartetStatus: '',
    },
    projects: [],     // proj_list 返回的 opaque project DTO
    profiles: [],     // list_profiles 返回；一站式提交资源选择
    importRows: [],   // 本地结果扫描候选(前端只持有修正值;commit 时后端会重新验证)
    importResult: null,
    preparedLis: null, // {projectId,fingerprint,name,submitted}:生成成功后复用，安全重试提交
    preparedConflictHint: null,
    methodCheck: null,
    methodCheckFingerprint: '',
    repairPlan: null,
    repairDecision: null, // {plan_id,mode:'keep'|'apply'}；仅作用于受管项目副本
    repairResume: 'submit',
    workflowPendingSubmit: false,
    workflowSubmitted: false,
    workflowAnalysisReady: false,
    workflowResultReady: false,
    workflowStage: '',
    workflowNeedsHuman: false,
    workflowProjectId: '',
    lisBusy: false,
    inputScanBusy: false,
    inputGeneration: 0,
    compareProjectIds: new Set(),
    compareSelectionRestored: false,
    comparePreview: null,
    comparePreviewGeneration: 0,
    comparePreviewTimer: null,
    compareFiguresBusy: false,
    batchReportBusy: false,
    candidateEvaluationGeneration: 0,
    candidateEvaluation: null,
    deltaResult: null,
    reportDiagnostic: false,
    reportBusy: false,
    reportCapabilities: initialReportCapabilities(),
    reportCapabilityState: 'pending',
    reportCapabilityGeneration: 0,
    currentProjectId: '',
    projectReloadGeneration: 0,
    projectReloadInFlight: null,
    projectSelectionGeneration: 0,
    requestedProject: null,
    explicitWorkflow: '',
    lifecycleBusy: false,
    lifecycleSelectionToken: '',
    lifecycleOperationToken: '',
    lifecycleAction: '',
    adoptSelectionToken: '',
    adoptOperationToken: '',
    lifecycleQueueOperation: null,
    adoptQueueOperation: null,
    lifecycleOperationSequence: 0,
    lifecycleGeneration: 0,
    lifecycleBusyGeneration: 0,
    lifecycleProjectIdentity: '',
  };

  function rawProjectId(project) {
    if (!project) return '';
    const value = String(project.project_id || '').trim();
    return PROJECT_ID_RE.test(value) ? value : '';
  }

  function projectId(project) {
    return rawProjectId(project);
  }

  function withoutProjectLocators(record) {
    const source = record && typeof record === 'object' ? record : {};
    const rejected = new Set([
      'path', 'project' + '_path', 'root', 'locator', 'project' + '_yaml',
    ]);
    const clean = {};
    Object.keys(source).forEach(key => {
      if (!rejected.has(key)) clean[key] = source[key];
    });
    return clean;
  }

  function sanitizeComparisonPreview(result) {
    if (!result || typeof result !== 'object') return result;
    const clean = withoutProjectLocators(result);
    clean.projects = Array.isArray(result.projects)
      ? result.projects.map(withoutProjectLocators) : [];
    return clean;
  }

  function projectContext(project) {
    if (!project) return null;
    const total = Math.max(0, Number(project.n_members || 0) || 0);
    const done = Math.max(0, Number(project.n_done || 0) || 0);
    const id = projectId(project);
    return {
      project_id: id,
      name: String(project.name || ''),
      counts: { members: total, done, pending: Math.max(0, total - done) },
    };
  }

  function publishProjectContext(project) {
    const detail = projectContext(project);
    if (!detail) return null;
    document.dispatchEvent(new CustomEvent('vcs:project-context', { detail }));
    return detail;
  }

  function currentProjectRecord() {
    const id = State.currentProjectId || val('pj-select');
    return State.projects.find(project => projectId(project) === String(id || '')) || null;
  }

  function lifecycleStatus(id, message, state = '') {
    const box = $(id);
    if (!box) return;
    box.textContent = String(message || '');
    box.classList.toggle('ready', state === 'ready');
    box.classList.toggle('blocked', state === 'blocked');
    box.classList.toggle('running', state === 'running');
  }

  function lifecycleOperationLabel(kind) {
    if (kind === 'adopt') return tr('runtime.project.operation.adopt', {},
      '接管项目文件夹', 'Adopt project folder');
    if (kind === 'move') return tr('runtime.project.operation.move', {},
      '移动项目', 'Move project');
    return tr('runtime.project.operation.clone', {}, '克隆项目', 'Clone project');
  }

  function publishLifecycleOperation(operation, status, error = '') {
    if (!operation || !VCS.operations || typeof VCS.operations.publish !== 'function') return;
    operation.status = status;
    VCS.operations.publish({
      id: operation.id,
      kind: `project-${operation.kind}`,
      status,
      label: operation.label,
      count: 1,
      error: String(error || ''),
      route: 'project-overview',
      started_at: operation.startedAt,
      updated_at: new Date().toISOString(),
    });
  }

  function beginLifecycleOperation(kind) {
    State.lifecycleOperationSequence += 1;
    const operation = {
      id: `projectop-${Date.now().toString(36)}-${State.lifecycleOperationSequence.toString(36)}`,
      kind,
      label: lifecycleOperationLabel(kind),
      status: 'confirming',
      startedAt: new Date().toISOString(),
    };
    publishLifecycleOperation(operation, 'confirming');
    return operation;
  }

  function supersedeLifecycleOperation(operation) {
    if (operation && operation.status === 'confirming') {
      publishLifecycleOperation(operation, 'failed', tr(
        'runtime.project.operation.superseded', {},
        '已由新的预检替代', 'Superseded by a newer preflight'));
    }
  }

  function lifecycleRequestCurrent(generation, identity) {
    return generation === State.lifecycleGeneration &&
      identity === State.lifecycleProjectIdentity &&
      identity === projectId(currentProjectRecord());
  }

  function syncLifecycleProjectIdentity(project) {
    const identity = projectId(project);
    if (identity === State.lifecycleProjectIdentity) return identity;
    State.lifecycleGeneration += 1;
    State.lifecycleProjectIdentity = identity;
    if (State.lifecycleBusyGeneration) {
      State.lifecycleBusyGeneration = 0;
      State.lifecycleBusy = false;
    }
    if (!State.lifecycleQueueOperation || State.lifecycleQueueOperation.status === 'confirming') {
      supersedeLifecycleOperation(State.lifecycleQueueOperation);
      State.lifecycleQueueOperation = null;
    }
    State.lifecycleSelectionToken = '';
    State.lifecycleOperationToken = '';
    State.lifecycleAction = '';
    lifecycleStatus('pj-lifecycle-status', '');
    const apply = $('pj-lifecycle-apply');
    if (apply) apply.disabled = true;
    return identity;
  }

  function updateProjectHub() {
    const page = $('page-project');
    const hub = $('project-empty-hub');
    const lifecycle = $('pj-lifecycle-card');
    const project = currentProjectRecord();
    const hasProject = !!project;
    const lifecycleIdentity = syncLifecycleProjectIdentity(project);
    const explicit = !!State.explicitWorkflow;
    if (page) page.classList.toggle('project-neutral', !hasProject && !explicit);
    if (hub) hub.hidden = hasProject || explicit;
    if (lifecycle) lifecycle.hidden = !hasProject;
    const recent = $('pj-hub-recent');
    if (recent) recent.disabled = !State.projects.length;
    const identity = $('pj-lifecycle-identity');
    if (identity) {
      identity.textContent = hasProject
        ? (projectId(project) || tr('runtime.project.lifecycle.legacy_identity', {},
          '旧项目（路径注册身份）', 'Legacy project (registry-path identity)')) : '';
    }
    const target = $('pj-lifecycle-target-name');
    if (target && hasProject) {
      if (target.dataset.projectId !== lifecycleIdentity) {
        target.dataset.projectId = lifecycleIdentity;
        target.value = `${String(project.name || 'project').trim() || 'project'}-copy`;
      }
    } else if (target) {
      target.dataset.projectId = '';
    }
  }

  function setExplicitWorkflow(mode) {
    State.explicitWorkflow = String(mode || '');
    updateProjectHub();
  }

  function lifecyclePreflightMessage(result) {
    if (!result) return tr('runtime.project.lifecycle.no_response', {},
      '预检没有返回结果。', 'The preflight returned no result.');
    const issues = (result.conflicts || []).map(item => String(item && item.message || '')).filter(Boolean);
    if (!result.ready) return issues.join('；') || result.error || tr(
      'runtime.project.lifecycle.blocked', {}, '预检未通过。', 'Preflight did not pass.');
    const impact = result.impact || {};
    const identity = impact.project_uuid_reminted
      ? tr('runtime.project.lifecycle.remint_impact', {}, '将生成新的项目 ID', 'A new project ID will be minted')
      : tr('runtime.project.lifecycle.preserve_impact', {}, '将保留当前项目 ID', 'The current project ID will be preserved');
    const ready = tr('runtime.project.lifecycle.ready_summary', {
      count: Number(impact.locator_rewrites || 0), identity,
    }, `预检通过：将安全重写 {count} 个项目内定位器；{identity}。确认前尚未修改文件。`,
    'Preflight passed: {count} project-local locators will be safely rebased; {identity}. No files have been changed yet.');
    const jobs = result.jobs && Array.isArray(result.jobs.entries)
      ? result.jobs.entries : [];
    const jobEntries = jobs.map(item => {
      const identityValue = String(item && (item.target_identity || item.source_identity) || '—');
      return `${String(item && item.label || 'job')} · ${String(item && item.action || 'update')} · ${identityValue}`;
    }).join('; ');
    const jobSummary = tr('runtime.project.lifecycle.ready_jobs', {
      count: jobs.length, entries: jobEntries || '—',
    }, 'Jobs 登记表：{count} 个条目（{entries}）。',
    'Jobs ledger: {count} entries ({entries}).');
    return `${ready} ${jobSummary}`;
  }

  async function chooseAdoptFolder() {
    if (State.lifecycleBusy) return;
    const panel = $('pj-adopt-panel');
    if (panel) panel.hidden = false;
    lifecycleStatus('pj-adopt-status', tr('runtime.project.adopt.selecting', {},
      '正在打开服务器文件夹选择器…', 'Opening the server folder picker…'), 'running');
    State.lifecycleBusy = true;
    try {
      const selected = await VCS.call('proj_lifecycle_select', 'adopt_source');
      if (!selected || !selected.ok) {
        State.adoptSelectionToken = '';
        State.adoptOperationToken = '';
        lifecycleStatus('pj-adopt-status', selected && selected.cancelled
          ? tr('runtime.project.adopt.cancelled', {}, '未选择文件夹。', 'No folder was selected.')
          : ((selected && selected.error) || tr('runtime.project.adopt.select_failed', {},
            '无法检查所选文件夹。', 'The selected folder could not be inspected.')), 'blocked');
        return;
      }
      State.adoptSelectionToken = String(selected.selection_token || '');
      const label = String(selected.selection && selected.selection.label || '');
      const copy = $('pj-adopt-selection');
      if (copy) copy.textContent = tr('runtime.project.adopt.selected', { name: label },
        '已由服务器选择：{name}。下一步将检查 project.yaml、注册表路径和项目 ID。',
        'Selected by the server: {name}. Next, project.yaml, the registry path, and the project ID will be checked.');
    } finally {
      State.lifecycleBusy = false;
    }
    await preflightAdopt();
  }

  async function preflightAdopt() {
    if (!State.adoptSelectionToken || State.lifecycleBusy) return;
    supersedeLifecycleOperation(State.adoptQueueOperation);
    State.adoptQueueOperation = beginLifecycleOperation('adopt');
    const mode = val('pj-adopt-mode') || 'remint';
    const button = $('pj-adopt-apply');
    if (button) button.disabled = true;
    lifecycleStatus('pj-adopt-status', tr('runtime.project.adopt.preflighting', {},
      '正在预检身份与重复注册…', 'Checking identity and duplicate registration…'), 'running');
    State.lifecycleBusy = true;
    try {
      const result = await VCS.call('proj_lifecycle_preflight', 'adopt', null,
        State.adoptSelectionToken, null, mode);
      State.adoptOperationToken = result && result.ready
        ? String(result.operation_token || '') : '';
      lifecycleStatus('pj-adopt-status', lifecyclePreflightMessage(result),
        result && result.ready ? 'ready' : 'blocked');
      if (button) button.disabled = !State.adoptOperationToken;
      if (!result || !result.ready) publishLifecycleOperation(
        State.adoptQueueOperation, 'failed', (result && result.error) ||
        lifecyclePreflightMessage(result));
    } catch (error) {
      const message = String(error && error.message || error);
      State.adoptOperationToken = '';
      lifecycleStatus('pj-adopt-status', message, 'blocked');
      publishLifecycleOperation(State.adoptQueueOperation, 'failed', message);
    } finally {
      State.lifecycleBusy = false;
    }
  }

  async function applyAdopt() {
    if (!State.adoptOperationToken || State.lifecycleBusy) return;
    const button = $('pj-adopt-apply');
    if (button) button.disabled = true;
    lifecycleStatus('pj-adopt-status', tr('runtime.project.adopt.applying', {},
      '正在接管并原子更新注册表…', 'Adopting and atomically updating the registry…'), 'running');
    const token = State.adoptOperationToken;
    State.adoptOperationToken = '';
    State.lifecycleBusy = true;
    publishLifecycleOperation(State.adoptQueueOperation, 'running');
    try {
      const result = await VCS.call('proj_lifecycle_apply', token);
      if (!result || !result.ok) {
        const message = (result && result.error) || tr(
          'runtime.project.adopt.failed', {}, '接管未完成；没有登记不确定状态。',
          'Adoption did not complete; no uncertain state was registered.');
        lifecycleStatus('pj-adopt-status', message, 'blocked');
        publishLifecycleOperation(State.adoptQueueOperation, 'failed', message);
        if (result && result.requires_manual_recovery) VCS.log(result.error, 'failc');
        return;
      }
      State.explicitWorkflow = '';
      await reloadProjects();
      if (result.project && result.project.project_id) {
        await selectById(result.project.project_id);
      }
      VCS.toast(tr('runtime.project.adopt.success', {},
        '项目文件夹已接管并登记。', 'The project folder was adopted and registered.'), 'ok');
      publishLifecycleOperation(State.adoptQueueOperation, 'succeeded');
    } catch (error) {
      const message = String(error && error.message || error);
      lifecycleStatus('pj-adopt-status', message, 'blocked');
      publishLifecycleOperation(State.adoptQueueOperation, 'failed', message);
    } finally {
      State.lifecycleBusy = false;
      updateProjectHub();
    }
  }

  async function preflightProjectLifecycle(action) {
    if (State.lifecycleBusy) return;
    const project = currentProjectRecord();
    if (!project) {
      lifecycleStatus('pj-lifecycle-status', tr('runtime.project.lifecycle.no_project', {},
        '请先选择一个项目。', 'Select a project first.'), 'blocked');
      return;
    }
    const targetName = val('pj-lifecycle-target-name');
    if (!targetName) {
      lifecycleStatus('pj-lifecycle-status', tr('runtime.project.lifecycle.name_required', {},
        '请填写目标文件夹名。', 'Enter a destination folder name.'), 'blocked');
      return;
    }
    const identity = projectId(project);
    if (!identity || identity !== State.lifecycleProjectIdentity) {
      syncLifecycleProjectIdentity(project);
    }
    const generation = ++State.lifecycleGeneration;
    supersedeLifecycleOperation(State.lifecycleQueueOperation);
    State.lifecycleQueueOperation = beginLifecycleOperation(action);
    const apply = $('pj-lifecycle-apply');
    if (apply) apply.disabled = true;
    lifecycleStatus('pj-lifecycle-status', tr('runtime.project.lifecycle.selecting', {},
      '正在选择目标父文件夹…', 'Selecting the destination parent folder…'), 'running');
    State.lifecycleBusy = true;
    State.lifecycleBusyGeneration = generation;
    try {
      const selected = await VCS.call('proj_lifecycle_select', `${action}_destination`);
      if (!lifecycleRequestCurrent(generation, identity)) return;
      if (!selected || !selected.ok) {
        const message = selected && selected.cancelled
          ? tr('runtime.project.lifecycle.cancelled', {}, '未选择目标文件夹。', 'No destination was selected.')
          : ((selected && selected.error) || tr('runtime.project.lifecycle.select_failed', {},
            '目标文件夹选择失败。', 'Destination selection failed.'));
        lifecycleStatus('pj-lifecycle-status', message, 'blocked');
        publishLifecycleOperation(State.lifecycleQueueOperation, 'failed', message);
        return;
      }
      const selectionToken = String(selected.selection_token || '');
      const result = await VCS.call('proj_lifecycle_preflight', action,
        identity, selectionToken, targetName, null);
      if (!lifecycleRequestCurrent(generation, identity)) return;
      State.lifecycleSelectionToken = selectionToken;
      State.lifecycleAction = action;
      State.lifecycleOperationToken = result && result.ready
        ? String(result.operation_token || '') : '';
      lifecycleStatus('pj-lifecycle-status', lifecyclePreflightMessage(result),
        result && result.ready ? 'ready' : 'blocked');
      if (apply) {
        apply.disabled = !State.lifecycleOperationToken;
        apply.textContent = action === 'move'
          ? tr('runtime.project.lifecycle.confirm_move', {}, '确认移动', 'Confirm move')
          : tr('runtime.project.lifecycle.confirm_clone', {}, '确认克隆', 'Confirm clone');
      }
      if (!result || !result.ready) publishLifecycleOperation(
        State.lifecycleQueueOperation, 'failed', (result && result.error) ||
        lifecyclePreflightMessage(result));
    } catch (error) {
      if (!lifecycleRequestCurrent(generation, identity)) return;
      const message = String(error && error.message || error);
      State.lifecycleOperationToken = '';
      lifecycleStatus('pj-lifecycle-status', message, 'blocked');
      publishLifecycleOperation(State.lifecycleQueueOperation, 'failed', message);
    } finally {
      if (State.lifecycleBusyGeneration === generation) {
        State.lifecycleBusyGeneration = 0;
        State.lifecycleBusy = false;
      }
    }
  }

  async function applyProjectLifecycle() {
    if (!State.lifecycleOperationToken || State.lifecycleBusy) return;
    if (State.lifecycleAction === 'move' && !window.confirm(tr(
      'runtime.project.lifecycle.move_warning', {},
      '移动会改变项目位置并同步更新注册表。仅在预检内容正确时继续。',
      'Move changes the project location and updates the registry. Continue only if the preflight is correct.'))) {
      publishLifecycleOperation(State.lifecycleQueueOperation, 'failed', tr(
        'runtime.project.operation.cancelled', {}, '用户取消了确认', 'Confirmation cancelled'));
      State.lifecycleOperationToken = '';
      const cancelledApply = $('pj-lifecycle-apply');
      if (cancelledApply) cancelledApply.disabled = true;
      return;
    }
    const apply = $('pj-lifecycle-apply');
    if (apply) apply.disabled = true;
    lifecycleStatus('pj-lifecycle-status', tr('runtime.project.lifecycle.applying', {},
      '正在执行并核对注册表提交…', 'Applying and verifying the registry commit…'), 'running');
    const token = State.lifecycleOperationToken;
    State.lifecycleOperationToken = '';
    State.lifecycleBusy = true;
    publishLifecycleOperation(State.lifecycleQueueOperation, 'running');
    try {
      const result = await VCS.call('proj_lifecycle_apply', token);
      if (!result || !result.ok) {
        const message = (result && result.error) || tr(
          'runtime.project.lifecycle.failed', {}, '项目操作未完成。',
          'The project operation did not complete.');
        lifecycleStatus('pj-lifecycle-status', message, 'blocked');
        publishLifecycleOperation(State.lifecycleQueueOperation, 'failed', message);
        if (result && result.requires_manual_recovery) VCS.log(result.error, 'failc');
        return;
      }
      await reloadProjects();
      if (result.project && result.project.project_id) {
        await selectById(result.project.project_id);
      }
      VCS.toast(result.action === 'move'
        ? tr('runtime.project.lifecycle.move_success', {}, '项目已移动，注册表已同步。', 'Project moved and registry synchronized.')
        : tr('runtime.project.lifecycle.clone_success', {}, '项目已克隆为新的独立项目。', 'Project cloned as a new independent project.'), 'ok');
      publishLifecycleOperation(State.lifecycleQueueOperation, 'succeeded');
    } catch (error) {
      const message = String(error && error.message || error);
      lifecycleStatus('pj-lifecycle-status', message, 'blocked');
      publishLifecycleOperation(State.lifecycleQueueOperation, 'failed', message);
    } finally {
      State.lifecycleBusy = false;
      updateProjectHub();
    }
  }

  async function openRecentProject() {
    if (!State.projects.length) await reloadProjects();
    const recent = State.projects[State.projects.length - 1] || null;
    if (!recent) {
      VCS.toast(tr('runtime.project.lifecycle.no_recent', {},
        '还没有最近项目。', 'There are no recent projects yet.'), 'fail');
      return;
    }
    State.explicitWorkflow = '';
    const selected = await requestProjectSelection(recent, State.currentProjectId);
    if (selected) {
      setAccordionOpen('pj-results-card', true);
      scrollToCard('pj-results-card');
    }
  }

  function publicProject(project) {
    if (!project) return null;
    const total = Math.max(0, Number(project.n_members || 0) || 0);
    const done = Math.max(0, Number(project.n_done || 0) || 0);
    return {
      project_id: projectId(project),
      name: String(project.name || ''),
      counts: { members: total, done, pending: Math.max(0, total - done) },
    };
  }

  const LIS_MUTABLE_CONTROLS = [
    'lis-reference', 'lis-input-dir', 'pj-incar', 'pj-incar-btn',
    'pj-slab', 'pj-slab-btn', 'pj-cfg-add', 'pj-cfg-dir',
    'lis-bulk-species', 'lis-apply-species', 'lis-only-unmatched',
    'pj-name', 'pj-root', 'pj-root-btn', 'lis-profile', 'lis-cores',
    'lis-walltime', 'pj-create',
  ];

  function lisInputsLocked() {
    return State.lisBusy || State.inputScanBusy;
  }

  function syncLisInputLocks() {
    const locked = lisInputsLocked();
    LIS_MUTABLE_CONTROLS.forEach(id => {
      const element = $(id);
      if (element) element.disabled = locked;
    });
  }

  // 目录 + 默认文件名 → 完整保存路径(按目录内的分隔符风格拼接,兼容 Windows/Posix)
  function joinPath(dir, filename) {
    const d = String(dir).replace(/[\\/]+$/, '');
    const sep = d.indexOf('\\') >= 0 ? '\\' : '/';
    return d + sep + filename;
  }

  // ── 本地已算结果导入:四步向导 ─────────────────────────────────────────────
  const ROLE_LABELS = {
    clean_slab: () => tr("runtime.project.joinpath.text_1333fd3434", {}, '清洁表面', 'Clean surface'),
    config: () => tr("runtime.project.joinpath.text_3f8c1a0e1d", {}, '吸附构型', 'Adsorption configuration'),
    gas_ref: () => tr("runtime.project.joinpath.text_0aa9f27384", {}, '吸附质气相参考', 'Gas-phase adsorbate reference'),
    molecule_ref: () => tr("runtime.project.joinpath.text_f5b33b770a", {}, '锂硫 / 分子能量库', 'Li-S / molecular-energy library'),
    standalone: () => tr("runtime.project.joinpath.text_f696c7f1ce", {}, '独立计算结果', 'Standalone calculation result'),
    ignore: () => tr("runtime.project.joinpath.text_83a4be6e41", {}, '不导入', 'Do not import'),
  };
  const TASK_LABELS = {
    auto: () => tr("runtime.project.joinpath.text_4b874a93b7", {}, '自动识别', 'Detect automatically'),
    relax: () => tr("runtime.project.joinpath.text_3034a7858e", {}, '结构优化 relax', 'Structural relaxation (relax)'),
    cellopt: () => tr("runtime.project.joinpath.text_d8280c82f8", {}, '晶胞优化 cellopt', 'Cell optimization (cellopt)'),
    static: () => tr("runtime.project.joinpath.text_dc1ecf9b18", {}, '静态能量 static', 'Static energy (static)'),
    freq: () => tr("runtime.project.joinpath.text_8b0f011739", {}, '频率 freq', 'Frequency calculation (freq)'),
    dos: () => tr("runtime.project.joinpath.text_11da7d2aa5", {}, '态密度 DOS', 'Density of states (DOS)'),
    band: () => tr("runtime.project.joinpath.text_29a65e5403", {}, '能带 band', 'Band structure (band)'),
    unknown: () => tr("runtime.project.joinpath.text_06bccf5a69", {}, '尚未识别', 'Not identified yet'),
  };
  const labelFor = (labels, key) => typeof labels[key] === 'function' ? labels[key]() : key;
  const defaultConfirmationReason = () => tr("runtime.project.joinpath.text_94a1de12b2", {}, '已核对原始 OUTCAR/OSZICAR 与末结构，确认该任务收敛', 'I checked the original OUTCAR/OSZICAR and final structure and confirmed that this task converged');

  function pathBase(path) {
    const s = String(path || '').replace(/[\\/]+$/, '');
    const parts = s.split(/[\\/]/);
    return parts[parts.length - 1] || s;
  }

  function pathParent(path) {
    const s = String(path || '').replace(/[\\/]+$/, '');
    const at = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    // Windows 盘符根目录不截成单个 "C:"。
    if (at <= 2 && /^[A-Za-z]:/.test(s)) return s.slice(0, at + 1);
    return at > 0 ? s.slice(0, at) : s;
  }

  function textList(value) {
    if (value == null || value === '') return [];
    if (Array.isArray(value)) return value.flatMap(textList);
    if (typeof value === 'object') {
      for (const key of ['message', 'reason', 'text', 'description', 'detail']) {
        if (value[key]) return textList(value[key]);
      }
      return Object.values(value).flatMap(textList);
    }
    return [String(value)];
  }

  function setAccordionOpen(id, wanted) {
    const card = $(id);
    if (!card) return;
    const open = card.dataset.open === '1';
    if (open !== wanted) {
      const head = card.querySelector(':scope > .acc-h');
      if (head) head.click();
    }
  }

  function scrollToCard(id) {
    const card = $(id);
    if (card && typeof card.scrollIntoView === 'function') {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function setJourneyPrimary(id) {
    ['ads-route-import', 'ads-route-new', 'ads-route-results'].forEach(key => {
      const button = $(key);
      if (button) button.classList.toggle('primary', key === id);
    });
  }

  function restoreWorkflowState(project) {
    const stage = String(project && project.pipeline_stage || '').trim();
    State.workflowStage = stage;
    State.workflowNeedsHuman = !!(project && project.pipeline_needs_human);
    State.workflowProjectId = projectId(project);
    State.workflowPendingSubmit = stage === 'submit';
    State.workflowSubmitted = ['monitor', 'recover'].includes(stage);
    State.workflowAnalysisReady = ['analysis', 'report_done'].includes(stage);
    State.workflowResultReady = stage === 'report_done';
  }

  function updateJourney() {
    const status = $('ads-journey-status');
    const refs = State.projects.filter(p => referenceSpecies(p).length > 0);
    const gate = lisGate();
    let current = 1;
    if (State.workflowResultReady || State.workflowAnalysisReady) current = 6;
    else if (State.workflowSubmitted) current = 5;
    else if (State.workflowPendingSubmit) current = 4;
    else if (State.preparedLis) current = 4;
    else if (gate.step[1] && gate.step[2] && gate.step[3] && gate.step[4]) current = 4;
    else if (gate.step[1] && gate.step[2] && gate.step[3]) current = 3;
    else if (gate.step[1]) current = 2;
    document.querySelectorAll('#ads-journey [data-flow-step]').forEach(el => {
      const step = Number(el.dataset.flowStep);
      el.classList.toggle('done', step < current);
      el.classList.toggle('current', step === current);
    });
    if (State.workflowNeedsHuman) {
      if (status) status.textContent = tr("runtime.project.updatejourney.text_88d1cf1af0", {}, '当前项目有需要处理的任务。打开任务页即可查看原因并续算或重新下载。', 'The current project contains tasks that need attention. Open Jobs to review the reasons and continue the calculation or download the results again.');
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = tr("runtime.project.updatejourney.text_f6f89f5b2d", {}, '处理任务异常', 'Resolve task issues');
      setJourneyPrimary('ads-route-new');
    } else if (State.workflowResultReady || State.workflowStage === 'report_done') {
      if (status) status.textContent = tr("runtime.project.updatejourney.text_ea8f044511", {}, '任务已完成，报告产物已生成；科学状态可能是最终、诊断或阻断，请在报告卡中核对。', 'The tasks are complete and report artifacts were generated. The scientific state may be final, diagnostic, or blocked; verify it on the report card.');
      const resultButton = $('ads-route-results');
      if (resultButton) resultButton.textContent = tr("runtime.project.updatejourney.text_95d312da59", {}, '查看 ΔE 与报告', 'View ΔE and reports');
      setJourneyPrimary('ads-route-results');
    } else if (State.workflowAnalysisReady || State.workflowStage === 'analysis') {
      if (status) status.textContent = tr("runtime.project.updatejourney.text_1ace5962cb", {}, '整组任务已完成。下一步检查 ΔE；全部有效后即可生成报告。', 'All tasks in the group are complete. Check ΔE next; when every value is valid, the report can be generated.');
      const resultButton = $('ads-route-results');
      if (resultButton) resultButton.textContent = tr("runtime.project.updatejourney.text_351eaa7d8f", {}, '检查 ΔE 并生成报告', 'Check ΔE and generate report');
      setJourneyPrimary('ads-route-results');
    } else if (State.workflowSubmitted) {
      if (status) status.textContent = tr("runtime.project.updatejourney.text_91412ec3b3", {}, '整组任务已提交，自动托管正在监控、续算和下载关键结果。请保持软件运行。', 'All tasks in the group were submitted. Autopilot is monitoring them, continuing eligible calculations, and downloading key results. Keep the application running.');
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = tr("runtime.project.updatejourney.text_184c5eb9c2", {}, '查看任务进度', 'View task progress');
      setJourneyPrimary('ads-route-new');
    } else if (State.workflowPendingSubmit) {
      if (status) status.textContent = tr("runtime.project.updatejourney.text_b724f9d48c", {}, '整组输入已生成，但仍有作业等待提交。请到任务页完成提交。', 'All input sets were generated, but some jobs are still waiting for submission. Complete submission on Jobs.');
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = tr("runtime.project.updatejourney.text_6931896a36", {}, '提交已生成作业', 'Submit generated jobs');
      setJourneyPrimary('ads-route-new');
    } else if (refs.length) {
      if (status) status.textContent = tr("runtime.project.updatejourney.text_b876ecb689", { value1: (refs.length) }, `已找到 {value1} 个可复用的参考能项目。下一步添加 slab 与 adsorption 结构。`, 'Found {value1} reusable reference-energy projects. Next, add the slab and adsorption structures.');
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = tr("runtime.project.updatejourney.text_a4d3af3d8f", {}, '已有参考能，开始新的吸附计算', 'Start a new adsorption calculation with existing reference energies');
      setJourneyPrimary('ads-route-new');
    } else {
      if (status) status.textContent = tr("runtime.project.updatejourney.text_b7c893b686", {}, '还没有可复用的 Li-S 参考能。先导入你已经算好并收敛的 Li-S 化合物结果。', 'No reusable Li-S reference energies are available yet. First import Li-S compound results that have already been calculated and converged.');
      setJourneyPrimary('ads-route-import');
    }
  }

  function showImportProblem(message, fix) {
    const box = $('pj-import-problem');
    if (!box) return;
    if (!message) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML = tr("runtime.project.showimportproblem.text_b1758e3a83", {
      value1: VCS.esc(message),
      value2: VCS.esc(fix || tr('runtime.project.showimportproblem.default_fix', {},
        '按提示修正后重新检查。', 'Apply the suggested correction, then check again.')),
    }, `<b>{value1}</b><span>怎么处理：{value2}</span>`, '<b>{value1}</b><span>How to resolve it: {value2}</span>');
  }

  async function openProjectResults(projectIdValue) {
    await reloadProjects();
    const sel = $('pj-select');
    const hit = State.projects.find(p => projectId(p) === String(projectIdValue || ''));
    if (sel && hit) {
      sel.value = projectId(hit);
      restoreWorkflowState(hit);
    }
    updateProjectSummary();
    setAccordionOpen('pj-results-card', true);
    scrollToCard('pj-results-card');
  }

  function canonicalRole(value) {
    const role = String(value || '').toLowerCase();
    if (['clean', 'clean_slab', 'slab_clean', 'surface'].includes(role)) return 'clean_slab';
    if (['gas', 'gas_ref', 'ref', 'reference'].includes(role)) return 'gas_ref';
    if (['molecule', 'molecule_ref', 'species_ref'].includes(role)) return 'molecule_ref';
    if (['config', 'configs', 'ads', 'adsorbed', 'adsorbate'].includes(role)) return 'config';
    if (['standalone', 'independent'].includes(role)) return 'standalone';
    if (['ignore', 'skip', 'none'].includes(role)) return 'ignore';
    return role || 'standalone';
  }

  function evidenceList(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return textList(raw);
    const out = [];
    const energy = raw.energy || {};
    const electronic = raw.electronic || {};
    const ionic = raw.ionic || {};
    const completion = raw.completion || {};
    if (energy.value_eV != null) out.push(tr("runtime.project.evidencelist.text_648bed00f5", {
      value1: energy.value_eV,
      value2: energy.source || tr('runtime.project.evidencelist.unknown_source', {},
        '来源未知', 'Unknown source'),
    }, `能量 {value1} eV（{value2}）`, 'Energy {value1} eV ({value2})'));
    if (electronic.final_scf_steps != null) {
      out.push(tr("runtime.project.evidencelist.text_95aef18919", {
        value1: electronic.final_scf_steps,
        value2: electronic.nelm || tr('runtime.project.evidencelist.unknown_nelm', {},
          'NELM 未知', 'NELM unknown'),
      }, `末电子步 {value1}/{value2}`, 'Final electronic step {value1}/{value2}') +
        (electronic.nelm_saturated ? tr("runtime.project.evidencelist.text_bf521725b6", {}, '，已达到上限', ', limit reached') : tr("runtime.project.evidencelist.text_5807b073fd", {}, '，未达到上限', ', limit not reached')));
    }
    if (ionic.converged_marker) out.push(tr("runtime.project.evidencelist.text_3934a7fec6", {}, 'OUTCAR 含结构优化收敛标志', 'OUTCAR contains the structural-relaxation convergence marker'));
    if (ionic.final_force_max_eV_A != null) out.push(tr("runtime.project.evidencelist.text_39f9b48c46", { value1: (ionic.final_force_max_eV_A) }, `末最大力 {value1} eV/Å`, 'Final maximum force {value1} eV/Å'));
    if (completion.outcar_footer) out.push(tr("runtime.project.evidencelist.text_dec98d3f14", {}, 'OUTCAR 含正常结束页脚', 'OUTCAR contains a normal-termination footer'));
    if (completion.vasprun_complete) out.push(tr("runtime.project.evidencelist.text_b17c6cbb52", {}, 'vasprun.xml 结构完整', 'vasprun.xml is structurally complete'));
    if (completion.soft_stopped) out.push(tr("runtime.project.evidencelist.text_1007ec9cb5", {}, '检测到 STOPCAR / soft stop', 'STOPCAR / soft stop detected'));
    const cross = raw.cross_file_energy || {};
    const crossDelta = cross.spread_eV != null ? cross.spread_eV
      : cross.max_delta_eV != null ? cross.max_delta_eV
        : cross.max_difference_eV != null ? cross.max_difference_eV : cross.max_delta;
    if (cross.consistent === true) {
      out.push(tr("runtime.project.evidencelist.text_0bd8fa7c1f", {}, '跨文件末能量一致', 'Final energies agree across files') +
        (crossDelta != null ? tr("runtime.project.evidencelist.text_900cb9fa6c", { value1: (crossDelta) }, `（最大差 {value1} eV）`, ' (maximum difference {value1} eV)') : '（OSZICAR / OUTCAR / vasprun.xml）'));
    } else if (cross.consistent === false) {
      out.push(tr("runtime.project.evidencelist.text_cdda10ac24", {}, '跨文件末能量不一致', 'Final energies disagree across files') + (crossDelta != null ? tr("runtime.project.evidencelist.text_900cb9fa6c", { value1: (crossDelta) }, `（最大差 {value1} eV）`, ' (maximum difference {value1} eV)') : ''));
    }
    if (raw.fatal_error) out.push(tr("runtime.project.evidencelist.text_673e30c330", { value1: (raw.fatal_error) }, `致命错误：{value1}`, 'Fatal error: {value1}'));
    return out.length ? out : textList(raw);
  }

  function actionLabel(value) {
    const action = String(value || '');
    return ({
      import_done: tr("runtime.project.actionlabel.text_b5b0ebd372", {}, '无需额外处理，可直接导入', 'No further action is required; ready to import'),
      manual_confirm: tr("runtime.project.actionlabel.text_8ec396e1bb", {}, '核对原始输出确已正常结束后，可勾选人工确认', 'After verifying that the original output ended normally, select manual confirmation'),
      repair_or_recalculate: tr("runtime.project.actionlabel.text_88b652b396", {}, '按硬性问题补齐输出或续算，然后重新检查', 'Resolve the blocking issues by completing the output or continuing the calculation, then check again'),
      inspect_output: tr("runtime.project.actionlabel.text_eac5bf042b", {}, '打开原始输出核对结束状态，补齐文件后重新检查', 'Open the original output, verify its completion state, add any missing files, and check again'),
      submit_created: tr("runtime.project.actionlabel.text_796ff94dca", {}, '四件套已就绪；导入后前往任务页选择服务器提交', 'The four-file input set is ready; after import, select a server and submit it from Jobs'),
      submit: tr("runtime.project.actionlabel.text_796ff94dca", {}, '四件套已就绪；导入后前往任务页选择服务器提交', 'The four-file input set is ready; after import, select a server and submit it from Jobs'),
    })[action] || action;
  }

  function isCreatedInput(row) {
    return String(row && row.state || '').toUpperCase() === 'CREATED' &&
      (!row.raw || row.raw.input_complete !== false);
  }

  function convergenceImportStatus(row) {
    if (row.manualConfirm && row.confirmationEligible &&
      String(row.confirmationReason || '').trim()) return 'ready';
    const state = String(row.state || '').toUpperCase();
    const hard = row.blocked || ['FAILED', 'ERROR', 'INVALID', 'BLOCKED'].includes(state);
    if (hard) return 'blocked';
    if (['DONE', 'READY', 'CONVERGED', 'IMPORTABLE'].includes(state)) return 'ready';
    if (['NEEDS_HUMAN', 'REVIEW', 'UNKNOWN', 'NEEDS_CONFIRMATION'].includes(state)) {
      return row.confirmationEligible ? 'review' : 'blocked';
    }
    return row.importable === true ? 'ready' : row.confirmationEligible ? 'review' : 'blocked';
  }

  function importStatus(row) {
    const convergence = convergenceImportStatus(row);
    if (convergence === 'ready' && row.mappingRequired && !row.mappingConfirmed) return 'review';
    return convergence;
  }

  function normalizeImportCandidate(candidate, index) {
    const c = candidate || {};
    const path = String(c.path || c.source_dir || c.job_dir || c.dir || '');
    const state = c.state_suggestion || c.state || c.status || 'NEEDS_HUMAN';
    const role = canonicalRole(c.role || c.suggested_role || c.role_suggestion);
    const diag = c.diagnosis && typeof c.diagnosis === 'object' ? c.diagnosis : {};
    const blocking = textList(c.blocking_reasons || c.blockers || c.errors || diag.blockers);
    const warnings = textList(c.warnings || c.input_issues);
    const evidence = evidenceList(c.convergence_evidence || c.evidence);
    const diagnosis = [
      ...textList(diag.summary || (typeof c.diagnosis === 'string' ? c.diagnosis : null)),
      ...textList(diag.reasons), ...textList(c.reason || c.role_reason),
    ];
    const suggestions = textList(diag.suggestions);
    const eligible = c.confirmation_eligible === true;
    const row = {
      raw: c,
      index,
      path,
      name: String(c.name || pathBase(path) || tr("runtime.project.normalizeimportcandidate.text_f8e61a2106", { value1: (index + 1) }, `结果 {value1}`, 'Result {value1}')),
      role,
      species: String(c.species || ''),
      speciesSource: String(c.species_source || ''),
      speciesConfidence: String(c.species_confidence || ''),
      roleReason: String(c.role_reason || ''),
      roleConfidence: String(c.role_confidence || ''),
      mappingRequired: c.mapping_requires_confirmation === true,
      mappingConfirmed: c.mapping_confirmed === true || c.mapping_requires_confirmation !== true,
      taskType: String(c.task_type || c.detected_task_type || 'unknown').toLowerCase(),
      state: String(state),
      importable: c.importable,
      blocked: c.blocked === true || blocking.length > 0,
      confirmationEligible: eligible,
      manualConfirm: c.manual_confirmed === true || c.manual_confirm === true,
      confirmationReason: String(c.confirmation_reason || c.confirmationReason || ''),
      diagnosis,
      blocking,
      warnings,
      evidence,
      action: actionLabel(c.recommended_action || c.next_action || suggestions[0]),
      selected: false,
    };
    const status = importStatus(row);
    row.selected = c.selected == null ? status === 'ready' && role !== 'ignore' : !!c.selected;
    return row;
  }

  function setImportStep(step) {
    document.querySelectorAll('#pj-import-card .pj-import-flow span').forEach(el => {
      const n = Number(el.dataset.step || 0);
      el.classList.toggle('done', n < step);
      el.classList.toggle('on', n === step);
    });
  }

  function selectedImportRows() {
    return State.importRows.filter(row => row.selected && row.role !== 'ignore');
  }

  function importCounts() {
    const out = { total: State.importRows.length, ready: 0, created: 0, review: 0, blocked: 0 };
    State.importRows.forEach(row => {
      out[importStatus(row)] += 1;
      if (isCreatedInput(row)) out.created += 1;
    });
    return out;
  }

  function importMethodGate() {
    const selected = selectedImportRows();
    const unresolved = selected.filter(row => convergenceImportStatus(row) !== 'ready');
    const clean = selected.filter(row => row.role === 'clean_slab').length;
    const configs = selected.filter(row => row.role === 'config').length;
    const gas = selected.filter(row => row.role === 'gas_ref').length;
    const molecules = selected.filter(row => row.role === 'molecule_ref').length;
    const standalone = selected.filter(row => row.role === 'standalone').length;
    const missingSpecies = selected.filter(row => row.role === 'molecule_ref' && !row.species.trim());
    const scannedSpeciesRefs = State.importResult && State.importResult.species_refs;
    const usesSpeciesRefs = molecules > 0 || !!(scannedSpeciesRefs && Object.keys(scannedSpeciesRefs).length);
    const missingConfigSpecies = usesSpeciesRefs
      ? selected.filter(row => row.role === 'config' && !row.species.trim()) : [];
    const unsafeMolecules = selected.filter(row => row.role === 'molecule_ref' &&
      String(row.state).toUpperCase() !== 'DONE' && !isCreatedInput(row));
    const pendingMappings = selected.filter(row => row.mappingRequired && !row.mappingConfirmed);
    const selectedMoleculeSpecies = selected.filter(row => row.role === 'molecule_ref')
      .map(row => row.species.trim()).filter(Boolean);
    const duplicateMoleculeSpecies = selectedMoleculeSpecies.filter((value, index, values) =>
      values.findIndex(other => other.toLowerCase() === value.toLowerCase()) !== index);
    const referenceSet = selectedMoleculeSpecies.map(value => value.toLowerCase());
    const unmatchedConfigSpecies = referenceSet.length
      ? selected.filter(row => row.role === 'config' && row.species.trim() &&
        !referenceSet.includes(row.species.trim().toLowerCase())) : [];
    const invalidTasks = selected.filter(row => !['relax', 'static', 'freq', 'dos', 'band'].includes(row.taskType));
    const missingConfirmationReasons = selected.filter(row => row.manualConfirm &&
      row.confirmationEligible && !row.confirmationReason.trim());
    let issue = '';
    if (!selected.length) issue = tr("runtime.project.importmethodgate.text_5a37c6948a", {}, '请至少勾选一个可导入结果', 'Select at least one importable result');
    else if (missingConfirmationReasons.length) issue =
      tr("runtime.project.importmethodgate.text_cc9bc5bb50", { value1: (missingConfirmationReasons.length) }, `有 {value1} 个人工确认结果尚未填写核对依据`, '{value1} manually confirmed results still need a verification rationale');
    else if (unresolved.length) issue = tr("runtime.project.importmethodgate.text_22631ec6b9", { value1: (unresolved.length) }, `仍有 {value1} 个已选结果需要处理`, '{value1} selected results still need attention');
    else if (pendingMappings.length) issue = tr("runtime.project.importmethodgate.text_24eb314ac7", { value1: (pendingMappings.length) }, `请确认 {value1} 个已选结果的智能角色/物种分组`, 'Confirm the inferred role/species grouping for {value1} selected results');
    else if (clean > 1) issue = tr("runtime.project.importmethodgate.text_4bf7ba3488", {}, '一个吸附能项目只能选择 1 个清洁表面', 'An adsorption-energy project can contain only one clean surface');
    else if (gas > 1) issue = tr("runtime.project.importmethodgate.text_0cac64179b", {}, '一个吸附能项目只能选择 1 个吸附质气相参考', 'An adsorption-energy project can contain only one gas-phase adsorbate reference');
    else if (missingSpecies.length) issue = tr("runtime.project.importmethodgate.text_feac85ebcd", { value1: (missingSpecies.length) }, `有 {value1} 个分子参考未填写物种名称（如 Li2S4、S8）`, '{value1} molecular references are missing species names (for example, Li2S4 or S8)');
    else if (missingConfigSpecies.length) issue =
      tr("runtime.project.importmethodgate.text_3fecfe8cfe", { value1: (missingConfigSpecies.length) }, `已选择逐物种分子参考：请为 {value1} 个吸附构型填写对应物种（如 Li2S8）`, 'Per-species molecular references are selected. Assign the corresponding species (for example, Li2S8) to {value1} adsorption configurations.');
    else if (duplicateMoleculeSpecies.length) issue =
      tr("runtime.project.importmethodgate.text_66bec779e5", { value1: (Array.from(new Set(duplicateMoleculeSpecies)).join('、')) }, `分子参考物种重复：{value1}`, 'Duplicate molecular-reference species: {value1}');
    else if (unmatchedConfigSpecies.length) issue =
      tr("runtime.project.importmethodgate.text_075fc1a8b0", { value1: (unmatchedConfigSpecies.length) }, `有 {value1} 个吸附构型没有同名分子参考`, '{value1} adsorption configurations do not have molecular references with the same species name');
    else if (unsafeMolecules.length) issue =
      tr("runtime.project.importmethodgate.text_425b86bf4d", {}, '分子参考仅接收 DONE 结果或已验证的完整四件套待提交；待核项请改为“独立计算结果”', 'Molecular references accept only DONE results or verified complete four-file input sets awaiting submission. Change pending-review items to Standalone calculation result.');
    else if (invalidTasks.length) issue = tr("runtime.project.importmethodgate.text_e59c82dece", { value1: (invalidTasks.length) }, `有 {value1} 个结果尚未选择受支持的任务类型`, '{value1} results do not yet have a supported task type selected');
    else if (configs && clean !== 1) issue = tr("runtime.project.importmethodgate.text_84d19f9237", {}, '已选择吸附构型，请再指定 1 个清洁表面', 'Adsorption configurations are selected; also specify one clean surface');
    else if (clean && !configs) issue = tr("runtime.project.importmethodgate.text_a31bab58f5", {}, '已选择清洁表面，请至少再选择 1 个吸附构型', 'A clean surface is selected; also select at least one adsorption configuration');
    else if (gas && !clean) issue = tr("runtime.project.importmethodgate.text_a1350f8232", {}, '吸附质气相参考需与清洁表面和吸附构型一起导入', 'The gas-phase adsorbate reference must be imported together with a clean surface and adsorption configuration');
    else if (!clean && !molecules && !standalone) issue = tr("runtime.project.importmethodgate.text_4c03391b4c", {}, '请修正结果角色后再导入', 'Correct the result roles before importing');
    return { ok: !issue, issue, selected, clean, configs, gas, molecules, standalone };
  }

  function renderImportSummary() {
    const box = $('pj-import-summary');
    const attention = $('pj-import-attention');
    if (!box || !attention) return;
    const n = importCounts();
    box.innerHTML = [
      ['total', n.total, tr("runtime.project.renderimportsummary.text_502d09fbf8", {}, '扫描到的计算目录', 'Calculation directories scanned')],
      ['ready', n.ready, tr("runtime.project.renderimportsummary.text_ca8cae839d", {}, '可直接导入', 'Ready to import')],
      ['created', n.created, tr("runtime.project.renderimportsummary.text_5cb25b54c0", {}, '四件套待提交', 'Four-file input sets awaiting submission')],
      ['review', n.review, tr("runtime.project.renderimportsummary.text_157de8355b", {}, '需你确认', 'Needs your confirmation')],
      ['blocked', n.blocked, tr("runtime.project.renderimportsummary.text_b7f4ee0577", {}, '暂不可导入', 'Cannot be imported yet')],
    ].map(x => `<div class="pj-import-stat ${x[0]}"><b>${x[1]}</b><span>${x[2]}</span></div>`).join('');
    const gate = importMethodGate();
    const pending = n.review + n.blocked;
    attention.classList.toggle('warn', pending > 0 || !gate.ok);
    if (pending) {
      attention.textContent = tr("runtime.project.renderimportsummary.text_6da4b45008", { value1: (n.ready - n.created) }, `软件已先选中 {value1} 个可靠结果`, 'The application preselected {value1} reliable results') +
        (n.created ? tr("runtime.project.renderimportsummary.text_225b75919b", { value1: (n.created) }, `和 {value1} 个待提交四件套`, ' and {value1} four-file input sets awaiting submission') : '') +
        tr("runtime.project.renderimportsummary.text_fe8c4a0eba", {}, '。请处理黄色/红色条目；点击每行“查看证据”可看到无法导入的具体原因。', '. Resolve the yellow/red items; select View evidence on a row to see why it cannot be imported.');
    } else if (n.created) {
      attention.textContent = tr("runtime.project.renderimportsummary.text_534f374a94", { value1: (n.created) }, `其中 {value1} 个目录只有完整四件套、尚未计算；会以“待提交”导入，随后前往任务页选择服务器提交，不会冒充已收敛结果。`, '{value1} directories contain complete four-file input sets but have not been calculated. They will be imported as Awaiting submission, then submitted from Jobs after a server is selected; they will not be presented as converged results.');
    } else {
      attention.textContent = tr("runtime.project.renderimportsummary.text_8d5b60e5cf", {}, '所有结果均已通过检查。确认清洁表面和吸附构型角色后即可建立项目。', 'All results passed the checks. Confirm the clean-surface and adsorption-configuration roles to create the project.');
    }
  }

  function rowMatches(row) {
    const filter = val('pj-import-filter') || 'attention';
    const query = val('pj-import-search').toLowerCase();
    const status = importStatus(row);
    const visibleStatus = isCreatedInput(row) ? 'created' : status;
    if (filter === 'attention' && status === 'ready' && !isCreatedInput(row)) return false;
    if (filter !== 'all' && filter !== 'attention' && filter !== visibleStatus) return false;
    if (!query) return true;
    const haystack = [row.name, row.path, row.role, row.species, row.speciesSource,
      row.roleReason, row.taskType, row.state, row.action,
      ...row.diagnosis, ...row.blocking, ...row.warnings, ...row.evidence].join(' ').toLowerCase();
    return haystack.includes(query);
  }

  function optionHtml(options, current, labels) {
    const values = Array.from(new Set([current, ...options].filter(Boolean)));
    return values.map(value => `<option value="${VCS.esc(value)}"${value === current ? ' selected' : ''}>` +
      `${VCS.esc(labelFor(labels, value))}</option>`).join('');
  }

  function renderImportRows() {
    const body = $('pj-import-rows');
    const empty = $('pj-import-empty');
    if (!body || !empty) return;
    const visible = State.importRows.filter(rowMatches);
    const groups = [];
    visible.forEach(row => {
      const key = `${row.role}:${row.species || '未识别'}`;
      let group = groups.find(item => item.key === key);
      if (!group) { group = { key, role: row.role, species: row.species || tr("runtime.project.renderimportrows.text_6ea09ad378", {}, '未识别', 'Unidentified'), rows: [] }; groups.push(group); }
      group.rows.push(row);
    });
    groups.sort((left, right) => left.key.localeCompare(right.key));
    body.innerHTML = groups.map((group, groupIndex) => {
      const pending = group.rows.filter(row => row.mappingRequired && !row.mappingConfirmed);
      const groupHeader = '<tr class="pj-import-group-row"><td colspan="6"><span><b>' +
        `${VCS.esc(labelFor(ROLE_LABELS, group.role))}</b> / ${VCS.esc(group.species)} · ` +
        tr("runtime.project.renderimportrows.text_2397863033", { value1: (group.rows.length) }, `{value1} 项</span>`, '{value1} items</span>') + (pending.length
          ? tr("runtime.project.renderimportrows.text_fca3d9d7f8", { value1: (groupIndex) }, `<button class="btn quiet" type="button" data-confirm-import-group="{value1}">确认本组并选中</button>`, '<button class="btn quiet" type="button" data-confirm-import-group="{value1}">Confirm and select this group</button>') : '') +
        '</td></tr>';
      const groupRows = group.rows.map(row => {
      const status = importStatus(row);
      const convergenceStatus = convergenceImportStatus(row);
      const created = isCreatedInput(row);
      const statusLabel = created ? tr("runtime.project.renderimportrows.text_3ad85eb4bd", {}, '四件套完整，待提交', 'Complete four-file input set; awaiting submission')
        : { ready: tr("runtime.project.renderimportrows.text_eb9e2c08c7", {}, '可导入', 'Importable'), review: tr("runtime.project.renderimportrows.text_da8ce55c93", {}, '需确认', 'Needs confirmation'), blocked: tr("runtime.project.renderimportrows.text_b7f4ee0577", {}, '暂不可导入', 'Cannot be imported yet') }[status];
      const statusClass = created ? 'run'
        : { ready: 'ok', review: 'warn', blocked: 'fail' }[status];
      const reasons = [...row.blocking, ...row.diagnosis, ...row.warnings, ...row.evidence];
      const mappingPending = row.mappingRequired && !row.mappingConfirmed;
      const mainReason = (mappingPending ? tr("runtime.project.renderimportrows.text_3639145889", {
        value1: row.roleReason || tr('runtime.project.renderimportrows.review_role_species', {},
          '请核对角色与物种', 'Review the role and species'),
      }, `智能分组待确认：{value1}`, 'Inferred grouping needs confirmation: {value1}') : '') ||
        row.blocking[0] || row.diagnosis[0] || row.warnings[0] ||
        (created ? tr("runtime.project.renderimportrows.text_e33305863e", {}, '输入文件已通过检查；尚无输出，导入后需要提交计算', 'Input files passed validation; no output exists yet, so the calculation must be submitted after import')
          : status === 'ready' ? tr("runtime.project.renderimportrows.text_f3c5445ea7", {}, '能量与收敛证据已通过检查', 'Energy and convergence evidence passed validation') : tr("runtime.project.renderimportrows.text_d68c757e2b", {}, '尚缺少足够的完成证据', 'Insufficient completion evidence'));
      const detail = reasons.length ? tr("runtime.project.renderimportrows.text_cb265b6367", {}, '<details><summary>查看证据与完整原因</summary><ul class="pj-import-evidence">', '<details><summary>View evidence and full reasons</summary><ul class="pj-import-evidence">') +
        reasons.map(item => `<li>${VCS.esc(item)}</li>`).join('') + '</ul></details>' : '';
      const roleOptions = ['clean_slab', 'config', 'gas_ref', 'molecule_ref', 'standalone', 'ignore'];
      const taskOptions = ['unknown', 'relax', 'static', 'freq', 'dos', 'band'];
      const disabled = status === 'blocked' && !row.confirmationEligible ? ' disabled' : '';
      const speciesPlaceholder = row.role === 'molecule_ref'
        ? tr('runtime.project.import.species_placeholder_molecule', {},
          '物种，如 Li2S4', 'Species, e.g. Li2S4')
        : tr('runtime.project.import.species_placeholder_adsorption', {},
          '吸附物种，如 Li2S8（使用分子参考时必填）',
          'Adsorbed species, e.g. Li2S8 (required when using a molecular reference)');
      const speciesInput = ['config', 'molecule_ref'].includes(row.role)
        ? `<input class="ipt pj-import-species" data-act="species" value="${VCS.esc(row.species)}" ` +
          `placeholder="${VCS.esc(speciesPlaceholder)}">`
        : '';
      const mappingState = mappingPending
        ? tr('runtime.project.import.mapping_pending', {}, '待确认', 'Awaiting confirmation')
        : tr('runtime.project.import.mapping_confirmed', {}, '已确认', 'Confirmed');
      const mappingSource = row.speciesSource || row.roleReason || tr(
        'runtime.project.import.mapping_manual_source', {}, '人工设置', 'Set manually');
      const mappingHint = `<span class="pj-import-mapping ${mappingPending ? 'pending' : 'confirmed'}">` +
        `${VCS.esc(mappingState)} · ${VCS.esc(mappingSource)}</span>`;
      const confirmationInput = row.confirmationEligible
        ? `<textarea class="ipt pj-import-confirm-reason" data-act="confirm-reason" rows="2" ` +
          tr("runtime.project.renderimportrows.text_44d7aa83d4", { value1: (VCS.esc(row.confirmationReason)) }, `placeholder="请填写你核对了哪些输出证据">{value1}</textarea>`, 'placeholder="Describe the output evidence you checked">{value1}</textarea>')
        : '';
      const manualLabel = created ? tr("runtime.project.renderimportrows.text_719572715d", {}, '输入检查已通过，不是收敛结果', 'Input validation passed; this is not a converged result')
        : convergenceStatus === 'ready' ? tr("runtime.project.renderimportrows.text_8ed8e28e25", {}, '自动检查已通过，无需人工确认', 'Automatic checks passed; manual confirmation is unnecessary')
        : row.confirmationEligible ? tr("runtime.project.renderimportrows.text_1ed86bf93a", {}, '我已核对并确认收敛', 'I checked the evidence and confirm convergence') : tr("runtime.project.renderimportrows.text_aa19bd5f9b", {}, '存在硬性问题，不能人工跳过', 'Blocking issues cannot be bypassed manually');
      const manualHint = created ? tr("runtime.project.renderimportrows.text_29606e476e", {}, '导入后在任务页选择服务器、核数和墙时再提交', 'After import, select a server, core count, and wall time on Jobs, then submit')
        : convergenceStatus === 'ready' ? tr("runtime.project.renderimportrows.text_403b6b1621", {}, '可直接导入；提交时仍会复核源文件是否变化', 'Ready to import; the source files will be revalidated during submission')
        : row.confirmationEligible ? tr("runtime.project.renderimportrows.text_689d56f7f8", {}, '请保留可审计的核对依据；提交时仍会复核硬性门禁', 'Keep an auditable verification rationale; blocking gates will be revalidated during submission')
          : tr("runtime.project.renderimportrows.text_468dfcbb5b", {}, '请按左侧原因补齐结果', 'Resolve the reasons shown on the left');
      return `<tr data-import-index="${row.index}" class="pj-import-${status}">` +
        `<td class="pj-import-check"><input type="checkbox" data-act="select"${row.selected ? ' checked' : ''}${disabled}></td>` +
        `<td class="pj-import-dir"><span class="name" title="${VCS.esc(row.path)}">${VCS.esc(row.name)}</span>` +
        `<span class="sub" title="${VCS.esc(row.path)}">${VCS.esc(row.path)}</span></td>` +
        `<td><select class="ipt" data-act="role">${optionHtml(roleOptions, row.role, ROLE_LABELS)}</select>${speciesInput}${mappingHint}</td>` +
        `<td><select class="ipt" data-act="task">${optionHtml(taskOptions, row.taskType, TASK_LABELS)}</select></td>` +
        `<td class="pj-import-reason"><span class="pill ${statusClass}"><i></i>${statusLabel}</span> ` +
        `<span class="pj-import-mainreason">${VCS.esc(mainReason)}</span>` +
        (row.action ? tr("runtime.project.renderimportrows.text_f4e2a52f1d", { value1: (VCS.esc(row.action)) }, `<span class="pj-import-action">下一步：{value1}</span>`, '<span class="pj-import-action">Next: {value1}</span>') : '') + detail + '</td>' +
        `<td class="pj-import-manual"><label><input type="checkbox" data-act="confirm"` +
        `${row.manualConfirm ? ' checked' : ''}${row.confirmationEligible ? '' : ' disabled'}>` +
        `${manualLabel}</label><span class="sub">${manualHint}</span>` +
        `${confirmationInput}</td></tr>`;
      }).join('');
      return groupHeader + groupRows;
    }).join('');
    empty.hidden = visible.length > 0;
    body.querySelectorAll('[data-confirm-import-group]').forEach(button => {
      button.addEventListener('click', () => {
        const group = groups[Number(button.dataset.confirmImportGroup)];
        if (!group) return;
        group.rows.forEach(row => {
          row.mappingConfirmed = true;
          if (convergenceImportStatus(row) === 'ready' && row.role !== 'ignore') row.selected = true;
        });
        renderImport();
      });
    });
    body.querySelectorAll('tr[data-import-index]').forEach(tr => {
      const row = State.importRows[Number(tr.dataset.importIndex)];
      tr.querySelector('[data-act="select"]').addEventListener('change', e => {
        row.selected = e.target.checked;
        updateImportCommit();
      });
      tr.querySelector('[data-act="role"]').addEventListener('change', e => {
        row.role = e.target.value;
        row.mappingConfirmed = true;
        if (row.role === 'ignore') row.selected = false;
        renderImport();
      });
      tr.querySelector('[data-act="task"]').addEventListener('change', e => {
        row.taskType = e.target.value;
        renderImportSummary();
        updateImportCommit();
      });
      const species = tr.querySelector('[data-act="species"]');
      if (species) species.addEventListener('input', e => {
        row.species = e.target.value;
        row.mappingConfirmed = true;
        updateImportCommit();
      });
      const confirmationReason = tr.querySelector('[data-act="confirm-reason"]');
      if (confirmationReason) {
        confirmationReason.addEventListener('input', e => {
          row.confirmationReason = e.target.value;
          renderImportSummary();
          updateImportCommit();
        });
        confirmationReason.addEventListener('change', () => renderImport());
      }
      tr.querySelector('[data-act="confirm"]').addEventListener('change', e => {
        row.manualConfirm = row.confirmationEligible && e.target.checked;
        if (row.manualConfirm && !row.confirmationReason.trim()) {
          row.confirmationReason = defaultConfirmationReason();
        }
        if (row.manualConfirm && row.role !== 'ignore') row.selected = true;
        renderImport();
      });
    });
    const master = $('pj-import-check-visible');
    if (master) {
      const selectable = visible.filter(row => !(importStatus(row) === 'blocked' && !row.confirmationEligible));
      master.checked = selectable.length > 0 && selectable.every(row => row.selected);
      master.indeterminate = selectable.some(row => row.selected) && !master.checked;
    }
  }

  function updateImportCommit() {
    const gate = importMethodGate();
    const note = $('pj-import-selection-note');
    const button = $('pj-import-commit');
    const name = val('pj-import-name');
    const root = val('pj-import-root');
    if (note) {
      note.textContent = gate.ok
        ? tr("runtime.project.updateimportcommit.text_aa3ceaf4b3", { value1: (gate.selected.length), value2: (gate.clean), value3: (gate.configs), value4: (gate.molecules), value5: (gate.standalone) }, `将导入 {value1} 个结果（清洁表面 {value2}、吸附构型 {value3}、分子参考 {value4}、独立结果 {value5}）`, 'Import {value1} results (clean surfaces {value2}, adsorption configurations {value3}, molecular references {value4}, standalone results {value5})')
        : gate.issue;
    }
    if (button) button.disabled = !gate.ok || !name || !root;
    persistImportDraftReference();
  }

  function importDrafts() {
    return VCS.workspace && VCS.workspace.drafts || null;
  }

  function hasImportDraftContent() {
    return !!(val('pj-import-source') || val('pj-import-name') || val('pj-import-root') ||
      State.importResult || State.importRows.length);
  }

  function updateImportDraftAction() {
    const button = $('pj-import-cancel');
    const drafts = importDrafts();
    if (button) button.disabled = !(drafts && drafts.load(IMPORT_DRAFT_ID));
  }

  function persistImportDraftReference() {
    const drafts = importDrafts();
    if (!drafts) return false;
    if (!hasImportDraftContent()) {
      if (drafts.load(IMPORT_DRAFT_ID)) drafts.remove(IMPORT_DRAFT_ID);
      updateImportDraftAction();
      return false;
    }
    drafts.save(IMPORT_DRAFT_ID, IMPORT_DRAFT_MARKER, {
      label: tr('runtime.project.import_draft.label', {},
        '项目导入流程尚未完成', 'Project import flow is unfinished'),
      kind: 'project-import',
    });
    updateImportDraftAction();
    return true;
  }

  function clearImportDraftReference() {
    const drafts = importDrafts();
    if (drafts && drafts.load(IMPORT_DRAFT_ID)) drafts.remove(IMPORT_DRAFT_ID);
    updateImportDraftAction();
  }

  function discardImportDraft() {
    clearImportDraftReference();
    State.importRows = [];
    State.importResult = null;
    ['pj-import-source', 'pj-import-name', 'pj-import-root', 'pj-import-search'].forEach(
      id => setVal(id, ''));
    if ($('pj-import-filter')) $('pj-import-filter').value = 'attention';
    if ($('pj-import-review')) $('pj-import-review').hidden = true;
    if ($('pj-import-done')) $('pj-import-done').hidden = true;
    showImportProblem('', '');
    setImportStep(1);
    renderImport();
    const source = $('pj-import-source');
    if (source) source.focus();
    VCS.toast(tr('runtime.project.import_draft.discarded', {},
      '已取消本次导入并清除恢复入口', 'The import was cancelled and its resume entry was cleared'));
  }

  function resumeImportDraftEntry() {
    const drafts = importDrafts();
    if (!(drafts && drafts.load(IMPORT_DRAFT_ID))) return false;
    setExplicitWorkflow('import');
    setAccordionOpen('pj-create-card', false);
    setAccordionOpen('pj-import-card', true);
    scrollToCard('pj-import-card');
    if (!hasImportDraftContent()) {
      showImportProblem(tr('runtime.project.import_draft.restored', {},
        '已恢复到项目导入入口', 'Returned to the project import entry'),
      tr('runtime.project.import_draft.private', {},
        '为保护本地路径和核对说明，恢复引用不保存表单正文；请重新选择结果文件夹。',
        'To protect local paths and review notes, the resume reference stores no form body. Reselect the results folder.'));
    }
    const source = $('pj-import-source');
    if (source) source.focus();
    updateImportDraftAction();
    return true;
  }

  function renderImport() {
    renderImportSummary();
    renderImportRows();
    updateImportCommit();
  }

  async function chooseImportSource() {
    const r = await VCS.call('pick_dir');
    if (r && r.error) {
      VCS.log(tr("runtime.project.chooseimportsource.text_88689b28ee", {}, '选择结果文件夹失败:', 'Failed to select results folder:') + r.error, 'failc');
      showImportProblem(tr("runtime.project.chooseimportsource.text_715116e131", {}, '没有选中结果文件夹', 'No results folder was selected'), tr("runtime.project.chooseimportsource.text_52c91b302d", {}, '重新点击“选择整个文件夹”；应选择包含多个计算子目录的上层文件夹。', 'Select Choose entire folder again and choose a parent folder that contains multiple calculation subdirectories.'));
      return;
    }
    if (!r || !r.path) return;
    showImportProblem('', '');
    setVal('pj-import-source', r.path);
    if (!val('pj-import-name')) setVal('pj-import-name', pathBase(r.path));
    if (!val('pj-import-root')) setVal('pj-import-root', pathParent(r.path));
    await scanImport();
  }

  async function scanImport() {
    const source = val('pj-import-source');
    if (!source) {
      showImportProblem(tr("runtime.project.scanimport.text_70d0a50069", {}, '尚未选择结果根文件夹', 'No results root folder has been selected'), tr("runtime.project.scanimport.text_d8ddead88b", {}, '点击“选择整个文件夹”，选择包含 OUTCAR / OSZICAR 等结果的上层目录。', 'Select Choose entire folder and choose the parent folder containing OUTCAR, OSZICAR, or other result files.'));
      VCS.toast(tr("runtime.project.scanimport.text_1df4aff6b5", {}, '请先选择包含计算结果的根文件夹', 'Select the root folder containing calculation results first'), 'fail');
      return;
    }
    // Folder pickers assign values programmatically and therefore do not emit
    // an input event.  Record the safe resume marker before validation so a
    // failed/interrupted scan still leaves a truthful entry point.
    persistImportDraftReference();
    const button = $('pj-import-scan');
    const review = $('pj-import-review');
    const done = $('pj-import-done');
    if (button) { button.disabled = true; button.textContent = tr("runtime.project.scanimport.text_a5b0d8e078", {}, '正在检查…', 'Checking…'); }
    if (done) done.hidden = true;
    setImportStep(2);
    VCS.log(tr("runtime.project.scanimport.text_c99805cbc9", {}, '正在检查本地结果:', 'Checking local results:') + source + '…');
    showImportProblem('', '');
    try {
      const r = await VCS.call('proj_import_scan', source);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr("runtime.project.scanimport.text_a6d0abed72", {}, '结果检查失败:', 'Result validation failed:') + ((r && r.error) || tr("runtime.project.scanimport.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
        showImportProblem(tr("runtime.project.scanimport.text_0ed65668a2", {}, '结果检查未完成：', 'Result validation did not complete:') + ((r && r.error) || tr("runtime.project.scanimport.text_bd5e21c357", {}, '未知错误', 'Unknown error')),
          tr("runtime.project.scanimport.text_a62b9f427c", {}, '确认目录仍可访问，并选择含 OUTCAR、OSZICAR 或 vasprun.xml 的上层文件夹后重新检查。', 'Confirm that the directory is still accessible, select a parent folder containing OUTCAR, OSZICAR, or vasprun.xml, and check again.'));
        VCS.toast(tr("runtime.project.scanimport.text_61981b51f9", {}, '没有完成检查，请查看日志中的具体原因', 'Validation did not complete; see the log for the specific reason'), 'fail');
        setImportStep(1);
        return;
      }
      State.importResult = r;
      State.importRows = (r.candidates || []).map(normalizeImportCandidate);
      setVal('pj-import-name', r.suggested_name || val('pj-import-name') || pathBase(source));
      setVal('pj-import-root', r.suggested_out_root || val('pj-import-root') || pathParent(source));
      if (review) review.hidden = false;
      setImportStep(3);
      const n = importCounts();
      // 没有异常时直接展示全部角色，避免“只看需处理”得到一张空表，用户却找不到
      // 清洁表面/吸附构型在哪里确认。
      if (!n.review && !n.blocked && $('pj-import-filter')) $('pj-import-filter').value = 'all';
      renderImport();
      VCS.log(tr("runtime.project.scanimport.text_da9c2b91be", { value1: (n.total), value2: (n.ready), value3: (n.review), value4: (n.blocked) }, `已检查 {value1} 个计算目录：{value2} 个可导入，{value3} 个需确认，{value4} 个暂不可导入`, 'Checked {value1} calculation directories: {value2} importable, {value3} needing confirmation, and {value4} not yet importable'),
        n.blocked ? 'warnc' : 'okc');
      if (!n.total) {
        showImportProblem(tr("runtime.project.scanimport.text_2d590676e2", {}, '没有发现可识别的 VASP 结果', 'No recognizable VASP results were found'),
          tr("runtime.project.scanimport.text_d522a68c65", {}, '不要只选空的项目目录；请选择包含 OUTCAR、OSZICAR 或 vasprun.xml 的计算目录或其上层文件夹。', 'Do not select an empty project directory. Select a calculation directory containing OUTCAR, OSZICAR, or vasprun.xml, or its parent folder.'));
        VCS.toast(tr("runtime.project.scanimport.text_0eeedf5208", {}, '没有发现可识别的 VASP 结果，请确认选择的是上层根文件夹', 'No recognizable VASP results were found; confirm that you selected the parent root folder'), 'fail');
      }
    } finally {
      if (button) { button.disabled = false; button.textContent = tr("runtime.project.scanimport.text_20089c4224", {}, '重新检查', 'Check again'); }
    }
  }

  function importSelections() {
    return State.importRows.map(row => ({
      path: row.path,
      selected: !!row.selected && row.role !== 'ignore',
      role: row.role,
      task_type: row.taskType,
      manual_confirm: !!row.manualConfirm && row.confirmationEligible,
      confirmation_reason: row.manualConfirm ? row.confirmationReason.trim() : null,
      species: row.species || null,
      mapping_confirmed: row.mappingConfirmed === true,
      species_source: row.speciesSource || null,
      source_fingerprint: row.raw && row.raw.source_fingerprint || null,
    }));
  }

  function projectIdFrom(result) {
    return rawProjectId(result && result.project) || rawProjectId(result);
  }

  function projectNameFrom(result, fallback = '') {
    return String(result && (result.name || result.project_name ||
      (result.project && result.project.name)) || fallback);
  }

  async function commitImport() {
    const gate = importMethodGate();
    const source = val('pj-import-source'), root = val('pj-import-root');
    const name = val('pj-import-name');
    if (!gate.ok || !source || !root || !name) { updateImportCommit(); return; }
    const button = $('pj-import-commit');
    if (button) { button.disabled = true; button.textContent = tr("runtime.project.commitimport.text_d14fcc9562", {}, '正在导入并复核…', 'Importing and revalidating…'); }
    VCS.log(tr("runtime.project.commitimport.text_9988c46628", { value1: (gate.selected.length), value2: (name) }, `正在导入 {value1} 个本地结果并建立项目「{value2}」…`, 'Importing {value1} local results and creating project “{value2}”…'));
    try {
      const r = await VCS.call('proj_import_commit', source, root, name, importSelections());
      if (!r || r.ok === false || r.error) {
        VCS.log(tr("runtime.project.commitimport.text_b79a0db205", {}, '导入失败:', 'Import failed:') + ((r && r.error) || tr("runtime.project.commitimport.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
        showImportProblem(tr("runtime.project.commitimport.text_bc45eb83a8", {}, '导入未完成：', 'Import did not complete:') + ((r && r.error) || tr("runtime.project.commitimport.text_bd5e21c357", {}, '未知错误', 'Unknown error')),
          tr("runtime.project.commitimport.text_2fb54d1268", {}, '按黄色/红色条目的“下一步”修正；若目标项目已存在，请更换项目名或保存位置后重试。', 'Follow Next on each yellow/red item. If the target project already exists, use a different project name or save location, then try again.'));
        textList(r && (r.errors || r.blocking_reasons)).forEach(x => VCS.log(x, 'warnc'));
        VCS.toast(tr("runtime.project.commitimport.text_f06f2852dd", {}, '导入未完成；扫描后结果可能有变化，请按提示重新检查', 'Import did not complete; results may have changed since scanning, so check again as instructed'), 'fail');
        return;
      }
      // The project was durably created.  Follow-up analysis/report refreshes
      // may still fail independently, but this import draft is complete.
      clearImportDraftReference();
      textList(r.warnings).forEach(x => VCS.log(x, 'warnc'));
      showImportProblem('', '');
      const createdProjectId = projectIdFrom(r);
      const createdProjectName = projectNameFrom(r, name);
      VCS.log(tr("runtime.project.commitimport.text_54912ff73c", {}, '本地结果已建立吸附能项目:', 'Local results created an adsorption-energy project:') + createdProjectName, 'okc');
      if (r.auto_report && r.auto_report.ok) {
        const files = (r.auto_report.files || [r.auto_report.file]).filter(Boolean);
        VCS.log((r.auto_report_reason ? tr("runtime.project.commitimport.text_7dd8c823cf", {}, '诊断报告', 'Diagnostic report') : tr("runtime.project.commitimport.text_2c48c34392", {}, '最终报告', 'Final report')) +
          tr("runtime.project.commitimport.text_20271719ff", {}, '已自动生成:', 'Generated automatically:') + files.join('；'), r.auto_report_reason ? 'warnc' : 'okc');
      } else if (r.auto_report && r.auto_report.error) {
        VCS.log(tr("runtime.project.commitimport.text_7ca522b3d8", {}, '项目已导入，但自动报告暂未生成:', 'The project was imported, but the automatic report has not been generated yet:') + r.auto_report.error, 'warnc');
      }
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      await reloadProjects();
      const sel = $('pj-select');
      const hit = State.projects.find(p => projectId(p) === createdProjectId);
      if (sel && hit) {
        applyProjectSelection(hit);
      }
      // 建好项目后立即刷新 ΔE；后端已在全 DONE 时自动生成最终或诊断报告。
      const deltaResult = await delta();
      const deltaRows = (deltaResult && deltaResult.rows) || [];
      const deltaMethodBlocked = String(deltaResult && deltaResult.method_consistency &&
        deltaResult.method_consistency.status || '').toLowerCase() === 'incompatible';
      const isAdsorption = gate.clean > 0 && gate.configs > 0;
      const hasIncompleteDelta = isAdsorption &&
        (!deltaRows.length || deltaRows.some(row => row.delta_e == null));
      const hasNeedsHuman = Number(r.summary && r.summary.needs_human || 0) > 0;
      const createdCount = Number(r.summary && r.summary.created || 0);
      // A molecule-reference-only import is a reusable reference library, not an
      // adsorption project with a reportable data point.  Keep the report action
      // locked until both clean slab and at least one config are present.
      const reportReady = isAdsorption && !hasNeedsHuman && !hasIncompleteDelta && !deltaMethodBlocked;
      const referenceOnly = gate.molecules > 0 && !isAdsorption;
      const startWithReferences = () => startLiS(createdProjectId);
      const openCreatedJobs = () => openImportedCreatedJobs(createdProjectId);
      showImportDone(r, gate.selected.length, reportReady,
        gate.molecules > 0 && createdCount === 0,
        referenceOnly, createdCount, openCreatedJobs);
      setImportStep(4);
      if (!createdCount && isAdsorption && VCS.pipeline &&
          typeof VCS.pipeline.reconfigure === 'function') {
        await VCS.pipeline.reconfigure();
      }
      VCS.toast(createdCount ? tr("runtime.project.commitimport.text_2b2c2ce317", { value1: (createdCount) }, `已导入；{value1} 个四件套作业等待提交`, 'Imported; {value1} four-file input-set jobs are awaiting submission')
        : referenceOnly ? tr("runtime.project.commitimport.text_a27eb0bb92", {}, 'Li-S 参考能库已建立，可以开始新的吸附计算', 'The Li-S reference-energy library is ready; you can start a new adsorption calculation')
        : reportReady ? tr("runtime.project.commitimport.text_4bf2b58f2f", {}, '结果与 ΔE 已载入，报告将自动生成', 'Results and ΔE have been loaded; the report will be generated automatically')
          : tr("runtime.project.commitimport.text_ec0fd5f48a", {}, '结果已导入；将生成诊断报告并列出缺项', 'Results imported; a diagnostic report will list the missing items'));
      if (typeof VCS.nextStep === 'function') {
        VCS.nextStep({
          title: tr("runtime.project.commitimport.text_fc6573fc58", {}, '结果导入完成', 'Result import complete'),
          message: tr("runtime.project.commitimport.text_6bb19d80e7", { value1: (gate.selected.length), value2: createdProjectName }, `已导入 {value1} 个条目并建立项目「{value2}」。`, 'Imported {value1} entries and created project “{value2}”.'),
          detail: createdCount
            ? tr("runtime.project.commitimport.text_ebac0f1205", { value1: (createdCount) }, `其中 {value1} 个只有完整四件套、尚未运行。下一步到任务页选择服务器、核数和墙时后提交。`, '{value1} entries contain complete four-file input sets but have not run. Next, select a server, core count, and wall time on Jobs, then submit them.')
            : referenceOnly
            ? tr("runtime.project.commitimport.text_67dbb52494", {}, '这些已收敛的 Li-S 能量已加入参考库。下一步只需选择固定 INCAR、clean slab 和 adsorption 结构。', 'These converged Li-S energies were added to the reference library. Next, select a fixed INCAR, clean slab, and adsorption structures.')
            : reportReady
            ? tr("runtime.project.commitimport.text_d09ad2cc9a", {}, 'ΔE 已自动计算并显示在本页；最终报告将自动生成，也可立即打开报告入口。', 'ΔE was calculated automatically and is shown on this page. The final report will be generated automatically, and you can open the report entry now.')
            : tr("runtime.project.commitimport.text_b4e2289f9f", {}, 'ΔE 表已自动刷新；系统将生成诊断报告并列出缺角色、缺能量或待确认结果。', 'The ΔE table was refreshed automatically. A diagnostic report will list missing roles, missing energies, and results awaiting confirmation.'),
          primaryLabel: createdCount ? tr("runtime.project.commitimport.text_4fb032f671", { value1: (createdCount) }, `提交 {value1} 个待运行作业`, 'Submit {value1} jobs awaiting execution')
            : referenceOnly ? tr("runtime.project.commitimport.text_3f87e40b80", {}, '用这些参考能开始吸附计算', 'Start an adsorption calculation with these reference energies')
            : reportReady ? tr("runtime.project.commitimport.text_0fc5feb884", {}, '查看或立即生成报告', 'View or generate the report now') : tr("runtime.project.commitimport.text_328e3082d0", {}, '查看 ΔE 缺项', 'View missing ΔE items'),
          stayLabel: createdCount ? tr("runtime.project.commitimport.text_ac1637dfd4", {}, '先检查待提交成员', 'Inspect members awaiting submission first')
            : referenceOnly ? tr("runtime.project.commitimport.text_18f5b251ea", {}, '先检查参考能清单', 'Inspect the reference-energy list first') : tr("runtime.project.commitimport.text_56344b9d82", {}, '先检查导入清单', 'Inspect the import list first'),
          onPrimary: createdCount ? openCreatedJobs
            : referenceOnly ? startWithReferences : reportReady ? report : () => {
            const table = $('pj-table');
            if (table && typeof table.scrollIntoView === 'function') {
              table.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
          },
        });
      }
    } finally {
      if (button) { button.textContent = tr("runtime.project.commitimport.text_46487dd941", {}, '导入所选结果并建立项目', 'Import selected results and create project'); updateImportCommit(); }
    }
  }

  async function openImportedCreatedJobs(projectIdValue) {
    const out = await VCS.navigate('jobs', { source: 'import-created-inputs' });
    if (!out.ok) return;
    if (window.Jobs && typeof window.Jobs.selectCreatedProject === 'function') {
      const selected = await window.Jobs.selectCreatedProject(projectIdValue);
      if (!selected) VCS.toast(tr("runtime.project.openimportedcreatedjobs.text_9a2318a251", {}, '未找到待提交成员，请在任务页清除筛选后检查', 'No members awaiting submission were found; clear the filters on Jobs and check again'), 'fail');
    }
  }

  function showImportDone(result, count, reportReady, hasSpeciesReferences, referenceOnly,
                          createdCount, openCreatedJobs) {
    const box = $('pj-import-done');
    if (!box) return;
    box.hidden = false;
    box.innerHTML = tr("runtime.project.showimportdone.text_b7d65b63a3", { value1: VCS.esc(projectNameFrom(result, val('pj-import-name'))) }, `<b>导入完成：</b>{value1}，`, '<b>Import complete:</b> {value1}, ') +
      tr("runtime.project.showimportdone.text_1655f5a868", {
        value1: count,
        value2: createdCount ? tr('runtime.project.showimportdone.created_pending', {
          count: createdCount,
        }, '{count} 个四件套作业等待提交。',
        '{count} four-file input-set jobs are awaiting submission.')
          : referenceOnly ? tr('runtime.project.showimportdone.references_ready', {},
            'Li-S 参考能库已就绪。', 'The Li-S reference-energy library is ready.')
            : reportReady ? tr('runtime.project.showimportdone.delta_ready', {},
              'ΔE 已显示在下方，最终报告将自动生成。',
              'ΔE is shown below; the final report will be generated automatically.')
              : tr('runtime.project.showimportdone.diagnostic_pending', {},
                '将自动生成诊断报告并列出下方 ΔE 缺项。',
                'A diagnostic report will be generated automatically and list the missing ΔE items below.'),
      }, `共 {value1} 个条目。{value2}`, '{value1} entries in total. {value2}') +
      '<div class="actions">' + (referenceOnly ? ''
        : tr("runtime.project.showimportdone.text_fee5e6543c", {}, '<button class="btn" type="button" data-next="delta">重新计算 ΔE</button>', '<button class="btn" type="button" data-next="delta">Recalculate ΔE</button>')) +
      (createdCount ? tr("runtime.project.showimportdone.text_f662cf0125", { value1: (createdCount) }, `<button class="btn primary" type="button" data-next="submit-created">提交 {value1} 个待运行作业</button>`, '<button class="btn primary" type="button" data-next="submit-created">Submit {value1} jobs awaiting execution</button>') : '') +
      (hasSpeciesReferences
        ? tr("runtime.project.showimportdone.text_76283c0e5f", {}, '<button class="btn primary" type="button" data-next="lis">用这些参考能开始吸附计算</button>', '<button class="btn primary" type="button" data-next="lis">Start an adsorption calculation with these reference energies</button>') : '') +
      (referenceOnly ? '' : tr("runtime.project.showimportdone.text_ca078ec46f", {
        value1: reportReady ? '' : ' disabled title="' + tr(
          'runtime.project.showimportdone.report_blocked_title', {},
          'ΔE 或收敛状态仍有缺项，暂不生成最终报告',
          'ΔE or convergence evidence is incomplete; the final report cannot be generated yet') + '"',
      }, `<button class="btn primary" type="button" data-next="report"{value1}>查看或立即生成报告</button>`, '<button class="btn primary" type="button" data-next="report"{value1}>View or generate the report now</button>')) + '</div>';
    const deltaButton = box.querySelector('[data-next="delta"]');
    if (deltaButton) deltaButton.addEventListener('click', delta);
    const submitCreated = box.querySelector('[data-next="submit-created"]');
    if (submitCreated && openCreatedJobs) submitCreated.addEventListener('click', openCreatedJobs);
    const lis = box.querySelector('[data-next="lis"]');
    if (lis) lis.addEventListener('click', () => startLiS(projectIdFrom(result)));
    const reportButton = box.querySelector('[data-next="report"]');
    if (reportReady && reportButton) reportButton.addEventListener('click', report);
  }

  function selectReadyImports() {
    State.importRows.forEach(row => { row.selected = importStatus(row) === 'ready' && row.role !== 'ignore'; });
    renderImport();
  }

  function clearImportSelection() {
    State.importRows.forEach(row => { row.selected = false; });
    renderImport();
  }

  function applyImportTask() {
    const task = val('pj-import-batch-task');
    if (!task) { VCS.toast(tr("runtime.project.applyimporttask.text_2cc51c3cc1", {}, '请先选择要设置的任务类型', 'Select the task type to assign first'), 'fail'); return; }
    const rows = selectedImportRows();
    if (!rows.length) { VCS.toast(tr("runtime.project.applyimporttask.text_58e4cc61ce", {}, '请先勾选要修改的结果', 'Select the results to modify first'), 'fail'); return; }
    rows.forEach(row => { row.taskType = task; });
    renderImport();
  }

  function confirmSelectedImports() {
    const selected = selectedImportRows();
    const eligible = selected.filter(row => row.confirmationEligible);
    if (!selected.length) { VCS.toast(tr("runtime.project.confirmselectedimports.text_61252d3918", {}, '请先勾选要确认的结果', 'Select the results to confirm first'), 'fail'); return; }
    if (!eligible.length) {
      VCS.toast(tr("runtime.project.confirmselectedimports.text_94ddcbc145", {}, '所选结果没有可人工确认项；请按行内原因补齐文件或重新计算', 'The selected results have no manually confirmable items. Add the missing files or recalculate as instructed on each row.'), 'fail');
      return;
    }
    eligible.forEach(row => { row.manualConfirm = true; });
    renderImport();
    const refused = selected.length - eligible.length;
    VCS.toast(tr("runtime.project.confirmselectedimports.text_eff62a2aa4", {
      value1: eligible.length,
      value2: refused ? tr('runtime.project.confirmselectedimports.refused', { count: refused },
        '{count} 项硬性问题未被跳过', '{count} blocking issues were not bypassed')
        : tr('runtime.project.confirmselectedimports.revalidate', {},
          '提交时仍会重新复核', 'The evidence will be revalidated during submission'),
    }, `已标记 {value1} 项；{value2}`, 'Marked {value1} items; {value2}'));
  }

  async function openImport(sourceRoot) {
    // 公开入口可从仪表盘直接调用；旧调用方曾传过模式名 "results"，它不是路径。
    if (sourceRoot === 'results') sourceRoot = '';
    setExplicitWorkflow('import');
    if (typeof VCS.navigate === 'function') {
      await VCS.navigate('project', { source: 'result-import' });
    }
    const ana = $('analysis-type');
    if (ana && ana.value !== 'adsorption') {
      ana.value = 'adsorption';
      ana.dispatchEvent(new Event('change', { bubbles: true }));
    }
    setAccordionOpen('pj-create-card', false);
    setAccordionOpen('pj-import-card', true);
    scrollToCard('pj-import-card');
    if (sourceRoot) {
      setVal('pj-import-source', sourceRoot);
      if (!val('pj-import-name')) setVal('pj-import-name', pathBase(sourceRoot));
      if (!val('pj-import-root')) setVal('pj-import-root', pathParent(sourceRoot));
      await scanImport();
    } else {
      const input = $('pj-import-source');
      if (input) input.focus();
    }
  }

  // ── 新建项目:文件/目录选择 ────────────────────────────────────────────────
  async function pickInto(id, kind) {
    const r = await VCS.call('pick_file', kind);
    if (r && r.error) { VCS.log(tr("runtime.project.pickinto.text_1e0a7f688a", {}, '选择文件失败:', 'Failed to select file:') + r.error, 'failc'); return ''; }
    if (r && r.path) { setVal(id, r.path); return r.path; }
    return '';
  }
  async function pickDirInto(id) {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log(tr("runtime.project.pickdirinto.text_d802c7d8d3", {}, '选择目录失败:', 'Failed to select directory:') + r.error, 'failc'); return; }
    if (r && r.path) setVal(id, r.path);
  }

  // ── 一站式 Li-S 项目:参考物种、构型物种映射、提交资源 ────────────────────
  function referenceSpecies(project) {
    if (!project) return [];
    const raw = project.reference_species || project.species_refs || [];
    let values;
    if (Array.isArray(raw)) {
      values = raw.map(x => typeof x === 'object' && x ? (x.species || x.name || x.label) : x);
    } else if (raw && typeof raw === 'object') {
      values = Object.keys(raw);
    } else {
      values = String(raw || '').split(/[,，;；\s]+/);
    }
    return Array.from(new Set(values.map(x => String(x || '').trim()).filter(Boolean)));
  }

  function selectedReferenceProject() {
    const id = val('lis-reference');
    return State.projects.find(p => projectId(p) === id) || null;
  }

  function selectedReferenceSpecies() {
    return referenceSpecies(selectedReferenceProject());
  }

  function exactReferenceSpecies(value) {
    const wanted = String(value || '').trim().toLowerCase();
    return selectedReferenceSpecies().find(x => x.toLowerCase() === wanted) || '';
  }

  function guessSpeciesEvidence(path, suggested) {
    const refs = selectedReferenceSpecies();
    const exact = refs.find(x => x.toLowerCase() === String(suggested || '').trim().toLowerCase());
    if (exact) return { value: exact, source: 'backend_suggestion', ambiguous: false, warnings: [] };
    const base = pathBase(path);
    const parent = pathBase(pathParent(path));
    const names = [base, parent].filter(Boolean).map(value => value.toLowerCase());
    const matches = refs.filter(ref => {
      const token = ref.toLowerCase().replace(/[^a-z0-9]/g, '');
      if (!token) return false;
      const boundary = new RegExp(`(^|[^a-z0-9])${token}(?=$|[^a-z0-9])`, 'i');
      if (names.some(name => boundary.test(name))) return true;
      return names.some(name => name.replace(/[^a-z0-9]/g, '').includes(token));
    });
    if (matches.length === 1) {
      return { value: matches[0], source: 'path_name', ambiguous: false, warnings: [] };
    }
    const fallback = String(suggested || '').trim();
    return {
      value: fallback, source: fallback ? 'backend_suggestion' : 'unresolved',
      ambiguous: matches.length > 1,
      warnings: matches.length > 1
        ? [tr("runtime.project.guessspeciesevidence.text_2d65f863ba", { value1: (matches.join('、')) }, `文件夹名同时匹配 {value1}，没有自动选择`, 'The folder name matches {value1}; no value was selected automatically')] : [],
    };
  }

  function guessSpecies(path, suggested) {
    return guessSpeciesEvidence(path, suggested).value;
  }

  function lisConfigItems() {
    return State.configs.map(path => {
      const meta = State.configSpeciesMeta[path] || {};
      return {
        path, species: String(State.configSpecies[path] || '').trim(),
        species_source: String(meta.source || ''),
        species_confidence: String(meta.confidence || ''),
        species_confirmed: meta.confirmed === true,
        incar_path: String(meta.incarPath || ''),
        incar_sha256: String(meta.incarSha256 || ''),
        incar_status: String(meta.incarStatus || (meta.incarPath ? 'ready' : 'missing')),
        incar_issues: textList(meta.incarIssues),
        quartet: meta.quartet && typeof meta.quartet === 'object' ? meta.quartet : null,
        input_mode: String(meta.inputMode || ''),
        quartet_status: String(meta.quartetStatus || ''),
      };
    });
  }

  function lisContentFingerprint() {
    return JSON.stringify({
      name: val('pj-name'), root: val('pj-root'), slab: val('pj-slab'),
      fallbackIncar: val('pj-incar'), cleanIncar: State.cleanIncar,
      reference: val('lis-reference'), configs: lisConfigItems(),
    });
  }

  function reusablePreparedLis() {
    return State.preparedLis && State.preparedLis.fingerprint === lisContentFingerprint()
      ? State.preparedLis : null;
  }

  function methodComparabilityStatus(check) {
    const value = String(check && (check.comparability_status || check.status ||
      check.state || check.result) || '').toLowerCase();
    if (['incompatible', 'analysis_blocked', 'not_comparable'].includes(value)) return 'incompatible';
    if (['unverified', 'unknown', 'needs_confirmation', 'review'].includes(value)) return 'unverified';
    if (['compatible', 'verified', 'pass', 'passed', 'ok', 'advisory',
      'compatible_with_advisory'].includes(value)) return 'verified';
    return check ? 'unverified' : '';
  }

  function methodExecutionStatus(check) {
    const value = String(check && check.execution_status || '').toLowerCase();
    return value === 'blocked' ? 'blocked' : 'ready';
  }

  function renderMethodSection(id, title, values, formatter) {
    const section = $(id);
    if (!section) return;
    const rows = (values || []).map(value => formatter ? formatter(value) : String(value || ''))
      .filter(Boolean);
    section.hidden = rows.length === 0;
    section.innerHTML = rows.length
      ? `<b>${VCS.esc(title)}</b><ul>${rows.map(value => `<li>${VCS.esc(value)}</li>`).join('')}</ul>`
      : '';
  }

  function methodRepairText(value) {
    if (!value || typeof value !== 'object') return String(value || '');
    const target = [value.member, value.key].filter(Boolean).join(' / ');
    const change = value.old != null || value.new != null
      ? `${value.old == null ? tr('runtime.project.method.missing_value', {}, '缺失', 'Missing') : value.old} → ` +
        `${value.new == null ? tr('runtime.project.method.filled_value', {}, '已补齐', 'Filled in') : value.new}`
      : '';
    const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
    const reason = textList(value.reason || value.message || value.description)
      .join(english ? '; ' : '；');
    return [target, change, reason].filter(Boolean).join(english ? ': ' : '：');
  }

  function renderMethodCheck(check, legacyNeedsReview) {
    const box = $('lis-method-check');
    if (!box) return;
    if (!check && legacyNeedsReview) {
      check = {
        execution_status: 'ready', comparability_status: 'unverified',
        warnings: [tr("runtime.project.rendermethodcheck.text_0a1cb6e7f0", {}, '方法可比性证据尚未完整；作业可提交，自动 ΔE 与最终报告暂停', 'Method-comparability evidence is incomplete. Jobs may be submitted, but automatic ΔE and the final report are paused.')],
      };
    }
    const fingerprint = lisContentFingerprint();
    State.methodCheck = check || null;
    State.methodCheckFingerprint = check ? fingerprint : '';
    State.repairPlan = check && check.repair_plan || null;
    const executionStatus = methodExecutionStatus(check);
    const comparabilityStatus = methodComparabilityStatus(check);
    const issues = [...new Set(textList(check && check.issues))];
    const notes = [...new Set(textList(check && [check.notes, check.advisories]))];
    const warnings = [...new Set(textList(check && check.warnings))];
    const repairs = check && Array.isArray(check.repairs) ? check.repairs : [];
    const hasDetails = issues.length || notes.length || warnings.length || repairs.length ||
      !!(check && check.repair_plan);
    if (!check || (comparabilityStatus === 'verified' && executionStatus === 'ready' && !hasDetails)) {
      box.hidden = true;
      box.className = 'lis-method-check';
      renderRepairPlan(null);
      updateLisReadiness();
      return;
    }
    box.hidden = false;
    box.className = 'lis-method-check ' + (executionStatus === 'blocked' ? 'bad' :
      comparabilityStatus === 'verified' ? 'ok' : 'analysis');
    const title = $('lis-method-title');
    if (title) title.textContent = executionStatus === 'blocked'
      ? tr("runtime.project.rendermethodcheck.text_4dc7ff4adc", {}, '输入执行检查未通过', 'Input execution validation failed')
      : comparabilityStatus === 'incompatible'
        ? tr("runtime.project.rendermethodcheck.text_50e187061a", {}, '作业可提交；方法不一致会暂停自动 ΔE 与最终报告', 'Jobs may be submitted; method differences will pause automatic ΔE and the final report')
        : comparabilityStatus === 'unverified'
          ? tr("runtime.project.rendermethodcheck.text_6ffab5355b", {}, '作业可提交；方法证据待核验', 'Jobs may be submitted; method evidence still needs verification')
          : tr("runtime.project.rendermethodcheck.text_b551fe38ef", {}, '作业可提交；方法可比性已核验', 'Jobs may be submitted; method comparability was verified');
    const outcome = $('lis-method-outcome');
    if (outcome) {
      const submitText = executionStatus === 'blocked'
        ? tr("runtime.project.rendermethodcheck.text_39d54c7d0e", {}, '作业生成 / 提交：已阻止', 'Job generation / submission: blocked')
        : tr("runtime.project.rendermethodcheck.text_9521dcd8c6", {}, '作业生成 / 提交：可继续', 'Job generation / submission: may continue');
      const analysisText = executionStatus === 'blocked'
        ? tr("runtime.project.rendermethodcheck.text_ca84d77346", {}, '自动 ΔE / 最终报告：尚未运行，请先修正本目录输入', 'Automatic ΔE / final report: not run; correct the input in this directory first')
        : comparabilityStatus === 'verified'
          ? tr("runtime.project.rendermethodcheck.text_178223f9e3", {}, '自动 ΔE / 最终报告：可继续', 'Automatic ΔE / final report: may continue')
          : comparabilityStatus === 'incompatible'
            ? tr("runtime.project.rendermethodcheck.text_eeed7c7083", {}, '自动 ΔE / 最终报告：已暂停，需重算或修正方法差异', 'Automatic ΔE / final report: paused; recalculate or resolve the method differences')
            : tr("runtime.project.rendermethodcheck.text_fdfa2159ac", {}, '自动 ΔE / 最终报告：待补齐证据后继续', 'Automatic ΔE / final report: continues after the missing evidence is provided');
      outcome.innerHTML = `<span>${VCS.esc(submitText)}</span><span>${VCS.esc(analysisText)}</span>`;
    }
    renderMethodSection('lis-method-issues', tr("runtime.project.rendermethodcheck.text_f60b1dcbb9", {}, '影响 ΔE / 报告的问题（不阻止作业提交）', 'Issues affecting ΔE / reports (do not block job submission)'), issues);
    renderMethodSection('lis-method-notes', tr("runtime.project.rendermethodcheck.text_0ff74ab0d7", {}, '体系说明（包括 ISPIN）', 'System notes (including ISPIN)'), notes);
    renderMethodSection('lis-method-warnings', tr("runtime.project.rendermethodcheck.text_dbec2358a1", {}, '需留意', 'Needs attention'), warnings);
    renderMethodSection('lis-method-repairs', tr("runtime.project.rendermethodcheck.text_9cc61621ea", {}, '已在受管副本安全修复（源文件未改）', 'Safely repaired in the managed copy (source files unchanged)'), repairs,
      methodRepairText);
    renderRepairPlan(check && check.repair_plan);
    updateLisReadiness();
  }

  function renderRepairPlan(plan) {
    const box = $('lis-repair-actions');
    if (!box) return;
    const actions = plan && Array.isArray(plan.actions) ? plan.actions : [];
    const suggestions = plan && Array.isArray(plan.suggestions) ? plan.suggestions : [];
    const rows = [...actions, ...suggestions];
    if (!rows.length) { box.hidden = true; box.innerHTML = ''; return; }
    const safeActions = actions.filter(action => String(action && action.risk || '').toLowerCase() === 'low');
    const reviewActions = [
      ...actions.filter(action => String(action && action.risk || '').toLowerCase() !== 'low'),
      ...suggestions,
    ];
    const current = State.repairDecision && State.repairDecision.plan_id === plan.plan_id
      ? State.repairDecision.mode : '';
    box.hidden = false;
    box.innerHTML = tr("runtime.project.renderrepairplan.text_f81016ddfc", {}, '<div class="lis-repair-title"><b>智能修复预览</b>', '<div class="lis-repair-title"><b>Smart-repair preview</b>') +
      tr("runtime.project.renderrepairplan.text_da9ad50919", {}, '<span>只有低风险项可自动写入受管副本；源目录四件套保持原字节不变。', '<span>Only low-risk items can be written automatically to the managed copy; the source four-file input sets remain byte-for-byte unchanged.') +
      (reviewActions.length ? tr("runtime.project.renderrepairplan.text_bcd03a1450", {}, ' MAGMOM、ISPIN 等科学选择只给建议，不自动改。', ' Scientific choices such as MAGMOM and ISPIN are recommendations only and will not be changed automatically.') : '') + '</span></div>' +
      tr("runtime.project.renderrepairplan.text_5ae9b87fe5", {}, '<table><thead><tr><th>成员</th><th>参数</th><th>原值</th><th>建议值</th><th>处理</th><th>原因</th></tr></thead><tbody>', '<table><thead><tr><th>Member</th><th>Parameter</th><th>Original value</th><th>Suggested value</th><th>Action</th><th>Reason</th></tr></thead><tbody>') +
      rows.map(action => `<tr><td>${VCS.esc(action.member || '')}</td>` +
        `<td>${VCS.esc(action.key || '')}</td><td>${VCS.esc(action.old == null
          ? tr('runtime.project.method.missing_value', {}, '缺失', 'Missing') : action.old)}</td>` +
        `<td>${VCS.esc(action.new)}</td><td>${String(action.risk || '').toLowerCase() === 'low'
          ? tr('runtime.project.repair.low_risk_copy', {},
            '低风险，可修复副本', 'Low risk; managed copy can be repaired')
          : tr('runtime.project.repair.recommendation_only', {},
            '仅建议，不自动', 'Recommendation only; not automatic')}</td>` +
        `<td>${VCS.esc(action.reason || '')}</td></tr>`).join('') +
      '</tbody></table><div class="actions">' +
      (safeActions.length
        ? tr("runtime.project.renderrepairplan.text_af16ce0ec2", { value1: (current === 'apply' ? ' disabled' : ''), value2: (safeActions.length) }, `<button class="btn primary" type="button" data-lis-repair="apply"{value1}>仅修复 {value2} 个低风险副本项并继续</button>`, '<button class="btn primary" type="button" data-lis-repair="apply"{value1}>Repair only {value2} low-risk items in managed copies and continue</button>') +
          tr("runtime.project.renderrepairplan.text_c98f4c2e3b", { value1: (current === 'keep' ? ' disabled' : '') }, `<button class="btn" type="button" data-lis-repair="keep"{value1}>保持各目录原样继续</button>`, '<button class="btn" type="button" data-lis-repair="keep"{value1}>Keep each directory unchanged and continue</button>')
        : tr("runtime.project.renderrepairplan.text_14ac7e8ca5", {}, '<span class="sub">这些是科学设置建议，不会自动修改，也不阻止提交。</span>', '<span class="sub">These are scientific-setting recommendations. They will not be applied automatically and do not block submission.</span>')) +
      '</div>';
    box.querySelectorAll('[data-lis-repair]').forEach(button => {
      button.addEventListener('click', () => {
        State.repairDecision = { plan_id: plan.plan_id, mode: button.dataset.lisRepair };
        VCS.log(button.dataset.lisRepair === 'apply'
          ? tr("runtime.project.renderrepairplan.text_2152d69e6b", {}, '已确认：仅低风险项修复到受管项目副本；MAGMOM/ISPIN 只建议，源目录不改', 'Confirmed: only low-risk items will be repaired in managed project copies; MAGMOM/ISPIN remain recommendations and source directories are unchanged')
          : tr("runtime.project.renderrepairplan.text_4547023915", {}, '已确认：保持每个目录的原始四件套提交；最终 ΔE 仍受方法门禁约束', "Confirmed: submit each directory's original four-file input set unchanged; final ΔE remains subject to the method gate"), 'warnc');
        const resume = $(State.repairResume === 'create' ? 'pj-create' : 'pj-submit-all');
        if (resume && !resume.disabled) resume.click();
      });
    });
  }

  function invalidatePreparedLis() {
    const prepared = State.preparedLis;
    const fingerprint = lisContentFingerprint();
    if (State.methodCheck && State.methodCheckFingerprint !== fingerprint) {
      State.methodCheck = null;
      State.methodCheckFingerprint = '';
      State.repairPlan = null;
      State.repairDecision = null;
      const methodBox = $('lis-method-check');
      if (methodBox) methodBox.hidden = true;
    }
    if (!prepared || prepared.fingerprint === fingerprint) return;
    State.preparedLis = null;
    State.workflowSubmitted = false;
    State.preparedConflictHint = { oldName: prepared.name, projectId: prepared.projectId };
    State.methodCheck = null;
    State.methodCheckFingerprint = '';
    State.repairPlan = null;
    State.repairDecision = null;
    const methodBox = $('lis-method-check');
    if (methodBox) methodBox.hidden = true;
    showLisFailure(tr("runtime.project.invalidatepreparedlis.text_f41190806b", {}, '输入已改变，不能复用刚才生成的项目', 'The input changed, so the project generated moments ago cannot be reused'),
      tr("runtime.project.invalidatepreparedlis.text_c25ebd7c98", { value1: prepared.name }, `原项目“{value1}”仍安全保留。如果要按新输入再生成，请把项目名改成新名称；不要覆盖旧项目。`, 'The original project “{value1}” remains safely stored. To generate again from the new input, use a new project name; do not overwrite the old project.'),
      'prepare');
  }

  function showConfigScanStatus(kind, message) {
    const box = $('lis-config-scan-status');
    if (!box) return;
    box.hidden = !message;
    box.className = 'lis-scan-status' + (kind ? ' ' + kind : '');
    box.textContent = message || '';
  }

  function showBundleStatus(kind, message) {
    const box = $('lis-bundle-status');
    if (!box) return;
    box.hidden = !message;
    box.className = 'lis-scan-status' + (kind ? ' ' + kind : '');
    box.textContent = message || '';
  }

  function renderCleanIncarStatus() {
    const box = $('lis-clean-incar-status');
    if (!box) return;
    const local = State.cleanIncar || {};
    const quartet = quartetPresentation(local, val('pj-incar'));
    if (quartet.hasEvidence) {
      box.className = `sub lis-quartet ${quartet.blocked ? 'blocked' : quartet.mode === 'copy' ? 'copy' : 'generate'}`;
      box.innerHTML = quartetHtml(quartet);
    } else if (local.status === 'ready' && local.path) {
      box.className = 'sub';
      box.textContent = tr("runtime.project.rendercleanincarstatus.text_7ad53e0964", { value1: (local.path) }, `将使用同目录 INCAR：{value1}`, 'Using INCAR from the same directory: {value1}');
    } else if (local.status && local.status !== 'missing') {
      box.className = 'sub';
      box.textContent = tr("runtime.project.rendercleanincarstatus.text_b0851d92d7", { value1: (textList(local.issues).join('；') || local.status) }, `clean slab INCAR 不可用：{value1}`, 'The clean-slab INCAR is unavailable: {value1}');
    } else if (val('pj-incar')) {
      box.className = 'sub';
      box.textContent = tr("runtime.project.rendercleanincarstatus.text_cedb15d9e2", { value1: (val('pj-incar')) }, `同目录无 INCAR，将使用你显式选择的备用文件：{value1}`, 'No INCAR exists in the same directory; using the fallback you explicitly selected: {value1}');
    } else {
      box.className = 'sub';
      box.textContent = tr("runtime.project.rendercleanincarstatus.text_da55322e17", {}, 'clean slab 同目录尚未找到 INCAR。', 'No INCAR has been found in the clean-slab directory yet.');
    }
  }

  const QUARTET_FILES = ['POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'];

  function normaliseQuartet(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const quartet = source.quartet && typeof source.quartet === 'object' ? source.quartet : {};
    const files = quartet.files && typeof quartet.files === 'object' ? quartet.files
      : source.files && typeof source.files === 'object' ? source.files : {};
    const mode = String(source.inputMode || source.input_mode || quartet.input_mode ||
      quartet.mode || source.mode || '').toLowerCase();
    const status = String(source.quartetStatus || source.quartet_status ||
      quartet.quartet_status || quartet.status || '').toLowerCase();
    const missing = textList(quartet.missing || source.missing).map(name => String(name).toUpperCase());
    const issues = textList(quartet.issues || source.quartet_issues);
    const hasEvidence = !!(source.quartet || source.inputMode || source.input_mode ||
      source.quartetStatus || source.quartet_status || Object.keys(files).length || missing.length);
    return { files, mode, status, missing, issues, hasEvidence };
  }

  function quartetBlocked(raw) {
    const quartet = normaliseQuartet(raw);
    return quartet.mode === 'blocked' || ['blocked', 'invalid'].includes(quartet.status);
  }

  function quartetPresentation(raw, fallbackIncar) {
    const quartet = normaliseQuartet(raw);
    const fileRows = QUARTET_FILES.flatMap(name => {
      const record = quartet.files[name] || quartet.files[name.toLowerCase()];
      const path = typeof record === 'string' ? record : record && record.path;
      return path ? [`${name}：${path}`] : [];
    });
    const present = QUARTET_FILES.filter(name => {
      const record = quartet.files[name] || quartet.files[name.toLowerCase()];
      return !!(typeof record === 'string' ? record : record && record.path);
    });
    const missing = quartet.missing.length ? quartet.missing
      : QUARTET_FILES.filter(name => !present.includes(name));
    const copyMode = ['copy', 'copied_quartet', 'verbatim', 'verbatim_quartet'].includes(quartet.mode);
    const generateMode = ['generate', 'generated', 'smart_generate', 'partial'].includes(quartet.mode) ||
      (!copyMode && quartet.status === 'partial');
    const blocked = quartetBlocked(raw);
    let summary = '';
    if (blocked) {
      summary = tr("runtime.project.quartetpresentation.text_21b470e49f", {
        value1: quartet.issues.join('; ') || tr(
          'runtime.project.quartetpresentation.invalid_or_conflicting', {},
          '输入文件无效或冲突', 'Input files are invalid or conflicting'),
      }, `四件套不可提交：{value1}`, 'The four-file input set cannot be submitted: {value1}');
    } else if (copyMode) {
      summary = tr("runtime.project.quartetpresentation.text_9450d05165", {}, '完整四件套原样绑定（源文件不改）', 'Bind the complete four-file input set unchanged (source files unchanged)');
    } else if (generateMode || quartet.hasEvidence) {
      const generated = missing.length ? missing.join('、') : tr("runtime.project.quartetpresentation.text_54e953f1bb", {}, '无', 'None');
      summary = tr("runtime.project.quartetpresentation.text_edd4d2ee93", { value1: (generated) }, `不完整输入（缺 {value1}）：将以本目录 POSCAR+INCAR 生成受管四件套；源目录不改`, "Incomplete input (missing {value1}): generate a managed four-file input set from this directory's POSCAR+INCAR; source directory unchanged");
    }
    if (!present.includes('INCAR') && fallbackIncar && !blocked) {
      fileRows.push(tr("runtime.project.quartetpresentation.text_85ba3dc20a", { value1: (fallbackIncar) }, `INCAR 备用：{value1}`, 'Fallback INCAR: {value1}'));
    }
    return { ...quartet, blocked, mode: copyMode ? 'copy' : blocked ? 'blocked' : 'generate',
      summary, details: fileRows };
  }

  function quartetHtml(quartet) {
    return `<span class="lis-quartet-summary">${VCS.esc(quartet.summary)}</span>` +
      quartet.details.map(line => `<span title="${VCS.esc(line)}">${VCS.esc(line)}</span>`).join('');
  }

  function normaliseLocalPath(value) {
    const normalised = String(value || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
    return /^[A-Za-z]:(?:\/|$)/.test(normalised) ? normalised.toLowerCase() : normalised;
  }

  function sameLocalPath(left, right) {
    return normaliseLocalPath(left) === normaliseLocalPath(right);
  }

  function removeConfigPath(path) {
    const removed = State.configs.filter(item => sameLocalPath(item, path));
    if (!removed.length) return false;
    State.configs = State.configs.filter(item => !sameLocalPath(item, path));
    removed.forEach(item => {
      delete State.configSpecies[item];
      delete State.configSpeciesMeta[item];
    });
    return true;
  }

  function appendConfig(path, species, evidence) {
    const clean = String(path || '').trim();
    if (!clean) return false;
    if (State.configs.some(item => sameLocalPath(item, clean))) return false;
    const raw = evidence && typeof evidence === 'object' ? evidence : {};
    const assignment = raw.assignment && typeof raw.assignment === 'object' ? raw.assignment : {};
    const guessed = guessSpeciesEvidence(clean, species);
    const confidence = String(raw.species_confidence || assignment.confidence ||
      (guessed.value ? 'hint' : 'unknown'));
    const source = String(raw.species_source || assignment.source || guessed.source);
    const incarPath = String(raw.incar_path || raw.incar || '').trim();
    const incarSha256 = String(raw.incar_sha256 || '').trim();
    const incarStatus = String(raw.incar_status || (incarPath ? 'ready' : 'missing'));
    const quartet = raw.quartet && typeof raw.quartet === 'object' ? raw.quartet : null;
    const inputMode = String(raw.input_mode || (quartet && quartet.input_mode) ||
      (quartet && quartet.mode) || raw.mode || '');
    const quartetStatus = String(raw.quartet_status || (quartet && quartet.quartet_status) ||
      (quartet && quartet.status) || '');
    State.configs.push(clean);
    State.configSpecies[clean] = guessed.value;
    State.configSpeciesMeta[clean] = {
      source, confidence,
      confirmed: (raw.species_confirmed === true || assignment.confirmed === true) && confidence === 'exact',
      ambiguous: guessed.ambiguous,
      warnings: [...textList(raw.warnings || assignment.warnings), ...guessed.warnings],
      incarPath, incarSha256, incarStatus,
      incarSource: String(raw.incar_source || ''),
      incarIssues: textList(raw.incar_issues),
      quartet, inputMode, quartetStatus,
    };
    invalidatePreparedLis();
    return true;
  }

  async function addLisInputDirectory() {
    if (lisInputsLocked()) {
      VCS.toast(tr("runtime.project.addlisinputdirectory.text_043e0e5aab", {}, '当前输入仍在扫描、生成或提交，请完成后再导入新组', 'The current input is still being scanned, generated, or submitted. Finish that operation before importing a new group.'), 'fail');
      return;
    }
    const picked = await VCS.call('pick_dir');
    if (picked && picked.error) {
      VCS.log(tr("runtime.project.addlisinputdirectory.text_9d39c3ca1c", {}, '选择本次计算文件夹失败:', "Failed to select this calculation's folder:") + picked.error, 'failc');
      showBundleStatus('bad', tr("runtime.project.addlisinputdirectory.text_9cdecab650", {}, '没有选中文件夹。请选择包含 clean slab 与 adsorption 各成员输入的上层目录。', 'No folder was selected. Select the parent folder containing the clean-slab and adsorption-member inputs.'));
      return;
    }
    if (!picked || !picked.path) return;
    const generation = ++State.inputGeneration;
    State.inputScanBusy = true;
    // 这是“替换为新数据组”而非追加。先清空再扫描，使旧组在扫描期间也绝不
    // 可能通过 gate 被提交；失败时保持空状态，要求用户明确重试。
    State.configs = [];
    State.configSpecies = Object.create(null);
    State.configSpeciesMeta = Object.create(null);
    State.cleanIncar = {
      path: '', sha256: '', status: 'missing', source: '', issues: [],
      quartet: null, inputMode: '', quartetStatus: '',
    };
    setVal('pj-slab', '');
    setVal('pj-incar', '');
    invalidatePreparedLis();
    const previousResult = $('lis-submit-result');
    if (previousResult) previousResult.hidden = true;
    renderConfigs();
    syncLisInputLocks();
    updateLisReadiness();
    const button = $('lis-input-dir');
    if (button) { button.disabled = true; button.textContent = tr("runtime.project.addlisinputdirectory.text_7abf81f7bf", {}, '正在识别整套输入…', 'Identifying the complete input set…'); }
    showBundleStatus('', tr("runtime.project.addlisinputdirectory.text_b9abe2844a", {}, '正在只读扫描 clean slab、adsorption 结构及各自 POSCAR/INCAR/KPOINTS/POTCAR…', 'Read-only scan of the clean slab, adsorption structures, and their POSCAR/INCAR/KPOINTS/POTCAR files…'));
    try {
      const result = await VCS.call(
        'proj_scan_lis_inputs', picked.path, selectedReferenceSpecies());
      if (generation !== State.inputGeneration) return;
      if (!result || result.ok === false || result.error) {
        const message = (result && result.error) || tr("runtime.project.addlisinputdirectory.text_bd5e21c357", {}, '未知错误', 'Unknown error');
        VCS.log(tr("runtime.project.addlisinputdirectory.text_bb440f7745", {}, '识别本次计算文件夹失败:', "Failed to identify this calculation's folder:") + message, 'failc');
        showBundleStatus('bad', tr("runtime.project.addlisinputdirectory.text_7599901047", {}, '识别失败：', 'Identification failed: ') + message + tr("runtime.project.addlisinputdirectory.text_d62e7a9821", {}, '。原始文件没有被修改，请修正目录后重试。', '. Original files were not modified; correct the directory and try again.'));
        return;
      }
      // 顶层 legacy INCAR 只代表用户所选根目录中的显式备用；它只补成员目录
      // 真正缺失的 INCAR，绝不覆盖同目录文件，也不能绕过冲突/无效状态。
      const rootFallback = String(result.incar || '').trim();
      setVal('pj-incar', rootFallback);
      // 无论后端是否成功识别 clean，都保持替换语义；绝不能沿用上一组 slab。
      setVal('pj-slab', String(result.clean_slab || ''));
      if (result.clean_slab) {
        removeConfigPath(result.clean_slab);
        const cleanItem = [...(result.structures || []), ...(result.clean_candidates || [])]
          .find(item => item && sameLocalPath(item.path, result.clean_slab)) || {};
        const cleanQuartet = result.clean_quartet && typeof result.clean_quartet === 'object'
          ? result.clean_quartet : cleanItem.quartet || null;
        State.cleanIncar = {
          path: String(result.clean_incar || ''),
          sha256: String(result.clean_incar_sha256 || ''),
          status: String(result.clean_incar_status || (result.clean_incar ? 'ready' : 'missing')),
          source: String(result.clean_incar_source || ''),
          issues: textList(result.clean_incar_issues),
          quartet: cleanQuartet,
          inputMode: String(result.clean_input_mode || cleanItem.input_mode ||
            (cleanQuartet && cleanQuartet.input_mode) ||
            (cleanQuartet && cleanQuartet.mode) || cleanItem.mode || ''),
          quartetStatus: String(result.clean_quartet_status || cleanItem.quartet_status ||
            (cleanQuartet && cleanQuartet.quartet_status) ||
            (cleanQuartet && cleanQuartet.status) || ''),
        };
      }
      if (!val('pj-name')) setVal('pj-name', pathBase(picked.path) + '_adsorption');
      if (!val('pj-root')) setVal('pj-root', pathParent(picked.path));
      let added = 0;
      (result.configs || []).forEach(item => {
        const path = typeof item === 'string' ? item : item && item.path;
        const species = typeof item === 'object' && item
          ? (item.species || item.suggested_species || '') : '';
        if (path && !sameLocalPath(path, result.clean_slab) && appendConfig(path, species, item)) added++;
      });
      invalidatePreparedLis();
      renderConfigs();
      const warnings = textList(result.warnings);
      const readyIncars = (result.configs || []).filter(item => item &&
        String(item.incar_status || (item.incar_path ? 'ready' : '')) === 'ready').length;
      const quartetMembers = [State.cleanIncar, ...State.configs.map(path =>
        State.configSpeciesMeta[path] || {})].filter(item => normaliseQuartet(item).hasEvidence);
      const copiedQuartets = quartetMembers.filter(item =>
        quartetPresentation(item, rootFallback).mode === 'copy').length;
      const generatedQuartets = quartetMembers.filter(item =>
        quartetPresentation(item, rootFallback).mode === 'generate').length;
      const summary = tr("runtime.project.addlisinputdirectory.text_fb7e5272cf", { value1: (result.clean_slab ? 'clean slab，' : ''), value2: (added) }, `已识别 {value1}新增 {value2} 个 adsorption 结构，`, 'Identified {value1}and added {value2} adsorption structures; ') +
        tr("runtime.project.addlisinputdirectory.text_4aac6169a3", { value1: (readyIncars + (State.cleanIncar.status === 'ready' ? 1 : 0)) }, `{value1} 个成员已绑定本目录 INCAR。`, '{value1} members are bound to INCAR files from their own directories.') +
        (quartetMembers.length
          ? tr("runtime.project.addlisinputdirectory.text_9507d5b141", { value1: (copiedQuartets), value2: (generatedQuartets) }, ` 完整四件套原样绑定 {value1} 组，受管副本智能补齐 {value2} 组。`, ' {value1} complete four-file sets were bound unchanged, and {value2} managed copies were completed safely.') : '') +
        (rootFallback ? tr("runtime.project.addlisinputdirectory.text_e58c6c27a7", { value1: (rootFallback) }, ` 根目录 INCAR 已作为显式备用：{value1}。`, ' The root INCAR is an explicit fallback: {value1}.') : '');
      showBundleStatus(warnings.length ? 'warn' : 'ok', summary +
        (warnings.length ? tr("runtime.project.addlisinputdirectory.text_4b17ccf927", {}, ' 还需确认：', ' Still needs confirmation: ') + warnings.join('；') : tr("runtime.project.addlisinputdirectory.text_7f4ec7381e", {}, ' 请检查物种映射后继续。', ' Check the species mapping before continuing.')));
      warnings.forEach(message => VCS.log(message, 'warnc'));
      VCS.log(tr("runtime.project.addlisinputdirectory.text_229debc051", { value1: (summary) }, `整套输入识别完成：{value1}`, 'Complete input-set identification finished: {value1}'), warnings.length ? 'warnc' : 'okc');
    } finally {
      if (generation === State.inputGeneration) {
        State.inputScanBusy = false;
        syncLisInputLocks();
        updateLisReadiness();
        if (button) {
          button.disabled = false;
          button.textContent = tr("runtime.project.addlisinputdirectory.text_f133aeaae8", {}, '导入本次计算文件夹（推荐）', "Import this calculation's folder (recommended)");
        }
      }
    }
  }

  function lisGate() {
    const ref = selectedReferenceProject();
    const refs = selectedReferenceSpecies();
    const items = lisConfigItems();
    const invalidSpecies = items.filter(item => !refs.some(x => x.toLowerCase() === item.species.toLowerCase()));
    const unconfirmedSpecies = items.filter(item => item.species_confirmed !== true);
    const cores = Number(val('lis-cores'));
    const walltime = val('lis-walltime');
    const validWalltime = /^\d{1,3}:\d{2}:\d{2}$/.test(walltime) &&
      Number(walltime.split(':')[1]) < 60 && Number(walltime.split(':')[2]) < 60;
    const nameConflict = State.preparedConflictHint &&
      val('pj-name') === String(State.preparedConflictHint.oldName || '');
    const methodStatus = methodComparabilityStatus(State.methodCheck);
    const executionStatus = methodExecutionStatus(State.methodCheck);
    const methodBlocked = executionStatus === 'blocked';
    const fallbackIncar = val('pj-incar');
    const cleanIncarReady = State.cleanIncar.status === 'ready' ||
      (State.cleanIncar.status === 'missing' && !!fallbackIncar);
    const cleanQuartetBlocked = quartetBlocked(State.cleanIncar);
    const invalidIncars = items.filter(item =>
      !['ready', 'missing'].includes(item.incar_status));
    const missingIncars = items.filter(item => item.incar_status === 'missing' && !fallbackIncar);
    const blockedQuartets = items.filter(quartetBlocked);
    const step = {
      1: !!ref && refs.length > 0,
      2: !State.inputScanBusy && !!val('pj-slab') && cleanIncarReady && !cleanQuartetBlocked &&
        items.length > 0 && invalidIncars.length === 0 && missingIncars.length === 0 &&
        blockedQuartets.length === 0 &&
        invalidSpecies.length === 0 && unconfirmedSpecies.length === 0,
      3: !!val('pj-name') && !!val('pj-root') && !nameConflict,
      4: !!val('lis-profile') && Number.isInteger(cores) && cores > 0 && validWalltime,
    };
    let issue = '';
    if (State.inputScanBusy) issue = tr("runtime.project.lisgate.text_754ceeef53", {}, '第 2 步：正在扫描并核对新输入组，请稍候', 'Step 2: scanning and validating the new input group; please wait');
    else if (!ref) issue = tr("runtime.project.lisgate.text_763ede7826", {}, '第 1 步：请选择已经导入的 Li-S 参考能项目', 'Step 1: select an imported Li-S reference-energy project');
    else if (!refs.length) issue = tr("runtime.project.lisgate.text_43c7fd4ae8", {}, '第 1 步：所选项目没有可用的 Li-S 物种参考能', 'Step 1: the selected project has no usable Li-S species reference energies');
    else if (!val('pj-slab')) issue = tr("runtime.project.lisgate.text_8973d81b96", {}, '第 2 步：请选择 clean slab POSCAR', 'Step 2: select a clean-slab POSCAR');
    else if (cleanQuartetBlocked) issue = tr("runtime.project.lisgate.text_3d8eed6bfe", {}, '第 2 步：clean slab 的四件套无效或冲突，请按成员明细修正', 'Step 2: the clean-slab four-file input set is invalid or conflicting; correct it using the member details');
    else if (!cleanIncarReady) issue = State.cleanIncar.status === 'missing'
      ? tr("runtime.project.lisgate.text_21372eb8f6", {}, '第 2 步：clean slab 同目录缺少 INCAR；补齐文件或显式选择备用 INCAR', 'Step 2: the clean-slab directory has no INCAR; add the file or explicitly select a fallback INCAR')
      : tr("runtime.project.lisgate.text_c296d5b6d9", { value1: (textList(State.cleanIncar.issues).join('；') || State.cleanIncar.status) }, `第 2 步：clean slab 的 INCAR 不可用（{value1}）`, 'Step 2: the clean-slab INCAR is unavailable ({value1})');
    else if (!items.length) issue = tr("runtime.project.lisgate.text_ea884bd5eb", {}, '第 2 步：至少添加一个 adsorption POSCAR', 'Step 2: add at least one adsorption POSCAR');
    else if (invalidIncars.length) issue =
      tr("runtime.project.lisgate.text_9a62c9ca07", { value1: (invalidIncars.length) }, `第 2 步：有 {value1} 个构型的同目录 INCAR 冲突或无效`, 'Step 2: {value1} configurations have conflicting or invalid INCAR files in their own directories');
    else if (missingIncars.length) issue =
      tr("runtime.project.lisgate.text_b9d6b13b9b", { value1: (missingIncars.length) }, `第 2 步：有 {value1} 个构型缺少同目录 INCAR；补齐文件或显式选择备用 INCAR`, 'Step 2: {value1} configurations are missing an INCAR in their own directory; add the file or explicitly select a fallback INCAR');
    else if (blockedQuartets.length) issue =
      tr("runtime.project.lisgate.text_6be901a714", { value1: (blockedQuartets.length) }, `第 2 步：有 {value1} 个构型的四件套无效或冲突`, 'Step 2: {value1} configurations have invalid or conflicting four-file input sets');
    else if (invalidSpecies.length) issue = tr("runtime.project.lisgate.text_2e79018c32", { value1: (invalidSpecies.length) }, `第 2 步：有 {value1} 个构型的物种不在参考能集合中`, 'Step 2: the species for {value1} configurations are not in the reference-energy set');
    else if (unconfirmedSpecies.length) issue =
      tr("runtime.project.lisgate.text_60f822638a", { value1: (unconfirmedSpecies.length) }, `第 2 步：请确认 {value1} 个构型的智能物种分组`, 'Step 2: confirm the inferred species grouping for {value1} configurations');
    else if (!val('pj-name')) issue = tr("runtime.project.lisgate.text_d4222e9ef6", {}, '第 3 步：填写项目名', 'Step 3: enter a project name');
    else if (!val('pj-root')) issue = tr("runtime.project.lisgate.text_9810ca3fe0", {}, '第 3 步：选择输出根目录', 'Step 3: select an output root');
    else if (nameConflict) issue = tr("runtime.project.lisgate.text_a46e61eebf", { value1: (State.preparedConflictHint.oldName) }, `第 3 步：旧项目“{value1}”已存在；请改用新项目名`, 'Step 3: project “{value1}” already exists; use a new project name');
    else if (!val('lis-profile')) issue = tr("runtime.project.lisgate.text_1af7c2c238", {}, '第 4 步：选择用于提交的服务器', 'Step 4: select the server used for submission');
    else if (!Number.isInteger(cores) || cores < 1) issue = tr("runtime.project.lisgate.text_134ac63735", {}, '第 4 步：核数必须是正整数', 'Step 4: the core count must be a positive integer');
    else if (!validWalltime) issue = tr("runtime.project.lisgate.text_a2f81dc55d", {}, '第 4 步：墙时请填写为 HH:MM:SS（例如 24:00:00）', 'Step 4: enter wall time as HH:MM:SS (for example, 24:00:00)');
    else if (methodBlocked) issue = tr("runtime.project.lisgate.text_592887ca40", {}, '输入执行检查已阻止生成 / 提交；请按方法面板中的输入错误修正', 'Input execution validation blocked generation / submission; resolve the input errors shown on the method panel');
    const memberIncars = {
      clean_slab: {
        path: State.cleanIncar.path || '', sha256: State.cleanIncar.sha256 || '',
        quartet: State.cleanIncar.quartet || null,
        input_mode: State.cleanIncar.inputMode || '',
        quartet_status: State.cleanIncar.quartetStatus || '',
      },
      configs: items.map(item => ({ path: item.path, incar_path: item.incar_path,
        incar_sha256: item.incar_sha256, quartet: item.quartet,
        input_mode: item.input_mode, quartet_status: item.quartet_status })),
    };
    return { ok: Object.values(step).every(Boolean) && !methodBlocked, issue, step, ref, refs, items,
      cores, walltime, methodConfirmation: null, methodBlocked, methodStatus, executionStatus,
      memberIncars,
      repairRequest: State.repairDecision || null };
  }

  function updateLisReadiness(requestedStep) {
    renderCleanIncarStatus();
    const gate = lisGate();
    const firstIncomplete = [1, 2, 3, 4].find(step => !gate.step[step]) || 4;
    const activeStep = requestedStep && requestedStep <= firstIncomplete ? requestedStep : firstIncomplete;
    document.querySelectorAll('#pj-create-card .lis-step').forEach(el => {
      const step = Number(el.dataset.lisStep);
      el.classList.toggle('ready', !!gate.step[step]);
      el.classList.toggle('current', step === activeStep);
      el.classList.toggle('locked', step > firstIncomplete);
    });
    const note = $('lis-readiness');
    if (note) {
      note.classList.toggle('ready', gate.ok);
      const prepared = reusablePreparedLis();
      const analysisPaused = ['unverified', 'incompatible'].includes(gate.methodStatus);
      note.textContent = prepared && !prepared.submitted
        ? tr("runtime.project.updatelisreadiness.text_c92c179248", { value1: prepared.name }, `本地项目“{value1}”已经生成。直接重试提交即可，不会重复生成或覆盖。`, 'The local project “{value1}” is already generated. Retry submission directly; nothing will be regenerated or overwritten.')
        : gate.ok
          ? analysisPaused
            ? tr("runtime.project.updatelisreadiness.text_3d79a69bba", { value1: (val('lis-profile')) }, `作业已就绪，可在 {value1} 提交；自动 ΔE / 最终报告将暂停，等方法证据修正或补齐后继续。`, 'Jobs are ready for submission on {value1}. Automatic ΔE and the final report will remain paused until method evidence is corrected or completed.')
            : tr("runtime.project.updatelisreadiness.text_4a420e5e04", { value1: (gate.items.length), value2: (val('lis-profile')) }, `已就绪：将生成 clean slab + {value1} 个 adsorption 作业，并在 {value2} 提交。`, 'Ready to generate a clean slab plus {value1} adsorption jobs and submit them on {value2}.')
          : gate.issue;
    }
    const button = $('pj-submit-all');
    if (button) {
      const prepared = reusablePreparedLis();
      button.disabled = State.lisBusy || State.inputScanBusy || !gate.ok ||
        !!(prepared && prepared.submitted);
      if (!State.lisBusy) {
        button.textContent = prepared && prepared.submitted
          ? tr("runtime.project.updatelisreadiness.text_3fe6ea482c", {}, '整组已提交，自动托管运行中', 'The group was submitted; autopilot is running')
          : prepared ? tr("runtime.project.updatelisreadiness.text_cae1b23b18", {}, '重试未提交成员（不会重复生成）', 'Retry unsubmitted members (do not regenerate)')
            : ['unverified', 'incompatible'].includes(gate.methodStatus)
              ? tr("runtime.project.updatelisreadiness.text_a63336ae96", {}, '生成并提交整组（ΔE / 报告待核验）', 'Generate and submit the group (ΔE / report pending validation)')
              : tr("runtime.project.updatelisreadiness.text_8869314e61", {}, '生成并提交整组，开启自动续算/下载/报告', 'Generate and submit the group; enable automatic continuation, download, and reporting');
      }
    }
    const status = $('lis-reference-status');
    if (status) {
      status.classList.toggle('ready', !!gate.ref && gate.refs.length > 0);
      status.textContent = gate.ref && gate.refs.length
        ? tr("runtime.project.updatelisreadiness.text_bd267c3642", { value1: (gate.refs.length), value2: (gate.refs.join('、')) }, `已找到 {value1} 个参考物种：{value2}。每个 adsorption 构型必须从中选择。`, 'Found {value1} reference species: {value2}. Select one for every adsorption configuration.')
        : gate.ref ? tr("runtime.project.updatelisreadiness.text_5667fcc4aa", {}, '这个项目没有可用的物种参考能，请先导入已收敛的 Li-S 化合物结果。', 'This project has no usable species reference energies. First import converged Li-S compound results.')
          : tr("runtime.project.updatelisreadiness.text_62be8c2cc7", {}, '请选择包含 Li-S 分子参考能的已导入项目。', 'Select an imported project containing Li-S molecular reference energies.');
    }
    updateJourney();
  }

  function openLisStep(step) {
    const gate = lisGate();
    const firstIncomplete = [1, 2, 3, 4].find(value => !gate.step[value]) || 4;
    if (step > firstIncomplete) {
      VCS.toast(tr("runtime.project.openlisstep.text_c4cb9d76b5", { value1: (firstIncomplete) }, `请先完成第 {value1} 步；完成后下一步会自动展开`, 'Complete step {value1} first; the next step will expand automatically'), 'fail');
      updateLisReadiness();
      return;
    }
    updateLisReadiness(step);
  }

  function renderReferenceProjects(preferred) {
    const sel = $('lis-reference');
    if (!sel) return;
    const previous = preferred || sel.value;
    const refs = State.projects.filter(p => referenceSpecies(p).length || Number(p.n_species_refs || 0) > 0);
    const emptyLabel = refs.length
      ? tr('runtime.project.reference.select_prompt', {},
        '请选择 Li-S 参考能项目', 'Select a Li-S reference-energy project')
      : tr('runtime.project.reference.none_imported', {},
        '暂无已导入的 Li-S 参考能项目', 'No imported Li-S reference-energy projects');
    sel.innerHTML = `<option value="">${VCS.esc(emptyLabel)}</option>` + refs.map(p => {
      const species = referenceSpecies(p);
      const count = species.length || Number(p.n_species_refs || 0);
      const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
      const detail = species.length
        ? `${english ? ': ' : '：'}${species.join(english ? ', ' : '、')}` : '';
      return tr("runtime.project.renderreferenceprojects.text_1d981dafe2", { value1: VCS.esc(projectId(p)), value2: VCS.esc(p.name || projectId(p)), value3: count, value4: VCS.esc(detail) }, `<option value="{value1}">{value2}（{value3} 个物种{value4}）</option>`, '<option value="{value1}">{value2} ({value3} species{value4})</option>');
    }).join('');
    const hit = refs.find(p => projectId(p) === previous) ||
      (!previous && refs.length === 1 ? refs[0] : null);
    if (hit) sel.value = projectId(hit);
    const options = $('lis-species-options');
    if (options) options.innerHTML = selectedReferenceSpecies()
      .map(x => `<option value="${VCS.esc(x)}"></option>`).join('');
    updateLisReadiness();
  }

  function onReferenceChanged() {
    invalidatePreparedLis();
    State.configs.forEach(path => {
      if (!State.configSpecies[path]) {
        const guessed = guessSpeciesEvidence(path, '');
        const previous = State.configSpeciesMeta[path] || {};
        State.configSpecies[path] = guessed.value;
        State.configSpeciesMeta[path] = {
          ...previous,
          source: guessed.source, confidence: guessed.value ? 'hint' : 'unknown',
          confirmed: false, ambiguous: guessed.ambiguous, warnings: guessed.warnings,
        };
      }
    });
    const options = $('lis-species-options');
    if (options) options.innerHTML = selectedReferenceSpecies()
      .map(x => `<option value="${VCS.esc(x)}"></option>`).join('');
    renderConfigs();
  }

  function updateBulkSpeciesOptions(refs) {
    const select = $('lis-bulk-species');
    if (!select) return;
    const previous = select.value;
    const values = refs || selectedReferenceSpecies();
    select.innerHTML = tr("runtime.project.updatebulkspeciesoptions.text_ddcde6022e", {}, '<option value="">选择物种后批量应用…</option>', '<option value="">Select a species to apply in bulk…</option>') + values.map(value =>
      `<option value="${VCS.esc(value)}">${VCS.esc(value)}</option>`).join('');
    if (values.includes(previous)) select.value = previous;
  }

  // 构型列表按物种成组；POSCAR 组成差可自动确认，文件名建议必须由用户确认。
  function renderConfigs() {
    const box = $('pj-cfg-list');
    const count = $('lis-config-count');
    const refs = selectedReferenceSpecies();
    const validSpecies = path => refs.some(value => value.toLowerCase() ===
      String(State.configSpecies[path] || '').trim().toLowerCase());
    const confirmedSpecies = path => validSpecies(path) &&
      !!(State.configSpeciesMeta[path] && State.configSpeciesMeta[path].confirmed);
    const matched = State.configs.filter(confirmedSpecies).length;
    const incarReady = path => {
      const meta = State.configSpeciesMeta[path] || {};
      return meta.incarStatus === 'ready' ||
        ((meta.incarStatus || 'missing') === 'missing' && !!val('pj-incar'));
    };
    const incars = State.configs.filter(incarReady).length;
    const quartetRows = State.configs.map(path => quartetPresentation(
      State.configSpeciesMeta[path] || {}, val('pj-incar')));
    const copiedQuartets = quartetRows.filter(item => item.hasEvidence && item.mode === 'copy').length;
    const generatedQuartets = quartetRows.filter(item => item.hasEvidence && item.mode === 'generate').length;
    if (count) count.textContent = State.configs.length
      ? tr("runtime.project.renderconfigs.text_9b721810b2", { value1: (matched), value2: (State.configs.length), value3: (incars), value4: (State.configs.length) }, `物种已确认 {value1}/{value2}，INCAR 已绑定 {value3}/{value4}`, 'Species confirmed {value1}/{value2}; INCAR bound {value3}/{value4}') +
        (copiedQuartets || generatedQuartets
          ? tr("runtime.project.renderconfigs.text_ca49938584", { value1: (copiedQuartets), value2: (generatedQuartets) }, `；四件套原样 {value1}，受管副本补齐 {value2}`, '; {value1} four-file sets unchanged, {value2} managed copies completed') : '')
      : tr("runtime.project.renderconfigs.text_37d3aaf153", {}, '尚未添加构型', 'No configurations added yet');
    updateBulkSpeciesOptions(refs);
    if (!box) return;
    if (!State.configs.length) {
      box.innerHTML = tr("runtime.project.renderconfigs.text_9d36216783", {}, '<div class="pj-cfgempty">尚未添加 adsorption POSCAR；可逐个添加，也可一次扫描整个文件夹。</div>', '<div class="pj-cfgempty">No adsorption POSCAR has been added. Add them individually or scan an entire folder.</div>');
      updateLisReadiness();
      return;
    }
    const onlyUnmatched = !!($('lis-only-unmatched') && $('lis-only-unmatched').checked);
    const rows = State.configs.map((path, index) => ({ path, index }))
      .filter(item => !onlyUnmatched || !confirmedSpecies(item.path) ||
        !incarReady(item.path) || quartetBlocked(State.configSpeciesMeta[item.path]));
    if (!rows.length) {
      box.innerHTML = tr("runtime.project.renderconfigs.text_c11368873c", {}, '<div class="pj-cfgempty">所有构型的物种映射都已确认。</div>', '<div class="pj-cfgempty">Species mappings have been confirmed for every configuration.</div>');
      updateLisReadiness();
      return;
    }
    const groups = [];
    rows.forEach(row => {
      const species = String(State.configSpecies[row.path] || '').trim();
      const key = species || tr("runtime.project.renderconfigs.text_6ea09ad378", {}, '未识别', 'Unidentified');
      let group = groups.find(item => item.key.toLowerCase() === key.toLowerCase());
      if (!group) { group = { key, rows: [] }; groups.push(group); }
      group.rows.push(row);
    });
    groups.sort((left, right) => (left.key === '未识别') - (right.key === '未识别') ||
      left.key.localeCompare(right.key));
    box.innerHTML = groups.map((group, groupIndex) => {
      const canConfirm = group.rows.some(row => validSpecies(row.path) && !confirmedSpecies(row.path));
      const confirmed = group.rows.filter(row => confirmedSpecies(row.path)).length;
      const header = `<div class="lis-species-group"><span><b>${VCS.esc(group.key)}</b> · ` +
        tr("runtime.project.renderconfigs.text_80f91df239", { value1: (group.rows.length), value2: (confirmed) }, `{value1} 个构型 · {value2} 个已确认</span>`, '{value1} configurations · {value2} confirmed</span>') +
        (canConfirm ? tr("runtime.project.renderconfigs.text_c56d3f87a7", { value1: (groupIndex) }, `<button class="btn quiet" type="button" data-confirm-group="{value1}">确认本组映射</button>`, '<button class="btn quiet" type="button" data-confirm-group="{value1}">Confirm this group\'s mappings</button>') : '') +
        '</div>';
      const body = group.rows.map(({ path: p, index: i }) => {
        const species = String(State.configSpecies[p] || '');
        const valid = validSpecies(p);
        const meta = State.configSpeciesMeta[p] || {};
        const confirmedRow = valid && meta.confirmed === true;
        const memberIncarReady = incarReady(p);
        const quartet = quartetPresentation(meta, val('pj-incar'));
        const incarStatus = String(meta.incarStatus || 'missing');
        const incarText = incarStatus === 'ready' && meta.incarPath
          ? `INCAR：${meta.incarPath}`
          : incarStatus === 'missing' && val('pj-incar')
            ? tr("runtime.project.renderconfigs.text_a121aa08d6", { value1: (val('pj-incar')) }, `本目录无 INCAR；将使用显式备用：{value1}`, 'No INCAR in this directory; using the explicit fallback: {value1}')
            : tr("runtime.project.renderconfigs.text_c329704ebc", {
              value1: textList(meta.incarIssues).join('; ') || tr(
                'runtime.project.renderconfigs.missing_same_directory', {},
                '同目录缺失', 'Missing from the same directory'),
            }, `INCAR 不可用：{value1}`, 'INCAR unavailable: {value1}');
        const inputEvidence = quartet.hasEvidence
          ? `<div class="lis-quartet ${quartet.blocked ? 'blocked' : quartet.mode}">${quartetHtml(quartet)}</div>`
          : `<span class="sub" title="${VCS.esc(incarText)}">${VCS.esc(incarText)}</span>`;
        const sourceLabel = meta.source === 'poscar_minus_clean_slab' ||
          String(meta.source || '').startsWith('poscar_minus_clean_slab+')
          ? tr("runtime.project.renderconfigs.text_1886efc918", {}, 'POSCAR−clean slab 组成差', 'POSCAR−clean-slab composition difference')
          : meta.source === 'user' ? tr("runtime.project.renderconfigs.text_f4c944fa98", {}, '人工选择', 'Selected manually') : meta.source === 'path_name' ? tr("runtime.project.renderconfigs.text_f0a01b1c3a", {}, '文件夹/文件名建议', 'Folder/file-name suggestion') : tr("runtime.project.renderconfigs.text_6ea09ad378", {}, '未识别', 'Unidentified');
        const speciesCheck = confirmedRow ? tr("runtime.project.renderconfigs.text_917c0ed620", { value1: (sourceLabel) }, `已确认：{value1}`, 'Confirmed: {value1}')
          : valid ? tr("runtime.project.renderconfigs.text_980097fae4", { value1: (species), value2: (sourceLabel) }, `自动建议 {value1}，待确认（{value2}）`, 'Automatic suggestion {value1}, awaiting confirmation ({value2})')
            : tr("runtime.project.renderconfigs.text_80dbd4bd3b", {
              value1: refs.join(', ') || tr('runtime.project.renderconfigs.select_reference_first', {},
                '先选参考项目', 'Select a reference project first'),
            }, `未匹配参考能；请选择：{value1}`, 'No matching reference energy; select one: {value1}');
        const speciesOptions = tr("runtime.project.renderconfigs.text_f0b110751e", {}, '<option value="">请选择对应物种</option>', '<option value="">Select the corresponding species</option>') +
          (!valid && species ? tr("runtime.project.renderconfigs.text_df45dfb36d", { value1: (VCS.esc(species)), value2: (VCS.esc(species)) }, `<option value="{value1}" selected>{value2}（未匹配）</option>`, '<option value="{value1}" selected>{value2} (unmatched)</option>') : '') +
          refs.map(value => `<option value="${VCS.esc(value)}"${value.toLowerCase() === species.trim().toLowerCase() ? ' selected' : ''}>${VCS.esc(value)}</option>`).join('');
        return `<div class="pj-cfgrow lis-cfgrow${quartet.blocked ? ' invalid' :
          confirmedRow && memberIncarReady ? '' : valid && memberIncarReady ? ' pending' : ' invalid'}">` +
          `<div class="lis-cfgpath"><span class="path" title="${VCS.esc(p)}">${VCS.esc(pathBase(p))}</span>` +
          `<span class="sub" title="${VCS.esc(p)}">${VCS.esc(p)}</span>` +
          `${inputEvidence}</div>` +
          tr("runtime.project.renderconfigs.text_d1d9cb205a", { value1: (i), value2: (speciesOptions) }, `<label>对应物种<select class="ipt lis-species" data-species-index="{value1}">{value2}</select></label>`, '<label>Corresponding species<select class="ipt lis-species" data-species-index="{value1}">{value2}</select></label>') +
          `<span class="lis-species-check">${VCS.esc(speciesCheck)}</span>` +
          tr("runtime.project.renderconfigs.text_8a4636eecb", { value1: (i) }, `<button class="btn quiet" type="button" data-rm="{value1}">移除</button></div>`, '<button class="btn quiet" type="button" data-rm="{value1}">Remove</button></div>');
      }).join('');
      return header + body;
    }).join('');
    box.querySelectorAll('button[data-confirm-group]').forEach(button => {
      button.addEventListener('click', () => {
        if (lisInputsLocked()) return;
        const group = groups[Number(button.dataset.confirmGroup)];
        if (!group) return;
        let changed = 0;
        group.rows.forEach(row => {
          if (!validSpecies(row.path)) return;
          const meta = State.configSpeciesMeta[row.path] || {};
          if (!meta.confirmed) changed++;
          State.configSpeciesMeta[row.path] = { ...meta, confirmed: true };
        });
        invalidatePreparedLis();
        renderConfigs();
        VCS.toast(tr("runtime.project.renderconfigs.text_1f8b649a8d", { value1: (changed), value2: (group.key) }, `已确认 {value1} 个 {value2} 构型的物种映射`, 'Confirmed species mappings for {value1} {value2} configurations'));
      });
    });
    box.querySelectorAll('select[data-species-index]').forEach(input => {
      input.addEventListener('change', () => {
        if (lisInputsLocked()) return;
        const path = State.configs[Number(input.dataset.speciesIndex)];
        if (!path) return;
        State.configSpecies[path] = exactReferenceSpecies(input.value) || input.value.trim();
        const previous = State.configSpeciesMeta[path] || {};
        State.configSpeciesMeta[path] = {
          ...previous,
          source: 'user', confidence: 'confirmed', confirmed: !!State.configSpecies[path],
          warnings: [],
        };
        invalidatePreparedLis();
        renderConfigs();
      });
    });
    box.querySelectorAll('button[data-rm]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (lisInputsLocked()) return;
        const path = State.configs[Number(btn.dataset.rm)];
        State.configs.splice(Number(btn.dataset.rm), 1);
        if (path) {
          delete State.configSpecies[path];
          delete State.configSpeciesMeta[path];
        }
        invalidatePreparedLis();
        renderConfigs();
      });
    });
    updateLisReadiness();
  }

  function applyBulkSpecies() {
    if (lisInputsLocked()) return;
    const species = exactReferenceSpecies(val('lis-bulk-species'));
    if (!species) {
      VCS.toast(tr("runtime.project.applybulkspecies.text_8cc177f7d0", {}, '请先从参考物种中选择一个值', 'Select a value from the reference species first'), 'fail');
      return;
    }
    let changed = 0;
    State.configs.forEach(path => {
      const meta = State.configSpeciesMeta[path] || {};
      if (meta.confirmed === true) return;
      State.configSpecies[path] = species;
      State.configSpeciesMeta[path] = {
        ...meta,
        source: 'user', confidence: 'confirmed', confirmed: true, warnings: [],
      };
      changed++;
    });
    invalidatePreparedLis();
    renderConfigs();
    VCS.toast(changed ? tr("runtime.project.applybulkspecies.text_e3ae8be9cb", { value1: (changed), value2: (species) }, `已为 {value1} 个未确认构型绑定并确认 {value2}`, 'Assigned and confirmed {value2} for {value1} unconfirmed configurations') : tr("runtime.project.applybulkspecies.text_0c5eb165e4", {}, '所有构型已经确认，无需修改', 'Every configuration is already confirmed; no change is needed'));
  }

  async function resolveMemberIncar(path) {
    const result = await VCS.call('proj_resolve_member_incar', path, val('pj-incar'));
    return {
      incar_path: String(result && (result.incar_path || result.path) || ''),
      incar_sha256: String(result && (result.incar_sha256 || result.sha256) || ''),
      incar_status: String(result && (result.incar_status || result.status) || 'missing'),
      incar_source: String(result && result.source || ''),
      incar_issues: textList(result && (result.incar_issues || result.issues || result.error)),
    };
  }

  async function addConfig() {
    if (lisInputsLocked()) return;
    const generation = State.inputGeneration;
    const r = await VCS.call('pick_file', 'poscar');
    if (generation !== State.inputGeneration || lisInputsLocked()) return;
    if (r && r.error) { VCS.log(tr("runtime.project.addconfig.text_3df70eb2f3", {}, '选择构型失败:', 'Failed to select configuration:') + r.error, 'failc'); return; }
    if (r && r.path) {
      const incar = await resolveMemberIncar(r.path);
      if (generation !== State.inputGeneration || lisInputsLocked()) return;
      if (!appendConfig(r.path, '', incar)) {
        VCS.log(tr("runtime.project.addconfig.text_07efeaa935", {}, '该构型已在列表中，已跳过:', 'This configuration is already in the list and was skipped:') + r.path);
        return;
      }
      renderConfigs();
    }
  }

  function preferredScannedStructures(raw) {
    const chosen = new Map();
    let suppressed = 0;
    function score(item) {
      const path = String(item.path || '');
      const base = pathBase(path).toLowerCase();
      if (item.preferred === true) return 100;
      if (item.valid === false || item.parseable === false) return -10;
      if (base === 'poscar') return 20;
      if (base === 'contcar') return 10;
      return 0;
    }
    (raw || []).forEach(item => {
      const path = typeof item === 'string' ? item
        : item && (item.path || item.poscar_path || item.file || item.source_path);
      if (!path) { suppressed++; return; }
      const candidate = typeof item === 'string' ? { path: item, species: '' } : Object.assign({}, item, { path });
      const base = pathBase(path).toLowerCase();
      const standard = base === 'poscar' || base === 'contcar';
      const key = standard ? 'folder:' + pathParent(path).toLowerCase() : 'file:' + String(path).toLowerCase();
      const previous = chosen.get(key);
      // 这是新计算输入：同目录优先后端显式 preferred；否则优先原始 POSCAR，
      // 避免把上一轮残留 CONTCAR 当成用户本次准备提交的结构。
      if (!previous || score(candidate) > score(previous)) {
        if (previous) suppressed++;
        chosen.set(key, candidate);
      } else {
        suppressed++;
      }
    });
    const items = Array.from(chosen.values());
    return { items, suppressed,
      finalStructures: items.filter(item => pathBase(item.path).toLowerCase() === 'contcar').length };
  }

  async function addConfigDirectory() {
    if (lisInputsLocked()) return;
    const generation = State.inputGeneration;
    const picked = await VCS.call('pick_dir');
    if (generation !== State.inputGeneration || lisInputsLocked()) return;
    if (picked && picked.error) {
      VCS.log(tr("runtime.project.addconfigdirectory.text_d85d1ab904", {}, '选择构型文件夹失败:', 'Failed to select configuration folder:') + picked.error, 'failc');
      showConfigScanStatus('bad', tr("runtime.project.addconfigdirectory.text_91cf51500d", {}, '没有选中结构文件夹。请重新选择包含 POSCAR / CONTCAR / .vasp 文件的上层目录。', 'No structure folder was selected. Select the parent folder containing POSCAR, CONTCAR, or .vasp files.'));
      return;
    }
    if (!picked || !picked.path) return;
    if (!val('pj-name')) setVal('pj-name', pathBase(picked.path) + '_adsorption');
    if (!val('pj-root')) setVal('pj-root', pathParent(picked.path));
    const button = $('pj-cfg-dir');
    State.inputScanBusy = true;
    syncLisInputLocks();
    updateLisReadiness();
    if (button) { button.disabled = true; button.textContent = tr("runtime.project.addconfigdirectory.text_4dc57b639d", {}, '正在扫描结构…', 'Scanning structures…'); }
    showConfigScanStatus('', tr("runtime.project.addconfigdirectory.text_7f9d0900ad", {}, '正在递归寻找 POSCAR / CONTCAR / .vasp 结构文件…', 'Recursively searching for POSCAR, CONTCAR, and .vasp structure files…'));
    VCS.log(tr("runtime.project.addconfigdirectory.text_e2d132baf6", {}, '正在递归扫描 adsorption POSCAR:', 'Recursively scanning adsorption POSCAR files:') + picked.path + '…');
    try {
      const r = await VCS.call(
        'proj_scan_structures', picked.path, val('pj-slab'), selectedReferenceSpecies());
      if (generation !== State.inputGeneration) return;
      if (!r || r.ok === false || r.error) {
        VCS.log(tr("runtime.project.addconfigdirectory.text_156c42206c", {}, '扫描结构文件夹失败:', 'Failed to scan structure folder:') + ((r && r.error) || tr("runtime.project.addconfigdirectory.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
        showConfigScanStatus('bad', tr("runtime.project.addconfigdirectory.text_fb421fc5fd", {}, '扫描失败：', 'Scan failed: ') + ((r && r.error) || tr("runtime.project.addconfigdirectory.text_bd5e21c357", {}, '未知错误', 'Unknown error')) +
          tr("runtime.project.addconfigdirectory.text_a9ae1d19b0", {}, '。请确认目录可访问，并选择包含结构文件的上层目录。', '. Confirm that the directory is accessible and select the parent folder containing the structure files.'));
        return;
      }
      const raw = r.structures || r.items || r.configs || r.candidates || r.paths || [];
      const preferred = preferredScannedStructures(raw);
      let added = 0;
      preferred.items.forEach(item => {
        const path = typeof item === 'string' ? item
          : item && (item.path || item.poscar_path || item.file || item.source_path);
        const species = typeof item === 'object' && item
          ? (item.species || item.suggested_species || item.species_suggestion || '') : '';
        if (appendConfig(path, species, item)) added++;
      });
      textList(r.warnings).forEach(x => VCS.log(x, 'warnc'));
      renderConfigs();
      VCS.log(tr("runtime.project.addconfigdirectory.text_adc73ae2a9", { value1: (added), value2: (raw.length - added) }, `结构扫描完成：新增 {value1} 个，重复或无效跳过 {value2} 个`, 'Structure scan complete: added {value1}; skipped {value2} duplicates or invalid files'), added ? 'okc' : 'warnc');
      if (!raw.length) {
        showConfigScanStatus('bad', tr("runtime.project.addconfigdirectory.text_f8a77ceb25", {}, '没有找到结构文件。请确认文件名为 POSCAR / CONTCAR，或扩展名为 .vasp / .poscar。', 'No structure files were found. Confirm that files are named POSCAR or CONTCAR, or use the .vasp or .poscar extension.'));
        VCS.toast(tr("runtime.project.addconfigdirectory.text_f15ed493fa", {}, '没有找到 POSCAR / CONTCAR 结构文件', 'No POSCAR / CONTCAR structure files were found'), 'fail');
      } else {
        const choice = preferred.suppressed
          ? tr("runtime.project.addconfigdirectory.text_bf83944034", { value1: (preferred.suppressed) }, `；同目录冲突时已优先选择本次输入 POSCAR（跳过 {value1} 个重复文件）`, "; when files conflicted in the same directory, this calculation's POSCAR was preferred ({value1} duplicates skipped)") : '';
        const finalNote = preferred.finalStructures
          ? tr("runtime.project.addconfigdirectory.text_5da426fe93", { value1: (preferred.finalStructures) }, `；{value1} 个目录仅能回退使用 CONTCAR，请确认不是旧结果残留`, '; {value1} directories could only fall back to CONTCAR; confirm that these are not stale results') : '';
        showConfigScanStatus('ok', tr("runtime.project.addconfigdirectory.text_aca817a143", { value1: (added), value2: (choice), value3: (finalNote) }, `已新增 {value1} 个结构{value2}{value3}。请逐行确认“对应物种”，红色项必须修正后才能提交。`, 'Added {value1} structures{value2}{value3}. Confirm Corresponding species on every row; red items must be resolved before submission.'));
      }
    } finally {
      if (generation === State.inputGeneration) {
        State.inputScanBusy = false;
        syncLisInputLocks();
        updateLisReadiness();
        if (button) { button.disabled = false; button.textContent = tr("runtime.project.addconfigdirectory.text_8b3e2e34d1", {}, '导入整个结构文件夹', 'Import entire structure folder'); }
      }
    }
  }

  function updateLisResourceSummary() {
    const box = $('lis-resource-summary');
    if (!box) return;
    const profile = State.profiles.find(item => item.name === val('lis-profile'));
    if (!profile) {
      box.textContent = tr("runtime.project.updatelisresourcesummary.text_7aabe9e704", {}, '选择服务器后会自动带入该服务器的默认核数与墙时；本项目只绑定所选服务器。', 'Selecting a server fills in its default core count and wall time. This project is bound only to the selected server.');
      return;
    }
    const queue = profile.queue ? tr("runtime.project.updatelisresourcesummary.text_8bf8e66261", { value1: (profile.queue) }, `，队列 {value1}`, ', queue {value1}') : '';
    box.textContent = tr("runtime.project.updatelisresourcesummary.text_f236905f5a", { value1: (profile.name), value2: (queue), value3: (val('lis-cores') || '—') }, `本项目将提交到 {value1}{value2}：每个作业 {value3} 核，`, 'This project will be submitted to {value1}{value2}: {value3} cores per job, ') +
      tr("runtime.project.updatelisresourcesummary.text_cd44da726b", { value1: (val('lis-walltime') || '—') }, `墙时 {value1}。其他项目可同时选择别的服务器，软件会并行监控。`, 'wall time {value1}. Other projects may use different servers; the application monitors them in parallel.');
  }

  function applyLisProfileDefaults() {
    const profile = State.profiles.find(item => item.name === val('lis-profile'));
    if (!profile) { updateLisResourceSummary(); return; }
    if (Number(profile.ppn) > 0) setVal('lis-cores', String(Number(profile.ppn)));
    if (profile.walltime) setVal('lis-walltime', profile.walltime);
    updateLisResourceSummary();
    updateLisReadiness();
  }

  async function loadLisProfiles() {
    const sel = $('lis-profile');
    if (!sel) return;
    const r = await VCS.call('list_profiles');
    State.profiles = (r && r.profiles) || [];
    if (r && r.error) VCS.log(tr("runtime.project.loadlisprofiles.text_6bebecf3b2", {}, '读取服务器列表失败:', 'Failed to load the server list:') + r.error, 'failc');
    const previous = sel.value;
    let saved = '';
    try { saved = localStorage.getItem('vcs.jobs.profile') || ''; } catch (e) { /* 不阻塞 */ }
    if (!State.profiles.length) {
      sel.innerHTML = tr("runtime.project.loadlisprofiles.text_dcb559991e", {}, '<option value="">尚未配置服务器</option>', '<option value="">No server configured</option>');
      updateLisReadiness();
      return;
    }
    sel.innerHTML = State.profiles.map(p =>
      `<option value="${VCS.esc(p.name)}">${VCS.esc(p.name)}</option>`).join('');
    const wanted = [previous, saved].find(x => State.profiles.some(p => p.name === x));
    sel.value = wanted || State.profiles[0].name;
    applyLisProfileDefaults();
  }

  async function submitLiSProject(projectIdValue, gate) {
    let password = null;
    let trust = false;
    const operationId = 'project-submit-' + Date.now().toString(36) + '-' +
      String(projectIdValue || '').replace(/[^A-Za-z0-9_.-]/g, '-').slice(-32);
    for (let attempt = 0; attempt < 6; attempt++) {
      const r = await VCS.call('submit_project_with_resources', projectIdValue,
        val('lis-profile'), gate.cores, gate.walltime, password, trust, operationId);
      if (!r || r.cancelled) return null;
      if (r.needPassword) { password = r.password; continue; }
      if (r.error === 'NEED_PASSWORD') {
        password = await VCS.password();
        if (password === null) return null;
        continue;
      }
      if (r.needs_trust) {
        const pin = await VCS.confirmHostKey(r);
        if (!pin) return { ok: false, results: r.results || [], skipped: r.skipped || [],
          error: tr("runtime.project.submitlisproject.text_02633fe5d6", {}, '服务器主机指纹未通过核对。请到“集群”页测试连接后再重试。', 'The server host fingerprint was not verified. Test the connection on Clusters before trying again.') };
        trust = pin;
        continue;
      }
      return r;
    }
    return { ok: false, error: tr("runtime.project.submitlisproject.text_7aed13d7ed", {}, '密码或主机信任重试次数过多，请检查服务器配置', 'Too many password or host-trust retries; check the server configuration') };
  }

  function preparedProjectId(result) { return projectIdFrom(result); }

  function submissionRows(result) {
    const raw = result && (result.results || result.submissions || result.jobs || result.items) || [];
    const rows = raw.map((item, index) => {
      if (Array.isArray(item)) {
        return { name: pathBase(item[0] || tr("runtime.project.submissionrows.text_ca271f224f", { value1: (index + 1) }, `作业 {value1}`, 'Job {value1}')), ok: item.length > 2 ? !!item[1] : true,
          message: item.length > 2 ? item[2] : item[1] };
      }
      const row = item || {};
      const state = String(row.state || row.status || '').toUpperCase();
      return {
        name: row.name || pathBase(row.dir || row.job_dir || row.path || tr("runtime.project.submissionrows.text_ca271f224f", { value1: (index + 1) }, `作业 {value1}`, 'Job {value1}')),
        ok: row.ok != null ? !!row.ok : !['FAILED', 'ERROR', 'BLOCKED'].includes(state),
        message: row.message || row.error || row.job_id || state || tr("runtime.project.submissionrows.text_69df1816f0", {}, '已提交', 'Submitted'),
      };
    });
    (result && result.skipped || []).forEach((item, index) => {
      const row = item || {};
      rows.push({
        name: row.name || pathBase(row.dir || row.job_dir || row.path || tr("runtime.project.submissionrows.text_c4b783edd7", { value1: (index + 1) }, `跳过项 {value1}`, 'Skipped item {value1}')),
        ok: true,
        skipped: true,
        message: row.reason || row.message || tr("runtime.project.submissionrows.text_762befbe3b", {}, '状态无需重复提交', 'Current status does not require resubmission'),
      });
    });
    return rows;
  }

  function lisRepairHint(message, stage) {
    const text = String(message || '');
    if (/已存在|不会覆盖/.test(text)) return tr("runtime.project.lisrepairhint.text_003f43f9fb", {}, '目标项目已经存在。请回到第 3 步换一个项目名，旧项目不会被覆盖。', 'The target project already exists. Return to step 3 and use a new project name; the old project will not be overwritten.');
    if (/方法|核验|不能直接相减|可比性/.test(text)) return tr("runtime.project.lisrepairhint.text_63ed3e168b", {}, '作业输入可继续生成和提交；这些差异只会暂停自动 ΔE 与最终报告。请查看方法面板，按具体能量项补齐证据或重算；ISPIN 体系说明无需勾选确认。', 'Job input may still be generated and submitted; these differences only pause automatic ΔE and the final report. Review the method panel and provide evidence or recalculate the affected energy terms. ISPIN system notes do not require confirmation.');
    if (/INCAR|POSCAR|结构文件|文件不存在|无法读取/.test(text)) return tr("runtime.project.lisrepairhint.text_0eec3fd4e9", {}, '文件路径已经失效或不可读。回到第 2 步重新选择对应文件。', 'The file path is stale or unreadable. Return to step 2 and select the file again.');
    if (/物种|species|参考/.test(text)) return tr("runtime.project.lisrepairhint.text_bd132731bd", {}, '回到第 1、2 步：确认参考能项目，并把每个红色构型改为参考集合中的物种。', 'Return to steps 1 and 2: confirm the reference-energy project and change every red configuration to a species in the reference set.');
    if (/POTCAR|赝势/.test(text)) return tr("runtime.project.lisrepairhint.text_e04d564731", {}, '到“设置”中配置可用的 POTCAR 库，再返回重试；不要手工拼接不一致的赝势。', 'Configure an available POTCAR library in Settings, then return and try again. Do not manually concatenate inconsistent pseudopotentials.');
    if (/凭据.*保存|keyring|无人值守|自动(?:托管|驾驶).*保存/i.test(text)) {
      return tr("runtime.project.lisrepairhint.text_401ba95ead", {}, '任务已经提交，不要重新生成项目。请到“集群”页重新保存可供无人值守使用的凭据，再回来重试接管。', 'The tasks were submitted; do not regenerate the project. On Clusters, save credentials that can be used unattended, then return and retry takeover.');
    }
    if (/密码|认证|credential/i.test(text)) return tr("runtime.project.lisrepairhint.text_368b9fb2f8", {}, '再次点击“重试未提交成员”，重新输入正确密码；已经生成的本地项目会直接复用。', 'Select Retry unsubmitted members and enter the correct password. The generated local project will be reused directly.');
    if (/主机|指纹|host/i.test(text)) return tr("runtime.project.lisrepairhint.text_b39e2be8a9", {}, '核对服务器指纹；确认无误后再次提交并选择信任。', 'Verify the server fingerprint; if it is correct, submit again and choose to trust it.');
    if (/集群|服务器|profile|队列/.test(text)) return tr("runtime.project.lisrepairhint.text_9779a0783c", {}, '回到第 4 步选择有效服务器；如列表为空，点击“配置服务器”。', 'Return to step 4 and select a valid server. If the list is empty, select Configure server.');
    return stage === 'prepare'
      ? tr("runtime.project.lisrepairhint.text_e597375658", {}, '按上面的错误回到对应步骤修正；若刚才已经生成过同名项目，请使用新项目名。', 'Use the error above to return to the corresponding step. If a same-named project was already generated, use a new project name.')
      : tr("runtime.project.lisrepairhint.text_6d1b909450", {}, '本地项目已经保留。修正服务器、密码或资源后，再点“重试未提交成员”；成功项不会重复提交。', 'The local project was preserved. After correcting the server, password, or resources, select Retry unsubmitted members; successful items will not be resubmitted.');
  }

  function lisRepairAction(message, stage) {
    const text = String(message || '');
    if (/方法|核验|不能直接相减|可比性/.test(text)) return ['method', tr("runtime.project.lisrepairaction.text_fc5b0d432a", {}, '查看 ΔE / 报告门禁', 'View ΔE / report gate')];
    if (/已存在|不会覆盖/.test(text)) return ['step3', tr("runtime.project.lisrepairaction.text_bc8a6ca2b8", {}, '返回修改项目名', 'Return to edit project name')];
    if (/参考项目|参考能/.test(text)) return ['step1', tr("runtime.project.lisrepairaction.text_cc7673f5a1", {}, '返回选择参考能', 'Return to select reference energies')];
    if (/INCAR|POSCAR|结构文件|物种|species/.test(text)) return ['step2', tr("runtime.project.lisrepairaction.text_5f92f1e351", {}, '返回检查输入', 'Return to check input')];
    if (/主机|指纹|集群|服务器|profile|密码|认证|凭据|keyring/i.test(text)) return ['cluster', tr("runtime.project.lisrepairaction.text_961fb7cb49", {}, '前往集群配置', 'Open cluster configuration')];
    return stage === 'submit' ? ['retry', tr("runtime.project.lisrepairaction.text_3e91bdbf35", {}, '修正后重试未提交成员', 'Retry unsubmitted members after correcting the issue')] : ['step1', tr("runtime.project.lisrepairaction.text_f290d0fa8c", {}, '返回逐步检查', 'Return to step-by-step checks')];
  }

  function bindLisRepairAction(box) {
    const button = box && box.querySelector('[data-lis-fix]');
    if (!button) return;
    button.addEventListener('click', () => {
      const kind = button.dataset.lisFix;
      if (kind === 'cluster') {
        if (typeof VCS.navigate === 'function') VCS.navigate('cluster', { source: 'lis-repair' });
      } else if (kind === 'retry') {
        const submit = $('pj-submit-all'); if (submit && !submit.disabled) submit.click();
      } else if (kind === 'method') {
        const method = $('lis-method-check');
        if (method && typeof method.scrollIntoView === 'function') method.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } else {
        setAccordionOpen('pj-create-card', true);
        openLisStep(Number(kind.replace('step', '')) || 1);
        scrollToCard('pj-create-card');
      }
    });
  }

  function showLisFailure(title, message, stage) {
    const box = $('lis-submit-result');
    if (!box) return;
    const reusable = reusablePreparedLis();
    box.hidden = false;
    const action = lisRepairAction(message, stage);
    box.innerHTML = `<div class="lis-result-head bad"><b>${VCS.esc(title)}</b>` +
      (reusable ? `<span>${VCS.esc(reusable.name)}</span>` : '') + '</div>' +
      `<div class="lis-result-error">${VCS.esc(message || tr(
        'runtime.project.common.unknown_error', {}, '未知错误', 'Unknown error'))}</div>` +
      tr("runtime.project.showlisfailure.text_abaf4ecfde", { value1: (VCS.esc(lisRepairHint(message, stage))) }, `<div class="lis-result-fix"><b>怎么处理</b><span>{value1}</span>`, '<div class="lis-result-fix"><b>How to resolve it</b><span>{value1}</span>') +
      `<button class="btn" type="button" data-lis-fix="${action[0]}">${VCS.esc(action[1])}</button></div>`;
    bindLisRepairAction(box);
  }

  function renderLisSubmitResult(prepared, submitted) {
    const box = $('lis-submit-result');
    if (!box) return;
    const rows = submissionRows(submitted);
    const preparedId = preparedProjectId(prepared);
    const preparedName = projectNameFrom(prepared, val('pj-name'));
    const ok = submitted && submitted.ok !== false && !submitted.error;
    const automationNotReady = !ok && /凭据.*保存|keyring|无人值守|自动(?:托管|驾驶).*保存/i.test(
      String(submitted && submitted.error || '')) && rows.some(row => row.ok && !row.skipped);
    const heading = ok ? tr("runtime.project.renderlissubmitresult.text_0accad88ea", {}, '整组已生成并提交', 'The complete group was generated and submitted')
      : automationNotReady ? tr("runtime.project.renderlissubmitresult.text_99522945ec", {}, '任务已提交，但自动续算尚未接管', 'Tasks were submitted, but automatic continuation has not taken over') : tr("runtime.project.renderlissubmitresult.text_c51ae610f1", {}, '项目已生成，但提交没有全部完成', 'The project was generated, but submission did not complete for every member');
    let html = `<div class="lis-result-head ${ok ? 'ok' : 'bad'}"><b>${heading}</b>` +
      `<span>${VCS.esc(preparedName)}</span></div>`;
    if (rows.length) {
      html += tr("runtime.project.renderlissubmitresult.text_a30d2b90af", {}, '<table><thead><tr><th>成员作业</th><th>提交结果</th><th>说明 / 作业号</th></tr></thead><tbody>', '<table><thead><tr><th>Member job</th><th>Submission result</th><th>Details / job ID</th></tr></thead><tbody>');
      rows.forEach(row => {
        const resultBadge = row.skipped
          ? tr('runtime.project.submission.skipped_badge', {},
            '<span class="lis-submit-skip">已跳过</span>',
            '<span class="lis-submit-skip">Skipped</span>')
          : row.ok ? VCS.pill('SUBMITTED') : VCS.pill('FAILED');
        html += `<tr><td>${VCS.esc(row.name)}</td><td>${resultBadge}</td>` +
          `<td class="sub">${VCS.esc(row.message || '')}</td></tr>`;
      });
      html += '</tbody></table>';
    }
    if (submitted && submitted.error) html += `<div class="lis-result-error">${VCS.esc(submitted.error)}</div>`;
    if (!ok) {
      const action = lisRepairAction(submitted && submitted.error, 'submit');
      html += tr("runtime.project.renderlissubmitresult.text_b7356ec468", { value1: (VCS.esc(lisRepairHint(submitted && submitted.error, 'submit'))) }, `<div class="lis-result-fix"><b>怎么处理</b><span>{value1}</span>`, '<div class="lis-result-fix"><b>How to resolve it</b><span>{value1}</span>') +
        `<button class="btn" type="button" data-lis-fix="${action[0]}">${VCS.esc(action[1])}</button></div>`;
    }
    if (ok) {
      html += tr("runtime.project.renderlissubmitresult.text_406259b1ed", {}, '<div class="lis-result-next"><b>自动托管已开启</b><span>请保持软件运行。软件会监控队列，未收敛时最多续算 3 轮，', '<div class="lis-result-next"><b>Autopilot is enabled</b><span>Keep the application running. It monitors the queue and continues unconverged jobs for up to three rounds, ') +
        tr("runtime.project.renderlissubmitresult.text_ee26eff34b", {}, '每轮轻量拉回 OUTCAR、OSZICAR、CONTCAR，全部完成后自动生成报告。</span></div>', 'retrieving OUTCAR, OSZICAR, and CONTCAR after each round, then generating the report when all jobs complete.</span></div>') +
        tr("runtime.project.renderlissubmitresult.text_54d2d76ad1", {}, '<div class="actions lis-result-actions"><button class="btn primary" type="button" data-lis-next="jobs">查看任务进度</button>', '<div class="actions lis-result-actions"><button class="btn primary" type="button" data-lis-next="jobs">View task progress</button>') +
        tr("runtime.project.renderlissubmitresult.text_b861ebf90c", {}, '<button class="btn" type="button" data-lis-next="results">查看项目 ΔE 与报告</button></div>', '<button class="btn" type="button" data-lis-next="results">View project ΔE and reports</button></div>');
    }
    box.innerHTML = html;
    box.hidden = false;
    bindLisRepairAction(box);
    const jobs = box.querySelector('[data-lis-next="jobs"]');
    if (jobs) jobs.addEventListener('click', () => {
      if (typeof VCS.navigate === 'function') VCS.navigate('jobs', { source: 'lis-submitted' });
    });
    const results = box.querySelector('[data-lis-next="results"]');
    if (results) results.addEventListener('click', () => openProjectResults(preparedId));
  }

  async function prepareAndSubmitLiS() {
    if (lisInputsLocked()) return;
    const gate = lisGate();
    updateLisReadiness();
    if (!gate.ok) { VCS.toast(gate.issue, 'fail'); return; }
    const reusable = reusablePreparedLis();
    const button = $('pj-submit-all');
    const resultBox = $('lis-submit-result');
    const operationFingerprint = lisContentFingerprint();
    State.repairResume = 'submit';
    State.lisBusy = true;
    syncLisInputLocks();
    if (button) { button.disabled = true; button.textContent = reusable ? tr("runtime.project.prepareandsubmitlis.text_175082b3d3", {}, '正在重试未提交成员…', 'Retrying unsubmitted members…') : tr("runtime.project.prepareandsubmitlis.text_873fa4203d", {}, '正在生成整组输入…', 'Generating the complete input group…'); }
    if (resultBox) resultBox.hidden = true;
    VCS.log(reusable
      ? tr("runtime.project.prepareandsubmitlis.text_f63d528e50", {}, '复用已生成项目，直接重试尚未提交的成员：', 'Reusing the generated project and retrying only unsubmitted members:') + reusable.name
      : tr("runtime.project.prepareandsubmitlis.text_c7e00bfbb9", { value1: (gate.refs.length), value2: (gate.items.length) }, `正在按 {value1} 个 Li-S 参考物种准备 clean slab + {value2} 个 adsorption 作业…`, 'Preparing a clean slab plus {value2} adsorption jobs using {value1} Li-S reference species…'));
    try {
      let prepared;
      if (reusable) {
        prepared = { ok: true, project_id: reusable.projectId, project_name: reusable.name, warnings: [], advisories: [] };
      } else {
        prepared = await VCS.call('proj_prepare_lis', val('pj-name'), val('pj-slab'), gate.items,
          val('pj-incar'), val('pj-root'), projectId(gate.ref), gate.methodConfirmation,
          gate.memberIncars, gate.repairRequest);
        const methodCheck = prepared && prepared.method_check;
        const needsMethod = !!(prepared && prepared.needs_method_confirmation);
        const needsRepair = !!(prepared && prepared.needs_repair_decision);
        if (methodCheck || needsMethod) renderMethodCheck(methodCheck, needsMethod);
        if (methodExecutionStatus(methodCheck) === 'blocked') {
          const error = (prepared && prepared.error) || tr("runtime.project.prepareandsubmitlis.text_98597e6cb9", {}, '本地输入无法安全生成或提交', 'Local input cannot be generated or submitted safely');
          VCS.log(tr("runtime.project.prepareandsubmitlis.text_df5c1b18bb", {}, '输入执行检查阻止提交:', 'Input execution validation blocked submission:') + error, 'failc');
          showLisFailure(tr("runtime.project.prepareandsubmitlis.text_4dc7ff4adc", {}, '输入执行检查未通过', 'Input execution validation failed'), error, 'prepare');
          return;
        }
        if (needsRepair) {
          VCS.log(tr("runtime.project.prepareandsubmitlis.text_a1e47afffa", {}, '已生成智能修复预览；请选择“修复副本”或“保持原样”后继续', 'A smart-repair preview was generated. Select Repair copy or Keep unchanged to continue.'), 'warnc');
          VCS.toast(tr("runtime.project.prepareandsubmitlis.text_145885c75e", {}, '请在方法检查中选择智能修复或保持原样', 'Select smart repair or keep unchanged in the method check'));
          const method = $('lis-method-check');
          if (method && typeof method.scrollIntoView === 'function') {
            method.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
          return;
        }
        if (needsMethod) {
          VCS.log(tr("runtime.project.prepareandsubmitlis.text_93c72c1e18", {}, '旧版后端标记了方法待核对；不要求 ISPIN 确认，作业继续，自动 ΔE / 报告暂停', 'The legacy backend marked the method for review. ISPIN confirmation is not required; jobs continue, while automatic ΔE and reports remain paused.'), 'warnc');
        }
        if (!prepared || prepared.ok === false || prepared.error) {
          const error = (prepared && prepared.error) || tr("runtime.project.prepareandsubmitlis.text_bd5e21c357", {}, '未知错误', 'Unknown error');
          if (!methodCheck && /方法.*(?:不一致|不兼容)|不能直接相减/.test(error)) {
            renderMethodCheck({
              execution_status: 'ready', comparability_status: 'incompatible',
              status: 'incompatible', issues: [error],
            });
          }
          VCS.log(tr("runtime.project.prepareandsubmitlis.text_e9c267c42d", {}, 'Li-S 项目生成失败:', 'Li-S project generation failed:') + error, 'failc');
          textList(prepared && prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
          showLisFailure(tr("runtime.project.prepareandsubmitlis.text_6383f2c392", {}, '项目生成未完成', 'Project generation did not complete'), error, 'prepare');
          return;
        }
      }
      textList(prepared.advisories).forEach(x => VCS.log(tr("runtime.project.prepareandsubmitlis.text_b0364d757b", {}, '方法学提示:', 'Methodology note:') + x, 'warnc'));
      textList(prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
      if (['unverified', 'incompatible'].includes(methodComparabilityStatus(
        prepared && prepared.method_check))) {
        VCS.log(tr("runtime.project.prepareandsubmitlis.text_8469a9d68e", {}, '作业继续提交；自动 ΔE 与最终报告将等方法证据通过后再继续', 'Jobs will still be submitted; automatic ΔE and the final report resume after method evidence passes validation'), 'warnc');
      }
      const preparedId = preparedProjectId(prepared);
      const preparedName = projectNameFrom(prepared, val('pj-name'));
      if (!preparedId) {
        VCS.log(tr("runtime.project.prepareandsubmitlis.text_4d130c0198", {}, 'Li-S 项目生成失败:后端没有返回项目 ID', 'Li-S project generation failed: the backend returned no project ID'), 'failc');
        showLisFailure(tr("runtime.project.prepareandsubmitlis.text_6383f2c392", {}, '项目生成未完成', 'Project generation did not complete'), tr("runtime.project.prepareandsubmitlis.text_e2bc8e129a", {}, '后端没有返回项目 ID', 'The backend returned no project ID'), 'prepare');
        return;
      }
      if (!reusable) {
        State.preparedLis = {
          projectId: preparedId, fingerprint: operationFingerprint, name: preparedName, submitted: false,
        };
        State.preparedConflictHint = null;
      }
      VCS.log(tr("runtime.project.prepareandsubmitlis.text_f804637f11", {}, '项目已生成，正在上传到 ', 'Project generated; uploading to ') + val('lis-profile') + tr("runtime.project.prepareandsubmitlis.text_fac92c00eb", {}, ' 并提交…', ' and submitting…'), 'okc');
      if (button) button.textContent = tr("runtime.project.prepareandsubmitlis.text_fe3f2f5151", {}, '正在上传并提交整组…', 'Uploading and submitting the complete group…');
      const submitted = await submitLiSProject(preparedId, gate);
      if (!submitted) {
        VCS.log(tr("runtime.project.prepareandsubmitlis.text_8d7f1cb39e", {}, '已取消提交；本地项目仍保留：', 'Submission canceled; the local project remains: ') + preparedName, 'warnc');
        showLisFailure(tr("runtime.project.prepareandsubmitlis.text_c120dd5cc7", {}, '本地项目已生成，提交已取消', 'The local project was generated; submission was canceled'),
          tr("runtime.project.prepareandsubmitlis.text_e43a0afc44", {}, '没有提交任何新成员。再次点击主按钮即可直接重试，不会重复生成项目。', 'No new members were submitted. Select the primary button again to retry directly; the project will not be regenerated.'), 'submit');
        return;
      }
      renderLisSubmitResult(prepared, submitted);
      submissionRows(submitted).forEach(row => {
        const fallbackStatus = row.ok
          ? tr('runtime.project.submission.submitted', {}, '已提交', 'Submitted')
          : tr('runtime.project.submission.failed', {}, '失败', 'Failed');
        VCS.log(tr('runtime.project.submission.log_entry', {
          name: row.name, result: row.message || fallbackStatus,
        }, '{name}：{result}', '{name}: {result}'), row.ok ? 'okc' : 'failc');
      });
      if (submitted.ok === false || submitted.error) {
        VCS.log(tr("runtime.project.prepareandsubmitlis.text_4863988582", {}, '整组提交未完成:', 'Group submission did not complete:') + (submitted.error || tr("runtime.project.prepareandsubmitlis.text_4e3adff1ca", {}, '部分成员失败，请查看上方逐项结果', 'Some members failed; review the per-item results above')), 'failc');
        return;
      }
      if (State.preparedLis) State.preparedLis.submitted = true;
      State.workflowSubmitted = true;
      VCS.log(tr("runtime.project.prepareandsubmitlis.text_4a778e4c11", {}, '整组提交完成；自动续算、轻量下载和报告流程已开启', 'Group submission complete; automatic continuation, lightweight retrieval, and reporting are enabled'), 'okc');
      if (window.Jobs && typeof window.Jobs.reload === 'function') await window.Jobs.reload();
      await reloadProjects();
      if (VCS.pipeline && typeof VCS.pipeline.reconfigure === 'function') await VCS.pipeline.reconfigure();
      VCS.toast(tr("runtime.project.prepareandsubmitlis.text_5ece070042", {}, '整组已提交；请保持软件运行以完成自动流程', 'The group was submitted; keep the application running to complete the automatic workflow'));
    } finally {
      State.lisBusy = false;
      syncLisInputLocks();
      if (button) updateLisReadiness();
    }
  }

  async function startLiS(referenceProjectId) {
    setExplicitWorkflow('structure');
    const ana = $('analysis-type');
    if (ana && ana.value !== 'adsorption') {
      ana.value = 'adsorption';
      ana.dispatchEvent(new Event('change', { bubbles: true }));
    }
    await reloadProjects(referenceProjectId);
    setAccordionOpen('pj-import-card', false);
    setAccordionOpen('pj-results-card', false);
    setAccordionOpen('pj-create-card', true);
    scrollToCard('pj-create-card');
    const sel = $('lis-reference');
    const hit = State.projects.find(p => projectId(p) === String(referenceProjectId || ''));
    if (sel && hit) { sel.value = projectId(hit); onReferenceChanged(); }
    else updateLisReadiness();
  }

  async function routeNewCalculation() {
    if ((State.workflowPendingSubmit || State.workflowSubmitted || State.workflowNeedsHuman) &&
        !State.workflowResultReady) {
      if (typeof VCS.navigate === 'function') await VCS.navigate('jobs', { source: 'adsorption-journey' });
      return;
    }
    await startLiS();
  }

  async function routeQuartetSubmit() {
    if (typeof VCS.navigate === 'function') {
      await VCS.navigate('jobs', {
        source: 'adsorption-quartets', focusSelector: '#quick-submit-card',
      });
    }
    const card = $('quick-submit-card');
    if (card) {
      if (card.getAttribute('data-open') !== '1') {
        const head = card.querySelector(':scope > .acc-h');
        if (head) head.click();
        else card.setAttribute('data-open', '1');
      }
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    const button = $('qs-add-dir');
    if (button) button.focus();
  }

  // ── 仅生成：复用主流程的逐成员 INCAR 契约，但不触发集群提交 ───────────────
  async function create() {
    if (lisInputsLocked()) return;
    const btn = $('pj-create');
    const gate = lisGate();
    updateLisReadiness();
    let issue = '';
    if (!gate.step[1] || !gate.step[2] || !gate.step[3]) issue = gate.issue;
    else if (gate.methodBlocked) issue = tr("runtime.project.create.text_29227be002", {}, '输入执行检查已阻止生成；请修正本目录输入错误', 'Input execution validation blocked generation; correct the input errors in this directory');
    if (issue) { VCS.toast(issue, 'fail'); return; }
    const operationFingerprint = lisContentFingerprint();
    State.repairResume = 'create';
    State.lisBusy = true;
    syncLisInputLocks();
    if (btn) { btn.disabled = true; btn.textContent = tr("runtime.project.create.text_2a3eebf49b", {}, '正在生成逐目录作业…', 'Generating per-directory jobs…'); }
    VCS.log(tr("runtime.project.create.text_0207cf0a1b", { value1: (gate.items.length) }, `仅生成：正在准备 clean slab + {value1} 个逐成员四件套作业…`, 'Generate only: preparing a clean slab plus {value1} per-member four-file input-set jobs…'));
    try {
      const prepared = await VCS.call(
        'proj_prepare_lis', val('pj-name'), val('pj-slab'), gate.items,
        val('pj-incar'), val('pj-root'), projectId(gate.ref), gate.methodConfirmation,
        gate.memberIncars, gate.repairRequest);
      const methodCheck = prepared && prepared.method_check;
      const needsMethod = !!(prepared && prepared.needs_method_confirmation);
      const needsRepair = !!(prepared && prepared.needs_repair_decision);
      if (methodCheck || needsMethod) renderMethodCheck(methodCheck, needsMethod);
      if (methodExecutionStatus(methodCheck) === 'blocked') {
        const error = (prepared && prepared.error) || tr("runtime.project.create.text_52027290d4", {}, '本地输入无法安全生成', 'Local input cannot be generated safely');
        VCS.log(tr("runtime.project.create.text_5cb57a8992", {}, '仅生成已停止：', 'Generate-only operation stopped:') + error, 'failc');
        showLisFailure(tr("runtime.project.create.text_4dc7ff4adc", {}, '输入执行检查未通过', 'Input execution validation failed'), error, 'prepare');
        return;
      }
      if (needsRepair) {
        VCS.log(tr("runtime.project.create.text_f01effc54b", {}, '已生成智能修复预览；选择如何处理后再次执行“只生成”', 'A smart-repair preview was generated. Choose how to handle it, then run Generate only again.'), 'warnc');
        VCS.toast(tr("runtime.project.create.text_cfb67edcbd", {}, '请先选择智能修复或保持原样', 'Select smart repair or keep unchanged first'));
        return;
      }
      if (needsMethod) VCS.log(
        tr("runtime.project.create.text_2a22c1fd76", {}, '方法证据待核对；作业继续生成，自动 ΔE / 报告暂停', 'Method evidence needs review; jobs will still be generated, while automatic ΔE and reports remain paused'), 'warnc');
      if (!prepared || prepared.ok === false || prepared.error) {
        const error = (prepared && prepared.error) || tr("runtime.project.create.text_bd5e21c357", {}, '未知错误', 'Unknown error');
        VCS.log(tr("runtime.project.create.text_49b380622b", {}, '逐目录作业生成失败:', 'Per-directory job generation failed:') + error, 'failc');
        showLisFailure(tr("runtime.project.create.text_6383f2c392", {}, '项目生成未完成', 'Project generation did not complete'), error, 'prepare');
        return;
      }
      textList(prepared.advisories).forEach(x => VCS.log(tr("runtime.project.create.text_b0364d757b", {}, '方法学提示:', 'Methodology note:') + x, 'warnc'));
      textList(prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
      const preparedId = preparedProjectId(prepared);
      const preparedName = projectNameFrom(prepared, val('pj-name'));
      if (!preparedId) {
        showLisFailure(tr("runtime.project.create.text_6383f2c392", {}, '项目生成未完成', 'Project generation did not complete'), tr("runtime.project.create.text_e2bc8e129a", {}, '后端没有返回项目 ID', 'The backend returned no project ID'), 'prepare');
        return;
      }
      State.preparedLis = {
        projectId: preparedId, fingerprint: operationFingerprint,
        name: preparedName, submitted: false,
      };
      State.preparedConflictHint = null;
      const resultBox = $('lis-submit-result');
      if (resultBox) {
        resultBox.hidden = false;
        resultBox.innerHTML = tr("runtime.project.create.text_9fca54aec8", {}, '<div class="lis-result-head ok"><b>逐目录作业已生成，尚未提交</b>', '<div class="lis-result-head ok"><b>Per-directory jobs generated; not yet submitted</b>') +
          `<span>${VCS.esc(preparedName)}</span></div>` +
          tr("runtime.project.create.text_e7525bccc8", {}, '<div class="lis-result-next"><b>输入已冻结</b><span>完整成员使用同目录原始四件套；不完整成员按本目录 POSCAR+INCAR 生成受管四件套。', '<div class="lis-result-next"><b>Input frozen</b><span>Complete members use their original four-file input sets; incomplete members use managed four-file sets generated from the directory\'s POSCAR+INCAR.') +
          tr("runtime.project.create.text_28e51f5f49", {}, '可回到主按钮选择服务器并提交；不会重复生成项目。</span></div>', 'Return to the primary button to select a server and submit; the project will not be regenerated.</span></div>');
      }
      VCS.log(tr("runtime.project.create.text_b955457174", {}, '逐目录作业已生成，尚未提交：', 'Per-directory jobs generated and awaiting submission:') + preparedName, 'okc');
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      await reloadProjects();
      if (typeof VCS.nextStep === 'function') {
        VCS.nextStep({
          title: tr("runtime.project.create.text_364b855a24", {}, '逐目录吸附作业已生成', 'Per-directory adsorption jobs generated'),
          message: tr("runtime.project.create.text_33cbc78563", { value1: (gate.items.length) }, `clean slab 和 {value1} 个吸附构型已按各自四件套证据加入任务列表。`, "The clean slab and {value1} adsorption configurations were added to the job list using each member's four-file-set evidence."),
          detail: tr("runtime.project.create.text_e9a52499f3", {}, '下一步可在当前页面选择服务器后点击主按钮提交；生成阶段不会覆盖任何成员的本地 INCAR。', "Next, select a server on this page and use the primary button to submit. Generation does not overwrite any member's local INCAR."),
          primaryLabel: tr("runtime.project.create.text_d97cdf3d53", {}, '留在当前页选择服务器', 'Stay on this page and select a server'),
        });
      }
    } finally {
      State.lisBusy = false;
      syncLisInputLocks();
      if (btn) { btn.disabled = false; btn.textContent = tr("runtime.project.create.text_8391a3976f", {}, '只生成当前逐目录作业，不提交', 'Generate current per-directory jobs without submitting'); }
      updateLisReadiness();
    }
  }

  // ── 多项目选择:状态是唯一真相,DOM 仅负责显示 ─────────────────────────────
  function restoreCompareSelection() {
    if (State.compareSelectionRestored) return;
    State.compareSelectionRestored = true;
    try {
      localStorage.removeItem(LEGACY_CURRENT_PROJECT_KEY);
      localStorage.removeItem(LEGACY_COMPARE_PROJECTS_KEY);
      const raw = JSON.parse(localStorage.getItem(COMPARE_PROJECT_IDS_KEY) || '[]');
      if (Array.isArray(raw)) {
        State.compareProjectIds = new Set(raw.map(id => String(id || '').trim())
          .filter(id => PROJECT_ID_RE.test(id)));
      }
    } catch (_) {
      State.compareProjectIds = new Set();
    }
  }

  function persistCompareSelection() {
    try {
      localStorage.setItem(COMPARE_PROJECT_IDS_KEY,
        JSON.stringify(Array.from(State.compareProjectIds)));
    } catch (_) { /* 存储不可用不阻塞项目比较 */ }
  }

  function reconcileCompareSelection() {
    restoreCompareSelection();
    const known = new Set(State.projects.map(projectId).filter(Boolean));
    let changed = false;
    Array.from(State.compareProjectIds).forEach(id => {
      if (!known.has(id)) {
        State.compareProjectIds.delete(id);
        changed = true;
      }
    });
    if (changed) persistCompareSelection();
  }

  function selectedCompareProjectIds() {
    return State.projects
      .map(projectId)
      .filter(id => id && State.compareProjectIds.has(id));
  }

  function setCompareSelection(ids) {
    const known = new Set(State.projects.map(projectId).filter(Boolean));
    State.compareProjectIds = new Set((ids || [])
      .map(id => String(id || '').trim())
      .filter(id => PROJECT_ID_RE.test(id) && known.has(id)));
    State.comparePreview = null;
    State.comparePreviewGeneration += 1;
    persistCompareSelection();
    renderFigProjList();
    scheduleComparePreview();
  }

  function comparePreviewProject(id) {
    const projects = State.comparePreview && State.comparePreview.projects;
    return Array.isArray(projects)
      ? projects.find(project => projectId(project) === String(id || ''))
      : null;
  }

  function comparisonCounts() {
    const selected = selectedCompareProjectIds();
    const preview = State.comparePreview;
    if (preview && preview.ok !== false && Array.isArray(preview.projects)) {
      const blocked = preview.projects.filter(project => project.status === 'blocked').length;
      return {
        selected: Number(preview.selected_count != null ? preview.selected_count : selected.length),
        valid: Number(preview.ready_count != null
          ? preview.ready_count : Math.max(0, selected.length - blocked)),
        blocked,
        overlay: preview.can_plot === false ? 0 : Number(preview.ladder_ready_count || 0),
        checked: true,
      };
    }
    const known = new Set(State.projects.map(projectId).filter(Boolean));
    const valid = selected.filter(id => known.has(id)).length;
    return {
      selected: selected.length,
      valid,
      blocked: selected.length - valid,
      overlay: 0,
      checked: selected.length === 0,
    };
  }

  function renderCompareSummary() {
    const box = $('fig-selection-summary');
    const status = $('fig-compare-status');
    const counts = comparisonCounts();
    if (box) {
      box.innerHTML = tr("runtime.project.rendercomparesummary.text_1c82495bbb", {}, '<span>已选 <b>', '<span>Selected <b>') + counts.selected + '</b></span>' +
        tr("runtime.project.rendercomparesummary.text_6012327763", {}, '<span>有效 <b>', '<span>Valid <b>') + counts.valid + '</b></span>' +
        tr("runtime.project.rendercomparesummary.text_117ca28ec1", {}, '<span>阻断 <b>', '<span>Blocked <b>') + counts.blocked + '</b></span>' +
        tr("runtime.project.rendercomparesummary.text_09d92998fc", {}, '<span>可叠加台阶 <b>', '<span>Overlayable steps <b>') + counts.overlay + '</b></span>';
      box.classList.toggle('checking', !counts.checked);
    }
    const compareButton = $('pj-cmpfigs');
    const batchButton = $('pj-batch-report');
    if (compareButton) compareButton.disabled = counts.selected < 2 || State.compareFiguresBusy;
    if (batchButton) batchButton.disabled = counts.selected < 2 || State.batchReportBusy;
    ['fig-select-all', 'fig-select-comparable', 'fig-select-clear'].forEach(id => {
      const button = $(id);
      if (button) button.disabled = State.compareFiguresBusy || State.batchReportBusy;
    });
    if (!status) return;
    const preview = State.comparePreview;
    if (!counts.selected) {
      status.innerHTML = tr("runtime.project.rendercomparesummary.text_52270befc2", {}, '<span class="pj-compare-empty">选择至少两个催化剂项目后，软件会先核对方法与反应路径。</span>', '<span class="pj-compare-empty">Select at least two catalyst projects; the application will first validate their methods and reaction pathways.</span>');
      return;
    }
    if (!preview) {
      status.innerHTML = tr("runtime.project.rendercomparesummary.text_c6aee36e39", {}, '<span class="pj-compare-empty">正在核对所选项目的数据、方法与台阶图口径…</span>', '<span class="pj-compare-empty">Validating the selected projects\' data, methods, and step-diagram basis…</span>');
      return;
    }
    if (preview.ok === false) {
      status.innerHTML = `<span class="pj-compare-error">${VCS.esc(preview.error || tr(
        'runtime.project.compare.preflight_failed', {},
        '比较预检失败', 'Comparison preflight failed'))}</span>`;
      return;
    }
    let h = '';
    (preview.projects || []).forEach(project => {
      const ready = project.status === 'ready';
      const reasons = ready ? (project.warnings || []) : (project.block_reasons || []);
      h += `<div class="pj-compare-project ${ready ? 'ready' : 'blocked'}">` +
        `<b>${VCS.esc(project.display_name || project.name || projectId(project))}</b>` +
        `<span>${ready
          ? tr('runtime.project.compare.adsorption_valid', {}, '吸附能有效', 'Adsorption energy valid')
          : tr('runtime.project.compare.blocked', {}, '已阻断', 'Blocked')} · ${project.ladder
          ? tr('runtime.project.compare.ladder_available', {}, '台阶可用', 'Step diagram available')
          : tr('runtime.project.compare.no_ladder', {},
            '无可叠加台阶', 'No overlayable step diagram')}</span>` +
        (reasons.length ? `<small>${VCS.esc(reasons.join('；'))}</small>` : '') + '</div>';
    });
    const gate = preview.comparison_gate || {};
    (gate.blocking || []).forEach(reason => {
      h += `<div class="pj-compare-gate blocked">${VCS.esc(reason)}</div>`;
    });
    (gate.warnings || []).forEach(reason => {
      h += `<div class="pj-compare-gate warning">${VCS.esc(reason)}</div>`;
    });
    status.innerHTML = h || tr("runtime.project.rendercomparesummary.text_602596d9ab", {}, '<span class="pj-compare-empty">预检完成。</span>', '<span class="pj-compare-empty">Preflight complete.</span>');
  }

  function localComparisonPreview(ids) {
    const projects = (ids || []).map(id => {
      const project = State.projects.find(item => projectId(item) === id);
      const total = Number(project && project.n_members || 0);
      const done = Number(project && project.n_done || 0);
      const ready = !!project && total > 0 && done >= total;
      return {
        project_id: id,
        name: project && project.name || id,
        display_name: project && project.name || id,
        status: ready ? 'ready' : 'blocked',
        ladder: null,
        warnings: ready ? [tr("runtime.project.legacycomparisonpreview.text_abb5c72a71", {}, '旧版后端未提供跨项目方法与路径预检', 'The legacy backend did not provide cross-project method and pathway preflight')] : [],
        block_reasons: ready ? [] : [project ? tr("runtime.project.legacycomparisonpreview.text_6b2e33ad0e", {}, '项目成员尚未全部完成', 'Not all project members are complete') : tr("runtime.project.legacycomparisonpreview.text_8927cb3363", {}, '项目不存在或已移动', 'The project does not exist or was moved')],
      };
    });
    const readyCount = projects.filter(project => project.status === 'ready').length;
    return {
      ok: true,
      local_only: true,
      selected_count: projects.length,
      ready_count: readyCount,
      ladder_ready_count: 0,
      projects,
      can_plot: false,
      comparison_gate: {
        status: 'unverified',
        blocking: [],
        warnings: [tr("runtime.project.legacycomparisonpreview.text_0f696f4b74", {}, '当前后端未提供跨项目预检；生成图表或报告时仍会再次校验。', 'The current backend does not provide cross-project preflight; charts and reports will validate again during generation.')],
      },
    };
  }

  async function refreshComparePreview() {
    const ids = selectedCompareProjectIds();
    const generation = ++State.comparePreviewGeneration;
    if (!ids.length) {
      State.comparePreview = null;
      renderCompareSummary();
      return;
    }
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const result = await VCS.call('proj_compare_preview', ids, preset || null);
    if (generation !== State.comparePreviewGeneration) return;
    if (bridgeMethodUnavailable(result)) {
      State.comparePreview = localComparisonPreview(ids);
    } else {
      State.comparePreview = result && result.ok !== false && !result.error
        ? sanitizeComparisonPreview(result)
        : { ok: false, error: (result && result.error) || tr("runtime.project.refreshcomparepreview.text_15f465b7c9", {}, '比较预检失败', 'Comparison preflight failed'), projects: [] };
    }
    renderFigProjList();
  }

  function scheduleComparePreview() {
    if (State.comparePreviewTimer) clearTimeout(State.comparePreviewTimer);
    renderCompareSummary();
    State.comparePreviewTimer = setTimeout(() => {
      State.comparePreviewTimer = null;
      refreshComparePreview();
    }, 120);
  }

  function selectAllCompareProjects() {
    setCompareSelection(State.projects.map(projectId));
  }

  async function selectComparableProjects() {
    const ids = State.projects.map(projectId).filter(Boolean);
    if (!ids.length) return;
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const result = await VCS.call('proj_compare_preview', ids, preset || null);
    if (result && result.ok !== false && !result.error && Array.isArray(result.projects)) {
      setCompareSelection(result.projects
        .filter(project => project.status === 'ready')
        .map(projectId));
      return;
    }
    // 老后端没有预检接口时，只选已经完成全部成员的项目，不假装其台阶必然可比。
    setCompareSelection(State.projects.filter(project => {
      const total = Number(project.n_members || 0);
      return total > 0 && Number(project.n_done || 0) >= total;
    }).map(projectId));
    VCS.log(tr("runtime.project.selectcomparableprojects.text_8827694da8", {}, '当前后端未提供跨项目预检，已暂按“成员全部完成”筛选；生成时仍会再次校验。', 'The current backend does not provide cross-project preflight, so projects were temporarily filtered by all-members-complete; generation will validate again.'), 'warnc');
  }

  // ── 已有项目:下拉 + 刷新 ──────────────────────────────────────────────────
  function applyProjectSelection(project, { persist = true, publish = true } = {}) {
    const sel = $('pj-select');
    const id = projectId(project);
    if (sel && sel.value !== id) sel.value = id;
    State.currentProjectId = id;
    State.deltaResult = null;
    State.candidateEvaluation = null;
    restoreWorkflowState(project || null);
    if (persist) purgeLegacyProjectStorage();
    updateProjectSummary();
    updateJourney();
    updateProjectHub();
    refreshCandidateEvaluation(id);
    if (publish && project) publishProjectContext(project);
    return !!project;
  }

  async function requestProjectSelection(project, previousId) {
    const sel = $('pj-select');
    const before = String(previousId || '');
    const selectionGeneration = ++State.projectSelectionGeneration;
    // 原生下拉与工作区程序化选择共用同一世代；后发意图使旧确认失效。
    State.requestedProject = null;
    // change 事件发生时 select 已显示新值；拦截确认期间恢复已提交项目，
    // 避免表单和全局上下文暂时指向不同项目。
    if (sel) sel.value = before;
    if (!project) return false;

    const apply = async () => {
      if (selectionGeneration !== State.projectSelectionGeneration) return false;
      applyProjectSelection(project);
      return true;
    };
    let allowed = true;
    if (VCS.workspace && typeof VCS.workspace.requestProjectSwitch === 'function') {
      allowed = await VCS.workspace.requestProjectSwitch(projectId(project), apply);
    } else if (VCS.unsaved && typeof VCS.unsaved.confirm === 'function') {
      allowed = await VCS.unsaved.confirm(tr("runtime.project.requestprojectselection.text_361adf4473", {}, '切换项目', 'Switch project'));
      if (allowed) await apply();
    } else {
      allowed = await apply();
    }
    if (selectionGeneration !== State.projectSelectionGeneration) return false;
    if (!allowed && sel) sel.value = before;
    return !!allowed;
  }

  function matchesRequestedProject(project, request) {
    if (!project || !request) return false;
    if (request.kind === 'id') return projectId(project) === request.value;
    return false;
  }

  function purgeLegacyProjectStorage() {
    try {
      localStorage.removeItem(LEGACY_CURRENT_PROJECT_KEY);
      localStorage.removeItem(LEGACY_COMPARE_PROJECTS_KEY);
    } catch (_) { /* 本地存储不可用不阻塞 */ }
  }

  async function performProjectReload(generation, preferredReference) {
    const sel = $('pj-select');
    const [r, pipeline] = await Promise.all([
      VCS.call('proj_list'), VCS.call('pipeline_status'),
    ]);
    // 后发请求拥有工作区上下文；旧响应不得再改 State、DOM 或当前项目。
    if (generation !== State.projectReloadGeneration) {
      return { ok: false, stale: true, generation };
    }
    const listSucceeded = !!(r && r.ok !== false && !r.error && Array.isArray(r.projects));
    const pipelineById = new Map(((pipeline && pipeline.projects) || [])
      .map(item => [rawProjectId(item), item]).filter(entry => entry[0]));
    const projectRows = listSucceeded ? r.projects : State.projects;
    State.projects = projectRows.map(project => {
      const id = rawProjectId(project);
      if (!id) return null;
      const cleanProject = withoutProjectLocators(project);
      const progress = withoutProjectLocators(pipelineById.get(id) || {});
      const projectCounts = project.counts || {};
      const progressCounts = progress.counts || {};
      return Object.assign({}, cleanProject, {
        project_id: id,
        pipeline_stage: progress.stage || '',
        pipeline_needs_human: !!progress.needs_human,
        pipeline_recover_round: Number(progress.recover_round || 0),
        n_done: Number(progressCounts.done != null ? progressCounts.done
          : progress.done != null ? progress.done
            : projectCounts.done != null ? projectCounts.done : project.n_done || 0),
        n_members: Number(progressCounts.members != null ? progressCounts.members
          : progress.total != null ? progress.total
            : projectCounts.members != null ? projectCounts.members : project.n_members || 0),
      });
    }).filter(Boolean);
    State.comparePreview = null;
    State.comparePreviewGeneration += 1;
    if (listSucceeded) reconcileCompareSelection();
    else restoreCompareSelection();
    if (r && r.error) VCS.log(tr("runtime.project.performprojectreload.text_7c736d8790", {}, '读取项目列表失败:', 'Failed to load project list:') + r.error, 'failc');
    renderReferenceProjects(preferredReference);
    if (!sel) return { ok: true, stale: false, generation };
    sel.innerHTML = '';
    if (!State.projects.length) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = tr("runtime.project.performprojectreload.text_2115ae53c9", {}, '(暂无项目)', '(No projects)');
      sel.appendChild(o);
      State.currentProjectId = '';
      restoreWorkflowState(null);
      renderFigProjList();
      updateProjectSummary();
      updateJourney();
      updateProjectHub();
      refreshCandidateEvaluation('');
      scheduleComparePreview();
      return { ok: true, stale: false, generation };
    }
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = tr('runtime.project.performprojectreload.no_selection', {},
      '(未选择项目)', '(No project selected)');
    sel.appendChild(placeholder);
    State.projects.forEach(p => {
      const o = document.createElement('option');
      o.value = projectId(p);
      const refs = referenceSpecies(p).length;
      o.textContent = (p.name || tr("runtime.project.performprojectreload.text_6b1efca7fc", {}, '(未命名)', '(Unnamed)')) + tr("runtime.project.performprojectreload.text_b79209ecf1", { value1: (p.n_members) }, `（{value1} 成员`, ' ({value1} members') +
        (refs ? tr("runtime.project.performprojectreload.text_df72e94e17", { value1: (refs) }, ` · {value1} 参考物种`, ' · {value1} reference species') : '') + '）';
      sel.appendChild(o);
    });
    purgeLegacyProjectStorage();
    const requested = State.requestedProject;
    const requestedHit = requested && requested.generation === State.projectSelectionGeneration
      ? State.projects.find(project => matchesRequestedProject(project, requested)) : null;
    const active = State.projects.slice().reverse().find(p =>
      ['submit', 'monitor', 'recover'].includes(p.pipeline_stage) || p.pipeline_needs_human);
    const workspaceId = String(VCS.workspace && VCS.workspace.state &&
      VCS.workspace.state.project_id || '');
    const liveSelection = State.currentProjectId || (sel ? sel.value : '');
    const candidates = [requestedHit && projectId(requestedHit), preferredReference,
      workspaceId, liveSelection, active && projectId(active)];
    const want = candidates.find(id => State.projects.some(p => projectId(p) === id)) || '';
    applyProjectSelection(State.projects.find(p => projectId(p) === want) || null);
    if (requestedHit && State.requestedProject === requested) State.requestedProject = null;
    renderFigProjList();
    scheduleComparePreview();
    return { ok: true, stale: false, generation };
  }

  async function reloadProjects(preferredReference) {
    const generation = ++State.projectReloadGeneration;
    const pending = performProjectReload(generation, preferredReference);
    State.projectReloadInFlight = { generation, promise: pending };
    let result = await pending;
    // 调用者等待期间若被更新的刷新取代，则跟随到真正提交 DOM 的最新一轮；
    // 这样“刷新后继续处理”的旧流程也不会读取半旧 State。
    while (result && result.stale) {
      const latest = State.projectReloadInFlight;
      if (!latest || latest.generation <= result.generation) break;
      result = await latest.promise;
    }
    return result;
  }

  // ── 论文级出图:多项目对比勾选列表(随项目列表刷新) ──────────────────────
  function renderFigProjList() {
    const box = $('fig-projlist');
    if (!box) return;
    box.innerHTML = '';
    if (!State.projects.length) {
      box.textContent = tr("runtime.project.renderfigprojlist.text_2115ae53c9", {}, '(暂无项目)', '(No projects)');
      return;
    }
    State.projects.forEach(p => {
      const lab = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.projectId = projectId(p);
      cb.checked = State.compareProjectIds.has(projectId(p));
      const preview = comparePreviewProject(projectId(p));
      if (preview) lab.classList.add(preview.status === 'ready' ? 'ready' : 'blocked');
      cb.addEventListener('change', () => {
        if (cb.checked) State.compareProjectIds.add(projectId(p));
        else State.compareProjectIds.delete(projectId(p));
        State.comparePreview = null;
        State.comparePreviewGeneration += 1;
        persistCompareSelection();
        renderCompareSummary();
        scheduleComparePreview();
      });
      lab.appendChild(cb);
      const text = document.createElement('span');
      text.textContent = p.name || tr("runtime.project.renderfigprojlist.text_6b1efca7fc", {}, '(未命名)', '(Unnamed)');
      lab.appendChild(text);
      if (preview) {
        const state = document.createElement('small');
        state.textContent = preview.status === 'ready'
          ? (preview.ladder ? tr("runtime.project.renderfigprojlist.text_e156f53a54", {}, '有效 · 台阶可用', 'Valid · steps available') : tr("runtime.project.renderfigprojlist.text_77a5c2eb5e", {}, '有效 · 无台阶', 'Valid · no steps'))
          : tr("runtime.project.renderfigprojlist.text_2378769b47", {}, '阻断', 'Blocked');
        lab.appendChild(state);
      }
      box.appendChild(lab);
    });
    renderCompareSummary();
  }

  function logFigResult(r, what) {
    if (!r || r.ok === false || r.error) {
      VCS.log(what + tr("runtime.project.logfigresult.text_8021928e6b", {}, '失败:', 'Failed:') + ((r && r.error) || tr("runtime.project.logfigresult.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
      return;
    }
    (r.files || []).forEach(f => VCS.log(tr("runtime.project.logfigresult.text_1009e8f59a", {}, '已生成:', 'Generated:') + f, 'okc'));
    (r.skipped || []).forEach(s => VCS.log(
      (s.partial ? tr("runtime.project.logfigresult.text_8e9895d66d", {}, '部分对比说明 ', 'Partial comparison note ') : tr("runtime.project.logfigresult.text_bf429f3063", {}, '跳过 ', 'Skipped ')) + s.kind + ':' + s.reason, 'warnc'));
    (r.warnings || []).forEach(w => VCS.log(tr("runtime.project.logfigresult.text_b0364d757b", {}, '方法学提示:', 'Methodology note:') + w, 'warnc'));
    if ((r.files || []).length) {
      VCS.log(tr("runtime.project.logfigresult.text_98c06e3b4c", {}, '图已输出到:', 'Figure written to:') + r.out_dir, 'okc');
      VCS.call('open_dir', r.out_dir);          // 生成即可看
      VCS.toast(tr("runtime.project.logfigresult.text_9f83e11a41", {}, '已生成 ', 'Generated ') + r.files.length + tr("runtime.project.logfigresult.text_a44d163e69", {}, ' 个文件', ' files'));
    } else if ((r.skipped || []).length) {
      VCS.toast(tr("runtime.project.logfigresult.text_af58679cec", {}, '本次没有可生成的图(原因见日志)', 'No figures could be generated (see the log for reasons)'), 'fail');
    }
  }

  // 当前项目出图:按勾选的图类型调 proj_figures
  async function makeFigures() {
    const proj = currentProject();
    if (!proj) return;
    const kinds = [];
    if ($('fig-bar') && $('fig-bar').checked) kinds.push('bar');
    if ($('fig-table') && $('fig-table').checked) kinds.push('table');
    if ($('fig-ladder') && $('fig-ladder').checked) kinds.push('ladder');
    if (!kinds.length) { VCS.log(tr("runtime.project.makefigures.text_02117a2f61", {}, '请至少勾选一种图', 'Select at least one figure type'), 'failc'); return; }
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const btn = $('pj-figs');
    if (btn) btn.disabled = true;
    VCS.log(tr("runtime.project.makefigures.text_76f9eab9f4", {}, '出图中(', 'Generating figures (') + kinds.join('/') + (preset ? tr("runtime.project.makefigures.text_e801f1dccc", {}, ',反应 ', ', reaction ') + preset : '') + ')…');
    try {
      // preset 为空 → Li-S 默认(向后兼容);非空 → ladder 走通用反应引擎
      const r = await VCS.call('proj_figures', projectId(proj), kinds, null, preset || null);
      logFigResult(r, tr("runtime.project.makefigures.text_7f60352d5b", {}, '出图', 'Figure generation'));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // 多项目对比出图:勾选的项目 + 勾选的图类型调 proj_compare_figures
  async function makeCompareFigures() {
    const ids = selectedCompareProjectIds();
    if (ids.length < 2) { VCS.log(tr("runtime.project.makecomparefigures.text_6e949fcef4", {}, '多项目对比请勾选至少 2 个项目', 'Select at least two projects for multi-project comparison'), 'failc'); return; }
    const kinds = [];
    if ($('fig-heatmap') && $('fig-heatmap').checked) kinds.push('heatmap');
    if ($('fig-scaling') && $('fig-scaling').checked) kinds.push('scaling');
    if ($('fig-volcano') && $('fig-volcano').checked) kinds.push('volcano');
    if ($('fig-compare-ladder') && $('fig-compare-ladder').checked) kinds.push('ladder');
    if (!kinds.length) { VCS.log(tr("runtime.project.makecomparefigures.text_ebde079599", {}, '请至少勾选一种对比图', 'Select at least one comparison-figure type'), 'failc'); return; }
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    State.compareFiguresBusy = true;
    renderCompareSummary();
    VCS.log(tr("runtime.project.makecomparefigures.text_51dfa86f1d", {}, '对比出图中(', 'Generating comparison figures (') + ids.length + tr("runtime.project.makecomparefigures.text_36d2ed7a91", {}, ' 个项目,', ' projects,') + kinds.join('/') + ')…');
    try {
      let r = await VCS.call('proj_compare_figures', ids, kinds, null, preset || null);
      const unsupported = r && r.error &&
        /(positional argument|unexpected argument|桥方法不存在|method not found)/i.test(String(r.error));
      if (unsupported) {
        const legacyKinds = kinds.filter(kind => kind !== 'ladder');
        r = legacyKinds.length
          ? await VCS.call('proj_compare_figures', ids, legacyKinds, null)
          : { ok: true, files: [], skipped: [] };
        if (!r) r = { ok: false, files: [], skipped: [], error: tr("runtime.project.makecomparefigures.text_28c66f3dad", {}, '旧版对比接口没有返回结果', 'The legacy comparison endpoint returned no result') };
        if (kinds.includes('ladder')) {
          r.skipped = (r.skipped || []).concat([{
            kind: 'ladder',
            reason: tr("runtime.project.makecomparefigures.text_9718c651e7", {}, '当前后端版本尚不支持多项目台阶图，请升级后重试', 'The current backend does not support multi-project step diagrams yet; upgrade and try again'),
          }]);
        }
      }
      logFigResult(r, tr("runtime.project.makecomparefigures.text_49f4d5e8ec", {}, '对比出图', 'Comparison figure generation'));
    } finally {
      State.compareFiguresBusy = false;
      renderCompareSummary();
    }
  }

  // ── 反应预设下拉:充填 ΔG 台阶图可选的反应族(默认 Li-S 放电保留在首项) ──────
  async function loadPresets() {
    const sel = $('pj-preset');
    if (!sel) return;
    const sceneKey = (VCS.scenario && VCS.scenario.key) || null;
    const r = await VCS.call('reaction_presets', sceneKey);
    sel.innerHTML = tr("runtime.project.loadpresets.text_1729346044", {}, '<option value="">Li-S 放电(默认)</option>', '<option value="">Li-S discharge (default)</option>');
    (r && r.presets || []).forEach(p => {
      const o = document.createElement('option');
      o.value = p.key;
      o.textContent = p.description || p.name || p.key;
      o.title = p.description || '';
      sel.appendChild(o);
    });
  }

  function applyProjectMode(sc) {
    const ana = $('analysis-type');
    if (!ana || !sc) return;
    const allowed = Array.from(ana.options).filter(o => !o.hasAttribute('data-scene-hidden'));
    const preferred = (sc.defaults && sc.defaults.analysis_type) || '';
    const currentAllowed = allowed.some(o => o.value === ana.value);
    if (!currentAllowed) {
      const hit = allowed.find(o => o.value === preferred) || allowed[0];
      if (hit) {
        ana.value = hit.value;
        ana.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    loadPresets();
  }

  function applyProjectCalculation() {
    const ana = $('analysis-type');
    if (!ana) return;
    const active = VCS.activeCalculation || '';
    // spin 选项是“跳往任务页”的用户动作；计算类型变化时不要在设置页自动触发跳页。
    const preferred = active === 'adsorption_project' ? 'adsorption' : 'taskana';
    const allowed = Array.from(ana.options).filter(o =>
      !o.hasAttribute('data-scene-hidden') && !o.hasAttribute('data-task-hidden'));
    const target = allowed.find(o => o.value === preferred) ||
      allowed.find(o => o.value === ana.value) || allowed[0];
    if (target && ana.value !== target.value) {
      ana.value = target.value;
      ana.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  const CANDIDATE_PRIORITY_ZH = {
    advance: () => tr("runtime.project.applyprojectcalculation.text_32f3c37764", {}, '建议继续', 'Continue recommended'),
    hold_for_evidence: () => tr("runtime.project.applyprojectcalculation.text_bcd462ee6a", {}, '先补证据', 'Add evidence first'),
    lower_priority: () => tr("runtime.project.applyprojectcalculation.text_703ef9302d", {}, '降低优先级', 'Lower priority'),
    blocked: () => tr("runtime.project.applyprojectcalculation.text_c6dcb59e7f", {}, '阻止判断', 'Block the conclusion'),
  };
  const CLAIM_CEILING_ZH = {
    electronic_adsorption_screen: () => tr("runtime.project.applyprojectcalculation.text_a465149454", {}, '电子吸附能初筛', 'Electronic adsorption-energy screening'),
    corrected_thermodynamics: () => tr("runtime.project.applyprojectcalculation.text_5cc5aa09e6", {}, '热校正热力学', 'Thermodynamics with thermal corrections'),
    solvated_thermodynamics: () => tr("runtime.project.applyprojectcalculation.text_c4eb050bbf", {}, '含溶剂化热力学', 'Thermodynamics with solvation'),
    kinetically_supported: () => tr("runtime.project.applyprojectcalculation.text_7f53893bc0", {}, '动力学支持', 'Kinetic support'),
  };
  const SHORT_CHAIN_RISK_ZH = {
    high: () => tr("runtime.project.applyprojectcalculation.text_7bc19ee43c", {}, '高：终产物过强结合警戒', 'High: warning for excessively strong final-product binding'),
    medium: () => tr("runtime.project.applyprojectcalculation.text_6c71bfba67", {}, '中：需检查 Li₂S 分解/脱锂', 'Medium: inspect Li₂S decomposition / delithiation'),
    weak_terminal_binding: () => tr("runtime.project.applyprojectcalculation.text_a1739a4bd3", {}, '终产物结合偏弱', 'Final-product binding is weak'),
    low: () => tr("runtime.project.applyprojectcalculation.text_596381ccb8", {}, '低', 'Low'),
    unknown: () => tr("runtime.project.applyprojectcalculation.text_d408a0a3c1", {}, '证据不足', 'Insufficient evidence'),
  };
  const candidateLabel = (labels, key) => labels[key] ? labels[key]() : key;

  function hideCandidateEvaluation() {
    const box = $('pj-candidate-evaluation');
    if (!box) return;
    box.hidden = true;
    box.innerHTML = '';
    box.className = 'pj-candidate-card';
  }

  function renderCandidateEvaluation(result) {
    const box = $('pj-candidate-evaluation');
    if (!box) return;
    const evaluation = result && result.evaluation;
    if (!evaluation || typeof evaluation !== 'object') {
      box.hidden = false;
      box.className = 'pj-candidate-card error';
      box.innerHTML = tr("runtime.project.rendercandidateevaluation.text_439f5bf2d9", {}, '<div class="pj-candidate-head"><b>候选评价暂不可用</b></div>', '<div class="pj-candidate-head"><b>Candidate evaluation is temporarily unavailable</b></div>') +
        `<p>${VCS.esc((result && result.error) || tr(
          'runtime.project.rendercandidateevaluation.no_auditable_result', {},
          '后端没有返回可审计评价。', 'The backend returned no auditable evaluation.'))}</p>`;
      return;
    }
    const decision = evaluation.decision || {};
    const evidence = evaluation.evidence || {};
    const profile = evaluation.profile || {};
    const priority = Object.prototype.hasOwnProperty.call(
      CANDIDATE_PRIORITY_ZH, decision.priority) ? decision.priority : 'hold_for_evidence';
    const claim = String(decision.claim_ceiling || evidence.claim_ceiling ||
      'electronic_adsorption_screen');
    const risk = String((profile.short_chain_risk || {}).status || 'unknown');
    const recommendations = (Array.isArray(evaluation.recommendations)
      ? evaluation.recommendations : []).filter(
      item => item && typeof item === 'object').slice(0, 3);
    let html = tr("runtime.project.rendercandidateevaluation.text_4bf0ff4438", {}, `<div class="pj-candidate-head"><div><span>吸附能候选评价</span>`, '<div class="pj-candidate-head"><div><span>Adsorption-energy candidate evaluation</span>') +
      `<b>${VCS.esc(candidateLabel(CANDIDATE_PRIORITY_ZH, priority))}</b></div>` +
      tr("runtime.project.rendercandidateevaluation.text_c96dd21661", {}, '<small>Sabatier 初筛，不替代自由能与 NEB</small></div>', '<small>Sabatier screening; not a substitute for free-energy or NEB calculations</small></div>') +
      `<p class="pj-candidate-summary">${VCS.esc(decision.summary_zh || tr(
        'runtime.project.rendercandidateevaluation.insufficient_evidence', {},
        '当前证据不足。', 'Current evidence is insufficient.'))}</p>` +
      '<div class="pj-candidate-metrics">' +
      tr("runtime.project.rendercandidateevaluation.text_483a763f67", {
        value1: VCS.esc(candidateLabel(CLAIM_CEILING_ZH, claim)),
      }, `<span><small>结论上限</small><b>{value1}</b>`, '<span><small>Conclusion ceiling</small><b>{value1}</b>') +
      `<code>${VCS.esc(claim)}</code></span>` +
      tr("runtime.project.rendercandidateevaluation.text_cbc7517082", {
        value1: VCS.esc(candidateLabel(SHORT_CHAIN_RISK_ZH, risk)),
      }, `<span><small>短链风险</small><b>{value1}</b>`, '<span><small>Short-chain risk</small><b>{value1}</b>') +
      `<code>${VCS.esc(risk)}</code></span></div>`;
    if (recommendations.length) {
      html += tr("runtime.project.rendercandidateevaluation.text_aa249c39c5", {}, '<div class="pj-candidate-next"><b>建议的下一步（前 3 项）</b><ol>', '<div class="pj-candidate-next"><b>Recommended next steps (top 3)</b><ol>');
      recommendations.forEach(item => {
        const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
        const targets = (Array.isArray(item.targets) ? item.targets : []).map(String)
          .join(english ? ', ' : '、');
        html += '<li><b>' + VCS.esc(
          `${item.priority || 'P2'} · ${item.action_zh || item.code || tr(
            'runtime.project.rendercandidateevaluation.continue_validation', {},
            '继续核验', 'Continue validation')}`) +
          '</b><span>' + VCS.esc(item.reason || '') +
          (targets ? tr("runtime.project.rendercandidateevaluation.text_55acf455ef", { value1: (VCS.esc(targets)) }, `；目标：{value1}`, '; target: {value1}') : '') + '</span></li>';
      });
      html += '</ol></div>';
    }
    const disclaimer = evaluation.audit && evaluation.audit.policy_disclaimer;
    if (disclaimer) html += `<div class="pj-candidate-note">${VCS.esc(disclaimer)}</div>`;
    box.hidden = false;
    box.className = `pj-candidate-card ${priority}`;
    box.innerHTML = html;
  }

  function selectedReportFormats() {
    return SINGLE_REPORT_FORMATS
      .filter(item => {
        const input = $(item.id);
        const capability = State.reportCapabilities && State.reportCapabilities[item.value];
        return !!(input && input.checked && capability && capability.available === true);
      })
      .map(item => item.value);
  }

  function reportFormatLabel(formats) {
    const wanted = new Set(formats || []);
    return SINGLE_REPORT_FORMATS
      .filter(item => wanted.has(item.value))
      .map(item => item.label)
      .join(' / ');
  }

  function unavailableReportFormatText(items) {
    const groups = new Map();
    items.forEach(item => {
      const reason = item.reason || tr("runtime.project.unavailablereportformattext.text_892f4795ec", {}, '后端未明确报告此格式可用。', 'The backend did not explicitly report this format as available.');
      const labels = groups.get(reason) || [];
      labels.push(item.label);
      groups.set(reason, labels);
    });
    const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
    return Array.from(groups, ([reason, labels]) => tr(
      'runtime.project.report.unavailable_format_group', {
        formats: labels.join(english ? ', ' : '、'), reason,
      }, '{formats}（{reason}）', '{formats} ({reason})'))
      .join(english ? '; ' : '；');
  }

  function setReportCapabilityFailure(reason) {
    const detail = String(reason || tr("runtime.project.setreportcapabilityfailure.text_1114919714", {}, '报告格式能力检测失败。', 'Report-format capability detection failed.')).trim();
    const unavailableReason = tr("runtime.project.setreportcapabilityfailure.text_5a10b244e0", { value1: (detail) }, `{value1}；未获得可用的明确确认。`, '{value1}; no explicit confirmation of availability was obtained.');
    State.reportCapabilities = {
      html: { available: true, reason: '' },
      docx: { available: false, reason: unavailableReason },
      pdf: { available: false, reason: unavailableReason },
    };
    State.reportCapabilityState = 'failed';
    syncReportFormatControls();
  }

  function applyReportCapabilities(formats) {
    const normalized = { html: { available: true, reason: '' } };
    ['docx', 'pdf'].forEach(format => {
      const capability = formats && typeof formats[format] === 'object'
        ? formats[format] : null;
      const available = !!(capability && capability.available === true);
      normalized[format] = {
        available,
        reason: available ? '' : String(
          capability && capability.reason ||
          tr("runtime.project.applyreportcapabilities.text_892f4795ec", {}, '后端未明确报告此格式可用。', 'The backend did not explicitly report this format as available.')),
      };
    });
    State.reportCapabilities = normalized;
    State.reportCapabilityState = 'ready';
    syncReportFormatControls();
  }

  function syncReportFormatControls() {
    const formats = selectedReportFormats();
    const valid = formats.length > 0;
    const fieldset = $('pj-report-formats');
    const status = $('pj-report-format-status');
    const button = $('pj-report');
    const selectedProject = !!val('pj-select');
    const labels = reportFormatLabel(formats);
    const unavailable = [];
    SINGLE_REPORT_FORMATS.forEach(item => {
      const input = $(item.id);
      const capability = State.reportCapabilities && State.reportCapabilities[item.value];
      const disabledByCapability = !(capability && capability.available === true);
      const reason = disabledByCapability
        ? String(capability && capability.reason || tr("runtime.project.syncreportformatcontrols.text_892f4795ec", {}, '后端未明确报告此格式可用。', 'The backend did not explicitly report this format as available.')) : '';
      if (disabledByCapability) unavailable.push({ label: item.label, reason });
      if (input) {
        const controlDisabled = State.reportBusy || disabledByCapability;
        input.disabled = controlDisabled;
        if (disabledByCapability) input.checked = false;
        input.setAttribute('aria-disabled', controlDisabled ? 'true' : 'false');
        const label = input.closest('label');
        if (label) {
          label.classList.toggle('unavailable', disabledByCapability);
          label.classList.toggle(
            'pending', disabledByCapability && State.reportCapabilityState === 'pending');
          label.title = disabledByCapability ? reason : '';
          const description = label.querySelector('small');
          if (description) description.textContent = disabledByCapability ? reason : item.description();
        }
      }
    });
    if (fieldset) {
      fieldset.classList.toggle('invalid', !valid);
      fieldset.setAttribute('aria-invalid', valid ? 'false' : 'true');
      fieldset.setAttribute(
        'aria-busy', State.reportCapabilityState === 'pending' ? 'true' : 'false');
    }
    if (status) {
      const unavailableText = unavailableReportFormatText(unavailable);
      const unavailablePrefix = State.reportCapabilityState === 'pending'
        ? tr("runtime.project.syncreportformatcontrols.text_c5d534da64", {}, '能力检测中', 'Detecting capability') : tr("runtime.project.syncreportformatcontrols.text_2bf37fbe50", {}, '当前不可用', 'Currently unavailable');
      status.classList.toggle('bad', !valid);
      status.textContent = valid
        ? tr("runtime.project.syncreportformatcontrols.text_c1358558fd", { value1: (labels.split(' / ').join('、')) }, `已选择：{value1}`, 'Selected: {value1}') +
          (unavailable.length ? `；${unavailablePrefix}：${unavailableText}` : '')
        : tr("runtime.project.syncreportformatcontrols.text_322b55db53", {}, '请至少选择一种报告格式。', 'Select at least one report format.') +
          (unavailable.length ? ` ${unavailablePrefix}：${unavailableText}` : '');
    }
    if (button) {
      const prefix = State.reportDiagnostic ? tr("runtime.project.syncreportformatcontrols.text_a73e7ab0fe", {}, '在工作台配置诊断报告', 'Configure diagnostic report in the workbench') : tr("runtime.project.syncreportformatcontrols.text_a92bdc8948", {}, '打开报告工作台', 'Open Report Workbench');
      button.textContent = valid
        ? tr('runtime.project.report.selected_formats', { prefix, formats: labels },
          '{prefix}（{formats}）', '{prefix} ({formats})')
        : tr("runtime.project.syncreportformatcontrols.text_9abe43dfb0", { value1: (prefix) }, `{value1}（请选择格式）`, '{value1} (select a format)');
      button.disabled = State.reportBusy || !selectedProject || !valid;
    }
    return formats;
  }

  async function loadReportCapabilities() {
    const generation = ++State.reportCapabilityGeneration;
    State.reportCapabilities = initialReportCapabilities();
    State.reportCapabilityState = 'pending';
    syncReportFormatControls();
    try {
      const result = await VCS.call('proj_report_capabilities');
      if (generation !== State.reportCapabilityGeneration) return;
      if (bridgeMethodUnavailable(result)) {
        setReportCapabilityFailure(
          result && result.error || tr("runtime.project.loadreportcapabilities.text_ae789b9f8d", {}, '当前后端未提供报告格式能力检测。', 'The current backend does not provide report-format capability detection.'));
        return;
      }
      if (!result || result.ok === false) {
        setReportCapabilityFailure(result && result.error || tr("runtime.project.loadreportcapabilities.text_1114919714", {}, '报告格式能力检测失败。', 'Report-format capability detection failed.'));
        return;
      }
      if (!result.formats || typeof result.formats !== 'object') {
        setReportCapabilityFailure(tr("runtime.project.loadreportcapabilities.text_c980c806d4", {}, '能力检测未返回格式清单。', 'Capability detection returned no format list.'));
        return;
      }
      applyReportCapabilities(result.formats);
    } catch (error) {
      if (generation !== State.reportCapabilityGeneration) return;
      setReportCapabilityFailure(
        error && error.message || tr("runtime.project.loadreportcapabilities.text_9bad8763ac", {}, '报告格式能力检测调用失败。', 'The report-format capability request failed.'));
    }
  }

  function requireReportFormats() {
    const formats = syncReportFormatControls();
    if (formats.length) return formats;
    VCS.log(tr("runtime.project.requirereportformats.text_b4e051bf74", {}, '生成报告前请至少选择一种格式（HTML、DOCX 或 PDF）。', 'Select at least one format (HTML, DOCX, or PDF) before generating a report.'), 'failc');
    VCS.toast(tr("runtime.project.requirereportformats.text_7d6a817607", {}, '请至少选择一种报告格式', 'Select at least one report format'), 'fail');
    const first = $(SINGLE_REPORT_FORMATS[0].id);
    if (first && typeof first.focus === 'function') first.focus();
    return null;
  }

  async function refreshCandidateEvaluation(projectIdValue) {
    const generation = ++State.candidateEvaluationGeneration;
    const wanted = String(projectIdValue || '');
    if (!wanted) {
      State.candidateEvaluation = null;
      hideCandidateEvaluation();
      return;
    }
    // 切换项目时先移除旧结论，避免慢请求把上一项目的分级暂时留在新项目名下。
    hideCandidateEvaluation();
    const result = await VCS.call('proj_evaluate_candidate', wanted);
    if (generation !== State.candidateEvaluationGeneration) return;
    if (!result || bridgeMethodUnavailable(result)) {
      State.candidateEvaluation = null;
      hideCandidateEvaluation();
      return;
    }
    State.candidateEvaluation = result;
    renderCandidateEvaluation(result);
  }

  function updateProjectSummary(deltaResult) {
    const box = $('pj-project-summary');
    const sel = $('pj-select');
    const id = sel ? sel.value : '';
    const project = State.projects.find(p => projectId(p) === id);
    const deltaButton = $('pj-delta');
    const reportButton = $('pj-report');
    const csvButton = $('pj-csv');
    if (!box) return;
    if (!project) {
      box.innerHTML = tr("runtime.project.updateprojectsummary.text_f514e76994", {}, '<b>还没有可查看的项目</b><span>下一步：先导入已算结果，或用参考能开始新的吸附计算。</span>', '<b>No project is available to view</b><span>Next: import completed results, or start a new adsorption calculation with reference energies.</span>');
      [deltaButton, reportButton, csvButton].forEach(button => { if (button) button.disabled = true; });
      State.reportDiagnostic = false;
      syncReportFormatControls();
      return;
    }
    [deltaButton, reportButton, csvButton].forEach(button => { if (button) button.disabled = false; });
    const total = Number(project.n_members || project.members_total || project.total_members || 0);
    const rawDone = project.n_done != null ? project.n_done
      : project.done_members != null ? project.done_members : project.members_done;
    const done = rawDone == null ? null : Number(rawDone);
    const refs = referenceSpecies(project);
    const referenceMode = String(project.reference_mode || '').toLowerCase();
    const refText = refs.length
      ? tr("runtime.project.updateprojectsummary.text_5285830c2a", { value1: (refs.length), value2: (refs.join('、')) }, `逐物种参考 {value1} 个：{value2}`, '{value1} per-species references: {value2}')
      : ['species', 'species_refs'].includes(referenceMode) ? tr("runtime.project.updateprojectsummary.text_0c3f09a792", {}, '逐物种参考（具体物种见 ΔE 表）', 'Per-species references (see the ΔE table for species)')
        : ['single', 'gas_ref'].includes(referenceMode) ? tr("runtime.project.updateprojectsummary.text_2141611425", {}, '统一气相参考', 'Unified gas-phase reference') : tr("runtime.project.updateprojectsummary.text_1a7d5a3e1b", {}, '参考模式待 ΔE 检查确认', 'Reference mode awaits ΔE validation');
    const rows = deltaResult && (deltaResult.rows || []);
    const methodBlocked = String(deltaResult && deltaResult.method_consistency &&
      deltaResult.method_consistency.status || '').toLowerCase() === 'incompatible';
    const backendFinal = deltaResult && deltaResult.final_report_eligible;
    const complete = !!(rows && rows.length && rows.every(row => row.delta_e != null) &&
      !methodBlocked && backendFinal === true);
    const missing = rows ? rows.filter(row => row.delta_e == null).length : null;
    const pipelineStage = String(project.pipeline_stage || project.autopilot_stage || '').trim();
    let stage;
    let next;
    if (methodBlocked) {
      stage = tr("runtime.project.updateprojectsummary.text_202ca8b170", {}, '方法不一致，ΔE 已阻断', 'Method mismatch; ΔE blocked');
      next = tr("runtime.project.updateprojectsummary.text_6f1d108025", {}, '下一步：统一泛函、ENCUT、色散和 POTCAR 后重算。', 'Next: align the functional, ENCUT, dispersion, and POTCAR, then recalculate.');
    } else if (complete) {
      stage = tr("runtime.project.updateprojectsummary.text_78a04f189e", {}, 'ΔE 已完整', 'ΔE is complete'); next = tr("runtime.project.updateprojectsummary.text_b678f7c02f", {}, '下一步：后台会自动生成 HTML、Word 与 PDF，也可立即手动生成。', 'Next: HTML, Word, and PDF will be generated in the background, or you can generate them now.');
    } else if (rows && rows.length && rows.every(row => row.delta_e != null) && backendFinal === false) {
      stage = tr("runtime.project.updateprojectsummary.text_2cd4b30065", {}, 'ΔE 可预览，最终报告仍被门禁阻止', 'ΔE preview is available, but the final report is still blocked by the gate');
      next = tr("runtime.project.updateprojectsummary.text_dcc99113a2", {
        value1: deltaResult.final_report_reason || tr(
          'runtime.project.updateprojectsummary.complete_reference_and_method', {},
          '补齐参考态与方法确认。', 'Complete the reference-state and method confirmations.'),
      }, `下一步：{value1}`, 'Next: {value1}');
    } else if (missing != null && missing > 0) {
      stage = tr("runtime.project.updateprojectsummary.text_6447ef3714", { value1: (missing) }, `{value1} 个构型尚缺可靠 ΔE`, '{value1} configurations still lack reliable ΔE');
      next = tr("runtime.project.updateprojectsummary.text_79bd6b9f2d", {}, '下一步：保持软件运行等待自动下载/续算，完成后点“刷新并计算 ΔE”。', 'Next: keep the application running for automatic download/continuation; when complete, select Refresh and calculate ΔE.');
    } else if (done != null && total && done < total) {
      stage = tr("runtime.project.updateprojectsummary.text_649f897b50", { value1: (done), value2: (total) }, `自动运行中 {value1}/{value2}`, 'Automatic execution {value1}/{value2}');
      next = tr("runtime.project.updateprojectsummary.text_9374d1b15a", {}, '下一步：保持软件运行；任务完成后回来刷新 ΔE。', 'Next: keep the application running; return and refresh ΔE after the tasks complete.');
    } else if (!total && refs.length) {
      stage = tr("runtime.project.updateprojectsummary.text_8ba08115c9", {}, '参考能库已就绪', 'Reference-energy library ready'); next = tr("runtime.project.updateprojectsummary.text_bb26b8d3a6", {}, '下一步：用这些参考能开始新的 slab / adsorption 计算。', 'Next: use these reference energies for a new slab / adsorption calculation.');
    } else if (pipelineStage === 'submit') {
      stage = tr("runtime.project.updateprojectsummary.text_b4b81e5b9d", {}, '作业已生成，等待提交', 'Jobs generated and awaiting submission');
      next = tr("runtime.project.updateprojectsummary.text_343a3df991", {}, '下一步：到任务页提交尚未上传的成员；此时自动监控尚未开始。', 'Next: submit members that have not been uploaded from Jobs; automatic monitoring has not started yet.');
    } else if (pipelineStage) {
      stage = tr("runtime.project.updateprojectsummary.text_fde78e08e2", { value1: (pipelineStage) }, `管线阶段：{value1}`, 'Pipeline stage: {value1}');
      next = tr("runtime.project.updateprojectsummary.text_04e3993d4a", {}, '下一步：保持软件运行；任务完成后点“刷新并计算 ΔE”。', 'Next: keep the application running; when tasks complete, select Refresh and calculate ΔE.');
    } else {
      stage = State.workflowSubmitted ? tr("runtime.project.updateprojectsummary.text_9786ecf452", {}, '自动托管运行中', 'Autopilot running') : tr("runtime.project.updateprojectsummary.text_db3a5192cd", {}, '等待结果检查', 'Waiting for result validation');
      next = tr("runtime.project.updateprojectsummary.text_b95529f1af", {}, '下一步：点击“刷新并计算 ΔE”，软件会明确列出仍缺少的结果。', 'Next: select Refresh and calculate ΔE; the application will list every result that is still missing.');
    }
    const progressText = total
      ? tr('runtime.project.summary.member_progress', {
        done: done == null ? '?' : done, total,
      }, '成员 {done}/{total}', 'Members {done}/{total}')
      : tr('runtime.project.summary.reference_project', {}, '参考项目', 'Reference project');
    box.innerHTML = `<div class="pj-summary-chips"><span>${VCS.esc(progressText)}</span>` +
      `<span>${VCS.esc(refText)}</span><span>${VCS.esc(stage)}</span></div><b>${VCS.esc(next)}</b>`;
    if (deltaButton) deltaButton.classList.toggle('primary', !complete);
    if (reportButton) reportButton.classList.toggle('primary', complete);
    State.reportDiagnostic = !complete;
    syncReportFormatControls();
  }

  function currentProject() {
    const sel = $('pj-select');
    const id = sel ? sel.value : '';
    if (!id) { VCS.log(tr("runtime.project.currentproject.text_27420ded1e", {}, '请先选择一个项目', 'Select a project first'), 'failc'); return null; }
    return State.projects.find(p => projectId(p) === id) || null;
  }

  // ── 计算 ΔE:proj_delta → 表格渲染(缺员门控原样展示,绝不编数) ──────────
  async function delta() {
    const proj = currentProject();
    if (!proj) return null;
    const box = $('pj-table');
    if (box) box.innerHTML = '';
    VCS.log(tr("runtime.project.delta.text_b9609f43e7", {}, '计算项目「', 'Calculating ΔE for project “') + (proj.name || '') + tr("runtime.project.delta.text_0d02fca122", {}, '」的 ΔE…', '”…'));
    const r = await VCS.call('proj_delta', projectId(proj));
    if (!r || r.ok === false || r.error) {
      VCS.log(tr("runtime.project.delta.text_bebc908a4e", {}, '计算 ΔE 失败:', 'ΔE calculation failed:') + ((r && r.error) || tr("runtime.project.delta.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
      updateProjectSummary();
      return null;
    }
    State.deltaResult = r;
    renderDelta(r);
    refreshCandidateEvaluation(projectId(proj));
    const rows = r.rows || [];
    const methodBlocked = String(r.method_consistency && r.method_consistency.status || '')
      .toLowerCase() === 'incompatible';
    State.workflowAnalysisReady = !!(!methodBlocked && rows.length &&
      rows.every(row => row.delta_e != null) && r.final_report_eligible === true);
    // ΔE 通过门禁只代表可以生成报告；只有后端 report_done 标记才表示
    // 最终报告已经真正写出并与当前结果哈希绑定。
    State.workflowResultReady = State.workflowStage === 'report_done';
    updateProjectSummary(r);
    updateJourney();
    if (State.workflowAnalysisReady && !State.workflowResultReady) {
      VCS.toast(tr("runtime.project.delta.text_5fbd22e8e9", {}, 'ΔE 已完整；下一步生成完整报告', 'ΔE is complete; generate the full report next'));
    }
    return r;
  }

  function fmt(x, digits) {
    return (typeof x === 'number' && isFinite(x)) ? x.toFixed(digits) : '—';
  }

  function deltaRepair(row) {
    const note = String(row && row.note || '');
    if (row && row.delta_e != null) return '';
    const methodCheck = row && row.method_check || {};
    if (methodCheck.status === 'incompatible' || /方法不一致/.test(note)) {
      return tr("runtime.project.deltarepair.text_4a2b80130f", {}, '下一步：按方法检查列出的泛函、ENCUT、色散、共享元素 POTCAR/DFT+U 等硬冲突逐项修正后重算。合法的 ISPIN 差异本身不会触发此阻断。', 'Next: resolve each blocking conflict listed by the method check—functional, ENCUT, dispersion, shared-element POTCAR/DFT+U, and so on—then recalculate. A valid ISPIN difference alone does not trigger this block.');
    }
    if (methodCheck.status === 'unverified') {
      return tr("runtime.project.deltarepair.text_29614719ba", {}, '下一步：按方法检查逐项核对；分子参考、clean slab 与吸附构型可采用各自正确的基态自旋，自旋差异本身只作提示。', 'Next: review each method-check item. Molecular references, the clean slab, and adsorption configurations may use their own correct ground-state spins; spin differences alone are advisory.');
    }
    if (/构型未完成|清洁表面未完成|未完成/.test(note)) {
      return tr("runtime.project.deltarepair.text_64af0891f4", {}, '下一步：保持软件运行等待自动续算/下载；任务 DONE 后重新计算 ΔE。', 'Next: keep the application running for automatic continuation/download; recalculate ΔE after the task is DONE.');
    }
    if (/无匹配物种参考|参考能量/.test(note)) {
      return tr("runtime.project.deltarepair.text_76c53399d6", {}, '下一步：确认该构型的物种映射，并导入同名 Li-S 参考能。', 'Next: confirm the species mapping for this configuration and import the matching Li-S reference energy.');
    }
    if (/能量缺失|不合理/.test(note)) {
      return tr("runtime.project.deltarepair.text_2802c02350", {}, '下一步：拉回 OUTCAR / OSZICAR，若仍无可靠 E0 则修复或续算该成员。', 'Next: retrieve OUTCAR / OSZICAR; if reliable E0 is still unavailable, repair or continue that member.');
    }
    return tr("runtime.project.deltarepair.text_3ddf57fb7a", {}, '下一步：查看备注中的缺项，修正后点“刷新并计算 ΔE”。', 'Next: review the missing items in the notes, correct them, then select Refresh and calculate ΔE.');
  }

  function renderDelta(r) {
    const box = $('pj-table');
    if (!box) return;
    const rows = r.rows || [];
    const note = r.note || '';
    let h = note ? `<div class="pj-note">${VCS.esc(note)}</div>` : '';
    const method = r.method_consistency || {};
    const methodStatus = String(method.status || '').toLowerCase();
    const methodIssues = (method.issues || []).map(String);
    const methodWarnings = (method.warnings || []).map(String);
    const methodAdvisories = (method.advisories || []).map(String);
    if (methodStatus === 'incompatible') {
      h += tr("runtime.project.renderdelta.text_fc3e9fc6b4", {}, '<div class="pj-method-gate incompatible"><b>方法不一致：ΔE 已阻断</b>', '<div class="pj-method-gate incompatible"><b>Method mismatch: ΔE blocked</b>') +
        tr("runtime.project.renderdelta.text_21c2919912", {}, '<span>请按下列泛函、ENCUT、色散、共享元素 POTCAR/DFT+U 等硬冲突逐项修正后重新计算。合法的 ISPIN 差异不属于硬冲突。</span>', '<span>Resolve each blocking conflict below—functional, ENCUT, dispersion, shared-element POTCAR/DFT+U, and so on—then recalculate. Valid ISPIN differences are not blocking conflicts.</span>') +
        (methodIssues.length ? `<ul>${methodIssues.map(x => `<li>${VCS.esc(x)}</li>`).join('')}</ul>` : '') +
        '</div>';
    } else if (methodStatus === 'unverified') {
      h += tr("runtime.project.renderdelta.text_a9970cd3e2", {}, '<div class="pj-method-gate unverified"><b>方法一致性尚未完全核验</b>', '<div class="pj-method-gate unverified"><b>Method consistency is not fully verified</b>') +
        tr("runtime.project.renderdelta.text_6c7afbe712", {}, '<span>请按下列具体项目逐项核对；分子参考、clean slab 与吸附构型可采用各自正确的基态自旋，自旋差异本身只作提示。</span>', '<span>Review each item below. Molecular references, the clean slab, and adsorption configurations may use their own correct ground-state spins; spin differences alone are advisory.</span>') +
        (methodWarnings.length ? `<ul>${methodWarnings.map(x => `<li>${VCS.esc(x)}</li>`).join('')}</ul>` : '') +
        '</div>';
    } else if (methodStatus === 'verified') {
      h += tr("runtime.project.renderdelta.text_ff5287fde8", {}, '<div class="pj-method-gate verified"><b>方法一致性已核验</b>', '<div class="pj-method-gate verified"><b>Method consistency verified</b>') +
        tr("runtime.project.renderdelta.text_a5c91c27ee", {}, '<span>本项目能量相减项已通过记录层面的一致性检查。</span></div>', '<span>The energy terms subtracted in this project passed record-level consistency checks.</span></div>');
    }
    if (methodAdvisories.length) {
      h += tr("runtime.project.renderdelta.text_c3fe9dada3", {}, '<div class="pj-method-gate advisory"><b>体系自旋提示（不阻断 ΔE）</b>', '<div class="pj-method-gate advisory"><b>System-spin note (does not block ΔE)</b>') +
        tr("runtime.project.renderdelta.text_2b1eea08b1", {}, '<span>分子参考与周期体系 ISPIN 不同可以是合理的基态设置；分子、clean slab 与吸附体系可分别采用各自经验证的基态自旋；以下内容仅用于审计与复核。</span>', '<span>Different ISPIN values for molecular references and periodic systems may be valid ground-state settings. Molecules, the clean slab, and adsorption systems may each use their independently validated ground-state spin; the following information is for audit and review only.</span>') +
        `<ul>${methodAdvisories.map(x => `<li>${VCS.esc(x)}</li>`).join('')}</ul></div>`;
    }
    if (!rows.length) {
      h += tr("runtime.project.renderdelta.text_d7da2ed3d4", {}, '<div class="empty"><p>该项目暂无吸附构型成员</p></div>', '<div class="empty"><p>This project has no adsorption-configuration members</p></div>');
      box.innerHTML = h;
      return;
    }
    const referenceMode = String(r.reference_mode || '').toLowerCase();
    h += '<table><thead><tr>' +
      tr("runtime.project.renderdelta.text_ac3a1c1ce9", {}, '<th>构型</th><th>状态</th><th class="num">E_config (eV)</th>', '<th>Configuration</th><th>Status</th><th class="num">E_config (eV)</th>') +
      tr("runtime.project.renderdelta.text_84e02a4d13", {}, '<th>参考物种 / E_ref (eV)</th><th class="num">ΔE (eV)</th>', '<th>Reference species / E_ref (eV)</th><th class="num">ΔE (eV)</th>') +
      tr("runtime.project.renderdelta.text_6a385c2f9f", {}, '<th class="num">ΔΔE (eV)</th><th>组内比较</th><th>公式与备注</th>', '<th class="num">ΔΔE (eV)</th><th>Within-group comparison</th><th>Formula and notes</th>') +
      '</tr></thead><tbody>';
    rows.forEach(row => {
      const rowMode = String(row.reference_mode || referenceMode || '').toLowerCase();
      const species = row.reference_species || row.species || row.ref_species || '';
      const eRef = row.e_ref != null ? row.e_ref
        : row.reference_energy_e0_eV != null ? row.reference_energy_e0_eV : row.e_reference;
      let refLabel;
      if (['species', 'species_refs'].includes(rowMode) || species) {
        refLabel = `${species || tr('runtime.project.delta.species_reference', {},
          '逐物种参考', 'Per-species reference')} / ${fmt(eRef, 6)}`;
      } else if (['single', 'gas_ref'].includes(rowMode) || eRef != null) {
        refLabel = tr("runtime.project.renderdelta.text_b1c743dcff", { value1: (fmt(eRef, 6)) }, `统一气相参考 / {value1}`, 'Unified gas-phase reference / {value1}');
      } else {
        refLabel = tr("runtime.project.renderdelta.text_46cdc9a3db", {}, '参考信息待确认', 'Reference information awaiting confirmation');
      }
      const formula = row.formula || row.delta_formula ||
        (['species', 'species_refs'].includes(rowMode) || species
          ? `E_config − E_slab − E_ref(${species || 'species'})`
          : 'E_config − E_slab − E_ref');
      h += '<tr>' +
        `<td><span class="name">${VCS.esc(row.name)}</span></td>` +
        `<td>${VCS.pill(row.state)}</td>` +
        `<td class="num">${VCS.esc(fmt(row.e_config, 6))}</td>` +
        `<td class="mono">${VCS.esc(refLabel)}</td>` +
        `<td class="num">${row.delta_e == null ? '—' : VCS.esc(fmt(row.delta_e, 4))}</td>` +
        `<td class="num">${row.dd_e == null ? '—' : VCS.esc(fmt(row.dd_e, 4))}</td>` +
        `<td>${row.delta_e == null ? '—' : row.is_most_stable
          ? tr('runtime.project.delta.most_stable_badge', {},
            '<span class="pj-stable">最稳构型</span>',
            '<span class="pj-stable">Most stable</span>')
          : tr('runtime.project.delta.same_species_comparison', {},
            '同物种对照', 'Same-species comparison')}</td>` +
        `<td class="sub"><span class="pj-delta-formula">${VCS.esc(formula)}</span>` +
        `${VCS.esc(row.note ? ' · ' + row.note : '')}` +
        (row.delta_e == null ? `<span class="pj-delta-fix">${VCS.esc(deltaRepair(row))}</span>` : '') + '</td></tr>';
    });
    h += '</tbody></table>';
    box.innerHTML = h;
  }

  // ── 导出 CSV:pick_dir + 默认文件名 → proj_export_csv ──────────────────────
  async function exportCsv() {
    const proj = currentProject();
    if (!proj) return;
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log(tr("runtime.project.exportcsv.text_d802c7d8d3", {}, '选择目录失败:', 'Failed to select directory:') + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;   // 用户取消
    const save = joinPath(dr.path, (proj.name || 'project') + '_delta_e.csv');
    VCS.log(tr("runtime.project.exportcsv.text_ad1a1424a5", {}, '导出 ΔE 表到:', 'Exporting the ΔE table to:') + save + '…');
    const r = await VCS.call('proj_export_csv', projectId(proj), save);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr("runtime.project.exportcsv.text_37ffc76d79", {}, '导出 CSV 失败:', 'CSV export failed:') + ((r && r.error) || tr("runtime.project.exportcsv.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
      return;
    }
    VCS.log(tr("runtime.project.exportcsv.text_a3f7b7abdd", {}, '已导出 CSV:', 'CSV exported:') + (r.file || save), 'okc');
    VCS.call('open_dir', r.file || save);       // 输出反馈统一:打开所在目录
    VCS.toast(tr("runtime.project.exportcsv.text_bccd5bb6de", {}, '已导出 CSV', 'CSV exported'));
  }

  function bridgeMethodUnavailable(result) {
    const message = String(result && result.error || '');
    return /(桥方法不存在|method not found|unknown method|has no attribute)/i.test(message);
  }

  function reportScienceState(result) {
    const raw = String(result && (
      result.scientific_status || result.report_status || result.report_kind || result.kind
    ) || 'pending').trim().toLowerCase();
    if (raw === 'final') return { key: 'final', label: tr("runtime.project.reportsciencestate.text_04cbc58d7a", {}, '最终', 'Final') };
    if (raw === 'diagnostic') return { key: 'diagnostic', label: tr("runtime.project.reportsciencestate.text_bb9d7d9b21", {}, '诊断', 'Diagnostic') };
    if (raw === 'draft') return { key: 'draft', label: tr("runtime.project.reportsciencestate.text_f6afc42806", {}, '草稿', 'Draft') };
    if (raw === 'blocked') return { key: 'blocked', label: tr("runtime.project.reportsciencestate.text_2378769b47", {}, '阻断', 'Blocked') };
    return { key: 'pending', label: tr("runtime.project.reportsciencestate.text_4e1c2e9f82", {}, '待判定', 'Undetermined') };
  }

  function reportStateMarkup(result, files) {
    const science = reportScienceState(result);
    const artifactRaw = String(result && result.artifact_status || '').trim().toLowerCase();
    const generated = files.length > 0;
    const productKey = artifactRaw === 'stale' || artifactRaw === 'generated_unrecorded'
      ? 'stale'
      : artifactRaw === 'failed' || (result && result.error && !generated)
        ? 'failed'
        : generated ? 'ready' : 'pending';
    const productLabel = artifactRaw === 'generated_unrecorded'
      ? tr("runtime.project.reportstatemarkup.text_d80d825de3", { value1: (files.length) }, `已生成 {value1} 个文件但未登记`, 'Generated {value1} files but did not register them')
      : artifactRaw === 'stale'
        ? tr("runtime.project.reportstatemarkup.text_9ddd9ddc4f", {}, '产物已过期', 'Artifacts are stale')
        : generated
          ? tr("runtime.project.reportstatemarkup.text_c2a1fa04ac", { value1: (files.length) }, `已生成 {value1} 个文件`, 'Generated {value1} files')
          : (productKey === 'failed' ? tr("runtime.project.reportstatemarkup.text_9e378de73a", {}, '生成失败', 'Generation failed') : tr("runtime.project.reportstatemarkup.text_a52f4f1196", {}, '尚未生成', 'Not generated yet'));
    const reason = String(result && (result.gate_reason || result.report_reason) || '');
    const gateRaw = String(result && result.publication_gate_status || 'unknown').toLowerCase();
    const gate = gateRaw === 'eligible'
      ? { key: 'eligible', label: tr("runtime.project.reportstatemarkup.text_7997228f36", {}, '可发布最终版', 'Final version may be published') }
      : gateRaw === 'blocked'
        ? { key: 'blocked', label: tr("runtime.project.reportstatemarkup.text_2378769b47", {}, '阻断', 'Blocked') }
        : gateRaw === 'pending'
          ? { key: 'pending', label: tr("runtime.project.reportstatemarkup.text_887402ac81", {}, '等待计算', 'Waiting for calculations') }
          : { key: 'pending', label: tr("runtime.project.reportstatemarkup.text_8d3451355b", {}, '未知', 'Unknown') };
    return tr("runtime.project.reportstatemarkup.text_56710da0c0", {}, '<div class="pj-report-states" role="status" aria-label="报告产物状态、科学状态与发布门禁">', '<div class="pj-report-states" role="status" aria-label="Report artifact status, scientific state, and publication gate">') +
      tr("runtime.project.reportstatemarkup.text_29c40f9e3e", { value1: (productKey), value2: (VCS.esc(productLabel)) }, `<span class="pj-report-state product {value1}"><b>报告产物</b>{value2}</span>`, '<span class="pj-report-state product {value1}"><b>Report artifact</b>{value2}</span>') +
      `<span class="pj-report-state science ${science.key}"${reason ? ` title="${VCS.esc(reason)}"` : ''}>` +
      tr("runtime.project.reportstatemarkup.text_14347481f4", { value1: (VCS.esc(science.label)) }, `<b>科学状态</b>{value1}</span>`, '<b>Scientific state</b>{value1}</span>') +
      `<span class="pj-report-state gate ${gate.key}"${reason ? ` title="${VCS.esc(reason)}"` : ''}>` +
      tr("runtime.project.reportstatemarkup.text_24dd797a82", { value1: (VCS.esc(gate.label)) }, `<b>发布门禁</b>{value1}</span></div>`, '<b>Publication gate</b>{value1}</span></div>');
  }

  function collectReportFiles(value) {
    const found = [];
    const formatNames = { html: 'HTML', docx: 'Word', pdf: 'PDF' };
    const structural = new Set(['files', 'individual']);
    function visit(item, trail) {
      if (typeof item === 'string') {
        const match = item.match(/\.([^.\\/]+)$/);
        if (!match || !['html', 'htm', 'docx', 'pdf'].includes(match[1].toLowerCase())) return;
        const extension = match[1].toLowerCase();
        const format = extension === 'docx' ? 'Word' : extension === 'pdf' ? 'PDF' : 'HTML';
        const labels = trail[trail.length - 1] === format ? trail : [...trail, format];
        found.push({ path: item, label: labels.filter(Boolean).join(' · ') });
        return;
      }
      if (Array.isArray(item)) {
        item.forEach(child => {
          const name = child && typeof child === 'object' ? child.name : '';
          visit(child, name ? [...trail, String(name)] : trail);
        });
        return;
      }
      if (!item || typeof item !== 'object') return;
      Object.entries(item).forEach(([key, child]) => {
        if (['error', 'kind', 'ok', 'path', 'name'].includes(key)) return;
        let next = trail;
        if (key === 'comparison') next = [...trail, tr("runtime.project.visit.text_6a5ec4b18c", {}, '批次比较', 'Batch comparison')];
        else if (formatNames[key]) next = [...trail, formatNames[key]];
        else if (!structural.has(key)) next = [...trail, key];
        visit(child, next);
      });
    }
    visit(value, []);
    const seen = new Set();
    return found.filter(file => {
      if (!file.path || seen.has(file.path)) return false;
      seen.add(file.path);
      return true;
    });
  }

  function renderReportFiles(containerId, result, heading) {
    const box = $(containerId);
    if (!box) return;
    const files = collectReportFiles((result && result.files) ||
      (result && result.file ? { html: result.file } : {}));
    const stateMarkup = reportStateMarkup(result, files);
    if (!files.length) {
      box.innerHTML = stateMarkup + (result && result.error
        ? `<div class="pj-report-note bad">${VCS.esc(result.error)}</div>` : '');
      return;
    }
    const diagnostic = reportScienceState(result).key === 'diagnostic';
    let html = '<div class="pj-report-head"><b>' + VCS.esc(heading || tr("runtime.project.renderreportfiles.text_e60fbba2c4", {}, '报告文件', 'Report files')) + '</b>' +
      tr("runtime.project.renderreportfiles.text_a723b26c0d", { value1: (files.length), value2: (stateMarkup) }, `<span>{value1} 个文件</span></div>{value2}`, '<span>{value1} files</span></div>{value2}') +
      '<div class="pj-report-links">';
    files.forEach(file => {
      const name = pathBase(file.path);
      html += `<button type="button" class="btn quiet pj-report-file" data-report-open="${VCS.esc(file.path)}">` +
        `<b>${VCS.esc(file.label || name)}</b><small>${VCS.esc(name)}</small></button>`;
    });
    html += '</div>';
    if (diagnostic) {
      html += tr("runtime.project.renderreportfiles.text_91bd933d77", {}, '<div class="pj-report-note">当前生成的是诊断报告：保留真实结果与阻断原因，不冒充最终结论。</div>', '<div class="pj-report-note">This is a diagnostic report: it preserves real results and blocking reasons without presenting them as final conclusions.</div>');
    }
    const gateReason = String(result && (result.gate_reason || result.report_reason) || '');
    if (gateReason) {
      html += tr("runtime.project.renderreportfiles.text_ed21cc6337", { value1: (VCS.esc(gateReason)) }, `<div class="pj-report-note">发布门禁：{value1}</div>`, '<div class="pj-report-note">Publication gate: {value1}</div>');
    }
    box.innerHTML = html;
    box.querySelectorAll('[data-report-open]').forEach(button => {
      button.addEventListener('click', () => VCS.call('open_dir', button.dataset.reportOpen));
    });
  }

  async function legacyBatchReports(ids, outDir) {
    const individual = [];
    const failures = [];
    for (const [index, id] of ids.entries()) {
      const project = State.projects.find(item => projectId(item) === id);
      const name = String(project && project.name || `project_${index + 1}`);
      const safeName = name.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').trim() || `project_${index + 1}`;
      const save = joinPath(outDir, tr("runtime.project.legacybatchreports.text_fb7a064363", { value1: (String(index + 1).padStart(2, '0')), value2: (safeName) }, `{value1}_{value2}_完整报告.html`, '{value1}_{value2}_full_report.html'));
      const result = await VCS.call('proj_report', id, save, true);
      if (result && result.ok !== false && !result.error) {
        const returnedFiles = result.files && typeof result.files === 'object' &&
          !Array.isArray(result.files) ? result.files : {};
        individual.push(Object.assign({}, result, {
          name,
          files: Object.assign({}, returnedFiles, { html: result.file || returnedFiles.html || save }),
          ok: true,
        }));
      } else {
        failures.push(`${name}: ${(result && result.error) || tr(
          'runtime.project.common.unknown_error', {}, '未知错误', 'Unknown error')}`);
      }
    }
    return {
      ok: individual.length > 0,
      kind: 'diagnostic',
      files: { individual },
      warnings: [
        tr("runtime.project.legacybatchreports.text_169594dc7d", {}, '当前后端仅支持逐项目 HTML；没有生成跨项目比较报告或 Word/PDF。', 'The current backend supports only per-project HTML; it did not generate a cross-project comparison report or Word/PDF files.'),
        ...failures,
      ],
      out_dir: outDir,
      error: individual.length ? null : failures.join('；') || tr("runtime.project.legacybatchreports.text_5665d911e3", {}, '旧版批次报告生成失败', 'Legacy batch-report generation failed'),
    };
  }

  // ── Phase C 旧入口适配器 ──────────────────────────────────────────────────
  // 项目页按钮只负责把当前稳定项目身份与用户已选展示格式交给统一工作台。
  // 旧 report()/batchReport()/draftReady() 保留给兼容层和回归测试，但不再由
  // 页面按钮直接调用，避免前端继续分叉快照、门禁与 revision 语义。
  async function openReportWorkbench(mode) {
    const proj = currentProject();
    if (!proj) {
      VCS.toast(tr("runtime.project.openreportworkbench.text_3447494da6", {}, '请先选择项目', 'Select a project first'), 'fail');
      return { ok: false, missingProject: true };
    }
    const id = projectId(proj);
    if (!id) {
      VCS.toast(tr("runtime.project.openreportworkbench.text_ecd8621204", {}, '当前项目缺少稳定项目 ID，无法打开报告工作台', 'The current project lacks a stable project ID, so Report Workbench cannot be opened'), 'fail');
      return { ok: false, missingProjectId: true };
    }
    const kind = String(mode || 'report');
    const route = kind === 'draftpack' ? 'publish-draftpack' : 'publish-report';
    const compareIds = kind === 'comparison' ? selectedCompareProjectIds() : [];
    const intent = {
      mode: kind,
      projectId: id,
      formats: selectedReportFormats(),
      comparisonProjectIds: compareIds,
      source: `project-${kind}`,
    };
    if (window.ReportWorkbench && typeof window.ReportWorkbench.open === 'function') {
      return window.ReportWorkbench.open(intent);
    }
    if (VCS.workspace && typeof VCS.workspace.navigateRoute === 'function') {
      return VCS.workspace.navigateRoute(route, {
        projectId: id,
        query: { project: id },
        source: intent.source,
      });
    }
    return VCS.navigate('project', { source: intent.source });
  }

  // ── 单项目报告:同一快照生成 HTML + Word + PDF；旧后端回退 HTML ───────────
  async function report() {
    const proj = currentProject();
    if (!proj) return;
    const selectedFormats = requireReportFormats();
    if (!selectedFormats) return;
    const selectedLabel = reportFormatLabel(selectedFormats);
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log(tr("runtime.project.report.text_d802c7d8d3", {}, '选择目录失败:', 'Failed to select directory:') + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;   // 用户取消
    State.reportBusy = true;
    syncReportFormatControls();
    const output = $('pj-report-files');
    if (output) output.innerHTML = '';
    VCS.log(tr("runtime.project.report.text_70579408f2", { value1: (selectedLabel) }, `正在从同一份数据快照生成 {value1} 报告，可能需要几分钟…`, 'Generating {value1} reports from the same data snapshot; this may take several minutes…'));
    try {
      let r = await VCS.call(
        'proj_report_bundle', projectId(proj), dr.path, selectedFormats, true);
      if (bridgeMethodUnavailable(r)) {
        if (selectedFormats.length !== 1 || selectedFormats[0] !== 'html') {
          r = {
            ok: false,
            artifact_status: 'failed',
            kind: null,
            scientific_status: null,
            error: tr("runtime.project.report.text_56c4158b77", {}, '当前后端仅支持 HTML；请只勾选 HTML 后重试，或升级后端以导出 DOCX/PDF。', 'The current backend supports HTML only. Select only HTML and try again, or upgrade the backend to export DOCX/PDF.'),
          };
          VCS.log(r.error, 'failc');
          renderReportFiles('pj-report-files', r, tr("runtime.project.report.text_62d9d73bc6", {}, '单项目报告', 'Single-project report'));
          return;
        }
        const save = joinPath(dr.path, (proj.name || 'project') + tr("runtime.project.report.text_b428d9b3ca", {}, '_完整报告.html', '_full_report.html'));
        const legacy = await VCS.call('proj_report', projectId(proj), save, true);
        r = legacy && !legacy.error
          ? Object.assign({}, legacy, { files: { html: legacy.file || save } })
          : legacy;
        VCS.log(tr("runtime.project.report.text_d775d80fa0", {}, '当前后端仅支持 HTML，已使用兼容模式生成。', 'The current backend supports HTML only; compatibility mode was used.'), 'warnc');
      }
      if (!r || r.ok === false || r.error) {
        VCS.log(tr("runtime.project.report.text_74a017ddde", {}, '生成完整报告失败:', 'Full-report generation failed:') + ((r && r.error) || tr("runtime.project.report.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
        renderReportFiles('pj-report-files', r, tr("runtime.project.report.text_62d9d73bc6", {}, '单项目报告', 'Single-project report'));
        return;
      }
      collectReportFiles(r.files || { html: r.file }).forEach(file => {
        VCS.log(tr("runtime.project.report.text_d0462cce17", {}, '报告已生成:', 'Report generated:') + file.path, 'okc');
      });
      (r.warnings || []).forEach(warning => VCS.log(tr("runtime.project.report.text_4a4ddae974", {}, '报告提示:', 'Report note:') + warning, 'warnc'));
      if (r.kind === 'diagnostic') {
        VCS.log(tr("runtime.project.report.text_7dd893daee", {}, '最终报告门禁未通过，已生成带阻断原因的诊断报告。', 'The final-report gate did not pass, so a diagnostic report containing the blocking reasons was generated.'), 'warnc');
      }
      renderReportFiles('pj-report-files', r, tr("runtime.project.report.text_62d9d73bc6", {}, '单项目报告', 'Single-project report'));
      VCS.call('open_dir', r.out_dir || dr.path);
      // 重新读取管线状态；只有后端已经落下与当前输入/结果哈希绑定的
      // report_done 标记时，界面才把整个自动流程显示为完成。
      await reloadProjects(projectId(proj));
      VCS.toast(r.kind === 'diagnostic' ? tr("runtime.project.report.text_6d3deb78d8", { value1: (selectedLabel) }, `{value1} 诊断报告已生成`, '{value1} diagnostic report generated') : tr("runtime.project.report.text_0ebad8042a", { value1: (selectedLabel) }, `{value1} 报告已生成`, '{value1} report generated'));
    } finally {
      State.reportBusy = false;
      syncReportFormatControls();
    }
  }

  async function batchReport() {
    const ids = selectedCompareProjectIds();
    if (ids.length < 2) {
      VCS.log(tr("runtime.project.batchreport.text_ee03b663b9", {}, '批次报告请至少选择 2 个催化剂项目', 'Select at least two catalyst projects for a batch report'), 'failc');
      return;
    }
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log(tr("runtime.project.batchreport.text_d802c7d8d3", {}, '选择目录失败:', 'Failed to select directory:') + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const output = $('pj-batch-files');
    State.batchReportBusy = true;
    renderCompareSummary();
    if (output) output.innerHTML = '';
    VCS.log(tr("runtime.project.batchreport.text_023d43b8bb", { value1: ids.length }, `正在生成 {value1} 个单项目报告与一份批次比较报告…`, 'Generating {value1} single-project reports and one batch-comparison report…'));
    try {
      let r = await VCS.call(
        'proj_batch_report', ids, dr.path, preset || null,
        ['html', 'docx', 'pdf'], true, true);
      if (bridgeMethodUnavailable(r)) {
        r = await legacyBatchReports(ids, dr.path);
      }
      if (!r || r.ok === false || r.error) {
        VCS.log(tr("runtime.project.batchreport.text_0832df83b1", {}, '批次报告生成失败:', 'Batch-report generation failed:') + ((r && r.error) || tr("runtime.project.batchreport.text_bd5e21c357", {}, '未知错误', 'Unknown error')), 'failc');
        renderReportFiles('pj-batch-files', r, tr("runtime.project.batchreport.text_d25760be5e", {}, '批次报告', 'Batch report'));
        return;
      }
      collectReportFiles(r.files).forEach(file => VCS.log(tr("runtime.project.batchreport.text_d0462cce17", {}, '报告已生成:', 'Report generated:') + file.path, 'okc'));
      (r.blocked || []).forEach(reason => VCS.log(tr("runtime.project.batchreport.text_c8a1975bad", {}, '比较阻断:', 'Comparison blocked:') + reason, 'warnc'));
      (r.warnings || []).forEach(reason => VCS.log(tr("runtime.project.batchreport.text_2b0344490d", {}, '比较提示:', 'Comparison note:') + reason, 'warnc'));
      renderReportFiles('pj-batch-files', r, tr("runtime.project.batchreport.text_91f0451a3d", {}, '多催化剂批次报告', 'Multi-catalyst batch report'));
      VCS.call('open_dir', r.out_dir || dr.path);
      VCS.toast(r.kind === 'diagnostic'
        ? tr("runtime.project.batchreport.text_c501d12b45", {}, '批次诊断报告已生成', 'Batch diagnostic report generated') : tr("runtime.project.batchreport.text_05611cae28", {}, '批次 HTML / Word / PDF 报告已生成', 'Batch HTML / Word / PDF reports generated'));
    } finally {
      State.batchReportBusy = false;
      renderCompareSummary();
    }
  }

  // ── 一键成稿包:pick_dir → draft_ready → 列产物 + issues + open_dir ──────────
  async function draftReady() {
    const proj = currentProject();
    if (!proj) return;
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log(tr("runtime.project.draftready.text_d802c7d8d3", {}, '选择目录失败:', 'Failed to select directory:') + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;
    const btn = $('pj-draft');
    const box = $('pj-draft-out');
    if (btn) btn.disabled = true;
    if (box) box.innerHTML = '';
    VCS.log(tr("runtime.project.draftready.text_d3b79a5428", {}, '生成成稿包(SI + 三线表 + 口径稽核 + 方法学),生成中…', 'Generating the publication-ready package (SI + three-line tables + basis audit + methodology)…'));
    try {
      const r = await VCS.call('draft_ready', projectId(proj), dr.path);
      if (!r || r.ok === false && r.error) {
        // ok=False 但有产物(稽核不过仍出全套)时 error 为 null;仅真错误(error 非空)才失败
        if (r && r.error) { VCS.log(tr("runtime.project.draftready.text_76fa71f31b", {}, '成稿包生成失败:', 'Publication-ready package generation failed:') + r.error, 'failc'); return; }
      }
      if (!r) { VCS.log(tr("runtime.project.draftready.text_312a5e1ad1", {}, '成稿包生成失败:未知错误', 'Publication-ready package generation failed: unknown error'), 'failc'); return; }
      VCS.log((r.ok ? tr("runtime.project.draftready.text_754f66bc5d", {}, '✓ 稽核通过:', '✓ Audit passed:') : tr("runtime.project.draftready.text_b51fe5abe7", {}, '⚠ 稽核未通过(仍产全套工件):', '⚠ Audit failed (the complete artifact set was still generated):')) + (r.summary || ''),
        r.ok ? 'okc' : 'warnc');
      (r.products || []).forEach(p => VCS.log(tr("runtime.project.draftready.text_9c0aa3ce40", {}, '产物:', 'Artifacts:') + p, 'okc'));
      (r.issues || []).forEach(i => VCS.log(i, 'warnc'));
      renderDraft(r);
      if (r.out_dir) VCS.call('open_dir', r.out_dir);
      VCS.toast(r.ok ? tr("runtime.project.draftready.text_c3ef9b2afe", {}, '成稿包已生成', 'Publication-ready package generated') : tr("runtime.project.draftready.text_1e243758c0", {}, '成稿包已生成(有待确认项)', 'Publication-ready package generated (items still need confirmation)'), r.ok ? '' : 'fail');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderDraft(r) {
    const box = $('pj-draft-out');
    if (!box) return;
    let h = `<div class="pj-note" style="color:var(--${r.ok ? 'ok' : 'warn'})">` +
      VCS.esc(r.summary || '') + tr("runtime.project.renderdraft.text_a07d924437", { value1: (r.issues_total || 0) }, `(待确认 {value1} 项)</div>`, '({value1} items awaiting confirmation)</div>');
    h += '<div class="pj-cfglist">';
    (r.products || []).forEach(p => {
      h += `<div class="pj-cfgrow"><span class="path" title="${VCS.esc(p)}">${VCS.esc(p)}</span></div>`;
    });
    h += '</div>';
    box.innerHTML = h;
  }

  // ── 初始化 ─────────────────────────────────────────────────────────────────
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }

  function init() {
    wire('pj-import-source-btn', chooseImportSource);
    wire('pj-import-scan', scanImport);
    wire('pj-import-root-btn', () => pickDirInto('pj-import-root').then(updateImportCommit));
    wire('pj-import-select-ready', selectReadyImports);
    wire('pj-import-clear', clearImportSelection);
    wire('pj-import-apply-task', applyImportTask);
    wire('pj-import-confirm-selected', confirmSelectedImports);
    wire('pj-import-commit', commitImport);
    wire('pj-import-cancel', discardImportDraft);
    const importSearch = $('pj-import-search');
    if (importSearch) importSearch.addEventListener('input', renderImportRows);
    const importFilter = $('pj-import-filter');
    if (importFilter) importFilter.addEventListener('change', renderImportRows);
    const visibleCheck = $('pj-import-check-visible');
    if (visibleCheck) visibleCheck.addEventListener('change', e => {
      State.importRows.filter(rowMatches).forEach(row => {
        if (!(importStatus(row) === 'blocked' && !row.confirmationEligible)) row.selected = e.target.checked;
      });
      renderImport();
    });
    ['pj-import-source', 'pj-import-name', 'pj-import-root'].forEach(id => {
      const el = $(id); if (el) el.addEventListener('input', updateImportCommit);
    });
    wire('pj-slab-btn', () => {
      if (lisInputsLocked()) return;
      const generation = State.inputGeneration;
      return pickInto('pj-slab', 'poscar').then(async path => {
        if (generation !== State.inputGeneration || lisInputsLocked()) return;
        if (path && removeConfigPath(path)) renderConfigs();
        if (path) {
          const resolved = await resolveMemberIncar(path);
          if (generation !== State.inputGeneration || lisInputsLocked()) return;
          State.cleanIncar = {
            path: resolved.incar_path, sha256: resolved.incar_sha256,
            status: resolved.incar_status, source: resolved.incar_source,
            issues: resolved.incar_issues,
            quartet: null, inputMode: '', quartetStatus: '',
          };
        }
        invalidatePreparedLis(); updateLisReadiness();
      });
    });
    wire('pj-incar-btn', () => {
      if (lisInputsLocked()) return;
      const generation = State.inputGeneration;
      return pickInto('pj-incar', 'incar').then(() => {
        if (generation !== State.inputGeneration || lisInputsLocked()) return;
        invalidatePreparedLis(); renderConfigs(); updateLisReadiness();
      });
    });
    wire('pj-root-btn', () => pickDirInto('pj-root').then(() => {
      invalidatePreparedLis(); updateLisReadiness();
    }));
    wire('pj-cfg-add', addConfig);
    wire('pj-cfg-dir', addConfigDirectory);
    wire('lis-input-dir', addLisInputDirectory);
    wire('lis-apply-species', applyBulkSpecies);
    const onlyUnmatched = $('lis-only-unmatched');
    if (onlyUnmatched) onlyUnmatched.addEventListener('change', renderConfigs);
    wire('pj-submit-all', prepareAndSubmitLiS);
    wire('pj-create', create);
    wire('ads-route-import', () => openImport());
    wire('ads-route-new', routeNewCalculation);
    wire('ads-route-quartets', routeQuartetSubmit);
    wire('ads-route-results', () => openProjectResults());
    wire('pj-hub-import', () => openImport());
    wire('pj-hub-structure', () => startLiS());
    wire('pj-hub-recent', openRecentProject);
    wire('pj-hub-adopt', chooseAdoptFolder);
    wire('pj-adopt-apply', applyAdopt);
    const adoptMode = $('pj-adopt-mode');
    if (adoptMode) adoptMode.addEventListener('change', preflightAdopt);
    wire('pj-clone-preflight', () => preflightProjectLifecycle('clone'));
    wire('pj-move-preflight', () => preflightProjectLifecycle('move'));
    wire('pj-lifecycle-apply', applyProjectLifecycle);
    wire('lis-open-import', () => openImport());
    wire('lis-open-cluster', () => {
      if (typeof VCS.navigate === 'function') VCS.navigate('cluster', { source: 'lis-builder' });
    });
    const refSelect = $('lis-reference');
    if (refSelect) refSelect.addEventListener('change', onReferenceChanged);
    document.querySelectorAll('#pj-create-card [data-lis-open-step]').forEach(button => {
      button.addEventListener('click', () => openLisStep(Number(button.dataset.lisOpenStep)));
    });
    const profile = $('lis-profile');
    if (profile) profile.addEventListener('change', () => {
      try { localStorage.setItem('vcs.jobs.profile', profile.value); } catch (e) { /* 不阻塞 */ }
      applyLisProfileDefaults();
    });
    ['pj-name', 'pj-slab', 'pj-incar', 'pj-root'].forEach(id => {
      const el = $(id); if (el) el.addEventListener('input', () => {
        if (id === 'pj-slab') {
          State.cleanIncar = {
            path: '', sha256: '', status: 'missing', source: '', issues: [],
            quartet: null, inputMode: '', quartetStatus: '',
          };
        }
        invalidatePreparedLis(); updateLisReadiness();
        if (id === 'pj-incar') renderConfigs();
      });
    });
    ['lis-cores', 'lis-walltime'].forEach(id => {
      const el = $(id); if (el) el.addEventListener('input', () => {
        updateLisResourceSummary(); updateLisReadiness();
      });
    });
    wire('pj-refresh', () => reloadProjects());
    wire('pj-delta', delta);
    wire('pj-csv', exportCsv);
    wire('pj-report', () => openReportWorkbench('report'));
    SINGLE_REPORT_FORMATS.forEach(item => {
      const input = $(item.id);
      if (input) input.addEventListener('change', syncReportFormatControls);
    });
    syncReportFormatControls();
    loadReportCapabilities();
    const projectSelect = $('pj-select');
    if (projectSelect) projectSelect.addEventListener('change', async () => {
      const previous = State.currentProjectId;
      const requested = String(projectSelect.value || '');
      const hit = State.projects.find(p => projectId(p) === requested) || null;
      await requestProjectSelection(hit, previous);
    });
    wire('pj-figs', makeFigures);
    wire('pj-cmpfigs', makeCompareFigures);
    wire('fig-select-all', selectAllCompareProjects);
    wire('fig-select-comparable', selectComparableProjects);
    wire('fig-select-clear', () => setCompareSelection([]));
    wire('pj-batch-report', () => openReportWorkbench('comparison'));
    const presetSelect = $('pj-preset');
    if (presetSelect) presetSelect.addEventListener('change', () => {
      State.comparePreview = null;
      State.comparePreviewGeneration += 1;
      renderFigProjList();
      scheduleComparePreview();
    });
    wire('pj-draft', () => openReportWorkbench('draftpack'));
    const analysis = $('analysis-type');
    if (analysis) analysis.addEventListener('change', () => {
      if (analysis.value === 'spin') {
        analysis.value = 'taskana';
        analysis.dispatchEvent(new Event('change', { bubbles: true }));
        VCS.navigate('jobs', { source: 'spin-analysis' });
        VCS.toast(tr("runtime.project.init.text_e1e981c5e4", {}, '自旋态对比在任务页：勾选同一家族后点击“自旋对比”', 'Spin-state comparison is on Jobs: select members of the same family, then select Spin comparison'));
      }
    });
    renderConfigs();
    updateJourney();
    updateProjectHub();
    loadPresets();
    reloadProjects();
    loadLisProfiles();
  }

  async function selectRequestedProject(value) {
    const wanted = String(value || '').trim();
    if (!PROJECT_ID_RE.test(wanted)) return false;
    const generation = ++State.projectSelectionGeneration;
    const request = { kind: 'id', value: wanted, generation };
    State.requestedProject = request;
    await reloadProjects(wanted);
    // 较新的显式选择已接管；旧调用即使晚返回也不能重写项目上下文。
    if (generation !== State.projectSelectionGeneration) return false;
    const hit = State.projects.find(project => matchesRequestedProject(project, request)) || null;
    if (State.requestedProject === request) State.requestedProject = null;
    if (!hit) return false;
    applyProjectSelection(hit);
    return true;
  }

  async function selectById(id) {
    return selectRequestedProject(id);
  }

  function current() {
    return publicProject(currentProjectRecord());
  }

  function list() {
    return State.projects.map(publicProject);
  }

  function redrawLanguage() {
    if (State.reportCapabilityState === 'pending') {
      const reason = reportCapabilityPendingReason();
      State.reportCapabilities.docx = { available: false, reason };
      State.reportCapabilities.pdf = { available: false, reason };
    }
    updateJourney();
    updateProjectHub();
    if (State.importRows.length || State.importResult) renderImport();
    renderCleanIncarStatus();
    renderReferenceProjects(val('lis-reference'));
    renderConfigs();
    updateLisReadiness();
    renderFigProjList();
    renderCompareSummary();
    updateProjectSummary(State.deltaResult);
    if (State.deltaResult) renderDelta(State.deltaResult);
    if (State.candidateEvaluation) renderCandidateEvaluation(State.candidateEvaluation);
    syncReportFormatControls();
  }

  // 切回项目页时刷新项目下拉
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'project') {
      reloadProjects(); loadLisProfiles();
      if (e.detail.source === 'resume-center') resumeImportDraftEntry();
    } else {
      State.explicitWorkflow = '';
      updateProjectHub();
    }
  });
  document.addEventListener('vcs:scenario', e => applyProjectMode(e.detail && e.detail.scenario));
  document.addEventListener('vcs:calculation', applyProjectCalculation);
  document.addEventListener('vcs:language', redrawLanguage);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Project = {
    reload: reloadProjects,
    selectById,
    current,
    list,
    openImport,
    startLiS,
  };
  if (window.__VCS_TEST__) {
    window.Project.__test = {
      State,
      preflightProjectLifecycle,
      updateProjectHub,
      lifecycleRequestCurrent,
      applyProjectSelection,
      setCompareSelection,
      refreshComparePreview,
      makeFigures,
      makeCompareFigures,
      persistImportDraftReference,
      clearImportDraftReference,
      discardImportDraft,
      resumeImportDraftEntry,
    };
  }
})();
